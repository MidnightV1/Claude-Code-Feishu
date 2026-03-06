# -*- coding: utf-8 -*-
"""Standalone briefing pipeline runner.

Executes the full briefing pipeline as an independent process:
  collect → generate → review → email → keyword evolution

Usage:
    python3 scripts/briefing_run.py --domain <name> --date 2026-03-03 --config config.yaml
    python3 scripts/briefing_run.py --domain <name> --step collect
    python3 scripts/briefing_run.py --domain <name> --step evolve
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# Add hub root to path so we can import utility modules
HUB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HUB_DIR))

from agent.infra.models import LLMResult  # noqa: E402
from agent.llm.gemini_api import GeminiAPI  # noqa: E402
from agent.llm.gemini_cli import GeminiCli  # noqa: E402
from agent.llm.claude import ClaudeCli  # noqa: E402
from agent.platforms.feishu.dispatcher import Dispatcher  # noqa: E402
from agent.platforms.feishu.api import FeishuAPI  # noqa: E402
from agent.platforms.feishu.utils import text_to_blocks  # noqa: E402

log = logging.getLogger("briefing.run")

PYTHON = Path(os.environ.get("BRIEFING_PYTHON", sys.executable))
BRIEFING_DIR = Path.home() / "briefing"
DOMAINS_DIR = BRIEFING_DIR / "domains"
ENGINE_DIR = BRIEFING_DIR / "engine"


# ═══════════════════════════════════════════════════════════
# Domain Config
# ═══════════════════════════════════════════════════════════

class DomainConfig:
    """Loads and provides access to a domain's configuration."""

    def __init__(self, domain_name: str):
        self.name = domain_name
        self.dir = DOMAINS_DIR / domain_name
        if not self.dir.exists():
            raise ValueError(f"Domain '{domain_name}' not found at {self.dir}")

        self._cfg = yaml.safe_load((self.dir / "domain.yaml").read_text(encoding="utf-8"))
        self.data_dir = self.dir / "data"
        self.output_dir = self.data_dir / "output"

    @property
    def display_name(self) -> str:
        return self._cfg.get("name", self.name)

    @property
    def models(self) -> dict:
        return self._cfg.get("models", {})

    @property
    def distribution(self) -> dict:
        return self._cfg.get("distribution", {})

    @property
    def keyword_evolution(self) -> dict:
        return self._cfg.get("keyword_evolution", {})

    def load_prompt(self, stage: str) -> str:
        prompt_ref = self._cfg.get("prompts", {}).get(stage, f"prompts/{stage}.md")
        path = self.dir / prompt_ref
        if not path.exists():
            raise FileNotFoundError(f"Prompt not found: {path}")
        text = path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                text = text[end + 3:].lstrip("\n")
        return text

    def email_subject(self, date_str: str) -> str:
        tmpl = self.distribution.get("email", {}).get("subject_template", "{name} | {date}")
        return tmpl.format(name=self.display_name, date=date_str)


# ═══════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════

class BriefingRunner:
    """Standalone briefing pipeline — no hub process dependency."""

    def __init__(self, domain: str, gemini: GeminiAPI, claude: ClaudeCli,
                 dispatcher: Dispatcher, cfg: dict | None = None,
                 gemini_cli: GeminiCli | None = None):
        self.domain_name = domain
        self.dc = DomainConfig(domain)
        self.gemini = gemini
        self.gemini_cli = gemini_cli
        self.claude = claude
        self.dispatcher = dispatcher
        self._cfg = cfg or {}

        gen_cfg = self.dc.models.get("generate", {})
        self.gen_model = gen_cfg.get("model", "3-Flash")
        self.gen_thinking = gen_cfg.get("thinking", None)
        self.gen_timeout = gen_cfg.get("timeout_seconds", 180)
        self._gen_fallback = gen_cfg.get("fallback_model", "sonnet")
        self._gen_provider = (f"Gemini CLI {self.gen_model}"
                              if gemini_cli and gemini_cli.available
                              else f"Claude {self._gen_fallback}")  # updated by _generate()

        rev_cfg = self.dc.models.get("review", {})
        self.review_enabled = rev_cfg.get("enabled", False)
        self.review_model = rev_cfg.get("model", "sonnet")
        self.review_timeout = rev_cfg.get("timeout_seconds", 300)

    async def run(self, date_str: str | None = None, step: str | None = None) -> dict:
        """Run the pipeline. Returns a status dict."""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return {"status": "error", "error": f"Invalid date: {date_str}"}

        if step:
            return await self._run_step(step, date_str)
        return await self._run_full(date_str)

    # Patterns that indicate LLM returned an error instead of content
    _ERROR_PATTERNS = [
        re.compile(r'API Error:\s*\d{3}', re.IGNORECASE),
        re.compile(r'"error"\s*:\s*\{', re.IGNORECASE),
        re.compile(r'Failed to authenticate', re.IGNORECASE),
        re.compile(r'Request not allowed', re.IGNORECASE),
        re.compile(r'rate limit', re.IGNORECASE),
        re.compile(r'Inconsistency detected by ld\.so', re.IGNORECASE),
    ]

    def _is_error_content(self, text: str) -> str | None:
        """Check if LLM output is actually an error message. Returns match or None."""
        text_start = text[:500]
        for pat in self._ERROR_PATTERNS:
            m = pat.search(text_start)
            if m:
                return m.group(0)
        # Briefing must contain markdown heading
        if len(text.strip()) < 200 and '#' not in text:
            return "output too short and no markdown"
        return None

    def _check_dedup(self, date_str: str) -> str | None:
        """Check if this date was already run or is currently running. Returns reason or None."""
        status = self._load_last_status()
        if not status:
            return None
        if status.get("date") != date_str or not status.get("started_at"):
            return None
        elapsed_since = time.time() - status["started_at"]
        s = status.get("status")
        if s in ("ok", "partial") and elapsed_since < 3600:
            return f"already ran {elapsed_since:.0f}s ago (status={s})"
        if s == "running" and elapsed_since < 1800:  # 30min guard
            return f"already running ({elapsed_since:.0f}s ago)"
        return None

    def _load_last_status(self) -> dict | None:
        path = self.dc.data_dir / "run_status.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return None

    async def _run_full(self, date_str: str) -> dict:
        start = time.time()
        pid = os.getpid()
        status = {
            "domain": self.domain_name,
            "date": date_str,
            "started_at": start,
            "status": "running",
            "pid": pid,
        }
        errors = []
        total_cost = 0.0

        # ── Dedup guard ──
        dedup_reason = self._check_dedup(date_str)
        if dedup_reason:
            log.warning("[%s] %s: skipped — %s", self.domain_name, date_str, dedup_reason)
            status.update({"status": "skipped", "reason": dedup_reason})
            return status

        # Claim this slot immediately to block concurrent runs
        self._save_status(status)

        # ── Progress card (single card, updated in-place) ──
        pid_tag = f"\n<font color='grey'>pid:{pid}</font>"
        review_tag = f" → Claude {self.review_model} 审稿" if self.review_enabled else ""
        card_text = (
            f"📋 **{self.dc.display_name}** | {date_str}\n"
            f"采集 → {self._gen_provider} 生成{review_tag} → 分发\n\n"
            f"⏳ 采集中...{pid_tag}"
        )
        card_mid = await self.dispatcher.send_card_to_delivery(card_text)

        async def _update_card(new_text: str):
            """Update the progress card, or send new message as fallback."""
            nonlocal card_mid
            if card_mid:
                ok = await self.dispatcher.update_card(card_mid, new_text)
                if ok:
                    return
            card_mid = await self.dispatcher.send_card_to_delivery(new_text)

        # ── Step 1: Collect ──
        log.info("[%s] %s: collecting...", self.domain_name, date_str)
        ok, output = await self._run_collector(date_str)
        if not ok:
            await _update_card(
                f"❌ **{self.dc.display_name}** | {date_str}\n"
                f"采集失败\n```\n{output[-500:]}\n```{pid_tag}"
            )
            status.update({"status": "error", "error": "collector failed"})
            self._save_status(status)
            return status

        # ── Step 1.5: Low-yield check ──
        article_count = self._count_collected_articles()
        if article_count == 0:
            log.info("[%s] %s: 0 articles, skipping", self.domain_name, date_str)
            await _update_card(
                f"📭 **{self.dc.display_name}** | {date_str}\n"
                f"今日无新增匹配信号，跳过生成。{pid_tag}"
            )
            status.update({"status": "skipped", "elapsed_s": round(time.time() - start, 1)})
            self._save_status(status)
            return status
        elif article_count < 5:
            log.info("[%s] %s: sparse day (%d articles)", self.domain_name, date_str, article_count)

        # ── Step 2: Generate ──
        await _update_card(
            f"📋 **{self.dc.display_name}** | {date_str}\n"
            f"✅ 采集 {article_count} 篇 → ⏳ {self._gen_provider} 生成中...{pid_tag}"
        )
        log.info("[%s] %s: generating via %s...", self.domain_name, date_str, self._gen_provider)
        gen_result = await self._generate(date_str)
        if gen_result.is_error:
            await _update_card(
                f"❌ **{self.dc.display_name}** | {date_str}\n"
                f"生成失败\n{gen_result.text[:500]}{pid_tag}"
            )
            status.update({"status": "error", "error": "generation failed"})
            self._save_status(status)
            return status

        # Validate generation output
        gen_error = self._is_error_content(gen_result.text)
        if gen_error:
            await _update_card(
                f"❌ **{self.dc.display_name}** | {date_str}\n"
                f"生成输出异常: {gen_error}\n```\n{gen_result.text[:300]}\n```{pid_tag}"
            )
            log.error("[%s] Generation output is error content: %s", self.domain_name, gen_error)
            status.update({"status": "error", "error": f"gen output invalid: {gen_error}"})
            self._save_status(status)
            return status

        briefing_md, new_entities = self._parse_output(gen_result.text)
        total_cost += gen_result.cost_usd
        output_path = self.dc.output_dir / f"{date_str}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Step 3: Review ──
        review_model_used = "off"
        if self.review_enabled:
            await _update_card(
                f"📋 **{self.dc.display_name}** | {date_str}\n"
                f"✅ 采集 → ✅ 生成 → ⏳ Claude {self.review_model} 审稿中...{pid_tag}"
            )
            log.info("[%s] %s: reviewing via Claude %s...", self.domain_name, date_str, self.review_model)
            review_result = await self._review(briefing_md, date_str)
            if review_result.is_error:
                errors.append(f"审稿失败(用初稿): {review_result.text[:100]}")
                log.warning("[%s] Review error (using draft): %s", self.domain_name, review_result.text[:200])
            else:
                # Validate review output — reject if it's an error message
                review_error = self._is_error_content(review_result.text)
                if review_error:
                    errors.append(f"审稿输出异常(用初稿): {review_error}")
                    log.warning("[%s] Review output is error content: %s", self.domain_name, review_error)
                else:
                    (self.dc.output_dir / f"{date_str}-draft.md").write_text(briefing_md, encoding="utf-8")
                    briefing_md = review_result.text
                    total_cost += review_result.cost_usd
                    review_model_used = self.review_model

        output_path.write_text(briefing_md, encoding="utf-8")

        if new_entities:
            self._update_entities(new_entities)

        # ── Step 4: Email ──
        if self.dc.distribution.get("email", {}).get("enabled", True):
            log.info("[%s] %s: sending email...", self.domain_name, date_str)
            email_ok, email_out = await self._send_email(date_str)
            if not email_ok:
                errors.append(f"邮件失败: {email_out[-200:]}")

        # ── Step 4.5: Feishu Document ──
        doc_cfg = self.dc.distribution.get("feishu_doc", {})
        if doc_cfg.get("enabled", False):
            log.info("[%s] %s: creating Feishu doc...", self.domain_name, date_str)
            doc_ok, doc_out = await self._send_feishu_doc(date_str)
            if doc_ok:
                errors.append(f"📄 [文档]({doc_out})")  # include in final card
            else:
                errors.append(f"飞书文档失败: {doc_out[-200:]}")
                log.warning("[%s] Feishu doc failed: %s", self.domain_name, doc_out)

        # ── Step 5: Keyword Evolution (inline, not fire-and-forget) ──
        if self.dc.keyword_evolution.get("enabled", False):
            try:
                await self._evolve_keywords(date_str, briefing_md)
            except Exception:
                log.exception("[%s] Keyword evolution failed", self.domain_name)

        elapsed = time.time() - start
        final_status = "ok" if not errors else "partial"
        status.update({
            "status": final_status,
            "elapsed_s": round(elapsed, 1),
            "model": self._gen_provider,
            "review_model": review_model_used,
            "cost_usd": total_cost,
            "errors": errors,
        })
        self._save_status(status)

        # ── Final card update ──
        icon = "✅" if final_status == "ok" else "⚠️"
        final_text = (
            f"{icon} **{self.dc.display_name}** | {date_str}\n"
            f"生成: {self._gen_provider} ({gen_result.duration_ms / 1000:.1f}s)"
        )
        if review_model_used != "off":
            final_text += f" | 审稿: Claude {review_model_used}"
        final_text += f"\n成本: ${total_cost:.4f} | 耗时: {elapsed:.0f}s"
        if errors:
            final_text += "\n" + "\n".join(f"- {e}" for e in errors)
        final_text += pid_tag
        await _update_card(final_text)

        summary = (f"[{self.domain_name}] {date_str} | {self._gen_provider}"
                   + (f"+{review_model_used}" if review_model_used != "off" else "")
                   + f" | {elapsed:.0f}s | ${total_cost:.4f}")
        log.info(summary)
        return status

    async def _run_step(self, step: str, date_str: str) -> dict:
        """Run a single pipeline step."""
        if step == "collect":
            ok, output = await self._run_collector(date_str)
            return {"status": "ok" if ok else "error", "output": output[-500:]}
        elif step == "evolve":
            briefing_path = self.dc.output_dir / f"{date_str}.md"
            briefing_md = briefing_path.read_text(encoding="utf-8") if briefing_path.exists() else ""
            await self._evolve_keywords(date_str, briefing_md)
            return {"status": "ok", "step": "evolve"}
        else:
            return {"status": "error", "error": f"Unknown step: {step}"}

    # ── Pipeline steps ───────────────────────────────────

    async def _run_collector(self, date_str: str) -> tuple[bool, str]:
        cmd = [str(PYTHON), str(ENGINE_DIR / "collector.py"),
               "--domain", self.domain_name, "--date", date_str,
               "--config", str(HUB_DIR / "config.yaml")]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(BRIEFING_DIR))
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                log.error("[%s] Collector failed: %s", self.domain_name, output[-500:])
                return False, output
            log.info("[%s] Collector done: %s", self.domain_name, output.strip().split("\n")[-1])
            return True, output
        except asyncio.TimeoutError:
            proc.kill()
            return False, "Collector timed out (300s)"
        except Exception as e:
            return False, str(e)

    def _count_collected_articles(self) -> int:
        try:
            data = json.loads(self.dc.data_dir.joinpath("today_context.json").read_text(encoding="utf-8"))
            return len(data.get("articles", []))
        except Exception:
            return 0

    async def _generate(self, date_str: str) -> LLMResult:
        context_path = self.dc.data_dir / "today_context.json"
        if not context_path.exists():
            return LLMResult(text="today_context.json not found", is_error=True)

        context = context_path.read_text(encoding="utf-8")
        article_count = json.loads(context).get("stats", {}).get("articles_after_filter", 0)
        system_prompt = self.dc.load_prompt("generate")

        sparse_hint = ""
        if article_count < 5:
            sparse_hint = (
                "\n\n## ⚠️ Low Signal Day\n"
                f"Only {article_count} articles matched today. This is a sparse day.\n"
                "- Do NOT pad or inflate. If there are no strong signals, say so explicitly.\n"
                "- 'No significant developments today' is a valid briefing.\n"
                "- Only include sections that have real content. Skip empty sections entirely.\n"
                "- Keep '今日信号' honest: '今日无显著新增信号' is acceptable.\n"
            )

        user_prompt = (
            f"Date: {date_str}\n\n"
            f"## Collected article data:\n\n{context}\n\n"
            f"{sparse_hint}"
            f"Generate the daily briefing following the system instructions precisely."
        )
        # Try Gemini CLI first; fall back to Claude if unavailable or failed
        if self.gemini_cli and self.gemini_cli.available:
            result = await self.gemini_cli.run(
                prompt=user_prompt, system_prompt=system_prompt,
                model=self.gen_model,
                timeout_seconds=self.gen_timeout,
            )
            if not result.is_error:
                self._gen_provider = f"Gemini CLI {self.gen_model}"
                return result
            log.warning("[%s] Gemini CLI generation failed, falling back to Claude: %s",
                        self.domain_name, result.text[:200])

        log.info("[%s] Generating via Claude %s (fallback)...", self.domain_name, self._gen_fallback)
        result = await self.claude.run(
            prompt=user_prompt, system_prompt=system_prompt,
            model=self._gen_fallback, timeout_seconds=self.gen_timeout,
        )
        self._gen_provider = f"Claude {self._gen_fallback}"
        return result

    async def _review(self, draft: str, date_str: str) -> LLMResult:
        yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_path = self.dc.output_dir / f"{yesterday}.md"

        parts = []
        if yesterday_path.exists():
            parts.append(f"## 昨日日报（{yesterday}）\n\n{yesterday_path.read_text(encoding='utf-8')}")
        else:
            parts.append("（无昨日日报数据）")
        parts.append(f"## 今日初稿（{date_str}）\n\n{draft}")
        parts.append("请按照系统指令审稿，直接输出最终版日报 markdown。")

        review_prompt = self.dc.load_prompt("review")
        return await self.claude.run(
            prompt="\n\n---\n\n".join(parts),
            system_prompt=review_prompt,
            model=self.review_model,
            timeout_seconds=self.review_timeout,
        )

    async def _send_email(self, date_str: str) -> tuple[bool, str]:
        domain_email = self.dc.dir / "config" / "email.json"
        email_config = domain_email if domain_email.exists() else BRIEFING_DIR / "config" / "email.json"
        if not email_config.exists():
            return False, "email.json not configured"

        output_file = self.dc.output_dir / f"{date_str}.md"
        if not output_file.exists():
            return False, f"Briefing not found: {output_file}"

        cmd = [str(PYTHON), str(ENGINE_DIR / "notify.py"),
               "--domain", self.domain_name, "--date", date_str]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(BRIEFING_DIR))
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                return False, output
            return True, output
        except asyncio.TimeoutError:
            return False, "Email timed out (60s)"
        except Exception as e:
            return False, str(e)

    async def _send_feishu_doc(self, date_str: str) -> tuple[bool, str]:
        """Create a Feishu document with the briefing content."""
        output_file = self.dc.output_dir / f"{date_str}.md"
        if not output_file.exists():
            return False, f"Briefing not found: {output_file}"

        try:
            feishu_cfg = self._cfg.get("feishu", {})
            api = FeishuAPI(
                app_id=feishu_cfg.get("app_id", ""),
                app_secret=feishu_cfg.get("app_secret", ""),
                domain=feishu_cfg.get("domain", "https://open.feishu.cn"),
            )
        except Exception as e:
            return False, f"FeishuAPI init failed: {e}"

        title = f"{self.dc.display_name} | {date_str}"
        doc_cfg = self.dc.distribution.get("feishu_doc", {})
        body = {"title": title}
        folder = doc_cfg.get("folder_token")
        if folder:
            body["folder_token"] = folder

        resp = api.post("/open-apis/docx/v1/documents", body)
        if resp.get("code") != 0:
            return False, f"Create doc failed: {resp.get('msg')}"

        doc_id = resp["data"]["document"]["document_id"]
        content = output_file.read_text(encoding="utf-8")
        blocks = text_to_blocks(content)
        if blocks:
            resp2 = api.post(
                f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                {"children": blocks, "index": 0},
                params={"document_revision_id": "-1"},
            )
            if resp2.get("code") != 0:
                log.warning("[%s] Doc content write partial: %s", self.domain_name, resp2.get("msg"))

        share_to = doc_cfg.get("share_to")
        if share_to:
            api.post(
                f"/open-apis/drive/v1/permissions/{doc_id}/members",
                body={"member_type": "openid", "member_id": share_to, "perm": "full_access"},
                params={"type": "docx", "need_notification": "true"},
            )

        domain = feishu_cfg.get("domain", "https://open.feishu.cn").replace("open.", "")
        doc_url = f"{domain}/docx/{doc_id}"
        log.info("[%s] Feishu doc created: %s", self.domain_name, doc_url)
        return True, doc_url

    # ── Helpers ──────────────────────────────────────────

    @staticmethod
    def _parse_output(text: str) -> tuple[str, dict]:
        new_entities = {}
        m = re.search(r'```json\s*\n(\{.*?"new_entities".*?\})\s*\n```', text, re.DOTALL)
        if m:
            try:
                new_entities = json.loads(m.group(1)).get("new_entities", {})
            except json.JSONDecodeError:
                pass
            text = text[:m.start()].rstrip()
        return text, new_entities

    def _update_entities(self, new_entities: dict):
        existing = {}
        if self.dc.data_dir.joinpath("entities.json").exists():
            existing = json.loads(self.dc.data_dir.joinpath("entities.json").read_text(encoding="utf-8"))
        existing.update(new_entities)
        self.dc.data_dir.joinpath("entities.json").write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    def _save_status(self, status: dict):
        """Persist run status to file (survives hub restart)."""
        path = self.dc.data_dir / "run_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Keyword Evolution ─────────────────────────────────

    EVOLUTION_SYSTEM = (
        "You are a keyword optimization analyst for an industry intelligence briefing.\n\n"
        "Your job: analyze today's briefing output, rejected articles, and keyword performance "
        "to improve the keyword list — discover new terms and flag low-quality ones.\n\n"
        "## Key Principles\n"
        "- New keywords come from TWO sources: rejected articles that look relevant, "
        "AND new entities/terms discovered in the briefing content itself\n"
        "- A keyword is low-quality if it's too generic (high matches, low signal) "
        "OR too niche (zero matches for weeks). Both should be flagged\n"
        "- Prefer specific multi-word phrases over single generic words\n"
        "- Consider both Chinese and English terms\n"
        "- Max 5 new suggestions per cycle\n\n"
        "## Output Format\n"
        "```json\n"
        '{"add": [{"keyword": "...", "layer": "...", "reason": "..."}], '
        '"deprecate": [{"keyword": "...", "reason": "..."}], '
        '"observations": "..."}\n'
        "```\n"
        "If no changes needed: {\"add\": [], \"deprecate\": [], \"observations\": \"...\"}"
    )

    async def _evolve_keywords(self, date_str: str, briefing_md: str):
        log.info("[%s] %s: running keyword evolution...", self.domain_name, date_str)
        evo_cfg = self.dc.keyword_evolution

        feedback_path = self.dc.data_dir / "keyword_feedback.json"
        if not feedback_path.exists():
            log.info("[%s] No keyword feedback yet, skipping", self.domain_name)
            return
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        daily = feedback.get("daily", {})
        if len(daily) < 3:
            log.info("[%s] Only %d days of data, need 3+", self.domain_name, len(daily))
            return

        meta_path = self.dc.data_dir / "keywords_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if meta.get("last_evolution_date") == date_str:
            log.info("[%s] Evolution already ran for %s", self.domain_name, date_str)
            return

        dynamic_path = self.dc.dir / "keywords_dynamic.yaml"
        dynamic = yaml.safe_load(dynamic_path.read_text(encoding="utf-8")) if dynamic_path.exists() else {}
        dynamic.setdefault("keywords", {})

        sources_cfg = yaml.safe_load((self.dc.dir / "sources.yaml").read_text(encoding="utf-8"))
        static_kws = set()
        for kws in sources_cfg.get("keywords", {}).values():
            for kw in kws:
                static_kws.add(kw.lower())

        prompt = self._build_evolution_prompt(
            date_str, briefing_md, daily, dynamic, static_kws, evo_cfg)
        result = await self.claude.run(
            prompt, model="sonnet",
            system_prompt=self.EVOLUTION_SYSTEM,
            timeout_seconds=120,
        )
        if result.is_error:
            log.warning("[%s] Evolution LLM failed: %s", self.domain_name, result.text[:200])
            return

        changes = self._parse_evolution_response(result.text)
        added = self._apply_additions(dynamic, changes.get("add", []), static_kws, date_str)
        model_deprecated = self._apply_deprecations(dynamic, changes.get("deprecate", []), date_str)
        transitions = self._run_lifecycle(dynamic, daily, date_str)

        dynamic_path.write_text(
            yaml.dump(dynamic, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")

        history_entry = {
            "date": date_str,
            "added": added,
            "promoted": transitions.get("promoted", []),
            "deprecated": model_deprecated + transitions.get("deprecated", []),
            "observations": changes.get("observations", ""),
            "cost_usd": result.cost_usd,
        }
        meta.setdefault("evolution_history", []).append(history_entry)
        meta["evolution_history"] = meta["evolution_history"][-30:]
        meta["last_evolution_date"] = date_str
        meta["last_evolution_at"] = datetime.now().isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        all_deprecated = model_deprecated + transitions.get("deprecated", [])
        promoted = transitions.get("promoted", [])
        if added or promoted or all_deprecated:
            await self._notify_evolution(date_str, added, promoted, all_deprecated, result.cost_usd)

        log.info("[%s] Evolution done: +%d added, %d promoted, %d deprecated",
                 self.domain_name, len(added), len(promoted), len(all_deprecated))

    def _build_evolution_prompt(self, date_str, briefing_md, daily, dynamic, static_kws, cfg):
        today = daily.get(date_str, {})
        rejected = today.get("rejected_titles", [])
        kw_hits = today.get("keyword_hits", {})

        all_dates = sorted(daily.keys())
        zero_streaks = {}
        for kw in kw_hits:
            streak = 0
            for d in reversed(all_dates):
                if daily[d].get("keyword_hits", {}).get(kw, {}).get("hits", 0) == 0:
                    streak += 1
                else:
                    break
            if streak >= 3:
                zero_streaks[kw] = streak

        high_hit = {kw: info for kw, info in kw_hits.items() if info.get("hits", 0) >= 10}

        sc = self.dc._cfg.get("supply_chain", [])
        layers = "\n".join(f"- {s['key']}: {s['name']} — {s['scope'][:80]}" for s in sc)

        dyn_active = []
        for layer, entries in dynamic.get("keywords", {}).items():
            for e in entries:
                if e.get("status") in ("candidate", "active"):
                    dyn_active.append(f"  - `{e['keyword']}` ({layer}, {e['status']})")

        sections = [
            f"# Keyword Evolution | {date_str}\n",
            f"## Supply Chain Layers\n{layers}\n",
            f"## Today's Briefing (for entity/topic discovery)\n{briefing_md[:3000]}\n",
        ]

        if rejected:
            lines = [f"- [{r['source']}] {r['title']}" for r in rejected[:30]]
            sections.append(f"## Rejected Articles (missed by keywords)\n" + "\n".join(lines) + "\n")

        if zero_streaks:
            lines = [f"- `{kw}`: {d} days zero hits" for kw, d in
                     sorted(zero_streaks.items(), key=lambda x: -x[1])[:20]]
            sections.append(f"## Long Zero-Hit Streaks\n" + "\n".join(lines) + "\n")

        if high_hit:
            lines = [f"- `{kw}` ({info['layer']}): {info['hits']} hits today"
                     for kw, info in sorted(high_hit.items(), key=lambda x: -x[1]["hits"])[:15]]
            sections.append(f"## High-Hit Keywords (check if too generic)\n" + "\n".join(lines) + "\n")

        if dyn_active:
            sections.append(f"## Current Dynamic Keywords\n" + "\n".join(dyn_active) + "\n")

        sections.append(
            f"## Static Keywords ({len(static_kws)} total)\n"
            f"Sample: {', '.join(list(static_kws)[:25])}...\n"
            f"Do NOT suggest duplicates of these.\n"
        )

        max_add = cfg.get("max_auto_additions_per_cycle", 5)
        sections.append(
            f"Suggest up to {max_add} new keywords AND flag any keywords to deprecate "
            f"(zero-match OR high-noise). Be conservative — only suggest with clear evidence."
        )
        return "\n".join(sections)

    @staticmethod
    def _parse_evolution_response(text: str) -> dict:
        m = re.search(r'```json\s*\n(\{.*?\})\s*\n```', text, re.DOTALL)
        if not m:
            m = re.search(r'(\{"add".*?\})\s*$', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        return {"add": [], "deprecate": [], "observations": ""}

    def _apply_additions(self, dynamic, suggestions, static_kws, date_str) -> list:
        kw_section = dynamic["keywords"]
        existing = set()
        for entries in kw_section.values():
            for e in entries:
                existing.add(e["keyword"].lower())

        added = []
        valid_layers = {"infra", "content", "distribution", "regulation", "market"}
        for s in suggestions:
            kw = s.get("keyword", "").strip()
            layer = s.get("layer", "content")
            if not kw or layer not in valid_layers:
                continue
            if kw.lower() in static_kws or kw.lower() in existing:
                continue
            kw_section.setdefault(layer, []).append({
                "keyword": kw,
                "status": "candidate",
                "added": date_str,
                "reason": s.get("reason", ""),
                "match_days": 0,
                "no_match_streak": 0,
            })
            added.append({"keyword": kw, "layer": layer, "reason": s.get("reason", "")})
            existing.add(kw.lower())
        return added

    def _apply_deprecations(self, dynamic, deprecations, date_str) -> list:
        deprecated = []
        targets = {d.get("keyword", "").lower(): d.get("reason", "") for d in deprecations}
        for layer, entries in dynamic.get("keywords", {}).items():
            for entry in entries:
                if entry["status"] == "deprecated":
                    continue
                if entry["keyword"].lower() in targets:
                    entry["status"] = "deprecated"
                    entry["deprecated_at"] = date_str
                    entry["deprecation_reason"] = targets[entry["keyword"].lower()]
                    deprecated.append({
                        "keyword": entry["keyword"], "layer": layer,
                        "reason": entry["deprecation_reason"],
                    })
        return deprecated

    def _run_lifecycle(self, dynamic, daily, date_str) -> dict:
        transitions = {"promoted": [], "deprecated": []}
        all_dates = sorted(daily.keys())

        for layer, entries in dynamic.get("keywords", {}).items():
            for entry in entries:
                if entry["status"] == "deprecated":
                    continue
                kw = entry["keyword"].lower()

                match_days, streak = 0, 0
                for d in all_dates:
                    if d < entry.get("added", "1970-01-01"):
                        continue
                    hits = daily[d].get("keyword_hits", {}).get(kw, {}).get("hits", 0)
                    if hits > 0:
                        match_days += 1
                        streak = 0
                    else:
                        streak += 1

                entry["match_days"] = match_days
                entry["no_match_streak"] = streak

                if entry["status"] == "candidate" and match_days >= 3:
                    entry["status"] = "active"
                    transitions["promoted"].append({"keyword": entry["keyword"], "layer": layer})

                if streak >= 14:
                    entry["status"] = "deprecated"
                    entry["deprecated_at"] = date_str
                    entry["deprecation_reason"] = f"0 matches for {streak} consecutive days"
                    transitions["deprecated"].append({
                        "keyword": entry["keyword"], "layer": layer,
                        "reason": entry["deprecation_reason"],
                    })
        return transitions

    async def _notify_evolution(self, date_str, added, promoted, deprecated, cost):
        parts = [f"🔄 **Keyword Evolution** | {self.dc.display_name} | {date_str}"]
        if added:
            parts.append("\n**New Candidates:**")
            for a in added:
                parts.append(f"- `{a['keyword']}` ({a['layer']}) — {a['reason'][:80]}")
        if promoted:
            parts.append("\n**Promoted → Active:**")
            for p in promoted:
                parts.append(f"- `{p['keyword']}` ({p['layer']})")
        if deprecated:
            parts.append("\n**Deprecated:**")
            for d in deprecated:
                parts.append(f"- ~~`{d['keyword']}`~~ ({d['layer']}) — {d['reason'][:80]}")
        parts.append(f"\nCost: ${cost:.4f}")
        await self.dispatcher.send_to_delivery_target("\n".join(parts))


# ═══════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_domains() -> list[dict]:
    if not DOMAINS_DIR.exists():
        return []
    domains = []
    for d in sorted(DOMAINS_DIR.iterdir()):
        if (d / "domain.yaml").exists():
            cfg = yaml.safe_load((d / "domain.yaml").read_text(encoding="utf-8"))
            domains.append({
                "name": d.name,
                "display_name": cfg.get("name", d.name),
                "schedule": cfg.get("schedule", ""),
                "evolution": cfg.get("keyword_evolution", {}).get("enabled", False),
            })
    return domains


def read_status(domain: str) -> dict | None:
    path = DOMAINS_DIR / domain / "data" / "run_status.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


async def async_main(args):
    if args.command == "domains":
        domains = list_domains()
        print(json.dumps(domains, ensure_ascii=False, indent=2))
        return

    if args.command == "status":
        status = read_status(args.domain)
        print(json.dumps(status or {"status": "no data"}, ensure_ascii=False, indent=2))
        return

    # run / evolve need LLM clients
    config_path = args.config or str(HUB_DIR / "config.yaml")
    cfg = load_config(config_path)

    llm_cfg = cfg.get("llm", {})
    gemini = GeminiAPI(llm_cfg.get("gemini-api", {}))
    gemini_cli = GeminiCli(llm_cfg.get("gemini-cli", {}))
    claude = ClaudeCli(llm_cfg.get("claude-cli", {}))

    notify_cfg = cfg.get("notify", {}) or cfg.get("feishu", {})
    dispatcher = Dispatcher(notify_cfg)
    await dispatcher.start()

    runner = BriefingRunner(args.domain, gemini, claude, dispatcher, cfg=cfg,
                            gemini_cli=gemini_cli)

    step = None
    if args.command == "evolve":
        step = "evolve"

    result = await runner.run(date_str=args.date, step=step)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    await dispatcher.stop()


def main():
    parser = argparse.ArgumentParser(description="Briefing pipeline runner")
    parser.add_argument("command", choices=["run", "status", "domains", "evolve"],
                        help="Command to execute")
    parser.add_argument("--domain", "-d", default="ai-drama", help="Domain name")
    parser.add_argument("--date", default=None, help="Date (YYYY-MM-DD), defaults to today")
    parser.add_argument("--config", "-c", default=None, help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()

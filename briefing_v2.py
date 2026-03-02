# -*- coding: utf-8 -*-
"""Domain-aware daily briefing pipeline.

Replaces briefing.py with a fully parameterized pipeline.
Each domain has its own sources, prompts, data directory, and schedule.
Prompts are loaded from files (not hardcoded), supporting independent evolution per domain.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from models import LLMConfig, LLMResult
from gemini_api import GeminiAPI
from claude_cli import ClaudeCli
from dispatcher import Dispatcher

log = logging.getLogger("hub.briefing")

PYTHON = Path.home() / "python313/python/bin/python3"
BRIEFING_DIR = Path.home() / "briefing"
DOMAINS_DIR = BRIEFING_DIR / "domains"
ENGINE_DIR = BRIEFING_DIR / "engine"


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
        """Load a prompt file, stripping YAML frontmatter."""
        prompt_ref = self._cfg.get("prompts", {}).get(stage, f"prompts/{stage}.md")
        path = self.dir / prompt_ref
        if not path.exists():
            raise FileNotFoundError(f"Prompt not found: {path}")
        text = path.read_text(encoding="utf-8")
        # Strip YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                text = text[end + 3:].lstrip("\n")
        return text

    def email_subject(self, date_str: str) -> str:
        tmpl = self.distribution.get("email", {}).get("subject_template", "{name} | {date}")
        return tmpl.format(name=self.display_name, date=date_str)


class BriefingPipeline:
    """Domain-aware briefing pipeline: collect → generate → review → distribute."""

    def __init__(
        self,
        domain: str,
        gemini: GeminiAPI,
        claude: ClaudeCli,
        dispatcher: Dispatcher,
        config: dict | None = None,
    ):
        self.domain_name = domain
        self.dc = DomainConfig(domain)
        self.gemini = gemini
        self.claude = claude
        self.dispatcher = dispatcher

        # Model config from domain.yaml, with overrides from hub config
        gen_cfg = self.dc.models.get("generate", {})
        self.gen_model = gen_cfg.get("model", "3-Flash")
        self.gen_thinking = gen_cfg.get("thinking", None)
        self.gen_timeout = gen_cfg.get("timeout_seconds", 180)

        rev_cfg = self.dc.models.get("review", {})
        self.review_enabled = rev_cfg.get("enabled", False)
        self.review_model = rev_cfg.get("model", "sonnet")
        self.review_timeout = rev_cfg.get("timeout_seconds", 300)

        self._last_run: dict | None = None

    def descriptor(self) -> dict:
        return {
            "commands": [{
                "prefix": "#briefing",
                "handler": self.handle_command,
                "help": (
                    "| `#briefing run [date] [--domain X]` | Run briefing pipeline |\n"
                    "| `#briefing status` | Last run info |\n"
                    "| `#briefing domains` | List available domains |"
                ),
            }],
            "handlers": [{"name": "briefing", "fn": self.run}],
        }

    async def handle_command(self, cmd: str, args: str) -> str:
        after_prefix = cmd.replace("#briefing", "").strip()
        if after_prefix:
            subcmd = after_prefix.split()[0]
            rest = args
        elif args:
            parts = args.split(None, 1)
            subcmd = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            subcmd = "run"
            rest = ""

        if subcmd == "status":
            return self._format_status()

        if subcmd == "domains":
            return self._list_domains()

        if subcmd in ("run", "#briefing"):
            # Parse optional --domain flag
            domain = self.domain_name
            date_str = None
            parts = rest.strip().split()
            for i, p in enumerate(parts):
                if p == "--domain" and i + 1 < len(parts):
                    domain = parts[i + 1]
                elif not p.startswith("--"):
                    date_str = p

            if domain != self.domain_name:
                # Switch domain for this run
                self.domain_name = domain
                self.dc = DomainConfig(domain)

            asyncio.ensure_future(self._run_safe(date_str))
            label = f"{self.dc.display_name}"
            if date_str:
                label += f" ({date_str})"
            return f"日报 pipeline 已启动：{label}"

        return f"Unknown: `{subcmd}`. Try `#briefing run`, `#briefing status`, `#briefing domains`."

    @property
    def last_run(self) -> dict | None:
        return self._last_run

    def _format_status(self) -> str:
        last = self._last_run
        if not last:
            return "No briefing has run yet in this session."
        started = datetime.fromtimestamp(last["started_at"]).strftime("%H:%M:%S")
        return (
            f"**Last briefing run**\n"
            f"- Domain: {last.get('domain', '?')}\n"
            f"- Date: {last['date']}\n"
            f"- Status: {last['status']}\n"
            f"- Started: {started}\n"
            f"- Elapsed: {last.get('elapsed_s', '?')}s\n"
            f"- Generate: {last.get('model', '?')}\n"
            f"- Review: {last.get('review_model', 'off')}\n"
            f"- Cost: ${last.get('cost_usd', 0):.4f}\n"
            + (f"- Errors: {', '.join(last.get('errors', []))}" if last.get("errors") else "")
        )

    @staticmethod
    def _list_domains() -> str:
        if not DOMAINS_DIR.exists():
            return "No domains configured."
        domains = []
        for d in sorted(DOMAINS_DIR.iterdir()):
            if (d / "domain.yaml").exists():
                cfg = yaml.safe_load((d / "domain.yaml").read_text(encoding="utf-8"))
                domains.append(f"- **{d.name}**: {cfg.get('name', d.name)}")
        return "**Available domains:**\n" + "\n".join(domains) if domains else "No domains found."

    async def _run_safe(self, date_str: str | None = None):
        try:
            await self.run(date_str)
        except Exception:
            log.exception("Briefing pipeline crashed [%s]", self.domain_name)
            try:
                await self.dispatcher.send_to_delivery_target(
                    f"❌ **日报 pipeline 异常** | {self.dc.display_name}\n"
                    f"查看日志: `tail data/nas-claude-hub.log`"
                )
            except Exception:
                pass

    async def run(self, date_str: str | None = None) -> str:
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return f"Invalid date: `{date_str}`"

        start = time.time()
        self._last_run = {
            "domain": self.domain_name,
            "date": date_str,
            "started_at": start,
            "status": "running",
        }

        review_tag = f" → Claude {self.review_model} 审稿" if self.review_enabled else ""
        await self.dispatcher.send_to_delivery_target(
            f"📋 **{self.dc.display_name}** | {date_str}\n"
            f"采集 → Gemini {self.gen_model} 生成{review_tag} → 分发"
        )

        errors = []
        total_cost = 0.0

        # ── Step 1: Collect ──
        log.info("[%s] %s: collecting...", self.domain_name, date_str)
        ok, output = await self._run_collector(date_str)
        if not ok:
            msg = f"❌ **采集失败** | {self.dc.display_name}\n```\n{output[-500:]}\n```"
            await self.dispatcher.send_to_delivery_target(msg)
            self._last_run.update({"status": "error", "error": "collector failed"})
            return msg

        # ── Step 1.5: Low-yield check ──
        article_count = self._count_collected_articles()
        if article_count == 0:
            msg = (f"📭 **{self.dc.display_name}** | {date_str}\n"
                   f"今日无新增匹配信号，跳过生成。")
            log.info("[%s] %s: 0 articles after collection, skipping generation", self.domain_name, date_str)
            await self.dispatcher.send_to_delivery_target(msg)
            self._last_run.update({"status": "skipped", "elapsed_s": round(time.time() - start, 1)})
            return msg
        elif article_count < 5:
            log.info("[%s] %s: sparse day (%d articles), injecting low-yield hint",
                     self.domain_name, date_str, article_count)

        # ── Step 2: Generate ──
        log.info("[%s] %s: generating via Gemini %s...", self.domain_name, date_str, self.gen_model)
        gen_result = await self._generate(date_str)
        if gen_result.is_error:
            msg = f"❌ **生成失败** | {self.dc.display_name}\n{gen_result.text[:500]}"
            await self.dispatcher.send_to_delivery_target(msg)
            self._last_run.update({"status": "error", "error": "generation failed"})
            return msg

        briefing_md, new_entities = self._parse_output(gen_result.text)
        total_cost += gen_result.cost_usd
        output_path = self.dc.output_dir / f"{date_str}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Step 3: Review ──
        review_model_used = "off"
        if self.review_enabled:
            log.info("[%s] %s: reviewing via Claude %s...", self.domain_name, date_str, self.review_model)
            await self.dispatcher.send_to_delivery_target(
                f"🔍 **审稿中** | Claude {self.review_model}"
            )
            review_result = await self._review(briefing_md, date_str)
            if review_result.is_error:
                errors.append(f"审稿失败(用初稿): {review_result.text[:100]}")
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
            if email_ok:
                await self.dispatcher.send_to_delivery_target(
                    f"✅ **{self.dc.display_name}** | {date_str}\n"
                    f"生成: Gemini {self.gen_model} ({gen_result.duration_ms / 1000:.1f}s)"
                    + (f" | 审稿: Claude {review_model_used}" if review_model_used != "off" else "")
                    + f"\n总成本: ${total_cost:.4f}"
                )
            else:
                errors.append(f"邮件失败: {email_out[-200:]}")
                await self.dispatcher.send_to_delivery_target(
                    f"⚠️ **{self.dc.display_name}** 生成成功，邮件失败\n```\n{email_out[-300:]}\n```"
                )

        elapsed = time.time() - start
        self._last_run.update({
            "status": "ok" if not errors else "partial",
            "elapsed_s": round(elapsed, 1),
            "model": self.gen_model,
            "review_model": review_model_used,
            "cost_usd": total_cost,
            "errors": errors,
        })

        summary = (f"[{self.domain_name}] {date_str} | {self.gen_model}"
                   + (f"+{review_model_used}" if review_model_used != "off" else "")
                   + f" | {elapsed:.0f}s | ${total_cost:.4f}")
        log.info(summary)
        return summary

    # ── Pipeline steps ───────────────────────────────────

    async def _run_collector(self, date_str: str) -> tuple[bool, str]:
        cmd = [str(PYTHON), str(ENGINE_DIR / "collector.py"),
               "--domain", self.domain_name, "--date", date_str]
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
        """Read article count from today_context.json without loading full content."""
        try:
            data = json.loads(self.dc.data_dir.joinpath("today_context.json").read_text(encoding="utf-8"))
            return len(data.get("articles", []))
        except Exception:
            return -1

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
        return await self.gemini.run(
            prompt=user_prompt, system_prompt=system_prompt,
            model=self.gen_model, thinking=self.gen_thinking,
            timeout_seconds=self.gen_timeout,
        )

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
        # Use domain-specific email config if exists, else global
        domain_email = self.dc.dir / "config" / "email.json"
        email_config = domain_email if domain_email.exists() else BRIEFING_DIR / "config" / "email.json"
        if not email_config.exists():
            return False, "email.json not configured"

        output_file = self.dc.output_dir / f"{date_str}.md"
        if not output_file.exists():
            return False, f"Briefing not found: {output_file}"

        # Use the engine's notify.py with domain context
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

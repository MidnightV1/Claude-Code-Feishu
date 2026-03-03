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

        # ── Step 5: Keyword Evolution (fire-and-forget) ──
        if self.dc.keyword_evolution.get("enabled", False):
            asyncio.ensure_future(self._evolve_keywords_safe(date_str, briefing_md))

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

    async def _evolve_keywords_safe(self, date_str: str, briefing_md: str):
        try:
            await self._evolve_keywords(date_str, briefing_md)
        except Exception:
            log.exception("[%s] Keyword evolution failed", self.domain_name)

    async def _evolve_keywords(self, date_str: str, briefing_md: str):
        log.info("[%s] %s: running keyword evolution...", self.domain_name, date_str)
        evo_cfg = self.dc.keyword_evolution

        # Load feedback
        feedback_path = self.dc.data_dir / "keyword_feedback.json"
        if not feedback_path.exists():
            log.info("[%s] No keyword feedback yet, skipping", self.domain_name)
            return
        feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
        daily = feedback.get("daily", {})
        if len(daily) < 3:
            log.info("[%s] Only %d days of data, need 3+", self.domain_name, len(daily))
            return

        # Idempotency
        meta_path = self.dc.data_dir / "keywords_meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
        if meta.get("last_evolution_date") == date_str:
            log.info("[%s] Evolution already ran for %s", self.domain_name, date_str)
            return

        # Load dynamic keywords
        dynamic_path = self.dc.dir / "keywords_dynamic.yaml"
        dynamic = yaml.safe_load(dynamic_path.read_text(encoding="utf-8")) if dynamic_path.exists() else {}
        dynamic.setdefault("keywords", {})

        # Load static keywords
        sources_cfg = yaml.safe_load((self.dc.dir / "sources.yaml").read_text(encoding="utf-8"))
        static_kws = set()
        for kws in sources_cfg.get("keywords", {}).values():
            for kw in kws:
                static_kws.add(kw.lower())

        # Build prompt and call Sonnet
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

        # Parse response
        changes = self._parse_evolution_response(result.text)

        # Apply Sonnet's add suggestions
        added = self._apply_additions(dynamic, changes.get("add", []), static_kws, date_str)

        # Apply Sonnet's deprecate suggestions (model-driven quality judgment)
        model_deprecated = self._apply_deprecations(dynamic, changes.get("deprecate", []), date_str)

        # Run lifecycle transitions (data-driven: candidate→active, zero-streak→deprecated)
        transitions = self._run_lifecycle(dynamic, daily, date_str)

        # Save dynamic keywords
        dynamic_path.write_text(
            yaml.dump(dynamic, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8")

        # Update meta
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

        # Notify
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

        # Zero-hit streaks across days
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

        # High-hit keywords (potential noise — let Sonnet judge quality)
        high_hit = {kw: info for kw, info in kw_hits.items() if info.get("hits", 0) >= 10}

        # Supply chain layers
        sc = self.dc._cfg.get("supply_chain", [])
        layers = "\n".join(f"- {s['key']}: {s['name']} — {s['scope'][:80]}" for s in sc)

        # Active dynamic keywords
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
        """Apply Sonnet's deprecation suggestions (quality-based, not just zero-match)."""
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
        """Data-driven transitions: candidate→active (3d matches), *→deprecated (14d zero)."""
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

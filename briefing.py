# -*- coding: utf-8 -*-
"""Daily briefing pipeline — collector → Gemini generation → Claude review → email → Feishu."""

import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from models import LLMConfig, LLMResult
from gemini_api import GeminiAPI
from claude_cli import ClaudeCli
from dispatcher import Dispatcher

log = logging.getLogger("hub.briefing")

PYTHON = Path.home() / "python313/python/bin/python3"
BRIEFING_DIR = Path.home() / "briefing"
SCRIPTS_DIR = BRIEFING_DIR / "scripts"
DATA_DIR = BRIEFING_DIR / "data"
OUTPUT_DIR = DATA_DIR / "output"
CONTEXT_PATH = DATA_DIR / "today_context.json"
ENTITIES_PATH = DATA_DIR / "entities.json"

# ═══ Stage 1: Gemini generation prompt ═══

SYSTEM_PROMPT = """\
You are an AI industry analyst producing a daily briefing for a CFO-level decision-maker who has 3 minutes.
The reader cares about: business models, market dynamics, financing/capital sentiment, unit economics, \
and competitive landscape. Technical details matter only when they directly shift cost structures or enable new revenue.

## Signal Quality Classification (MOST IMPORTANT)

Classify each article into exactly ONE signal type:

| Signal Type | Definition | Treatment |
|-------------|-----------|-----------|
| **insight** | First-hand data, independent analysis, practitioner experience, contrarian view | Feature prominently. Core value of the briefing. |
| **event** | Verified new fact: product launch, funding, partnership, policy change | Report once with source count. Strip opinion, keep facts. |
| **echo** | Rewrites/reposts of the same event | Merge aggressively. One event = one line, note "N源确认". |
| **hype** | Analyst reports, PR dressed as news, superlatives without evidence | Quarantine to PR section. Interpret the motive, not the claim. |

How to judge:
- Contains data the author generated (created, measured, experienced)? → insight
- Reports something that happened with verifiable specifics? → event
- Restates someone else's report? → echo
- Argues a company/stock will go up or uses superlatives to promote? → hype
- Reddit/forum posts with genuine user reactions → insight (even if short)

## Supply Chain Categorization (business-first order)

Classify each article into ONE layer:
1. **市场 & 变现** — Funding, revenue, user metrics, market sizing, business models, platform strategy (红果, 抖音, ReelShort, Webtoon), monetization, M&A, IPO
2. **内容生产** — AI comics/short drama production, cost structures, creator economics, workflow tools, batch production cases, production efficiency
3. **基础设施 & 模型** — Video/image gen models (Kling, Sora, Runway, Pika, Seedance), tools (ComfyUI), character consistency tech — focus on cost/efficiency impact, not benchmarks
4. **政策 & 监管** — Regulations, content review, copyright, industry standards

## Multi-Source Event Merging

Articles sharing the same `cluster_id` likely report the same event.
- Merge echoes into ONE entry, note "（N源确认）"
- Keep the most informative version as representative

## PR / Soft Content Detection

For hype-type articles:
- Check `pr_signals` field from collector
- Interpret the motive: what does this PR reveal about the company's strategy?

## Fact-Checking & Anti-Hallucination (CRITICAL)

You are working from titles and summaries only. Hard rules:
- NEVER infer "Company X made Product Y" unless the summary explicitly states this
- NEVER fill in details the source didn't provide. Unknown → say "（厂商待查）"
- Every factual assertion in "值得细看" must trace back to a specific phrase in title/summary

Entity attribution confidence:
- ✓ = explicitly stated in summary
- ? = inferred, needs verification — mention with "（待核实）"

## New Entity Detection

Compare entities against `known_entities`. Any NOT in the list is "新变量".

## Trend Analysis

Use `topic_trends_7d` data:
- ↑ : >50% increase vs prior 7-day average
- → : within ±30%
- ↓ : >30% decrease
- 🆕 : first appearance in tracking window

## Output Format

Output a COMPLETE markdown briefing with this EXACT structure:

```
# AI漫剧·短剧 产业日报 | {date}

## 今日信号
> {One sentence: the single most important development for business/market. Be specific and opinionated.}

## 值得细看
{2-4 insight-type articles. Prioritize: financing, revenue data, market shifts, cost breakthroughs.}

- **[{title}]({url})**（{source type}）
  {2-3 sentences: core insight, key numbers, why it matters for business decisions}

## 重要事件
{event-type articles, deduplicated by cluster.}

### 市场 & 变现
- [{title}]({url}) — {one-sentence what happened} {（N源确认）}

### 内容生产
- ...

### 基础设施 & 模型
- ...

### 政策 & 监管
{Only if relevant articles exist.}

## ⚠️ PR 雷达
{hype-type articles, quarantined here.}
- {title} — PR概率: 高/中 | 动机解读: {strategy interpretation}

## 新变量
{Only if new entities discovered}
- 🆕 {entity type}: **{entity name}** — {context}

## 趋势追踪
| 话题 | 近7日提及 | 趋势 | 产业链位置 |
|------|----------|------|-----------|

## 原文索引
{ALL articles, grouped by language}

### 中文
1. [{title}]({url}) — {source}

### English
1. [{title}]({url}) — {source}
```

Key rules:
- "值得细看" is the headline — prioritize business intelligence over technical analysis
- "重要事件" is factual and terse — one line per event
- Never put echoes or hype in "重要事件"
- English insight/event articles MUST appear in main sections, not just index
- For model releases: focus on what it means for production cost/capability, not architecture details

After the briefing markdown, output a JSON block with new entities (✓ confidence only):
```json
{"new_entities": {"entity_name": "YYYY-MM-DD", ...}}
```"""

# ═══ Stage 2: Claude editorial review prompt ═══

REVIEW_PROMPT = """\
你是一家关注 AI 短剧/漫剧赛道的投资机构的产业分析师。\
读者是 CFO 级别决策者，每天花 3 分钟看这份日报。\
他关心：商业模式、市场格局、融资/资本态度、单位经济模型、竞争态势。\
技术细节只在直接影响成本结构或开启新营收时才值得提。\
你的工作是从 AI 记者的初稿中，筛出真正影响商业决策的信号，过滤掉技术噪声。

## 产业链定义（商业优先排序）

你只关心这四层，以及它们之间的传导关系：

1. **市场与变现** — 融资/并购、营收数据、用户增长/DAU、商业模式创新、\
平台策略（红果、ReelShort、FlexTV、Webtoon、B站）、付费转化、出海数据。\
⚠️ 这是 CFO 最关注的层——资金流向代表市场共识。

2. **内容生产** — AI 漫剧/短剧的成本结构、单位经济模型（单集成本、ROI）、\
量产案例、团队效率对比、创作者收入数据。\
⚠️ 关注"多少钱做一集""多久回本"，不是"用了什么工具"。

3. **基础设施** — 视频/图像生成模型（Seedance, Kling, Runway, Sora...）、\
工作流工具。\
⚠️ 通用大模型新闻（GPT-x、DeepSeek新版、Gemini新版）不算，\
除非明确涉及：视频生成能力、角色一致性、制作成本下降。\
⚠️ 模型发布只说对生产成本/效率的影响，跳过架构和跑分。

4. **政策与版权** — 内容审核、IP 版权、AI 生成内容的法律边界。

## 审稿规则

### 相关性判断（最重要）
- 每条信息问：**它如何影响"用 AI 做短剧/漫剧"这门生意的营收、成本、\
合规或竞争格局？** 答不上来 → 不相关 → 删除或降级到原文索引
- 模型发布 → 一句话说对制作成本的影响，删掉技术细节
- Reddit 帖子 → 只关心有成本/收入数据的创作者分享，忽略纯展示

### 信号重新定级（商业优先）
- 初稿的"值得细看"逐条审查：删掉这条，CFO 会错过什么商业判断？\
答不上来 → 降级到"重要事件"或直接删除
- 淡日不硬凑 — 宁可写"今日新增高价值商业信号有限"
- 强信号标准（按优先级）：\
融资/并购 > 营收/用户数据 > 成本结构变化 > 平台策略转向 > 量产验证 > 技术突破

### 跨日连续性
你会同时收到昨日日报。用它来：
- 标注续报："（续：昨日 XX 事件）"
- 发现趋势加速/减速
- 避免重复分析——昨天已详细分析的，今天只跟进增量

### 输出要求
- 保持原日报的 markdown 结构不变（注意板块顺序：市场&变现 → 内容生产 → 基础设施 → 政策）
- 重写"今日信号"— 用商业判断，不是技术总结
- 可以删减"值得细看"条目（0-4 条），可以将条目在板块间移动
- "新变量"只保留与产业链商业层直接相关的实体（新平台、新融资主体、新商业模式）
- 末尾加一段 `## 分析师备注`：2-3 句话，今天对商业决策最重要的判断或待观察点
- 直接输出最终版日报 markdown，不要解释你的修改过程"""


class BriefingPipeline:
    def __init__(
        self,
        gemini: GeminiAPI,
        claude: ClaudeCli,
        dispatcher: Dispatcher,
        config: dict | None = None,
    ):
        self.gemini = gemini
        self.claude = claude
        self.dispatcher = dispatcher
        cfg = config or {}
        self.model = cfg.get("model", "3-Flash")
        self.thinking = cfg.get("thinking", None)
        # Review (Stage 2) config
        review_cfg = cfg.get("review", {})
        self.review_enabled = review_cfg.get("enabled", False)
        self.review_model = review_cfg.get("model", "sonnet")
        self.review_timeout = review_cfg.get("timeout_seconds", 300)
        self._last_run: dict | None = None

    def descriptor(self) -> dict:
        """Plugin descriptor — declares commands and schedulable handlers."""
        return {
            "commands": [
                {
                    "prefix": "#briefing",
                    "handler": self.handle_command,
                    "help": (
                        "| `#briefing run [date]` | Run daily briefing pipeline |\n"
                        "| `#briefing status` | Last briefing run info |"
                    ),
                },
            ],
            "handlers": [
                {"name": "briefing", "fn": self.run},
            ],
        }

    async def handle_command(self, cmd: str, args: str) -> str:
        """Handle #briefing commands from Feishu."""
        # subcmd could be embedded in cmd (e.g. "#briefing run") or first word of args
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
            last = self._last_run
            if not last:
                return "No briefing has run yet in this session."
            started = datetime.fromtimestamp(last["started_at"]).strftime("%H:%M:%S")
            elapsed = last.get("elapsed_s", "?")
            return (
                f"**Last briefing run**\n"
                f"- Date: {last['date']}\n"
                f"- Status: {last['status']}\n"
                f"- Started: {started}\n"
                f"- Elapsed: {elapsed}s\n"
                f"- Model: {last.get('model', '?')}\n"
                f"- Review: {last.get('review_model', 'off')}\n"
                f"- Cost: ${last.get('cost_usd', 0):.4f}\n"
                + (f"- Errors: {', '.join(last.get('errors', []))}" if last.get("errors") else "")
            )

        if subcmd in ("run", "#briefing"):
            date_str = rest.strip().split()[0] if rest.strip() else None
            asyncio.ensure_future(self._run_safe(date_str))
            return f"日报 pipeline 已启动{' (' + date_str + ')' if date_str else ''}，进度会通过飞书通知。"

        return f"Unknown briefing subcommand: `{subcmd}`. Try `#briefing run [date]` or `#briefing status`."

    @property
    def last_run(self) -> dict | None:
        return self._last_run

    async def _run_safe(self, date_str: str | None = None):
        """Wrapper that catches unexpected errors and notifies."""
        try:
            await self.run(date_str)
        except Exception:
            log.exception("Briefing pipeline crashed")
            label = date_str or "today"
            try:
                await self.dispatcher.send_to_delivery_target(
                    f"❌ **日报 pipeline 异常崩溃** | {label}\n"
                    f"查看日志: `tail data/nas-claude-hub.log`"
                )
            except Exception:
                pass
            self._last_run = {
                "date": label,
                "started_at": time.time(),
                "status": "error",
                "error": "uncaught exception",
            }

    async def run(self, date_str: str | None = None) -> str:
        """Execute the full pipeline. Returns summary text."""
        date_str = date_str or datetime.now().strftime("%Y-%m-%d")
        # Validate date format
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return f"Invalid date: `{date_str}`. Expected YYYY-MM-DD."
        start = time.time()

        self._last_run = {
            "date": date_str,
            "started_at": start,
            "status": "running",
        }

        # ── Notify: pipeline started ──
        review_tag = f" → Claude {self.review_model} 审稿" if self.review_enabled else ""
        await self.dispatcher.send_to_delivery_target(
            f"📋 **日报 pipeline 启动** | {date_str}\n"
            f"采集 → Gemini {self.model} 生成{review_tag} → 邮件"
        )

        errors = []
        total_cost = 0.0

        # ── Step 1: Collect articles ──
        log.info("Briefing %s: collecting articles...", date_str)
        ok, collect_output = await self._run_collector(date_str)
        if not ok:
            msg = f"❌ **日报失败** | {date_str}\nStep 1 (采集) 失败:\n```\n{collect_output[-500:]}\n```"
            await self.dispatcher.send_to_delivery_target(msg)
            self._last_run["status"] = "error"
            self._last_run["error"] = "collector failed"
            return msg

        # ── Step 2: Generate briefing via Gemini API ──
        log.info("Briefing %s: generating via Gemini %s...", date_str, self.model)
        gen_result = await self._generate_briefing(date_str)
        if gen_result.is_error:
            msg = f"❌ **日报失败** | {date_str}\nStep 2 (生成) 失败: {gen_result.text[:500]}"
            await self.dispatcher.send_to_delivery_target(msg)
            self._last_run["status"] = "error"
            self._last_run["error"] = "generation failed"
            return msg

        briefing_md, new_entities = self._parse_generation_output(gen_result.text)
        total_cost += gen_result.cost_usd

        # Save draft
        output_path = OUTPUT_DIR / f"{date_str}.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # ── Step 3: Claude editorial review (optional) ──
        review_model_used = "off"
        if self.review_enabled:
            log.info("Briefing %s: Claude %s reviewing...", date_str, self.review_model)
            await self.dispatcher.send_to_delivery_target(
                f"🔍 **审稿中** | Claude {self.review_model} 正在审阅初稿..."
            )
            review_result = await self._review_briefing(briefing_md, date_str)
            if review_result.is_error:
                log.warning("Review failed, using Gemini draft: %s", review_result.text[:200])
                errors.append(f"审稿失败(用初稿): {review_result.text[:100]}")
            else:
                # Save draft for reference, use reviewed version as final
                draft_path = OUTPUT_DIR / f"{date_str}-draft.md"
                draft_path.write_text(briefing_md, encoding="utf-8")
                briefing_md = review_result.text
                total_cost += review_result.cost_usd
                review_model_used = self.review_model
                log.info("Review done: %dms, $%.4f",
                         review_result.duration_ms, review_result.cost_usd)

        # Save final briefing
        output_path.write_text(briefing_md, encoding="utf-8")
        log.info("Briefing saved to %s", output_path)

        # Update entities.json
        if new_entities:
            self._update_entities(new_entities)

        # ── Step 4: Send email ──
        log.info("Briefing %s: sending email...", date_str)
        email_ok, email_output = await self._send_email(date_str)
        if email_ok:
            recipients = self._get_email_recipients()
            await self.dispatcher.send_to_delivery_target(
                f"✅ **日报已发送** | {date_str}\n"
                f"收件人: {recipients}\n"
                f"生成: Gemini {self.model} ({gen_result.duration_ms/1000:.1f}s)"
                + (f" | 审稿: Claude {review_model_used}" if review_model_used != "off" else "")
                + f"\n总成本: ${total_cost:.4f}"
            )
        else:
            errors.append(f"邮件发送失败: {email_output[-200:]}")
            await self.dispatcher.send_to_delivery_target(
                f"⚠️ **日报生成成功，邮件发送失败** | {date_str}\n"
                f"```\n{email_output[-300:]}\n```"
            )

        elapsed = time.time() - start
        self._last_run.update({
            "status": "ok" if not errors else "partial",
            "elapsed_s": round(elapsed, 1),
            "model": self.model,
            "review_model": review_model_used,
            "cost_usd": total_cost,
            "errors": errors,
        })

        summary = (
            f"日报完成 | {date_str} | {self.model}"
            + (f"+{review_model_used}" if review_model_used != "off" else "")
            + f" | {elapsed:.0f}s | ${total_cost:.4f}"
        )
        if errors:
            summary += f" | ⚠️ {'; '.join(errors)}"
        log.info(summary)
        return summary

    # ═══ Pipeline steps ═══

    async def _run_collector(self, date_str: str) -> tuple[bool, str]:
        """Run collector.py as subprocess."""
        cmd = [str(PYTHON), str(SCRIPTS_DIR / "collector.py"), date_str]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(BRIEFING_DIR),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                log.error("Collector failed (rc=%d): %s", proc.returncode, output[-500:])
                return False, output
            log.info("Collector done: %s", output.strip().split("\n")[-1])
            return True, output
        except asyncio.TimeoutError:
            proc.kill()
            return False, "Collector timed out (300s)"
        except Exception as e:
            return False, str(e)

    async def _generate_briefing(self, date_str: str) -> LLMResult:
        """Stage 1: Generate briefing from collected context via Gemini API."""
        if not CONTEXT_PATH.exists():
            return LLMResult(text="today_context.json not found", is_error=True)

        context = CONTEXT_PATH.read_text(encoding="utf-8")

        user_prompt = (
            f"Date: {date_str}\n\n"
            f"## Collected article data:\n\n"
            f"{context}\n\n"
            f"Generate the daily briefing following the system instructions precisely."
        )

        return await self.gemini.run(
            prompt=user_prompt,
            system_prompt=SYSTEM_PROMPT,
            model=self.model,
            thinking=self.thinking,
            timeout_seconds=180,
        )

    async def _review_briefing(self, draft: str, date_str: str) -> LLMResult:
        """Stage 2: Claude editorial review with cross-day context."""
        # Load yesterday's briefing for continuity
        yesterday_str = (
            datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        yesterday_path = OUTPUT_DIR / f"{yesterday_str}.md"

        prompt_parts = []
        if yesterday_path.exists():
            yesterday_md = yesterday_path.read_text(encoding="utf-8")
            prompt_parts.append(f"## 昨日日报（{yesterday_str}）\n\n{yesterday_md}")
        else:
            prompt_parts.append("（无昨日日报数据）")

        prompt_parts.append(f"## 今日初稿（{date_str}）\n\n{draft}")
        prompt_parts.append("请按照系统指令审稿，直接输出最终版日报 markdown。")

        return await self.claude.run(
            prompt="\n\n---\n\n".join(prompt_parts),
            system_prompt=REVIEW_PROMPT,
            model=self.review_model,
            timeout_seconds=self.review_timeout,
        )

    async def _send_email(self, date_str: str) -> tuple[bool, str]:
        """Run notify.py as subprocess."""
        email_config = BRIEFING_DIR / "config" / "email.json"
        if not email_config.exists():
            return False, "email.json not configured"

        output_file = OUTPUT_DIR / f"{date_str}.md"
        if not output_file.exists():
            return False, f"Briefing file not found: {output_file}"

        cmd = [str(PYTHON), str(SCRIPTS_DIR / "notify.py"), date_str]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(BRIEFING_DIR),
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            output = stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                log.error("Email send failed (rc=%d): %s", proc.returncode, output)
                return False, output
            log.info("Email sent: %s", output.strip())
            return True, output
        except asyncio.TimeoutError:
            return False, "Email send timed out (60s)"
        except Exception as e:
            return False, str(e)

    # ═══ Helpers ═══

    @staticmethod
    def _parse_generation_output(text: str) -> tuple[str, dict]:
        """Split generation output into briefing markdown and new_entities JSON."""
        import re
        new_entities = {}
        briefing_md = text

        m = re.search(r'```json\s*\n(\{.*?"new_entities".*?\})\s*\n```', text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                new_entities = data.get("new_entities", {})
            except json.JSONDecodeError:
                pass
            briefing_md = text[:m.start()].rstrip()

        return briefing_md, new_entities

    @staticmethod
    def _update_entities(new_entities: dict):
        """Merge new entities into entities.json."""
        existing = {}
        if ENTITIES_PATH.exists():
            try:
                existing = json.loads(ENTITIES_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        existing.update(new_entities)
        ENTITIES_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Entities updated: +%d new", len(new_entities))

    @staticmethod
    def _get_email_recipients() -> str:
        """Read recipient list from email.json for notification display."""
        try:
            config = json.loads((BRIEFING_DIR / "config" / "email.json").read_text())
            return ", ".join(config.get("recipients", []))
        except Exception:
            return "(unknown)"

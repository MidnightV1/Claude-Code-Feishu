# -*- coding: utf-8 -*-
"""Session processing mixin — LLM routing, skill matching, reply caching.

Extracted from FeishuBot to reduce monolith size.
Used as a mixin: class FeishuBot(MediaMixin, SessionMixin, ...)
"""

import re
import random
import time
import asyncio
import logging
from dataclasses import replace

from agent.infra.models import LLMConfig, llm_config_from_dict
from agent.infra.store import save_json_sync
from agent.platforms.feishu.dispatcher import Dispatcher
log = logging.getLogger("hub.feishu_bot")

# Skill prefixes: one-shot model override with optional system prompt.
SKILL_ROUTES = {
    "#plan": ("claude-cli", "opus", (
        "你是架构师。基于之前的对话上下文，输出结构化的实施方案。"
        "包含：目标、方案对比（推荐+理由）、实施步骤、风险点。简洁精准。"
    )),
    "#review": ("claude-cli", "opus", (
        "你是 code reviewer。基于对话上下文，深度审查相关代码/方案。"
        "关注：正确性、边界情况、性能、安全性。给出具体改进建议。"
    )),
    "#analyze": ("claude-cli", "opus", (
        "你是分析师。基于对话上下文，做深度分析。"
        "厘清本质问题，给出有洞察的结论和可执行建议。"
    )),
}

# Skills that trigger max effort (deep reasoning tasks).
_MAX_EFFORT_RE = re.compile(
    r"投资|板块|基金|仓位|加仓|减仓|博弈|推演|对抗|策略分析|方案审查|学者画像|论文分析"
)


# ── Thinking card status word pools ──
# Rotated randomly during idle phases of LLM processing.

_THINKING_POOL = [
    "腌制中，让想法入味…", "慢炖，用文火…", "酝酿中…", "思考中…",
    "神游中…", "脑子在转…", "在线发呆（其实在想）…", "CPU 过热中…",
    "正在顿悟…", "酝酿思路…", "沉淀中…", "进入意识流…",
    "脑细胞正在开会…", "正在加载人生经验…", "让我消化一下…",
]

# After 60s: relaxed but implies working hard
_LONG_THINKING = [
    "慢工出细活，不要慌…", "卧槽，大活儿，还得想想…",
    "差点顿悟了，让我冷静下…", "GPU已经起飞…",
    "喝口水，别急，你也喝点，干杯", "Moss，这道题怎么解呀...",
    "正在求助祖师爷...", "神经网络迸发出了灵感...",
    "正在唤醒专家网络...", "正在烧香...", "正在掐指一算...",
    "嘿嘿嘿...", "什么情况...", "？？？", "hummmm...",
    "哦...", "嗯？", "尝试甩锅给CPU...", "正在蒸馏deepseek...",
    "正在和自己对线...", "Warning: 思路溢出...", "脑子：已读不回",
    "正在请求上级支援...", "快了快了（经典谎言）",
    "等等，好像悟了...又没有", "别催，灵感不接受加班",
    "正在向赛博佛祖祈祷...", "道生一，一生二，二生 bug...",
]

_ACTIVITY_MIN_INTERVAL = 5.0  # seconds between tool activity card updates

# Transient error markers — keep session alive for retry
_TRANSIENT_MARKERS = ("Timeout", "ld.so", "dl-open.c")

_REPLY_CACHE_MAX = 500   # max cached bot replies for quote-reply support
_MSG_KEY_MAP_MAX = 200   # max message→key mappings for recall support
_LONG_CONTENT_THRESHOLD = 3500  # chars — auto-redirect to Feishu doc above this

# Whitelist tag: CC wraps user-facing output in <reply-to-user> tags.
# If present, only tagged content is sent; if absent, full text is sent (fallback).
# Greedy match: grab outermost tags (model may quote the tag syntax in examples).
_REPLY_TAG_RE = re.compile(r'<reply-to-user>(.*)</reply-to-user>', re.DOTALL)
# Fallback: opening tag present but closing tag missing (context truncation).
# Extract everything after the opening tag.
_REPLY_OPEN_RE = re.compile(r'<reply-to-user>\s*', re.DOTALL)
_EXPLORE_TAG_RE = re.compile(r'<next-explore>(.*)</next-explore>', re.DOTALL)
# Fallback for unclosed explore tags
_EXPLORE_OPEN_RE = re.compile(r'<next-explore>.*', re.DOTALL)
_REFLECT_TAG_RE = re.compile(r'<next-reflect>(.*)</next-reflect>', re.DOTALL)
_REFLECT_OPEN_RE = re.compile(r'<next-reflect>.*', re.DOTALL)


class SessionMixin:
    """LLM session management, skill routing, batch processing, reply caching.

    Expects self to have: router, dispatcher, file_store, heartbeat,
    default_llm, _thinking_cards, _running_tasks, _reply_cache,
    _reply_cache_path, _msg_to_key, _feishu_api.
    """

    async def _process_batch(self, key, batch):
        """Process a single batch: combine parts → LLM → reply.

        Must be called under session lock.
        """
        from agent.platforms.feishu.bot import FEISHU_SYSTEM_PROMPT

        # Register flush task early for recall support
        self._running_tasks[key] = asyncio.current_task()

        combined = "\n\n".join(batch.parts)
        session_key = self._session_key(batch.chat_type, batch.chat_id, batch.sender_id)

        # MessageStore: mark all batch messages as processing
        _ms = getattr(self, 'message_store', None)
        _batch_mids = list(batch.message_ids) if batch.message_ids else []
        if _ms and _batch_mids:
            _ms.update_state(_batch_mids, "processing")

        # ── Orchestrator confirmation intercept ──
        if hasattr(self, 'orchestrator') and self.orchestrator and self.orchestrator.has_pending(session_key):
            from agent.orchestrator.engine import Orchestrator
            if Orchestrator.is_confirmation(combined):
                plan = self.orchestrator.confirm(session_key)
                if plan:
                    plan.chat_id = batch.chat_id
                    await self.dispatcher.send_text(
                        batch.chat_id,
                        "🚀 开始并行执行！执行期间你可以继续聊天。",
                        reply_to=batch.first_message_id,
                    )
                    asyncio.create_task(
                        self._orchestrate_execute(plan, batch.chat_id)
                    )
                    self._running_tasks.pop(key, None)
                    return
            elif Orchestrator.is_cancellation(combined):
                self.orchestrator.cancel(session_key)
                await self.dispatcher.send_text(
                    batch.chat_id, "已取消并行任务。",
                    reply_to=batch.first_message_id,
                )
                self._running_tasks.pop(key, None)
                return
            else:
                # Not a confirmation — silently cancel pending plan, process as normal
                self.orchestrator.cancel(session_key)

        # Build abort button (inline with thinking text, no emoji for clean look)
        _abort_btn_value = {"action": "abort_task", "key": key}
        _abort_btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "中止"},
            "type": "danger",
            "size": "small",
            "value": _abort_btn_value,
        }

        def _thinking_row(md_text: str) -> dict:
            """Column_set: markdown stretches, abort button auto-width."""
            return {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {"tag": "column", "width": "weighted", "weight": 1,
                     "vertical_align": "center",
                     "elements": [{"tag": "markdown", "content": md_text}]},
                    {"tag": "column", "width": "auto",
                     "vertical_align": "center",
                     "elements": [_abort_btn]},
                ],
            }

        _init_card = Dispatcher.build_interactive_card([
            _thinking_row("💭 脑子在转…"),
        ])

        # Send "thinking" card as immediate feedback (reuse if pre-created by audio flow)
        thinking_msg_id = self._thinking_cards.get(key)
        if thinking_msg_id:
            # Pre-created (e.g. voice "🎙️ 语音识别中…") → update with abort button
            await self.dispatcher.update_card_raw(thinking_msg_id, _init_card)
        else:
            thinking_msg_id = await self.dispatcher.send_card_raw(
                batch.chat_id, _init_card,
                reply_to=batch.first_message_id,
            )
            if thinking_msg_id:
                self._thinking_cards[key] = thinking_msg_id
        if batch.received_at:
            io_latency = time.time() - batch.received_at
            log.info("IO latency: %.0fms (recv → thinking card)", io_latency * 1000)

        try:
            llm_config, prompt = self._resolve_skill(combined, session_key)

            # Inject Feishu channel context + file context for Claude sessions
            if llm_config.provider == "claude-cli":
                from agent.orchestrator.prompts import ORCHESTRATION_PROMPT
                parts = [FEISHU_SYSTEM_PROMPT]
                if getattr(self, '_extra_system_prompt', ''):
                    parts.append(self._extra_system_prompt)
                if llm_config.system_prompt:
                    parts.append(llm_config.system_prompt)
                # Orchestration capability (only for Opus main sessions)
                if hasattr(self, 'orchestrator') and self.orchestrator:
                    parts.append(ORCHESTRATION_PROMPT)
                # Sender + timestamp injected into user prompt (dynamic per-message)
                from datetime import datetime as _dt
                _now = _dt.now().strftime("%Y-%m-%d %H:%M")
                sender_tag = batch.sender_name or ""
                if batch.chat_type == "group" and sender_tag:
                    sender_tag += f"@{batch.chat_id[:8]}"
                prompt = (
                    f"<user-input>\n[{_now}] {sender_tag}: {prompt}\n</user-input>"
                    if sender_tag
                    else f"<user-input>\n[{_now}] {prompt}\n</user-input>"
                )
                # File context: filtered by recent conversation history
                session_entry = self.router._sessions.get(session_key, {})
                history = session_entry.get("history", [])
                from agent.llm.router import SUMMARY_THRESHOLD
                recent = history[-(SUMMARY_THRESHOLD * 2):] if history else None
                file_context = self.file_store.get_context_prompt(
                    session_key, recent_history=recent,
                )
                if file_context:
                    parts.append(file_context)
                # Inject pending heartbeat notifications into user prompt
                notifications = self.heartbeat.drain_notifications(session_key)
                if notifications:
                    notice_block = (
                        "====== 后台通知（已通过飞书独立送达用户）======\n"
                        "以下事件发生在上次对话之后。优先回复用户当前消息；\n"
                        "仅当通知内容与用户话题相关或需要用户关注时，在回复末尾简要提及。\n"
                        + "\n".join(notifications)
                        + "\n============\n\n"
                    )
                    prompt = notice_block + prompt
                llm_config = replace(llm_config, system_prompt="\n\n".join(parts))

            # ── Progress: tool activity + todo progress on thinking card ──
            _last_activity = [time.monotonic()]  # tracks last real tool event
            _todos: list[dict] = []  # latest TodoWrite snapshot

            _last_tool_label = [""]  # persists latest tool activity across renders

            def _render_card(activity: str = "") -> str:
                """Build card JSON with activity, todos, and abort button."""
                if activity:
                    _last_tool_label[0] = activity
                top = _last_tool_label[0] or "💭 脑子在转…"
                elements: list[dict] = [_thinking_row(top)]
                if _todos:
                    _icons = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
                    lines = []
                    for t in _todos:
                        icon = _icons.get(t.get("status", "pending"), "⬜")
                        label = t.get("activeForm") if t.get("status") == "in_progress" else t.get("content", "")
                        lines.append(f"{icon} {label}")
                    elements.append({"tag": "hr"})
                    elements.append({"tag": "markdown", "content": "\n".join(lines)})
                return Dispatcher.build_interactive_card(elements)

            def _idle_label(elapsed: float) -> str:
                pool = _LONG_THINKING if elapsed >= 60 else _THINKING_POOL
                label = random.choice(pool)
                if elapsed >= 30:
                    m, s = divmod(int(elapsed), 60)
                    ts = f"{m}m{s:02d}s" if m else f"{s}s"
                    return f"💭 {label} ({ts})"
                return f"💭 {label}"

            async def _on_todo(todos):
                _todos.clear()
                if isinstance(todos, list):
                    _todos.extend(t for t in todos if isinstance(t, dict))
                _last_activity[0] = time.monotonic()
                if thinking_msg_id:
                    await self.dispatcher.update_card_raw(thinking_msg_id, _render_card())

            async def _on_activity(label: str):
                now = time.monotonic()
                if now - _last_activity[0] < _ACTIVITY_MIN_INTERVAL:
                    return
                _last_activity[0] = now
                if thinking_msg_id:
                    await self.dispatcher.update_card_raw(thinking_msg_id, _render_card(label))

            async def _pulse():
                """Background heartbeat: update card when idle."""
                start = time.monotonic()
                await asyncio.sleep(8)  # first pulse after 8s
                while True:
                    elapsed = time.monotonic() - start
                    since_activity = time.monotonic() - _last_activity[0]
                    if since_activity >= 6 and thinking_msg_id:
                        try:
                            idle = _idle_label(elapsed)
                            await self.dispatcher.update_card_raw(
                                thinking_msg_id,
                                _render_card(idle),
                            )
                        except Exception as exc:
                            log.warning("_pulse: update_card failed: %s", exc)
                    await asyncio.sleep(8)

            on_act = _on_activity if thinking_msg_id else None
            on_td = _on_todo if thinking_msg_id else None
            pulse_task = asyncio.create_task(_pulse()) if thinking_msg_id else None

            # Wrap LLM call in a tracked task for cancel support
            llm_coro = self.router.run(
                prompt=prompt, llm_config=llm_config, session_key=session_key,
                on_activity=on_act, on_todo=on_td,
            )
            llm_task = asyncio.create_task(llm_coro)
            self._running_tasks[key] = llm_task

            try:
                result = await llm_task
            except asyncio.CancelledError:
                log.info("LLM task cancelled for %s", key)
                # Ensure llm_task is also cancelled (in case outer task was cancelled)
                if not llm_task.done():
                    llm_task.cancel()
                # If abort handler already updated the card, it popped from _thinking_cards.
                # Only delete if still tracked (= recall, not abort).
                if thinking_msg_id and key in self._thinking_cards:
                    asyncio.create_task(self._safe_delete_card(thinking_msg_id))
                    self._thinking_cards.pop(key, None)
                self.router.remove_last_round(session_key)
                asyncio.create_task(self.router.save_session(session_key))
                return
            finally:
                self._running_tasks.pop(key, None)
                if pulse_task:
                    pulse_task.cancel()

            if result.cancelled:
                if thinking_msg_id:
                    asyncio.create_task(self._safe_delete_card(thinking_msg_id))
                if _ms and _batch_mids:
                    _ms.update_state(_batch_mids, "failed")
                return

            if result.is_error:
                log.warning("LLM error (session=%s): %s", session_key, result.text[:200])
                # MessageStore: mark failed on LLM error
                if _ms and _batch_mids:
                    _ms.update_state(_batch_mids, "failed")
                # Transient errors: keep session — next --resume may succeed
                is_transient = (
                    result.text == ""
                    or any(m in result.text for m in _TRANSIENT_MARKERS)
                )
                if not is_transient:
                    self.router.clear_session(session_key)
                    asyncio.create_task(self.router.save_session(session_key))

                err_hint = ""
                if "Timeout" in result.text:
                    err_hint = "（响应超时）"
                elif any(m in result.text for m in ("ld.so", "dl-open.c")):
                    err_hint = "（环境异常）"

                if is_transient:
                    reply_text = f"处理出错{err_hint}，请重新发送消息重试。"
                else:
                    reply_text = f"处理出错{err_hint}，已重置会话。请重新发送消息继续。"
            elif result.text:
                # ── Orchestrator: detect <task_plan> in Opus response ──
                if (hasattr(self, 'orchestrator') and self.orchestrator
                        and "<task_plan>" in result.text):
                    from agent.orchestrator.engine import Orchestrator as Orch
                    clean_text, plan = Orch.extract_plan_from_response(result.text)
                    if plan:
                        plan.chat_id = batch.chat_id
                        self.orchestrator.set_pending(session_key, plan)
                        # Replace result with clean text + plan confirmation card
                        result = result.__class__(
                            text=clean_text + "\n\n" + plan.render_plan(),
                            session_id=result.session_id,
                            duration_ms=result.duration_ms,
                            cost_usd=result.cost_usd,
                        )

                footer_parts = [f for f in batch.footers if f]
                if llm_config.provider == "gemini-api" and result.cost_usd > 0:
                    footer_parts.append(
                        f"`{llm_config.provider}/{llm_config.model}"
                        f" | ${result.cost_usd:.4f} | {result.duration_ms}ms`"
                    )
                footer = ("\n\n" + "\n".join(footer_parts)) if footer_parts else ""
                reply_text = result.text + footer
            else:
                reply_text = None

            # ── Reply-to-user whitelist filter ──
            # Tagged content is already prioritized at CLI layer (claude.py scans
            # assistant events). Here we just extract the tagged portion.
            # No retry nudge needed — if tags are missing, it means the model
            # genuinely didn't produce them (not a race condition).
            # NOTE: extraction happens BEFORE internal tag stripping, because
            # <next-explore>/<next-reflect> are outside <reply-to-user> tags —
            # extracting first naturally excludes them without regex that could
            # misfire on literal tag mentions inside the reply content.
            _reply_extracted = False
            if reply_text:
                tagged = _REPLY_TAG_RE.findall(reply_text)
                if tagged:
                    filtered = "\n\n".join(t.strip() for t in tagged if t.strip())
                    if filtered:
                        reply_text = filtered
                        _reply_extracted = True
                        # Re-append footer (was mixed into pre-filter text)
                        if footer:
                            reply_text += footer
                    else:
                        # All tagged content was empty → pure notification response
                        reply_text = None
                elif _REPLY_OPEN_RE.search(reply_text):
                    # Opening tag present but closing tag missing — context
                    # truncation or max_tokens cutoff.  Extract content after
                    # the opening tag and strip the dangling tag.
                    log.warning(
                        "reply-to-user closing tag missing (session=%s), "
                        "extracting content after opening tag",
                        session_key,
                    )
                    reply_text = _REPLY_OPEN_RE.sub("", reply_text).strip()
                    # Also strip a dangling </reply-to-user> if partially present
                    reply_text = reply_text.replace("</reply-to-user>", "").strip()
                    _reply_extracted = True
                    if footer:
                        reply_text += footer
                elif llm_config.provider == "claude-cli" and not result.is_error:
                    # Tags genuinely missing from LLM output (model didn't use them).
                    # Don't log for error-path replies (timeout/env errors) — those are
                    # user-friendly hardcoded strings, not semantic breaks.
                    log.info(
                        "reply-to-user tags absent (session=%s), sending raw output",
                        session_key,
                    )

            # ── Strip internal tags (defense in depth) ──
            # Only when reply-to-user extraction FAILED — if extraction
            # succeeded, <next-explore>/<next-reflect> are already excluded
            # (they live outside <reply-to-user>). Stripping on extracted
            # content would misfire on literal tag mentions in reply text.
            if reply_text and not _reply_extracted:
                reply_text = _EXPLORE_TAG_RE.sub("", reply_text)
                reply_text = _EXPLORE_OPEN_RE.sub("", reply_text)
                reply_text = _REFLECT_TAG_RE.sub("", reply_text)
                reply_text = _REFLECT_OPEN_RE.sub("", reply_text)
                reply_text = reply_text.strip()

            # Hub 3.0: detect dev signals in LLM output
            if reply_text and self.on_dev_signal and "<dev_signal>" in reply_text:
                _m = re.search(r"<dev_signal>(.*?)</dev_signal>", reply_text, re.DOTALL)
                if _m:
                    _signal_text = _m.group(1).strip()
                    asyncio.create_task(self._handle_dev_signal(_signal_text))
                    reply_text = re.sub(r"<dev_signal>.*?</dev_signal>", "", reply_text, flags=re.DOTALL).strip()

            # Delete thinking card, then send final reply as new message (with sound)
            if batch.received_at:
                total_latency = time.time() - batch.received_at
                log.info("Total latency: %.1fs (recv → reply ready)", total_latency)
            if thinking_msg_id:
                await self.dispatcher.delete_message(thinking_msg_id)
            if reply_text:
                if len(reply_text) > _LONG_CONTENT_THRESHOLD:
                    reply_mid = await self._send_long_as_doc(
                        batch.chat_id, reply_text,
                        reply_to=batch.first_message_id,
                    )
                else:
                    reply_mid = await self.dispatcher.send_text(
                        batch.chat_id, reply_text,
                        reply_to=batch.first_message_id,
                    )
                if reply_mid:
                    self._cache_reply(reply_mid, reply_text)
                    # MessageStore: mark completed with response_id
                    if _ms and _batch_mids:
                        _ms.update_state(_batch_mids, "completed", response_id=reply_mid)
                else:
                    # Delivery failed (all retries exhausted) — mark as failed so the
                    # semantic-break rate metric stays accurate. The Dispatcher already
                    # logs the underlying network error via error_tracker.
                    log.error("send_text returned None — response generated but not delivered "
                              "(chat=%s)", batch.chat_id)
                    if _ms and _batch_mids:
                        _ms.update_state(_batch_mids, "failed")

            # ── Explore hints: async task creation (non-blocking) ──
            if result and result.explore_hints:
                asyncio.create_task(self._process_explore_hints(result.explore_hints))

            # ── Reflect hints: async memory persistence (non-blocking) ──
            if result and result.reflect_hints:
                asyncio.create_task(self._process_reflect_hints(result.reflect_hints))
        except asyncio.CancelledError:
            log.info("Flush cancelled for %s during setup (user recalled)", key)
            if thinking_msg_id:
                asyncio.create_task(self._safe_delete_card(thinking_msg_id))
        except Exception as e:
            log.error("Batch processing error: %s", e, exc_info=True)
            # MessageStore: mark failed
            if _ms and _batch_mids:
                _ms.update_state(_batch_mids, "failed")
            if thinking_msg_id:
                await self.dispatcher.delete_message(thinking_msg_id)
            err_type = type(e).__name__
            if "timeout" in str(e).lower() or "Timeout" in err_type:
                err_msg = "请求超时，请稍后重试。"
            else:
                err_msg = f"处理出错（{err_type}），请重试。"
            try:
                await self.dispatcher.send_text(
                    batch.chat_id, err_msg,
                    reply_to=batch.first_message_id,
                )
            except Exception as exc:
                pass
        finally:
            self._thinking_cards.pop(key, None)
            self._running_tasks.pop(key, None)
            # Keep _msg_to_key entries for recall-after-completion support.
            if len(self._msg_to_key) > _MSG_KEY_MAP_MAX:
                excess = len(self._msg_to_key) - _MSG_KEY_MAP_MAX
                for k in list(self._msg_to_key)[:excess]:
                    self._msg_to_key.pop(k, None)

    # ═══ Dev Signal Routing ═══

    async def _handle_dev_signal(self, signal_text: str):
        """Route detected dev signal to MAQS pipeline via callback."""
        try:
            if self.on_dev_signal:
                await self.on_dev_signal(signal_text)
        except Exception as e:
            log.warning("Dev signal handler failed: %s", e)

    # ═══ Explore Hints Processing ═══

    async def _process_explore_hints(self, raw_text: str):
        """Extract <next-explore> content and create Feishu tasks asynchronously.

        Runs as fire-and-forget task after reply is sent. Errors are logged,
        never propagated. Uses Sonnet to evaluate hints, then task_ctl to create tasks.
        """
        try:
            match = _EXPLORE_TAG_RE.search(raw_text)
            if not match:
                return
            hints_text = match.group(1).strip()
            if not hints_text:
                return

            log.info("Explore hints captured: %s", hints_text[:200])

            # Evaluate hints with Goal Tree context (Sonnet, moderate depth)
            from agent.infra import goal_tree as _gt
            _tree = _gt.load()
            _tree_ctx = _gt.format_for_prompt(_tree, max_goals=5)

            eval_prompt = (
                "你是探索方向评估专家。严格筛选，宁缺勿滥。\n\n"
                f"## 系统目标\n{_tree_ctx}\n\n"
                f"## 候选方向\n{hints_text}\n\n"
                "## 通过条件（必须全部满足）\n"
                "1. **决策影响**：这个探索的结论是否会在 2 周内影响一个具体决策？"
                "（架构选型、优先级排序、技术路线）。如果只是\"了解一下\"，淘汰。\n"
                "2. **信息缺口**：答案是否需要外部搜索/实验/深度代码分析才能得到？"
                "如果读 10 分钟代码就能回答，淘汰。\n"
                "3. **杠杆效应**：解决后能消除一类问题还是只解决一个点？"
                "只解决一个点的，淘汰。\n\n"
                "## 直接淘汰\n"
                "- 部署验证、运维确认、配置检查\n"
                "- 已知答案但还没执行的事项（那是任务不是探索）\n"
                "- 过于宽泛无法用数据回答的问题\n"
                "- 和系统目标无关的技术好奇心\n\n"
                "通过筛选的方向输出 JSON 数组：\n"
                '```json\n[{"title": "具体的决策问题（问句形式）", "priority": "P1", '
                '"goal_id": "G1", "rationale": "一句话：回答这个问题后，什么决策会改变"}]\n```\n'
                "大部分候选应该被淘汰。如果没有值得探索的，输出空数组 []。只输出 JSON。"
            )
            eval_config = LLMConfig(
                provider="claude-cli", model="opus",
            )
            eval_result = await self.router.run(
                prompt=eval_prompt, llm_config=eval_config,
            )
            if eval_result.is_error or not eval_result.text.strip():
                log.warning("Explore hint evaluation failed: %s", eval_result.text[:100])
                return

            # Parse JSON from response
            import json as _json
            text = eval_result.text.strip()
            # Extract JSON from code block if present
            if "```" in text:
                import re as _re
                json_match = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, _re.DOTALL)
                if json_match:
                    text = json_match.group(1).strip()

            try:
                tasks = _json.loads(text)
            except _json.JSONDecodeError:
                if "overloaded" in text or "529" in text:
                    log.info("Explore hint eval skipped (API overloaded): %s", text[:100])
                else:
                    log.warning("Explore hint eval returned non-JSON: %s", text[:200])
                return

            if not isinstance(tasks, list) or not tasks:
                log.info("Explore hints evaluated: no actionable items")
                return

            # Create Feishu tasks via task_ctl.py
            from pathlib import Path as _Path
            _proj = str(_Path(__file__).resolve().parent.parent.parent.parent)
            task_script = f"{_proj}/.claude/skills/feishu-task/scripts/task_ctl.py"
            created = 0
            for item in tasks[:5]:  # cap at 5 per conversation
                title = item.get("title", "").strip()
                if not title:
                    continue
                tagged_title = f"[explore] {title}"
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "python3", task_script, "create", tagged_title,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=_proj,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
                    if proc.returncode == 0:
                        created += 1
                        log.info("Explore task created: %s", tagged_title)
                    else:
                        log.warning("task_ctl failed for '%s': rc=%d", tagged_title, proc.returncode)
                except Exception as e:
                    log.warning("Failed to create explore task '%s': %s", tagged_title, e)

            if created:
                log.info("Explore pipeline: %d tasks created from %d hints", created, len(tasks))

        except Exception as e:
            log.error("Explore hints processing error: %s", e, exc_info=True)

    # ═══ Reflect Hints Processing ═══

    async def _process_reflect_hints(self, raw_text: str):
        """Extract <next-reflect> content and persist as memory.

        Pipeline: extract → Sonnet evaluate (novelty + generalizability) →
        write memory file → update MEMORY.md index.
        feedback type: auto-persist (L1). soul/cognition type: notify user (L2).
        """
        try:
            match = _REFLECT_TAG_RE.search(raw_text)
            if not match:
                return
            hints_text = match.group(1).strip()
            if not hints_text:
                return

            log.info("Reflect hints captured: %s", hints_text[:200])

            # Load existing MEMORY.md to check for duplicates
            from pathlib import Path as _Path
            import os as _os
            _proj = _Path(__file__).resolve().parent.parent.parent.parent
            _home = _Path(_os.path.expanduser("~"))
            _proj_slug = str(_proj).replace("/", "-").lstrip("-")
            memory_dir = _home / ".claude" / "projects" / f"-{_proj_slug}" / "memory"
            memory_index = memory_dir / "MEMORY.md"
            existing_index = ""
            if memory_index.exists():
                existing_index = memory_index.read_text()

            eval_prompt = (
                "你是反思评估专家。评估以下从互动中提炼的模式/原则是否值得持久化为记忆。\n\n"
                f"## 现有记忆索引\n```\n{existing_index[:2000]}\n```\n\n"
                f"## 候选反思\n{hints_text}\n\n"
                "## 评估标准（全部满足才通过）\n"
                "1. **新颖性**：现有记忆中没有覆盖相同或近似的知识\n"
                "2. **可泛化**：不是特定任务细节，而是跨场景适用的模式\n"
                "3. **行为指导性**：记住它会具体改变未来的行为\n\n"
                "## 输出格式\n"
                "通过的输出 JSON：\n"
                '```json\n{"pass": true, "type": "feedback|soul|cognition", '
                '"filename": "简短英文文件名.md", "title": "简短标题", '
                '"description": "一行描述，用于未来检索", '
                '"content": "记忆正文内容（对于 feedback 类型，包含 Why 和 How to apply）"}\n```\n'
                "不通过的输出：\n"
                '```json\n{"pass": false, "reason": "不通过的原因"}\n```\n'
                "只输出 JSON。"
            )
            eval_config = LLMConfig(
                provider="claude-cli", model="sonnet",
            )
            eval_result = await self.router.run(
                prompt=eval_prompt, llm_config=eval_config,
            )
            if eval_result.is_error or not eval_result.text.strip():
                log.warning("Reflect hint evaluation failed: %s", eval_result.text[:100])
                return

            # Parse JSON
            import json as _json
            import re as _re
            text = eval_result.text.strip()
            if "```" in text:
                json_match = _re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, _re.DOTALL)
                if json_match:
                    text = json_match.group(1).strip()

            result = None
            try:
                result = _json.loads(text)
            except _json.JSONDecodeError:
                # LLM may append text after JSON; extract first complete JSON object
                json_match = _re.search(r'\{.*\}', text, _re.DOTALL)
                if json_match:
                    try:
                        result = _json.loads(json_match.group(0))
                    except _json.JSONDecodeError:
                        pass
                if result is None:
                    # JSON truncated at 'content' field — extract key fields via regex
                    _pass = _re.search(r'"pass"\s*:\s*(true|false)', text)
                    _type = _re.search(r'"type"\s*:\s*"([^"]+)"', text)
                    _fname = _re.search(r'"filename"\s*:\s*"([^"]+)"', text)
                    _title = _re.search(r'"title"\s*:\s*"([^"]+)"', text)
                    _desc = _re.search(r'"description"\s*:\s*"([^"]+)"', text)
                    _content_partial = _re.search(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', text, _re.DOTALL)
                    _t = _type.group(1) if _type else "feedback"
                    if _pass and _pass.group(1) == 'true':
                        _partial = _content_partial.group(1) if _content_partial else ""
                        log.warning("Reflect eval JSON truncated (type: %s), partial content recovered (%d chars): %s", _t, len(_partial), text)
                        result = {
                            "pass": True,
                            "type": _t,
                            "filename": _fname.group(1) if _fname else "reflect_unnamed.md",
                            "title": _title.group(1) if _title else "Unnamed reflection",
                            "description": _desc.group(1) if _desc else "",
                            "content": _partial,
                        }
                    elif _pass is None and _type and _title:
                        # pass field itself truncated but key fields present — assume pass:true
                        _partial = _content_partial.group(1) if _content_partial else ""
                        log.warning("Reflect eval JSON truncated before pass field (type: %s), partial content recovered (%d chars): %s", _t, len(_partial), text)
                        result = {
                            "pass": True,
                            "type": _t,
                            "filename": _fname.group(1) if _fname else "reflect_unnamed.md",
                            "title": _title.group(1) if _title else "Unnamed reflection",
                            "description": _desc.group(1) if _desc else "",
                            "content": _partial,
                        }
                    else:
                        log.warning("Reflect eval returned non-JSON (type: %s): %s", _t, text)
                        return

            if not result.get("pass"):
                log.info("Reflect hint rejected: %s", result.get("reason", "unknown"))
                return

            mem_type = result.get("type", "feedback")
            filename = result.get("filename", "reflect_unnamed.md")
            title = result.get("title", "Unnamed reflection")
            description = result.get("description", "")
            content = result.get("content", "")

            # soul/cognition require L2 confirmation — notify user, don't auto-persist
            if mem_type in ("soul", "cognition"):
                log.info("Reflect hint requires L2 confirmation (type=%s): %s", mem_type, title)
                try:
                    from agent.platforms.feishu.dispatcher import Dispatcher
                    notifier = Dispatcher.get_notifier()
                    if notifier:
                        await notifier.send_to_delivery_target(
                            f"**反思信号 [{mem_type}]**\n\n"
                            f"**{title}**\n{description}\n\n"
                            f"内容：\n{content}\n\n"
                            f"_需要确认后写入 {'CLAUDE.md' if mem_type == 'soul' else 'COGNITION.md'}_"
                        )
                except Exception as e:
                    log.warning("Failed to notify reflect hint: %s", e)
                return

            # feedback type: auto-persist as memory file (L1)
            if not filename.startswith("feedback_"):
                filename = f"feedback_{filename}"
            filepath = memory_dir / filename

            memory_content = (
                f"---\n"
                f"name: {title}\n"
                f"description: {description}\n"
                f"type: feedback\n"
                f"---\n\n"
                f"{content}\n"
            )
            filepath.write_text(memory_content)
            log.info("Reflect memory written: %s", filepath)

            # Update MEMORY.md index
            index_line = f"- [{title}]({filename}) — {description}"
            if filename not in existing_index:
                # Append under Feedback section
                if "## Feedback" in existing_index:
                    updated = existing_index.replace(
                        "## Feedback",
                        f"## Feedback\n{index_line}",
                        1,
                    )
                else:
                    updated = existing_index.rstrip() + f"\n\n## Feedback\n{index_line}\n"
                memory_index.write_text(updated)
                log.info("MEMORY.md index updated with: %s", title)

        except Exception as e:
            log.error("Reflect hints processing error: %s", e, exc_info=True)

    # ═══ Skill & Command Router ═══

    def _resolve_skill(self, text: str, session_key: str) -> tuple[LLMConfig, str]:
        """Check skill prefix in any paragraph (supports batched text+media)."""
        prompt = text

        effort = self.default_llm.effort
        if effort != "max" and _MAX_EFFORT_RE.search(text):
            effort = "max"
            log.info("Effort escalated to max (keyword match)")

        for para in text.split("\n\n"):
            first_word = para.strip().split(None, 1)[0].lower() if para.strip() else ""
            if first_word in SKILL_ROUTES:
                provider, model, sys_prompt = SKILL_ROUTES[first_word]
                rest = para.split(None, 1)[1] if len(para.split(None, 1)) > 1 else ""
                prompt = text.replace(para, rest, 1).strip()
                log.info("Skill '%s' → %s/%s", first_word, provider, model)
                return LLMConfig(
                    provider=provider, model=model, system_prompt=sys_prompt,
                    effort=effort,
                ), prompt

        session_llm = self.router.get_session_llm(session_key)
        if session_llm:
            cfg = llm_config_from_dict(session_llm)
            if not cfg.effort:
                cfg = replace(cfg, effort=effort)
            return cfg, prompt
        return LLMConfig(
            provider=self.default_llm.provider,
            model=self.default_llm.model,
            effort=effort,
            env=self.default_llm.env,
            workspace_dir=self.default_llm.workspace_dir,
            setting_sources=self.default_llm.setting_sources,
        ), prompt

    # ═══ Reply Cache ═══

    def _cache_reply(self, message_id: str, text: str):
        """Cache bot reply text, schedule async persist."""
        self._reply_cache[message_id] = text
        # Evict oldest entries if over capacity
        if len(self._reply_cache) > _REPLY_CACHE_MAX:
            excess = len(self._reply_cache) - _REPLY_CACHE_MAX
            for k in list(self._reply_cache)[:excess]:
                self._reply_cache.pop(k, None)
        # Schedule async persist (coalesce multiple writes)
        if not getattr(self, '_reply_cache_dirty', False):
            self._reply_cache_dirty = True
            asyncio.get_running_loop().call_later(30, self._flush_reply_cache)

    def _flush_reply_cache(self):
        """Persist reply cache to disk if dirty."""
        if not getattr(self, '_reply_cache_dirty', False):
            return
        self._reply_cache_dirty = False
        try:
            save_json_sync(self._reply_cache_path, self._reply_cache)
        except Exception as e:
            log.warning("Failed to persist reply cache: %s", e)

    # ═══ Long Content → Feishu Doc ═══

    async def _send_long_as_doc(self, chat_id: str, text: str, reply_to: str | None = None) -> str | None:
        """Create a Feishu doc for long content, send card with summary + link.

        Returns the message_id of the sent card, or None on failure.
        """
        from agent.platforms.feishu.utils import append_markdown_to_doc

        api = self._feishu_api
        # Extract title from first heading or first line
        title = "回复详情"
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("#"):
                title = line.lstrip("#").strip()[:50]
                break
            elif line:
                title = line[:50]
                break

        # Create doc
        try:
            resp = api.post("/open-apis/docx/v1/documents", {"title": title})
            if resp.get("code") != 0:
                log.warning("Long content doc creation failed: %s", resp.get("msg"))
                return await self.dispatcher.send_text(chat_id, text, reply_to)

            doc_id = resp["data"]["document"]["document_id"]

            # Write content
            append_markdown_to_doc(api, doc_id, text)

            # Share to user
            cfg = getattr(self, '_config', {}) or {}
            share_to = cfg.get("heartbeat", {}).get("notify_open_id", "")
            if share_to:
                api.post(
                    f"/open-apis/drive/v1/permissions/{doc_id}/members",
                    body={"member_type": "openid", "member_id": share_to, "perm": "full_access"},
                    params={"type": "docx", "need_notification": "false"},
                )

            doc_url = f"https://feishu.cn/docx/{doc_id}"

            # Build summary card: first ~500 chars + link
            summary_lines = []
            char_count = 0
            for line in text.split("\n"):
                if char_count > 500:
                    summary_lines.append("...")
                    break
                summary_lines.append(line)
                char_count += len(line)
            summary = "\n".join(summary_lines)
            card_text = f"{summary}\n\n---\n\n**[查看完整内容]({doc_url})**"

            return await self.dispatcher.send_text(chat_id, card_text, reply_to)
        except Exception as e:
            log.error("Long content → doc failed: %s", e, exc_info=True)
            # Fallback to chunked send
            return await self.dispatcher.send_text(chat_id, text, reply_to)

    def _fetch_quoted_message(self, parent_id: str) -> tuple[str, str, str]:
        """Fetch quoted message content and type.

        Returns (text, msg_type, raw_body) tuple.
        - text: parsed text content (same as old _fetch_quoted_text)
        - msg_type: message type (text/post/image/file etc.)
        - raw_body: raw body content string for further processing
        """
        # Check persistent cache — bot card replies can't be fetched via API
        if parent_id in self._reply_cache:
            return self._reply_cache[parent_id], "", ""
        try:
            resp = self._feishu_api.get(f"/open-apis/im/v1/messages/{parent_id}")
            if resp.get("code") != 0:
                log.warning("Failed to fetch parent message %s: %s",
                            parent_id, resp.get("msg"))
                return "", "", ""
            items = resp.get("data", {}).get("items", [])
            if not items:
                return "", "", ""
            item = items[0]
            msg_type = item.get("msg_type", "")
            body = item.get("body", {}).get("content", "")
            text = self._parse_content(body, msg_type)
            # Discard Feishu's degraded placeholder for card messages
            if self._DEGRADED_PLACEHOLDER in text:
                log.info("Discarded degraded content for parent %s", parent_id)
                return "", msg_type, body
            return text, msg_type, body
        except Exception as e:
            log.warning("Error fetching parent message %s: %s", parent_id, e)
            return "", "", ""

    def _fetch_quoted_text(self, parent_id: str) -> str:
        """Fetch quoted text (backward compat wrapper)."""
        text, _, _ = self._fetch_quoted_message(parent_id)
        return text

    def _expand_merged_forward(self, message_id: str, max_messages: int = 50) -> str:
        """Expand a merged-forwarded message by fetching sub-messages.

        Feishu merged-forward sends a placeholder text. The actual sub-messages
        can be fetched via the message list API with upper_message_id filter.
        """
        try:
            resp = self._feishu_api.get(
                "/open-apis/im/v1/messages",
                params={
                    "container_id_type": "thread",
                    "container_id": message_id,
                    "page_size": str(max_messages),
                },
            )
            if resp.get("code") != 0:
                log.warning("Failed to expand merged-forward %s: %s",
                            message_id, resp.get("msg"))
                return ""
            items = resp.get("data", {}).get("items", [])
            if not items:
                log.info("No sub-messages found for merged-forward %s", message_id)
                return ""

            parts = ["[合并转发消息内容]"]
            for item in items[:max_messages]:
                msg_type = item.get("msg_type", "")
                sender_id_short = item.get("sender", {}).get("id", "")[:8]
                body = item.get("body", {}).get("content", "")
                content_text = self._parse_content(body, msg_type)
                if content_text:
                    parts.append(f"- {sender_id_short}: {content_text}")
                elif msg_type == "image":
                    parts.append(f"- {sender_id_short}: [图片]")
                elif msg_type == "file":
                    parts.append(f"- {sender_id_short}: [文件]")
            return "\n".join(parts) if len(parts) > 1 else ""
        except Exception as e:
            log.warning("Error expanding merged-forward %s: %s", message_id, e)
            return ""

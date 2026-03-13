# -*- coding: utf-8 -*-
"""Session processing mixin — LLM routing, skill matching, reply caching.

Extracted from FeishuBot to reduce monolith size.
Used as a mixin: class FeishuBot(MediaMixin, SessionMixin, ...)
"""

import random
import time
import asyncio
import logging
from dataclasses import replace

from agent.infra.models import LLMConfig, llm_config_from_dict
from agent.infra.store import save_json_sync

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

        # Send "thinking" card as immediate feedback
        thinking_msg_id = await self.dispatcher.send_card_return_id(
            batch.chat_id, "💭 脑子在转…",
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
                """Build card content: activity on top, todos below divider."""
                if activity:
                    _last_tool_label[0] = activity
                top = _last_tool_label[0] or "💭 脑子在转…"
                if not _todos:
                    return top
                _icons = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
                lines = [top, "<hr>"]
                for t in _todos:
                    icon = _icons.get(t.get("status", "pending"), "⬜")
                    label = t.get("activeForm") if t.get("status") == "in_progress" else t.get("content", "")
                    lines.append(f"{icon} {label}")
                return "\n".join(lines)

            def _idle_label(elapsed: float) -> str:
                pool = _LONG_THINKING if elapsed >= 60 else _THINKING_POOL
                label = random.choice(pool)
                if elapsed >= 30:
                    m, s = divmod(int(elapsed), 60)
                    ts = f"{m}m{s:02d}s" if m else f"{s}s"
                    return f"💭 {label} ({ts})"
                return f"💭 {label}"

            async def _on_todo(todos: list[dict]):
                _todos.clear()
                _todos.extend(todos)
                _last_activity[0] = time.monotonic()
                if thinking_msg_id:
                    await self.dispatcher.update_card(thinking_msg_id, _render_card())

            async def _on_activity(label: str):
                now = time.monotonic()
                if now - _last_activity[0] < _ACTIVITY_MIN_INTERVAL:
                    return
                _last_activity[0] = now
                if thinking_msg_id:
                    await self.dispatcher.update_card(thinking_msg_id, _render_card(label))

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
                            await self.dispatcher.update_card(
                                thinking_msg_id,
                                _render_card(idle) if _todos else idle,
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
                log.info("LLM task cancelled for %s (user recalled)", key)
                # Ensure llm_task is also cancelled (in case outer task was cancelled)
                if not llm_task.done():
                    llm_task.cancel()
                # Fire-and-forget to avoid double CancelledError on await
                if thinking_msg_id:
                    asyncio.create_task(self._safe_delete_card(thinking_msg_id))
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
                elif _ms and _batch_mids:
                    _ms.update_state(_batch_mids, "completed")
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

    # ═══ Skill & Command Router ═══

    def _resolve_skill(self, text: str, session_key: str) -> tuple[LLMConfig, str]:
        """Check skill prefix in any paragraph (supports batched text+media)."""
        prompt = text

        for para in text.split("\n\n"):
            first_word = para.strip().split(None, 1)[0].lower() if para.strip() else ""
            if first_word in SKILL_ROUTES:
                provider, model, sys_prompt = SKILL_ROUTES[first_word]
                rest = para.split(None, 1)[1] if len(para.split(None, 1)) > 1 else ""
                prompt = text.replace(para, rest, 1).strip()
                log.info("Skill '%s' → %s/%s", first_word, provider, model)
                return LLMConfig(
                    provider=provider, model=model, system_prompt=sys_prompt
                ), prompt

        session_llm = self.router.get_session_llm(session_key)
        if session_llm:
            return llm_config_from_dict(session_llm), prompt
        return LLMConfig(
            provider=self.default_llm.provider,
            model=self.default_llm.model,
            env=self.default_llm.env,
            # timeout_seconds=None → idle-based timeout for chat
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

    def _fetch_quoted_text(self, parent_id: str) -> str:
        """Fetch the content of a quoted/replied-to message.

        Checks persistent reply cache first (bot card replies return degraded
        content from API), then falls back to API fetch.
        """
        # Check persistent cache — bot card replies can't be fetched via API
        if parent_id in self._reply_cache:
            return self._reply_cache[parent_id]
        try:
            resp = self._feishu_api.get(f"/open-apis/im/v1/messages/{parent_id}")
            if resp.get("code") != 0:
                log.warning("Failed to fetch parent message %s: %s",
                            parent_id, resp.get("msg"))
                return ""
            items = resp.get("data", {}).get("items", [])
            if not items:
                return ""
            item = items[0]
            msg_type = item.get("msg_type", "")
            body = item.get("body", {}).get("content", "")
            text = self._parse_content(body, msg_type)
            # Discard Feishu's degraded placeholder for card messages
            if self._DEGRADED_PLACEHOLDER in text:
                log.info("Discarded degraded content for parent %s", parent_id)
                return ""
            return text
        except Exception as e:
            log.warning("Error fetching parent message %s: %s", parent_id, e)
            return ""

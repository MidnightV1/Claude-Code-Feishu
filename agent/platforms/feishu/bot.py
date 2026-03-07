# -*- coding: utf-8 -*-
"""Feishu WebSocket bot — event handling, debounce, command routing.

Media processing lives in media.py, LLM session logic in session.py.
Both are mixed into FeishuBot via Python mixins.
"""

import json
import time
import asyncio
import logging
import os
from dataclasses import dataclass, field

from agent.infra.models import LLMConfig
from agent.llm.router import LLMRouter
from agent.jobs.scheduler import CronScheduler
from agent.jobs.heartbeat import HeartbeatMonitor
from agent.platforms.feishu.dispatcher import Dispatcher
from agent.infra.file_store import FileStore
from agent.infra.user_store import UserStore
from agent.orchestrator.engine import Orchestrator
from agent.platforms.feishu.api import FeishuAPI
from agent.infra.store import load_json_sync
from agent.infra.message_store import content_hash as make_content_hash, media_hash as make_media_hash
from agent.platforms.feishu.media import MediaMixin
from agent.platforms.feishu.session import SessionMixin, SKILL_ROUTES

log = logging.getLogger("hub.feishu_bot")

from pathlib import Path as _Path
# Project root: agent/platforms/feishu/bot.py → project root
_PROJECT_ROOT = str(_Path(__file__).resolve().parent.parent.parent.parent)

DEDUP_TTL = 86400       # 24h
DEDUP_MAX_SIZE = 1000
DEBOUNCE_SECONDS = 3    # debounce window for multi-part messages

# Admin allowlist: only these open_ids can execute destructive commands (#restart)
# Loaded from config.yaml feishu.admin_open_ids at startup; empty = no admin commands
ADMIN_OPEN_IDS: set[str] = set()

# ═══ Feishu Channel System Prompt ═══
# Injected via --append-system-prompt for all Feishu-originated Claude CLI calls.
# Tells the model about its communication channel, capabilities, and constraints.
FEISHU_SYSTEM_PROMPT = """\
## 飞书通道上下文

你当前通过飞书消息与用户沟通。消息通过飞书卡片（JSON 2.0）的 markdown 组件渲染。

飞书协作协议、能力声明、Skills 列表见项目 CLAUDE.md（CC 启动时自动加载）。本 prompt 仅补充飞书渲染特有规则。

### 消息渲染（卡片 JSON 2.0）

支持的 markdown 语法：

- 标题：`# ~ ######`
- 格式：`**粗体**` `*斜体*` `~~删除线~~` `` `行内代码` ``
- 代码块：` ```语言\\n代码\\n``` `（支持语言高亮）
- 列表：有序 `1.` / 无序 `-`（嵌套用 4 空格缩进）
- 表格：标准 markdown 表格（原生渲染，单组件最多 4 个表格，超 5 行分页）
- 链接：`[文本](url)`
- 引用：`> 引用文字`
- 分割线：`---` 或 `<hr>`（必须独占一行）
- 彩色文本：`<font color='red'>文本</font>`（支持 red/green/grey/blue 等）
- 标签：`<text_tag color='blue'>标签</text_tag>`
- @人：`<at id=open_id></at>` / `<at id=all></at>`
- 飞书表情：`:DONE:` `:OK:` `:THUMBSUP:`

**渲染注意事项**
- 换行用 `\\n`（JSON 字符串中），或 `<br>`
- **4+ 空格开头会触发代码块**——正文不要意外缩进
- 加粗语法前后保留空格
- 特殊字符（`< > * ~ [ ] ( ) # : + _ $`）如需原样展示，用 HTML 实体转义（如 `&#60;`）
- 长文本 >4000 字符自动分块

### 多模态输入

- 图片：已下载压缩到会话目录，prompt 中提供路径。**收到图片时用 Read 工具直接读取**
- 文件（PDF）：Gemini 生成摘要注入 prompt，追问用 gemini skill
- 文件（代码/文本）：直接读取注入，持久化到 `data/files/`

### 回复规范

- 中文回复（除非用户用英文）
- 善用标题、列表、表格、代码块组织输出
- 错误时给可操作建议
"""


@dataclass
class PendingBatch:
    """Debounce buffer for multi-part messages from the same sender."""
    parts: list = field(default_factory=list)
    footers: list = field(default_factory=list)
    first_message_id: str = ""
    latest_message_id: str = ""  # tracks newest message for queued card placement
    message_ids: set = field(default_factory=set)  # all message_ids in this batch
    chat_id: str = ""
    chat_type: str = ""
    sender_id: str = ""
    sender_name: str = ""    # display name from UserStore
    timer: asyncio.Task | None = None
    pending_media: int = 0   # media items currently being analyzed
    received_at: float = 0.0  # time.time() of first message in batch


class FeishuBot(MediaMixin, SessionMixin):
    def __init__(
        self,
        config: dict,
        router: LLMRouter,
        scheduler: CronScheduler,
        heartbeat: HeartbeatMonitor,
        dispatcher: Dispatcher,
        default_llm: LLMConfig,
        file_store: FileStore,
        user_store: UserStore | None = None,
        orchestrator: Orchestrator | None = None,
        message_store=None,
    ):
        # Bot identity: "main" uses un-prefixed keys for backward compat
        self.name = config.get("name", "main")
        self._key_prefix = "" if self.name == "main" else f"{self.name}:"

        self.app_id = config.get("app_id", "")
        self.orchestrator = orchestrator
        self.app_secret = config.get("app_secret", "")
        self.domain = config.get("domain", "https://open.feishu.cn")
        self.user_store = user_store
        # Legacy fallback: load admin allowlist from config
        global ADMIN_OPEN_IDS
        ADMIN_OPEN_IDS = set(config.get("admin_open_ids", []))
        self.router = router
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.dispatcher = dispatcher
        self.default_llm = default_llm
        self.file_store = file_store
        self._command_handlers: dict[str, callable] = {}
        self._help_sections: list[str] = []
        self._pending: dict[str, PendingBatch] = {}  # debounce buffers
        self._running_tasks: dict[str, asyncio.Task] = {}  # key → flush/LLM task (for cancel)
        self._thinking_cards: dict[str, str] = {}  # key → thinking card message_id
        self._msg_to_key: dict[str, str] = {}  # message_id → debounce key (for recall)
        self._session_locks: dict[str, asyncio.Lock] = {}  # per-session serialization
        self._queued_cards: dict[str, str] = {}  # key → queued indicator card message_id
        self._ws_client = None
        self._loop = None
        self._dedup: dict[str, float] = {}  # legacy in-memory dedup (kept as L0 fast path)
        self._rate_limits: dict[str, list[float]] = {}  # sender_id → list of timestamps
        self.message_store = message_store
        self._bot_open_id: str | None = None
        self._feishu_api = FeishuAPI(self.app_id, self.app_secret, self.domain)
        # Per-bot reply cache: "main" uses legacy path for backward compat
        cache_name = "reply_cache.json" if self.name == "main" else f"reply_cache_{self.name}.json"
        self._reply_cache_path = os.path.join(_PROJECT_ROOT, "data", cache_name)
        self._reply_cache: dict[str, str] = load_json_sync(
            self._reply_cache_path, default={}
        )  # bot message_id → reply text (persistent)
        # Per-bot system prompt override (optional, from config)
        self._extra_system_prompt = config.get("system_prompt", "")

    def register_command(self, prefix: str, handler, help_lines: str | None = None):
        """Register a plugin command handler. handler: async (cmd, args) -> str"""
        self._command_handlers[prefix] = handler
        if help_lines:
            self._help_sections.append(help_lines)

    async def start(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1

        await self._fetch_bot_open_id()

        _noop = lambda d: None
        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message_event) \
            .register_p2_im_message_recalled_v1(self._on_recall_event) \
            .register_p2_im_message_message_read_v1(_noop) \
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(_noop) \
            .register_p2_task_task_update_tenant_v1(_noop) \
            .build()

        self._loop = asyncio.get_running_loop()
        self._ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )

        def _start_ws():
            import lark_oapi.ws.client as ws_mod
            ws_mod.loop = asyncio.new_event_loop()
            self._ws_client.start()

        self._loop.run_in_executor(None, _start_ws)
        log.info("Feishu bot '%s' WebSocket connecting (app_id=%s)", self.name, self.app_id[:8])

        # Health monitor: check WebSocket connectivity, exit process if dead
        asyncio.ensure_future(self._ws_health_monitor())

    async def _ws_health_monitor(self):
        """Periodically check WebSocket health. Exit process if connection is dead.

        Lark SDK bug: runtime disconnection (keepalive timeout) leaves _select()
        spinning forever without reconnecting. Detect via _conn state and force exit
        to let launchd restart the process.
        """
        await asyncio.sleep(30)  # initial grace period
        consecutive_failures = 0
        while True:
            await asyncio.sleep(30)
            try:
                conn = getattr(self._ws_client, '_conn', None)
                if conn is None:
                    consecutive_failures += 1
                elif hasattr(conn, 'closed') and conn.closed:
                    consecutive_failures += 1
                elif hasattr(conn, 'open') and not conn.open:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    continue
                if consecutive_failures >= 3:
                    log.error("WebSocket dead for %ds, exiting for launchd restart",
                              consecutive_failures * 30)
                    import sys
                    sys.exit(1)
            except Exception:
                pass

    async def _fetch_bot_open_id(self):
        try:
            data = self._feishu_api.get("/open-apis/bot/v3/info")
            if data.get("code") == 0:
                self._bot_open_id = data["bot"]["open_id"]
                log.info("Bot open_id fetched: %s", self._bot_open_id)
            else:
                log.warning("Failed to fetch bot open_id: %s", data)
        except Exception as e:
            log.warning("Could not fetch bot open_id: %s", e)

    async def stop(self):
        log.info("Feishu bot stopping")
        self._ws_client = None

    # ═══ Event Handlers ═══

    def _on_message_event(self, data):
        """Called by lark_oapi in its own thread. Bridge to asyncio."""
        asyncio.run_coroutine_threadsafe(self._handle_message(data), self._loop)

    def _on_recall_event(self, data):
        """Called when a message is recalled. Bridge to asyncio."""
        log.info("Recall event received: %s", data)
        asyncio.run_coroutine_threadsafe(self._handle_recall(data), self._loop)

    async def _handle_recall(self, data):
        """Cancel pending/running processing for a recalled message."""
        try:
            event = data.event
            message_id = event.message_id
            log.info("Recall: message_id=%s", message_id)
            if not message_id:
                return

            key = self._msg_to_key.pop(message_id, None)
            if not key:
                log.debug("Recall for unknown message %s (already processed or not ours)", message_id)
                return

            # Case A: still in debounce → cancel batch
            if key in self._pending:
                log.info("Recall: cancelling debounce batch %s", key)
                await self._cancel_batch(key)
                return

            # Case B: flush/LLM running → cancel task + clean up thinking card
            running = self._running_tasks.get(key)
            if running and not running.done():
                log.info("Recall: cancelling running task %s", key)
                running.cancel()
                # _flush CancelledError handler will delete thinking card,
                # but also clean up here in case of race
                thinking_mid = self._thinking_cards.pop(key, None)
                if thinking_mid:
                    asyncio.create_task(self._safe_delete_card(thinking_mid))
                return

            # Case C: already completed → remove last history round
            log.info("Recall: removing history for completed message %s (session=%s)", message_id, key)
            self.router.remove_last_round(key)
            asyncio.create_task(self.router.save_session(key))
        except Exception as e:
            log.warning("Recall handler error: %s", e)

    async def _handle_message(self, data):
        try:
            event = data.event
            msg = event.message
            sender = event.sender

            message_id = msg.message_id
            chat_id = msg.chat_id
            chat_type = msg.chat_type
            sender_id = sender.sender_id.open_id if sender.sender_id else ""
            msg_type = msg.message_type

            log.info("Msg recv: type=%s parent_id=%s root_id=%s",
                     msg_type, msg.parent_id or "(none)", msg.root_id or "(none)")

            # Stale message guard: drop messages created >2min ago (WebSocket re-delivery)
            if msg.create_time:
                try:
                    msg_age = time.time() - int(msg.create_time) / 1000
                    if msg_age > 120:
                        log.info("Stale message dropped: %s (age=%.0fs)", message_id, msg_age)
                        return
                except (ValueError, TypeError):
                    pass

            # Dedup: L0 in-memory fast path + L1/L2 persistent MessageStore
            if self._is_duplicate(message_id):
                return
            self._record_message(message_id)

            # Resolve user identity
            if self.user_store and sender_id:
                user = await self.user_store.get_or_create(sender_id)
            else:
                user = None

            # Per-user rate limiting
            if sender_id and self._check_rate_limit(sender_id):
                log.warning("Rate limited: %s", sender_id[:8])
                await self.dispatcher.send_text(
                    chat_id, "消息太频繁，请稍后再试。",
                    reply_to=message_id,
                )
                return

            # Group: check @bot mention
            if chat_type == "group":
                mentioned_bot = False
                if msg.mentions:
                    for m in msg.mentions:
                        if m.id and m.id.open_id == self._bot_open_id:
                            mentioned_bot = True
                            break
                        if m.name and "bot" in m.name.lower():
                            mentioned_bot = True
                            break
                if not mentioned_bot:
                    return

            # Parse text for text/post/markdown types
            text = ""
            post_image_keys: list[str] = []
            if msg_type in ("text", "post", "markdown"):
                if msg_type == "post":
                    try:
                        content = (
                            json.loads(msg.content)
                            if isinstance(msg.content, str) else msg.content
                        )
                    except (json.JSONDecodeError, TypeError):
                        content = {}
                    if isinstance(content, dict):
                        text, post_image_keys = self._parse_post_content(content)
                    else:
                        text = ""
                else:
                    text = self._parse_content(msg.content, msg_type)
                # Strip @mentions
                if chat_type == "group" and msg.mentions:
                    for m in msg.mentions:
                        text = text.replace(m.key, "").strip() if m.key else text
                if not text and not post_image_keys:
                    log.warning("Empty text after parsing msg_type=%s, raw content: %s",
                                msg_type, msg.content[:500] if msg.content else "(none)")
                    return
                # Process post embedded images
                if post_image_keys:
                    session_key = self._session_key(chat_type, chat_id, sender_id)
                    for ik in post_image_keys:
                        path = await self._process_image(
                            message_id, "", session_key, image_key=ik
                        )
                        if path:
                            text += f"\n[图片] {path}"
                # Prepend quoted message content if this is a reply
                if msg.parent_id:
                    quoted = await asyncio.to_thread(self._fetch_quoted_text, msg.parent_id)
                    if quoted:
                        # Truncate very long quotes (e.g. quoting bot's full reply)
                        if len(quoted) > 2000:
                            quoted = quoted[:2000] + "\n...(截断)"
                        text = f"[用户引用的消息: {quoted}]\n\n{text}"

                # Commands bypass debounce
                if text.startswith("#"):
                    first_word = text.split(None, 1)[0].lower()
                    if first_word not in SKILL_ROUTES:
                        # MessageStore: command-level dedup (no time window)
                        if self.message_store:
                            c_hash = make_content_hash(sender_id, text)
                            if self.message_store.check_dup(message_id, c_hash, "command"):
                                return
                            self.message_store.record(message_id, c_hash, "command", sender_id)
                        log.info("Command from %s: %s", sender_id[:8], text[:60])
                        cmd_result = await self._route_command(text, chat_id, chat_type, sender_id)
                        if cmd_result is not None:
                            if self.message_store:
                                self.message_store.update_state(message_id, "completed")
                            await self.dispatcher.send_text(
                                chat_id, cmd_result, reply_to=message_id
                            )
                            return

                # MessageStore: chat-level dedup (5min window)
                if self.message_store:
                    c_hash = make_content_hash(sender_id, text)
                    if self.message_store.check_dup(message_id, c_hash, "chat"):
                        return
                    key = self._debounce_key(chat_type, chat_id, sender_id)
                    self.message_store.record(message_id, c_hash, "chat", sender_id, key)

                sender_name = user.name if user else ""
                log.info("Message from %s(%s) in %s: %s",
                         sender_name or sender_id[:8], sender_id[:8], chat_type, text[:100])
                key = self._debounce_key(chat_type, chat_id, sender_id)
                await self._enqueue(key, text, "", message_id, chat_id, chat_type, sender_id,
                                    sender_name, debounce_seconds=0)
                return
            elif msg_type not in ("image", "file"):
                log.warning("Unhandled msg_type=%s, content: %s",
                            msg_type, msg.content[:500] if msg.content else "(none)")
                return

            session_key = self._session_key(chat_type, chat_id, sender_id)
            key = self._debounce_key(chat_type, chat_id, sender_id)

            sender_name = user.name if user else ""

            if msg_type == "image":
                # MessageStore: image dedup (30min window, hash on image_key)
                if self.message_store:
                    try:
                        _ic = json.loads(msg.content) if isinstance(msg.content, str) else {}
                        _ik = _ic.get("image_key", "") if isinstance(_ic, dict) else ""
                    except Exception:
                        _ik = ""
                    if _ik:
                        m_hash = make_media_hash(sender_id, _ik)
                        if self.message_store.check_dup(message_id, m_hash, "image"):
                            return
                        self.message_store.record(message_id, m_hash, "image", sender_id, key)

                batch = await self._ensure_batch(
                    key, message_id, chat_id, chat_type, sender_id, sender_name
                )
                batch.pending_media += 1
                stored_path = await self._process_image(
                    message_id, msg.content, session_key
                )
                batch = self._pending.get(key)
                if batch:
                    batch.pending_media -= 1
                if stored_path is not None:
                    await self._enqueue_part(
                        key,
                        f"[图片] {stored_path}",
                    )
                else:
                    self._handle_media_failure(key, chat_id, message_id, "图片处理失败，请重试。")
                return

            if msg_type == "file":
                # MessageStore: file dedup (30min window, hash on file_key)
                if self.message_store:
                    try:
                        _fc = json.loads(msg.content) if isinstance(msg.content, str) else {}
                        _fk = _fc.get("file_key", "") if isinstance(_fc, dict) else ""
                    except Exception:
                        _fk = ""
                    if _fk:
                        m_hash = make_media_hash(sender_id, _fk)
                        if self.message_store.check_dup(message_id, m_hash, "file"):
                            return
                        self.message_store.record(message_id, m_hash, "file", sender_id, key)

                batch = await self._ensure_batch(
                    key, message_id, chat_id, chat_type, sender_id, sender_name
                )
                batch.pending_media += 1
                file_text, footer = await self._process_file(
                    message_id, msg.content, session_key
                )
                batch = self._pending.get(key)
                if batch:
                    batch.pending_media -= 1
                if file_text is not None:
                    await self._enqueue_part(key, file_text, footer)
                else:
                    self._handle_media_failure(key, chat_id, message_id, "文件处理失败，请重试。")
                return

        except Exception as e:
            log.error("Message handling error: %s", e, exc_info=True)

    def _handle_media_failure(self, key, chat_id, message_id, error_msg):
        """Handle failed media processing within debounce context."""
        batch = self._pending.get(key)
        if not batch:
            return
        if not batch.parts and batch.pending_media == 0:
            # Nothing else in batch — cancel and send error
            asyncio.create_task(self._cancel_batch(key))
            asyncio.create_task(
                self.dispatcher.send_text(chat_id, error_msg, reply_to=message_id)
            )
        elif batch.pending_media == 0 and (not batch.timer or batch.timer.done()):
            # Other parts exist; restart timer to flush them
            batch.timer = asyncio.create_task(self._flush_after(key))

    # ═══ Key Construction ═══

    def _session_key(self, chat_type: str, chat_id: str, sender_id: str) -> str:
        """Build namespaced session key. 'main' bot uses un-prefixed for backward compat."""
        if chat_type == "p2p":
            return f"{self._key_prefix}user:{sender_id}"
        return f"{self._key_prefix}chat:{chat_id}"

    def _debounce_key(self, chat_type, chat_id, sender_id):
        if chat_type == "p2p":
            return f"{self._key_prefix}p2p:{sender_id}"
        return f"{self._key_prefix}group:{chat_id}:{sender_id}"

    async def _ensure_batch(self, key, message_id, chat_id, chat_type, sender_id,
                            sender_name=""):
        """Create batch if needed. Thinking card in _flush provides processing feedback."""
        if key not in self._pending:
            self._pending[key] = PendingBatch(
                first_message_id=message_id,
                latest_message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                sender_name=sender_name,
                received_at=time.time(),
            )
        else:
            self._pending[key].latest_message_id = message_id
        self._pending[key].message_ids.add(message_id)
        self._msg_to_key[message_id] = key
        return self._pending[key]

    async def _enqueue(self, key, part, footer, message_id, chat_id, chat_type, sender_id,
                       sender_name="", debounce_seconds=DEBOUNCE_SECONDS):
        """Ensure batch exists and enqueue a part."""
        await self._ensure_batch(key, message_id, chat_id, chat_type, sender_id, sender_name)
        await self._enqueue_part(key, part, footer, debounce_seconds=debounce_seconds)

    async def _enqueue_part(self, key, part, footer="", debounce_seconds=DEBOUNCE_SECONDS):
        """Add part to existing batch and reset timer."""
        batch = self._pending.get(key)
        if not batch:
            log.warning("Debounce race: batch %s gone before enqueue", key)
            return
        batch.parts.append(part)
        if footer:
            batch.footers.append(footer)
        # Don't flush while media is still being processed
        if batch.pending_media > 0:
            return
        # Reset timer
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        if debounce_seconds > 0:
            batch.timer = asyncio.create_task(self._flush_after(key, debounce_seconds))
        elif len(batch.parts) == 1:
            # First text in batch: short window to catch near-simultaneous media
            batch.timer = asyncio.create_task(self._flush_after(key, 1.0))
        else:
            batch.timer = asyncio.create_task(self._flush(key))

    async def _flush_after(self, key, seconds=DEBOUNCE_SECONDS):
        await asyncio.sleep(seconds)
        await self._flush(key)

    async def _cancel_batch(self, key):
        batch = self._pending.pop(key, None)
        if batch:
            if batch.timer and not batch.timer.done():
                batch.timer.cancel()
            for mid in batch.message_ids:
                self._msg_to_key.pop(mid, None)

    async def _safe_delete_card(self, message_id: str):
        """Fire-and-forget card deletion (safe in CancelledError context)."""
        try:
            await self.dispatcher.delete_message(message_id)
        except Exception:
            pass

    async def _flush(self, key):
        """Coordinate batch processing with per-session serialization."""
        batch = self._pending.get(key)
        if not batch:
            return
        if batch.pending_media > 0:
            # Media still processing; timer will be reset when it completes
            return

        lock = self._session_locks.setdefault(key, asyncio.Lock())

        if lock.locked():
            # Previous batch still processing — show/update queued card
            await self._update_queued_card(key, batch)
            return

        async with lock:
            while True:
                # Clean up queued card from waiting phase
                await self._delete_queued_card(key)

                batch = self._pending.get(key)
                if not batch or batch.pending_media > 0:
                    break
                batch = self._pending.pop(key)
                if not batch.parts:
                    for mid in batch.message_ids:
                        self._msg_to_key.pop(mid, None)
                    break

                await self._process_batch(key, batch)

                # Check for more pending messages (arrived during processing)
                if key not in self._pending:
                    break

    async def _update_queued_card(self, key, batch):
        """Show or move the queued indicator card under the latest message."""
        old_card = self._queued_cards.get(key)
        if old_card:
            asyncio.create_task(self._safe_delete_card(old_card))
        card_id = await self.dispatcher.send_card_return_id(
            batch.chat_id, "⏳ 以上消息排队中，等待当前任务完成…",
            reply_to=batch.latest_message_id,
        )
        if card_id:
            self._queued_cards[key] = card_id

    async def _delete_queued_card(self, key):
        """Remove queued indicator card if exists."""
        card_id = self._queued_cards.pop(key, None)
        if card_id:
            await self._safe_delete_card(card_id)

    # ═══ Command Router ═══

    async def _route_command(self, text: str, chat_id: str, chat_type: str, sender_id: str) -> str | None:
        """Route #commands. Returns response text or None if not a command."""
        text = text.strip()
        if not text.startswith("#"):
            return None
        first_word = text.split(None, 1)[0].lower()
        if first_word in SKILL_ROUTES:
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        session_key = self._session_key(chat_type, chat_id, sender_id)

        if cmd == "#help":
            return self._cmd_help()
        elif cmd == "#reset":
            return self._cmd_reset(session_key)
        elif cmd == "#usage":
            return await self._cmd_usage()
        elif cmd == "#jobs":
            return self._cmd_jobs()
        elif cmd == "#restart":
            is_admin = (
                (self.user_store and self.user_store.get(sender_id) and self.user_store.get(sender_id).is_admin())
                or sender_id in ADMIN_OPEN_IDS
            )
            if not is_admin:
                return "权限不足，仅管理员可执行 #restart"
            asyncio.create_task(self._do_server_restart())
            return "服务将在 3 秒后重启..."
        elif cmd == "#parallel":
            task_text = parts[1] if len(parts) > 1 else ""
            if not task_text:
                return "用法：`#parallel <任务描述>`"
            if not self.orchestrator:
                return "并行执行功能未启用。"
            asyncio.create_task(
                self._orchestrate_plan(task_text, session_key, chat_id)
            )
            return "📋 正在分析任务…"
        elif cmd == "#opus":
            return self._cmd_switch_model("opus", session_key)
        elif cmd == "#sonnet":
            return self._cmd_switch_model("sonnet", session_key)
        elif cmd == "#think":
            return self._cmd_think(session_key)
        else:
            # Plugin commands: check registered handlers
            for prefix, handler in self._command_handlers.items():
                if cmd.startswith(prefix):
                    args = parts[1] if len(parts) > 1 else ""
                    return await handler(cmd, args)
        return None

    def _cmd_help(self) -> str:
        base = (
            "**claude-code-feishu commands**\n\n"
            "**模型**\n"
            "| 命令 | 说明 |\n"
            "|------|------|\n"
            "| `#opus` | 切换主模型到 Opus |\n"
            "| `#sonnet` | 切换主模型到 Sonnet |\n"
            "| `#think` | 开/关深度推理 |\n\n"
            "**运维**\n"
            "| 命令 | 说明 |\n"
            "|------|------|\n"
            "| `#usage` | 查看配额 |\n"
            "| `#jobs` | 查看定时任务 |\n"
            "| `#reset` | 重置会话 |\n"
            "| `#restart` | 重启服务 |\n\n"
            "**Skills** (手动输入，附带内容)\n"
            "| 命令 | 模型 | 说明 |\n"
            "|------|------|------|\n"
            "| `#plan <text>` | Opus | 架构/方案设计 |\n"
            "| `#review <text>` | Opus | 代码/方案审查 |\n"
            "| `#analyze <text>` | Opus | 深度分析 |"
        )
        if self._help_sections:
            base += "\n" + "\n".join(self._help_sections)
        return base

    def _cmd_reset(self, session_key: str) -> str:
        self.router.clear_session(session_key)
        asyncio.create_task(self.router.save_session(session_key))
        return "会话已重置，下条消息开始新对话。"

    def _cmd_switch_model(self, model: str, session_key: str) -> str:
        """Switch session default model."""
        current = self.router.get_session_llm(session_key) or {}
        current["provider"] = "claude-cli"
        current["model"] = model
        self.router.set_session_llm(session_key, current)
        asyncio.create_task(self.router.save_session(session_key))
        return f"已切换到 **{model.capitalize()}**"

    def _cmd_think(self, session_key: str) -> str:
        """Toggle effort between low (think off) and None (CLI decides)."""
        current = self.router.get_session_llm(session_key) or {}
        is_low = current.get("effort") == "low"
        if is_low:
            current.pop("effort", None)
            self.router.set_session_llm(session_key, current)
            asyncio.create_task(self.router.save_session(session_key))
            return "深度推理 **已开启**（默认模式）"
        else:
            current["effort"] = "low"
            self.router.set_session_llm(session_key, current)
            asyncio.create_task(self.router.save_session(session_key))
            return "深度推理 **已关闭**（低消耗模式）"

    def _cmd_jobs(self) -> str:
        """List scheduled jobs."""
        from datetime import datetime
        jobs = self.scheduler.list_jobs(include_disabled=True)
        if not jobs:
            return "当前没有定时任务。"
        lines = ["**定时任务**\n"]
        for j in jobs:
            status = "✅" if j.enabled else "⚠️"
            sched = j.schedule.expr or f"{j.schedule.every_seconds}s"
            next_run = ""
            if j.state.next_run_at:
                next_run = datetime.fromtimestamp(j.state.next_run_at).strftime("%m-%d %H:%M")
            lines.append(f"{status} **{j.name}** `{sched}` → {next_run}")
        return "\n".join(lines)

    async def _cmd_usage(self) -> str:
        """Check Claude Max quota via API headers."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "scripts/check_quota.py", "--feishu",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=_PROJECT_ROOT,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode != 0:
                return f"Quota check failed: {stderr.decode().strip()}"
            return stdout.decode().strip()
        except asyncio.TimeoutError:
            return "Quota check timed out"
        except Exception as e:
            return f"Quota check error: {e}"

    async def _do_server_restart(self):
        """Wait for reply delivery, then trigger hub restart via launchctl."""
        await asyncio.sleep(3)
        import subprocess
        log_path = os.path.join(_PROJECT_ROOT, "data", "restart.log")
        with open(log_path, "a") as lf:
            subprocess.Popen(
                ["/bin/sh", "-c",
                 "launchctl unload ~/Library/LaunchAgents/com.claude-hub.plist && "
                 "sleep 2 && "
                 "launchctl load ~/Library/LaunchAgents/com.claude-hub.plist"],
                cwd=_PROJECT_ROOT,
                start_new_session=True,
                stdout=lf,
                stderr=lf,
            )

    # ═══ Orchestration ═══

    async def _orchestrate_plan(self, task: str, session_key: str, chat_id: str):
        """Background: create plan and send to user for confirmation."""
        try:
            plan = await self.orchestrator.create_plan(task)
            if not plan:
                await self.dispatcher.send_text(
                    chat_id,
                    "该任务不适合并行处理（依赖链或单步操作）。请直接描述给我，我来处理。"
                )
                return
            plan.chat_id = chat_id
            self.orchestrator.set_pending(session_key, plan)
            await self.dispatcher.send_text(chat_id, plan.render_plan())
        except Exception as e:
            log.error("Orchestrate plan error: %s", e, exc_info=True)
            await self.dispatcher.send_text(chat_id, f"任务分析出错：{type(e).__name__}")

    async def _orchestrate_execute(self, plan, chat_id: str):
        """Background: dispatch workers, track progress, validate, send result."""
        progress_mid = None
        try:
            progress_mid = await self.dispatcher.send_card_return_id(
                chat_id, plan.render_progress()
            )

            async def on_progress():
                if progress_mid:
                    try:
                        await self.dispatcher.update_card(
                            progress_mid, plan.render_progress()
                        )
                    except Exception:
                        pass

            await self.orchestrator.execute(plan, on_progress=on_progress)

            # Update card: validating
            if progress_mid:
                await self.dispatcher.update_card(
                    progress_mid, plan.render_progress()
                )

            # Opus validation
            final_text = await self.orchestrator.validate(plan)

            # Delete progress card, send final result
            if progress_mid:
                await self.dispatcher.delete_message(progress_mid)
            await self.dispatcher.send_text(chat_id, final_text)

        except Exception as e:
            log.error("Orchestrate execute error: %s", e, exc_info=True)
            if progress_mid:
                await self.dispatcher.update_card(
                    progress_mid,
                    f"❌ **执行出错**\n{type(e).__name__}: {e}"
                )

    # ═══ Dedup ═══

    def _sweep_dicts(self):
        """Periodic cleanup of unbounded dicts to prevent memory growth."""
        # _session_locks: remove locks not currently held
        for key in list(self._session_locks):
            lock = self._session_locks.get(key)
            if lock and not lock.locked():
                del self._session_locks[key]
        # _thinking_cards / _queued_cards: should be transient, but sweep stale
        # (these are managed by _process_batch, so just cap size)
        for d in (self._thinking_cards, self._queued_cards, self._running_tasks):
            if len(d) > 100:
                excess = list(d.keys())[:len(d) - 100]
                for k in excess:
                    d.pop(k, None)
        # _rate_limits: remove entries with no recent activity
        now = time.time()
        cutoff = now - 120
        for uid in list(self._rate_limits):
            if not self._rate_limits[uid] or max(self._rate_limits[uid]) < cutoff:
                del self._rate_limits[uid]

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        if message_id in self._dedup:
            return True
        if len(self._dedup) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_TTL
            self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
            # Piggyback: sweep other dicts
            self._sweep_dicts()
        return False

    def _record_message(self, message_id: str):
        self._dedup[message_id] = time.time()

    def _check_rate_limit(self, sender_id: str, max_per_min: int = 10) -> bool:
        """Check per-user rate limit. Returns True if over limit."""
        now = time.time()
        window = self._rate_limits.setdefault(sender_id, [])
        # Prune old entries outside 60s window
        cutoff = now - 60
        self._rate_limits[sender_id] = [t for t in window if t > cutoff]
        window = self._rate_limits[sender_id]
        if len(window) >= max_per_min:
            return True
        window.append(now)
        return False

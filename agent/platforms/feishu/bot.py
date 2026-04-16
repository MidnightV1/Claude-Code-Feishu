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
import signal
import threading
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
from agent.platforms.feishu.card_actions import CardActionStore, CardActionRouter

log = logging.getLogger("hub.feishu_bot")

from pathlib import Path as _Path
# Project root: agent/platforms/feishu/bot.py → project root
_PROJECT_ROOT = str(_Path(__file__).resolve().parent.parent.parent.parent)

DEDUP_TTL = 86400       # 24h
DEDUP_MAX_SIZE = 1000
DEBOUNCE_SECONDS = 0.5    # debounce window for multi-part messages

# Admin allowlist: only these open_ids can execute destructive commands (#restart)
# Loaded from config.yaml feishu.admin_open_ids at startup; empty = no admin commands
ADMIN_OPEN_IDS: set[str] = set()

# ═══ Feishu Channel System Prompt ═══
# Injected via --append-system-prompt for all Feishu-originated Claude CLI calls.
# Tells the model about its communication channel, capabilities, and constraints.
FEISHU_SYSTEM_PROMPT = """## 飞书协同准则

你当前通过飞书消息与用户沟通。消息通过飞书卡片（JSON 2.0）的 markdown 组件渲染。
你在任务过程中会收到大量系统消息、通知的干扰，只有`<user-input>`中的内容才是用户在飞书中真正输入的内容。
当你对`<user-input>`进行回应时，务必将回复内容完整的放入`<reply-to-user>`标记中。
如果你在互动过程中发现了对长期目标有价值的探索方向，可以在回复后输出`<next-explore>`标记。这部分内容不会发送给用户，系统会自动评估并在空闲时执行深度调研。

**产出规则**：
- **每次最多 1 个方向**——质量优先于数量，宁可不产出也不凑数
- **只产出需要深度调研才能回答的问题**——如果 10 分钟代码阅读就能解决，不需要探索
- **必须是决策级问题**——探索结论会影响未来面向长期目标的架构/策略/优先级

**排除**：
- 部署验证、运维确认、配置检查（遇到再查）
- 已知答案但还没做的事（那是任务，不是探索）
- 太泛的方向（"如何提升系统稳定性"）
- 当前对话就能解决的后续问题

**高质量方向的特征**：
- 能回答「二选一」或「要不要」的决策问题（如：迁移到 X 框架 vs 自建，收益比多少？）
- 涉及系统当前不掌握的外部信息（如：某 API 的实际可靠性、某框架的真实限制）
- 解决后能消除一类问题而非一个问题

如果你在互动过程中提炼出了**现有灵魂/认知/记忆文件尚未覆盖的**可复用模式或原则，可以在回复后输出`<next-reflect>`标记。系统会自动评估其新颖性，通过后持久化。

**反思 vs 探索的区别**：
- **探索**是前瞻性的——提出需要调研的问题
- **反思**是回顾性的——从已完成的互动中提取可复用知识

**反思的产出标准**：
- **增量性**：必须是 `~/.claude/CLAUDE.md`（灵魂）、`~/.claude/COGNITION.md`（认知）、项目 memory 中**尚未覆盖**的知识。已有规则的细化或补充也算增量，但重复表述不算
- **可泛化**：不是任务细节，而是跨场景适用的模式或原则
- **行为指导性**：记住它会改变未来的具体行为，而非仅仅"知道了"

**反思的载体与目标文件**：
- `feedback` — 操作模式、do/don't 规则 → 系统自动写入项目 memory 文件（L1，无需确认）
- `soul` — 认知框架、分析方法论、工作原则 → 通知用户确认后更新 `~/.claude/CLAUDE.md`（L2）
- `cognition` — 用户行为模式的新发现 → 通知用户确认后更新 `~/.claude/COGNITION.md`（L2）

示例：
```
<user-input>用户在飞书输入的实际内容</user-input>
task-notification: 1
task-notification: 2
task-notification: 你可能收到的多条过程任务通知，你可以正常回应，系统会拦截这些回应
<reply-to-user>你对<user-input>的正式回复，只有这部分内容才会被用户看到</reply-to-user>
<next-explore>
- [方向] 具体的决策问题（问句形式，足够具体到可以用数据回答）
  [价值] 解决后对哪个决策或长期目标产生什么影响
</next-explore>
<next-reflect>
- [模式] 可复用的模式或原则（陈述句，足够具体到可以指导未来行为）
  [来源] 什么场景/互动触发了这个发现
  [载体] feedback/soul/cognition
</next-reflect>
```

## 飞书消息发送规范

以下规范能让你在飞书中发送高可读性的消息，有效提升用户的交互体验。作用范围仅限于飞书消息互动，文档、表格等其他载体的格式要求以具体能力说明为准。

### 消息渲染（卡片 JSON 2.0）

支持的 markdown 语法：

- 标题：`# ~ ######`
- 格式：`**粗体**` `*斜体*` `~~删除线~~` `` `行内代码` ``
- 代码块：` ```语言\\n代码\\n``` `（支持语言高亮）
- 列表：有序 `1.` / 无序 `-`（嵌套用 4 空格缩进）
- 表格：标准 markdown 表格（原生渲染，单组件最多 4 个表格，超 5 行分页展示）
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
- 特殊字符（`< > * ~ [ ] ( ) # : + _ $`）在卡片消息中如需原样展示，用 HTML 实体转义（如 `&#60;`）。注意：此规则仅适用于 `<reply-to-user>` 中的飞书卡片内容，**不适用于**传给 doc_ctl.py、wiki、bitable 等工具的文档内容（文档 API 接受原始字符）

### 多模态输入

- 图片：已下载压缩到会话目录，prompt 中提供路径。**收到图片时用 Read 工具直接读取**
- 文件（PDF、PPT 等）：Gemini 生成摘要注入 prompt，追问用 gemini skill
- 文件（代码/文本）：直接读取注入，持久化到 `data/files/`

### 回复规范

- 中文回复（除非用户用英文）
- 善用标题、列表、表格、代码块组织输出
- 超过 1000 字以上、涉及到 2 个以上问题或维度的反馈，以及项目方案，使用飞书文档给出方案，方便对具体细节讨论和交流。

### 善用视觉注意力

你可以在回复开头加上 `{{card:header=标题,color=颜色}}` 来为卡片添加带颜色的标题头。Dispatcher 会自动解析并渲染。

用法：`{{card:header=标题文字,color=颜色值}}`（独占一行，放在正文之前）

颜色语义规范（按严重度递增）：
- blue — 信息通知（日常告知，无需操作）
- wathet — 进行中（任务执行中，等待结果）
- green — 成功完成（按预期完成）
- turquoise — 常规变更（系统演化、配置更新）
- yellow — 需关注（异常但不阻断运行）
- orange — 需操作（需要人工介入处理）
- red — 错误/终止（任务失败或系统异常）
- grey — 跳过/失效（条件不满足，已跳过）

示例：
```
{{card:header=部署完成,color=green}}
服务已更新到最新版本，所有检查通过。
```

注意：不要过度使用——日常对话不需要卡片头，仅在结构化输出、操作结果、重要通知等场景使用。
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
        # Per-instance admin allowlist (legacy fallback from config)
        self._admin_open_ids: set[str] = set(config.get("admin_open_ids", []))
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
        self._last_user_message_at: float = time.time()  # idle detection for explore
        self._rate_limits: dict[str, list[float]] = {}  # sender_id → list of timestamps
        self.message_store = message_store
        self._bot_open_id: str | None = None
        self._feishu_api = FeishuAPI(self.app_id, self.app_secret, self.domain)
        # Card action handling
        self._card_action_store = CardActionStore(
            os.path.join(_PROJECT_ROOT, "data")
        )
        self._card_action_router = CardActionRouter(self._card_action_store)
        self._register_card_actions()
        # Per-bot reply cache: "main" uses legacy path for backward compat
        cache_name = "reply_cache.json" if self.name == "main" else f"reply_cache_{self.name}.json"
        self._reply_cache_path = os.path.join(_PROJECT_ROOT, "data", cache_name)
        self._reply_cache: dict[str, str] = load_json_sync(
            self._reply_cache_path, default={}
        )  # bot message_id → reply text (persistent)
        # Per-bot system prompt override (optional, from config)
        self._extra_system_prompt = config.get("system_prompt", "")
        # Tenant boundary: auto-learned from first message, filters cross-org messages
        self._tenant_key: str = config.get("tenant_key", "")
        # Hub 3.0: callback for dev signal detection
        self.on_dev_signal: callable | None = None

    def _register_card_actions(self):
        """Register built-in card action handlers."""
        router = self._card_action_router

        # ── Menu: quick action panel ──
        async def _handle_menu(action_type, value, operator_id, context):
            """Handle quick action button clicks from #menu panel."""
            cmd = value.get("command", "")
            if not cmd:
                return ("ignored", "", None)
            chat_id = context.get("chat_id", "")
            # Inject as synthetic user message
            if chat_id:
                asyncio.run_coroutine_threadsafe(
                    self._inject_card_action_as_message(
                        chat_id, operator_id, cmd, f"[快捷操作] {cmd}"
                    ),
                    self._loop,
                ) if not self._loop.is_running() else \
                    asyncio.ensure_future(
                        self._inject_card_action_as_message(
                            chat_id, operator_id, cmd, f"[快捷操作] {cmd}"
                        )
                    )
            return (cmd, f"已触发: {cmd}", None)

        router.register("menu_action", _handle_menu)

        # ── Confirm: dangerous operation confirmation ──
        async def _handle_confirm(action_type, value, operator_id, context):
            """Handle confirm/cancel for dangerous operations."""
            choice = value.get("choice", "cancel")
            label = value.get("label", "")
            msg_id = context.get("message_id", "")
            if choice == "confirm":
                display = f"✅ 已确认: {label}"
            else:
                display = f"❌ 已取消: {label}"
            # Update the card to show result (remove buttons)
            if msg_id:
                card_json = Dispatcher.build_interactive_card(
                    [{"tag": "markdown", "content": display}],
                    header=label,
                    color="green" if choice == "confirm" else "grey",
                )
                asyncio.ensure_future(
                    self.dispatcher.update_card_raw(msg_id, card_json)
                )
            return (choice, display, None)

        router.register("confirm", _handle_confirm)

        # ── Select: general selection (analysis mode, etc.) ──
        async def _handle_select(action_type, value, operator_id, context):
            """Handle selection buttons (e.g., analysis mode)."""
            choice = value.get("choice", "")
            label = value.get("label", "")
            group = value.get("group", "")
            msg_id = context.get("message_id", "")
            chat_id = context.get("chat_id", "")
            display = f"已选择: {label}"
            # Update card to show selection
            if msg_id:
                card_json = Dispatcher.build_interactive_card(
                    [{"tag": "markdown", "content": f"✅ {display}"}],
                    header=group,
                    color="blue",
                )
                asyncio.ensure_future(
                    self.dispatcher.update_card_raw(msg_id, card_json)
                )
            # Inject selection as synthetic user message for CC context
            if chat_id:
                asyncio.ensure_future(
                    self._inject_card_action_as_message(
                        chat_id, operator_id,
                        f"[选择] {group}: {label}",
                        display,
                    )
                )
            return (choice, display, None)

        router.register("select", _handle_select)

        # ── Explore feedback: thumbs up/down on exploration results ──
        async def _handle_explore_feedback(action_type, value, operator_id, context):
            """Handle explore feedback buttons (up/down)."""
            rating = value.get("choice", "")       # "up" or "down"
            task_id = value.get("task_id", "")
            title = value.get("title", "探索任务")
            msg_id = context.get("message_id", "")

            if rating not in ("up", "down") or not task_id:
                return ("ignored", "", None)

            # Record rating in exploration log + adjust related task priorities
            from agent.infra.exploration import (
                rate_log_entry, read_log, ExplorationQueue, Priority,
            )
            await rate_log_entry(task_id, rating)

            # Find the rated task's pillar from log, then adjust queue
            try:
                recent = await read_log(hours=168)  # 7 days
                rated_entry = next(
                    (e for e in recent if e.get("task_id") == task_id), None
                )
                if rated_entry and rated_entry.get("pillar"):
                    pillar = rated_entry["pillar"]
                    queue = ExplorationQueue()
                    await queue.load()
                    adjusted = 0
                    for t in queue.list_pending():
                        if t.pillar == pillar:
                            if rating == "up" and t.priority > Priority.P1_HIGH:
                                await queue.update(t.id, priority=t.priority - 1)
                                adjusted += 1
                            elif rating == "down" and t.priority < Priority.P3_WATCHING:
                                await queue.update(t.id, priority=t.priority + 1)
                                adjusted += 1
                    if adjusted:
                        log.info("Adjusted %d tasks in pillar=%s by %s",
                                 adjusted, pillar, rating)
            except Exception as e:
                log.warning("Explore priority adjustment failed: %s", e)

            emoji = "👍" if rating == "up" else "👎"
            display = f"{emoji} 已评价: {title}"

            # Update card to show rated state (remove buttons)
            if msg_id:
                card_json = Dispatcher.build_interactive_card(
                    [{"tag": "markdown", "content": display}],
                    header=f"[探索] {title}",
                    color="green" if rating == "up" else "grey",
                )
                asyncio.ensure_future(
                    self.dispatcher.update_card_raw(msg_id, card_json)
                )

            log.info("Explore feedback: %s = %s (task_id=%s)",
                     title, rating, task_id)
            return (rating, display, None)

        router.register("explore_feedback", _handle_explore_feedback)

        # ── Abort: stop a running task from thinking card button ──
        async def _handle_abort(action_type, value, operator_id, context):
            """Handle abort button click on thinking card."""
            task_key = value.get("key", "")
            msg_id = context.get("message_id", "")
            if not task_key:
                return ("ignored", "", None)

            running = self._running_tasks.get(task_key)
            if running and not running.done():
                log.info("Abort: cancelling running task %s (operator=%s)",
                         task_key, operator_id)
                running.cancel()
                thinking_mid = self._thinking_cards.pop(task_key, None)
                # Don't delete thinking card — update it to show aborted state
                mid = msg_id or thinking_mid
                if mid:
                    card_json = Dispatcher.build_interactive_card(
                        [{"tag": "markdown", "content": "⏹ 已中止"}],
                        color="grey",
                    )
                    asyncio.ensure_future(
                        self.dispatcher.update_card_raw(mid, card_json)
                    )
                return ("aborted", "⏹ 已中止", None)

            # Task already finished or not found
            if msg_id:
                card_json = Dispatcher.build_interactive_card(
                    [{"tag": "markdown", "content": "⏹ 任务已结束"}],
                    color="grey",
                )
                asyncio.ensure_future(
                    self.dispatcher.update_card_raw(msg_id, card_json)
                )
            return ("no_task", "任务已结束", None)

        router.register("abort_task", _handle_abort)

    async def _inject_card_action_as_message(
        self, chat_id: str, sender_id: str,
        text: str, history_text: str,
    ):
        """Inject a card action as a synthetic user message into conversation history.

        This ensures button clicks appear in the CC context for continuity.
        """
        session_key = f"{self._key_prefix}{sender_id}"
        # Append to conversation history as a user message
        self.router._append_history(
            session_key,
            user_msg=history_text,
            assistant_msg="[收到，继续执行]",
        )
        # Also record in card action store for audit
        self._card_action_store.create_pending(
            action_id=f"synthetic_{int(time.time()*1000)}",
            message_id="",
            action_type="synthetic",
            chat_id=chat_id,
            sender_id=sender_id,
            payload={"text": text, "history": history_text},
        )
        log.info("Injected card action into history: %s → %s",
                 session_key, history_text)

    def check_idle(self) -> tuple[bool, float]:
        """Check if the system is idle (no recent user messages, no active CLI tasks).

        Returns (is_idle, idle_seconds). Called by heartbeat for explore layer.
        """
        idle_secs = time.time() - self._last_user_message_at
        has_active_tasks = any(
            not t.done() for t in self._running_tasks.values()
        )
        idle_threshold = self.heartbeat.explore_idle_minutes * 60
        is_idle = idle_secs >= idle_threshold and not has_active_tasks
        return is_idle, idle_secs

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
            .register_p2_task_task_updated_v1(_noop) \
            .register_p2_task_task_comment_updated_v1(_noop) \
            .register_p2_card_action_trigger(self._on_card_action_sync) \
            .build()

        self._loop = asyncio.get_running_loop()
        self._ws_client = lark.ws.Client(
            self.app_id, self.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        def _start_ws():
            import concurrent.futures
            import lark_oapi.ws.client as ws_mod
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ws_mod.loop = loop
            # Guard: if the loop's default executor is shut down (e.g. during reconnect
            # after asyncio cleanup), recreate it so run_in_executor (used by getaddrinfo
            # inside websockets.connect) does not raise "cannot schedule new futures after
            # shutdown".
            _orig_run_in_executor = loop.run_in_executor
            def _safe_run_in_executor(executor, func, *args):
                if executor is None and loop._executor_shutdown_called:
                    loop._executor_shutdown_called = False
                    loop._default_executor = concurrent.futures.ThreadPoolExecutor(
                        thread_name_prefix='asyncio'
                    )
                return _orig_run_in_executor(executor, func, *args)
            loop.run_in_executor = _safe_run_in_executor
            # Patch SDK _configure: server pushes ping_interval=120s via
            # connect response and PONG frames, causing idle disconnects.
            # Cap at 30s to keep the connection alive.
            _client = self._ws_client
            _orig_configure = _client._configure
            def _patched_configure(conf):
                _orig_configure(conf)
                if _client._ping_interval > 30:
                    _client._ping_interval = 30
            _client._configure = _patched_configure
            import websockets
            # Patch SDK: route CARD messages through event handler
            # (SDK ws/client.py _handle_data_frame silently drops CARD messages)
            # CARD payloads lack the header/schema fields that do_without_validation
            # expects, so we directly invoke the registered processor.
            import lark_oapi.ws.client as _ws_client_mod
            _orig_handle_data = _client._handle_data_frame
            _card_processor = _client._event_handler._callback_processor_map.get(
                "p2.card.action.trigger"
            )
            async def _patched_handle_data(frame):
                # Extract message type from frame headers
                type_ = None
                for h in frame.headers:
                    if h.key == "type":
                        type_ = h.value
                        break
                if type_ and _ws_client_mod.MessageType(type_) == _ws_client_mod.MessageType.CARD:
                    import http
                    import base64
                    from lark_oapi.core.json import JSON
                    from lark_oapi.ws.model import Response
                    pl = frame.payload
                    sum_ = None
                    for h in frame.headers:
                        if h.key == "sum":
                            sum_ = h.value
                            break
                    if sum_ and int(sum_) > 1:
                        seq = None
                        msg_id = None
                        for h in frame.headers:
                            if h.key == "seq":
                                seq = h.value
                            elif h.key == "message_id":
                                msg_id = h.value
                        pl = _client._combine(msg_id, int(sum_), int(seq), pl)
                        if pl is None:
                            return
                    resp = Response(code=http.HTTPStatus.OK)
                    try:
                        if _card_processor:
                            data = JSON.unmarshal(pl.decode("utf-8"), _card_processor.type())
                            result = _card_processor.do(data)
                            if result is not None:
                                resp.data = base64.b64encode(
                                    JSON.marshal(result).encode("utf-8")
                                )
                        else:
                            log.warning("No card action processor registered")
                    except Exception as e:
                        log.error("Card action handler error: %s", e)
                        resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
                    frame.payload = JSON.marshal(resp).encode("utf-8")
                    await _client._write_message(frame.SerializeToString())
                    return
                # Non-CARD: use original handler
                await _orig_handle_data(frame)
            _client._handle_data_frame = _patched_handle_data

            # Disable websockets built-in ping — it conflicts with SDK ping
            # and causes false ping_timeout disconnects when event loop is busy
            # patch websockets.connect to disable built-in ping
            _orig_connect = websockets.connect
            def _patched_ws_connect(uri, **kwargs):
                kwargs.setdefault("ping_interval", None)  # disable built-in ping
                kwargs.setdefault("ping_timeout", None)
                return _orig_connect(uri, **kwargs)
            websockets.connect = _patched_ws_connect
            self._ws_client.start()
            websockets.connect = _orig_connect  # restore

        self._loop.run_in_executor(None, _start_ws)
        log.info("Feishu bot '%s' WebSocket connecting (app_id=%s)", self.name, self.app_id[:8])

        # Health monitor: check WebSocket connectivity, exit process if dead
        self._health_task = asyncio.ensure_future(self._ws_health_monitor())

        # Event loop watchdog: daemon thread that detects dead/unresponsive loop
        self._start_loop_watchdog()

    async def _ws_health_monitor(self):
        """Periodically check WebSocket health. Exit process if connection is dead.

        Lark SDK bug: runtime disconnection (keepalive timeout) leaves _select()
        spinning forever without reconnecting. Detect via _conn state and force exit
        to let launchd restart the process.
        """
        await asyncio.sleep(30)  # initial grace period
        consecutive_failures = 0
        sigterm_sent = False
        check_interval = 10  # seconds between health checks
        while True:
            await asyncio.sleep(check_interval)
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
                if consecutive_failures >= 3 and not sigterm_sent:
                    log.error("WebSocket dead for %ds, signaling shutdown for launchd restart",
                              consecutive_failures * check_interval)
                    sigterm_sent = True
                    try:
                        os.kill(os.getpid(), signal.SIGTERM)
                    except Exception:
                        os._exit(1)
                elif sigterm_sent:
                    log.warning("WebSocket still dead (%ds), waiting for restart",
                                consecutive_failures * check_interval)
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

    def flush_all_sync(self):
        """Best-effort flush of all in-memory state before shutdown."""
        # 1. Reply cache — direct sync write, skip call_later
        self._flush_reply_cache()

        # 2. Pending debounce batches — log and discard (can't process without event loop)
        if self._pending:
            keys = list(self._pending.keys())
            log.warning("Shutdown: discarding %d pending batch(es): %s", len(keys), keys)
            for key in keys:
                batch = self._pending.pop(key, None)
                if batch and batch.timer and not batch.timer.done():
                    batch.timer.cancel()

        # 3. Thinking/queued cards — best-effort delete via synchronous HTTP
        for label, card_dict in [("thinking", self._thinking_cards), ("queued", self._queued_cards)]:
            if card_dict:
                log.info("Shutdown: cleaning %d %s card(s)", len(card_dict), label)
                for key, msg_id in list(card_dict.items()):
                    try:
                        self._feishu_api.delete(f"/open-apis/im/v1/messages/{msg_id}")
                    except Exception:
                        pass
                card_dict.clear()

    async def stop(self):
        log.info("Feishu bot stopping")
        self._shutting_down = True  # signal watchdog to stand down
        self.flush_all_sync()
        if hasattr(self, '_health_task') and self._health_task:
            self._health_task.cancel()
            self._health_task = None
        self._ws_client = None

    # ═══ Event Loop Watchdog ═══

    def _check_loop_alive(self, caller: str = ""):
        """Check if asyncio event loop is still alive. Call from SDK threads.

        If the loop is closed, the process is a zombie — exit immediately
        so launchd can restart it.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            log.error("Event loop is closed (detected in %s), forcing exit for launchd restart", caller)
            os._exit(1)

    def _start_loop_watchdog(self):
        """Start a daemon thread that periodically verifies the event loop is alive.

        Unlike _ws_health_monitor (an asyncio task that dies with the loop),
        this thread runs independently and can detect a dead loop.
        """
        def _watchdog():
            import time as _time
            _time.sleep(30)  # initial grace period
            while True:
                _time.sleep(15)
                # Stand down during graceful shutdown — let main.py control exit
                if getattr(self, '_shutting_down', False):
                    return
                loop = self._loop
                if loop is None or loop.is_closed():
                    log.error("Event loop watchdog: loop is closed, forcing exit for launchd restart")
                    os._exit(1)
                # Also verify the loop is responsive — schedule a no-op and check it runs
                try:
                    fut = asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop)
                    fut.result(timeout=10)
                except Exception:
                    if getattr(self, '_shutting_down', False):
                        return  # loop busy with shutdown, don't interfere
                    log.error("Event loop watchdog: loop unresponsive, forcing exit for launchd restart")
                    os._exit(1)

        t = threading.Thread(target=_watchdog, name="loop-watchdog", daemon=True)
        t.start()
        log.info("Event loop watchdog thread started")

    # ═══ Event Handlers ═══

    def _on_message_event(self, data):
        """Called by lark_oapi in its own thread. Bridge to asyncio."""
        self._check_loop_alive("_on_message_event")
        asyncio.run_coroutine_threadsafe(self._handle_message(data), self._loop)

    def _on_card_action_sync(self, data):
        """Called by lark_oapi when a card button is clicked. Runs in SDK thread.

        Returns P2CardActionTriggerResponse synchronously (SDK requirement).
        """
        try:
            event = data.event
            operator_id = event.operator.open_id if event.operator else ""
            action = event.action
            value = action.value or {} if action else {}
            context = {}
            if event.context:
                context = {
                    "message_id": event.context.open_message_id or "",
                    "chat_id": event.context.open_chat_id or "",
                }

            action_type = value.get("action", "")
            if not action_type:
                log.warning("Card action with no action type: %s", value)
                return None

            log.info("Card action: type=%s operator=%s value=%s",
                     action_type, operator_id, value)

            # Run async handler from sync context
            self._check_loop_alive("_on_card_action_sync")
            future = asyncio.run_coroutine_threadsafe(
                self._card_action_router.handle(
                    action_type, value, operator_id, context
                ),
                self._loop,
            )
            result = future.result(timeout=10)  # 10s timeout

            if not result:
                return None

            # Build SDK response object
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse, CallBackToast, CallBackCard,
            )
            resp = P2CardActionTriggerResponse()

            toast_data = result.get("toast")
            if toast_data:
                resp.toast = CallBackToast()
                resp.toast.type = toast_data.get("type", "info")
                resp.toast.content = toast_data.get("content", "")

            card_data = result.get("card")
            if card_data:
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = card_data.get("data")

            return resp
        except Exception as e:
            log.error("Card action handler failed: %s", e, exc_info=True)
            return None

    def _on_recall_event(self, data):
        """Called when a message is recalled. Bridge to asyncio."""
        self._check_loop_alive("_on_recall_event")
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

            # Tenant boundary: auto-learn own tenant_key, then filter cross-org messages
            msg_tenant = getattr(sender, "tenant_key", "") or ""
            if msg_tenant:
                if not self._tenant_key:
                    self._tenant_key = msg_tenant
                    log.info("Tenant key learned: %s (bot=%s)", msg_tenant[:8], self.name)
                elif msg_tenant != self._tenant_key:
                    log.info("Cross-org message dropped: sender_tenant=%s own_tenant=%s sender=%s",
                             msg_tenant[:8], self._tenant_key[:8], sender_id[:8] if sender_id else "?")
                    return

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
            self._last_user_message_at = time.time()

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
            if msg_type in ("text", "post", "markdown", "merge_forward"):
                if msg_type == "merge_forward":
                    # Feishu sends msg_type="merge_forward" with placeholder content.
                    # Expand sub-messages via API — Issue #5.
                    expanded = await asyncio.to_thread(
                        self._expand_merged_forward, message_id
                    )
                    text = expanded if expanded else "[合并转发消息，无法展开内容]"
                elif msg_type == "post":
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
                    # Fallback: some clients send merge_forward as text with placeholder
                    if text.strip() == "Merged and Forwarded Message":
                        expanded = await asyncio.to_thread(
                            self._expand_merged_forward, message_id
                        )
                        text = expanded if expanded else "[合并转发消息，无法展开内容]"
                # Process @mentions: strip @bot, replace others with @name
                if chat_type == "group" and msg.mentions:
                    for m in msg.mentions:
                        if not m.key:
                            continue
                        is_bot = (m.id and m.id.open_id == self._bot_open_id)
                        if is_bot:
                            text = text.replace(m.key, "").strip()
                        else:
                            name = m.name or "未知用户"
                            text = text.replace(m.key, f"@{name}")
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
                # Skip if the quoted message is already in the current debounce batch
                # (e.g. doc share + comment sent as two events by Feishu client)
                _parent_in_batch = False
                if msg.parent_id:
                    _parent_key = self._msg_to_key.get(msg.parent_id)
                    if _parent_key and _parent_key in self._pending:
                        _parent_in_batch = True
                        log.info("Skipping quote: parent %s already in batch %s",
                                 msg.parent_id, _parent_key)
                if msg.parent_id and not _parent_in_batch:
                    quoted_text, quoted_type, quoted_body = await asyncio.to_thread(
                        self._fetch_quoted_message, msg.parent_id
                    )
                    # Handle quoted attachments (image/file) — Issue #4
                    if quoted_type == "image" and quoted_body:
                        session_key_q = self._session_key(chat_type, chat_id, sender_id)
                        path = await self._process_image(
                            msg.parent_id, quoted_body, session_key_q
                        )
                        if path:
                            text += f"\n[引用图片] {path}"
                            log.info("Processed quoted image from %s", msg.parent_id)
                    elif quoted_type == "file" and quoted_body:
                        session_key_q = self._session_key(chat_type, chat_id, sender_id)
                        result = await self._process_file(
                            msg.parent_id, quoted_body, session_key_q
                        )
                        if result:
                            text += f"\n[引用文件] {result}"
                            log.info("Processed quoted file from %s", msg.parent_id)
                    if quoted_text:
                        if len(quoted_text) > 2000:
                            quoted_text = quoted_text[:2000] + "\n...(截断)"
                        text = f"[用户引用的消息: {quoted_text}]\n\n{text}"

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
            elif msg_type == "location":
                try:
                    loc = json.loads(msg.content) if isinstance(msg.content, str) else (msg.content or {})
                except (json.JSONDecodeError, TypeError):
                    loc = {}
                loc_name = loc.get("name", "")
                lat = loc.get("latitude", "")
                lng = loc.get("longitude", "")
                if not (lat and lng):
                    log.warning("Location message missing coords: %s", msg.content[:200] if msg.content else "")
                    return
                text = f"[用户分享了位置] 名称: {loc_name}, 经度: {lng}, 纬度: {lat}"
                sender_name = user.name if user else ""
                log.info("Location from %s: %s (%s, %s)", sender_id[:8], loc_name, lat, lng)
                key = self._debounce_key(chat_type, chat_id, sender_id)
                await self._enqueue(key, text, "", message_id, chat_id, chat_type, sender_id,
                                    sender_name, debounce_seconds=0)
                return
            elif msg_type == "audio":
                key = self._debounce_key(chat_type, chat_id, sender_id)
                # Reuse thinking card: show transcription state, then hand off to _flush
                hint_mid = await self.dispatcher.send_card_return_id(
                    chat_id, "🎙️ 语音识别中…",
                    reply_to=message_id,
                )
                session_key = self._session_key(chat_type, chat_id, sender_id)
                transcription = await self._process_audio(
                    message_id, msg.content, session_key
                )
                if not transcription:
                    if hint_mid:
                        asyncio.create_task(self.dispatcher.delete_message(hint_mid))
                    await self.dispatcher.send_text(
                        chat_id, "语音识别失败，请重新发送或改用文字。",
                        reply_to=message_id,
                    )
                    return
                # Stash as thinking card so _flush reuses it (no duplicate card)
                if hint_mid:
                    self._thinking_cards[key] = hint_mid
                # Cache transcription so quoted voice messages resolve to text
                self._cache_reply(message_id, f"[语音转写] {transcription}")
                text = f"[语音消息转写]\n{transcription}"
                sender_name = user.name if user else ""
                log.info("Voice from %s: %s", sender_id[:8], transcription[:100])
                await self._enqueue(key, text, "", message_id, chat_id, chat_type, sender_id,
                                    sender_name, debounce_seconds=0)
                return
            elif msg_type == "interactive":
                log.debug("Ignored interactive card message %s", message_id)
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
            # First text in batch: wait to catch multi-part messages
            # (e.g. doc share + comment arrive as separate events ~1s apart)
            batch.timer = asyncio.create_task(self._flush_after(key, 2.0))
        else:
            # Subsequent parts reset to shorter window
            batch.timer = asyncio.create_task(self._flush_after(key, 1.0))

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
            asyncio.ensure_future(self._send_menu_card(chat_id))
            return self._cmd_help()
        elif cmd == "#reset":
            self._cmd_reset(session_key)
            card_json = self.dispatcher._build_card_json(
                "会话已重置，下条消息开始新对话。",
                header="Session Reset", color="grey",
            )
            await self.dispatcher.send_card_raw(chat_id, card_json)
            return None
        elif cmd == "#usage":
            return await self._cmd_usage()
        elif cmd == "#jobs":
            return self._cmd_jobs()
        elif cmd == "#restart":
            is_admin = (
                (self.user_store and self.user_store.get(sender_id) and self.user_store.get(sender_id).is_admin())
                or sender_id in self._admin_open_ids
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
        elif cmd == "#haiku":
            return self._cmd_switch_model("haiku", session_key)
        elif cmd == "#think":
            return self._cmd_think(session_key)
        elif cmd == "#menu":
            asyncio.ensure_future(self._send_menu_card(chat_id))
            return ""  # handled async, suppress LLM routing
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
            "| `#haiku` | 切换主模型到 Haiku |\n"
            "| `#think` | 开/关深度推理 |\n\n"
            "**运维**\n"
            "| 命令 | 说明 |\n"
            "|------|------|\n"
            "| `#usage` | 查看配额 |\n"
            "| `#jobs` | 查看定时任务 |\n"
            "| `#reset` | 重置会话 |\n"
            "| `#restart` | 重启服务 |\n"
            "| `#menu` | 快捷操作面板 |\n\n"
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

    async def _send_menu_card(self, chat_id: str):
        """Send a quick action menu card with buttons."""
        buttons = [
            {"text": "📊 服务状态", "type": "default",
             "value": {"action": "menu_action", "command": "#usage"}},
            {"text": "📅 今日日程", "type": "default",
             "value": {"action": "menu_action", "command": "今天有什么日程？"}},
            {"text": "📰 触发日报", "type": "default",
             "value": {"action": "menu_action", "command": "#briefing run"}},
            {"text": "🔁 重置会话", "type": "default",
             "value": {"action": "menu_action", "command": "#reset"}},
            {"text": "🔄 定时任务", "type": "default",
             "value": {"action": "menu_action", "command": "#jobs"}},
            {"text": "❓ 帮助", "type": "default",
             "value": {"action": "menu_action", "command": "#help"}},
        ]
        btn_group = Dispatcher.build_button_group(buttons, layout="bisected")
        elements = [{"tag": "markdown", "content": "选择一个快捷操作："}]
        if isinstance(btn_group, list):
            elements.extend(btn_group)
        else:
            elements.append(btn_group)
        card_json = Dispatcher.build_interactive_card(
            elements, header="快捷操作面板", color="blue",
        )
        await self.dispatcher.send_card_raw(chat_id, card_json)

    def _cmd_reset(self, session_key: str) -> None:
        self.router.clear_session(session_key)
        asyncio.create_task(self.router.save_session(session_key))

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
                 "launchctl unload ~/Library/LaunchAgents/com.claude-code-feishu-health.plist 2>/dev/null; "
                 "launchctl unload ~/Library/LaunchAgents/com.claude-code-feishu-invest.plist 2>/dev/null; "
                 "launchctl unload ~/Library/LaunchAgents/com.claude-code-feishu.plist && "
                 "sleep 2 && "
                 "launchctl load ~/Library/LaunchAgents/com.claude-code-feishu.plist && "
                 "launchctl load ~/Library/LaunchAgents/com.claude-code-feishu-health.plist 2>/dev/null; "
                 "launchctl load ~/Library/LaunchAgents/com.claude-code-feishu-invest.plist 2>/dev/null"],
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

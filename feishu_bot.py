# -*- coding: utf-8 -*-
"""Feishu WebSocket bot with debounce batching and file caching."""

import json
import time
import asyncio
import logging
import os
import re
from dataclasses import dataclass, field

from models import LLMConfig, llm_config_from_dict
from llm_router import LLMRouter
from scheduler import CronScheduler
from heartbeat import HeartbeatMonitor
from dispatcher import Dispatcher
from file_store import FileStore
from feishu_api import FeishuAPI

log = logging.getLogger("hub.feishu_bot")

DEDUP_TTL = 86400       # 24h
DEDUP_MAX_SIZE = 1000
SENDER_CACHE_TTL = 600  # 10 min
DEBOUNCE_SECONDS = 3    # debounce window for multi-part messages

# ═══ Feishu Channel System Prompt ═══
# Injected via --append-system-prompt for all Feishu-originated Claude CLI calls.
# Tells the model about its communication channel, capabilities, and constraints.
FEISHU_SYSTEM_PROMPT = """\
## 飞书通道上下文

你当前通过飞书消息与用户沟通。消息通过飞书卡片（JSON 2.0）渲染。

### 已实现的能力

**多模态输入**
- 图片：hub 自动调用 Gemini 3-Flash 视觉分析，结果注入 prompt
- 文件（PDF）：Gemini Files API 解析全文，注入 prompt
- 文件（代码/文本）：直接读取注入（.py, .js, .json, .yaml, .md 等 30+ 格式）
- 文件持久化到会话存储（`data/files/`），跨消息可引用

**消息渲染（卡片 JSON 2.0）**

你的回复会被 hub 包裹在飞书卡片的 markdown 组件中发送。支持的 markdown 语法：

- 标题：`# ~ ######`
- 格式：`**粗体**` `*斜体*` `~~删除线~~` `` `行内代码` ``
- 代码块：` ```语言\n代码\n``` `（支持语言高亮）
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
- 换行用 `\n`（JSON 字符串中），或 `<br>`
- **4+ 空格开头会触发代码块**——正文不要意外缩进
- 加粗语法前后保留空格
- 特殊字符（`< > * ~ [ ] ( ) # : + _ $`）如需原样展示，用 HTML 实体转义（如 `&#60;`）
- 长文本 >4000 字符自动分块

**会话管理**
- DM 按 open_id 独立 session / 群聊按 chat_id 共享 session
- `#reset` 重置 / `#model provider/model` 切换模型

**Skills**
- `#plan` / `#review` / `#analyze` — Opus + 专属提示词
- `#flash` — Gemini 3-Flash
- hub-ops skill — 定时任务管理

- long-task skill — 多步骤+长耗时任务走任务模式（用户不应干等），详见 `.claude/skills/long-task/SKILL.md`

### 回复规范

- 中文回复（除非用户用英文）
- 善用标题、列表、表格、代码块组织输出
- 错误时给可操作建议
"""

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
    "#opus": ("claude-cli", "opus", None),
    "#flash": ("gemini-api", "3-Flash", None),
}


@dataclass
class PendingBatch:
    """Debounce buffer for multi-part messages from the same sender."""
    parts: list = field(default_factory=list)
    footers: list = field(default_factory=list)
    first_message_id: str = ""
    message_ids: set = field(default_factory=set)  # all message_ids in this batch
    chat_id: str = ""
    chat_type: str = ""
    sender_id: str = ""
    reaction_id: str | None = None
    timer: asyncio.Task | None = None
    pending_media: int = 0   # media items currently being analyzed


class FeishuBot:
    def __init__(
        self,
        config: dict,
        router: LLMRouter,
        scheduler: CronScheduler,
        heartbeat: HeartbeatMonitor,
        dispatcher: Dispatcher,
        default_llm: LLMConfig,
        file_store: FileStore,
        task_runner=None,
    ):
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.domain = config.get("domain", "https://open.feishu.cn")
        self.router = router
        self.scheduler = scheduler
        self.heartbeat = heartbeat
        self.dispatcher = dispatcher
        self.default_llm = default_llm
        self.file_store = file_store
        self.task_runner = task_runner
        self._command_handlers: dict[str, callable] = {}
        self._help_sections: list[str] = []
        self._pending: dict[str, PendingBatch] = {}  # debounce buffers
        self._running_tasks: dict[str, asyncio.Task] = {}  # key → LLM task (for cancel)
        self._msg_to_key: dict[str, str] = {}  # message_id → debounce key (for recall)
        self._ws_client = None
        self._loop = None
        self._dedup: dict[str, float] = {}
        self._sender_cache: dict[str, tuple[str, float]] = {}
        self._bot_open_id: str | None = None
        self._feishu_api = FeishuAPI(self.app_id, self.app_secret, self.domain)

    def register_command(self, prefix: str, handler, help_lines: str | None = None):
        """Register a plugin command handler. handler: async (cmd, args) -> str"""
        self._command_handlers[prefix] = handler
        if help_lines:
            self._help_sections.append(help_lines)

    async def start(self):
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImMessageRecalledV1

        await self._fetch_bot_open_id()

        handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._on_message_event) \
            .register_p2_im_message_recalled_v1(self._on_recall_event) \
            .build()

        self._loop = asyncio.get_event_loop()
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
        log.info("Feishu bot WebSocket connecting (app_id=%s)", self.app_id[:8])

    async def _fetch_bot_open_id(self):
        try:
            import requests
            r = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            token = r.json().get("tenant_access_token", "")
            r2 = requests.get(
                "https://open.feishu.cn/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            data = r2.json()
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
        asyncio.run_coroutine_threadsafe(self._handle_recall(data), self._loop)

    async def _handle_recall(self, data):
        """Cancel pending/running processing for a recalled message."""
        try:
            event = data.event
            message_id = event.message_id
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

            # Case B: LLM running → cancel task
            running = self._running_tasks.get(key)
            if running and not running.done():
                log.info("Recall: cancelling running LLM task %s", key)
                running.cancel()
                # Cleanup is handled in _flush via CancelledError
                return

            log.debug("Recall: message %s already completed for key %s", message_id, key)
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

            # Dedup
            if self._is_duplicate(message_id):
                return
            self._record_message(message_id)

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
            if msg_type in ("text", "post", "markdown"):
                text = self._parse_content(msg.content, msg_type)
                # Strip @mentions
                if chat_type == "group" and msg.mentions:
                    for m in msg.mentions:
                        text = text.replace(m.key, "").strip() if m.key else text
                if not text:
                    log.warning("Empty text after parsing msg_type=%s, raw content: %s",
                                msg_type, msg.content[:500] if msg.content else "(none)")
                    return
                # Prepend quoted message content if this is a reply
                if msg.parent_id:
                    quoted = self._fetch_quoted_text(msg.parent_id)
                    if quoted:
                        text = f"[用户引用的消息: {quoted}]\n\n{text}"

                # Commands bypass debounce
                if text.startswith("#"):
                    first_word = text.split(None, 1)[0].lower()
                    if first_word not in SKILL_ROUTES:
                        log.info("Command from %s: %s", sender_id[:8], text[:60])
                        cmd_result = await self._route_command(text, chat_id, sender_id)
                        if cmd_result is not None:
                            await self.dispatcher.send_text(
                                chat_id, cmd_result, reply_to=message_id
                            )
                            return

                # Check if user has a task awaiting approval
                if self.task_runner:
                    session_key = (
                        f"user:{sender_id}" if chat_type == "p2p"
                        else f"chat:{chat_id}"
                    )
                    awaiting = self.task_runner.get_awaiting_task(session_key)
                    if awaiting:
                        resp = await self._handle_task_approval(awaiting, text)
                        if resp:
                            await self.dispatcher.send_text(
                                chat_id, resp, reply_to=message_id
                            )
                            return

                log.info("Message from %s in %s: %s", sender_id[:8], chat_type, text[:100])
                key = self._debounce_key(chat_type, chat_id, sender_id)
                await self._enqueue(key, text, "", message_id, chat_id, chat_type, sender_id)
                return
            elif msg_type not in ("image", "file"):
                log.warning("Unhandled msg_type=%s, content: %s",
                            msg_type, msg.content[:500] if msg.content else "(none)")
                return

            session_key = (
                f"user:{sender_id}" if chat_type == "p2p" else f"chat:{chat_id}"
            )
            key = self._debounce_key(chat_type, chat_id, sender_id)

            if msg_type == "image":
                batch = await self._ensure_batch(
                    key, message_id, chat_id, chat_type, sender_id
                )
                batch.pending_media += 1
                desc, footer = await self._analyze_image(
                    message_id, msg.content, session_key
                )
                batch = self._pending.get(key)
                if batch:
                    batch.pending_media -= 1
                if desc is not None:
                    await self._enqueue_part(
                        key,
                        f"[用户发送了一张图片]\n图片内容：{desc}",
                        footer,
                    )
                else:
                    self._handle_media_failure(key, chat_id, message_id, "图片处理失败，请重试。")
                return

            if msg_type == "file":
                batch = await self._ensure_batch(
                    key, message_id, chat_id, chat_type, sender_id
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

    # ═══ Debounce ═══

    def _debounce_key(self, chat_type, chat_id, sender_id):
        if chat_type == "p2p":
            return f"p2p:{sender_id}"
        return f"group:{chat_id}:{sender_id}"

    async def _ensure_batch(self, key, message_id, chat_id, chat_type, sender_id):
        """Create batch if needed — adds typing reaction for immediate feedback."""
        if key not in self._pending:
            reaction_id = await self.dispatcher.add_reaction(message_id)
            self._pending[key] = PendingBatch(
                first_message_id=message_id,
                chat_id=chat_id,
                chat_type=chat_type,
                sender_id=sender_id,
                reaction_id=reaction_id,
            )
        self._pending[key].message_ids.add(message_id)
        self._msg_to_key[message_id] = key
        return self._pending[key]

    async def _enqueue(self, key, part, footer, message_id, chat_id, chat_type, sender_id):
        """Ensure batch exists and enqueue a part."""
        await self._ensure_batch(key, message_id, chat_id, chat_type, sender_id)
        await self._enqueue_part(key, part, footer)

    async def _enqueue_part(self, key, part, footer=""):
        """Add part to existing batch and reset timer."""
        batch = self._pending.get(key)
        if not batch:
            log.warning("Debounce race: batch %s gone before enqueue", key)
            return
        batch.parts.append(part)
        if footer:
            batch.footers.append(footer)
        # Reset timer
        if batch.timer and not batch.timer.done():
            batch.timer.cancel()
        batch.timer = asyncio.create_task(self._flush_after(key))

    async def _flush_after(self, key):
        await asyncio.sleep(DEBOUNCE_SECONDS)
        await self._flush(key)

    async def _cancel_batch(self, key):
        batch = self._pending.pop(key, None)
        if batch:
            if batch.timer and not batch.timer.done():
                batch.timer.cancel()
            if batch.reaction_id:
                await self.dispatcher.remove_reaction(
                    batch.first_message_id, batch.reaction_id
                )
            # Clean up message_id → key mappings
            for mid in batch.message_ids:
                self._msg_to_key.pop(mid, None)

    async def _flush(self, key):
        """Process the accumulated batch: combine parts → LLM → reply."""
        batch = self._pending.get(key)
        if not batch:
            return
        if batch.pending_media > 0:
            # Media still processing; timer will be reset when it completes
            return

        batch = self._pending.pop(key)
        if not batch.parts:
            if batch.reaction_id:
                await self.dispatcher.remove_reaction(
                    batch.first_message_id, batch.reaction_id
                )
            for mid in batch.message_ids:
                self._msg_to_key.pop(mid, None)
            return

        combined = "\n\n".join(batch.parts)
        session_key = (
            f"user:{batch.sender_id}"
            if batch.chat_type == "p2p"
            else f"chat:{batch.chat_id}"
        )

        # Send "thinking" progress card (will be replaced with final reply)
        progress_msg_id = await self.dispatcher.send_card_return_id(
            batch.chat_id, "💭 正在思考…",
            reply_to=batch.first_message_id,
        )

        try:
            llm_config, prompt = self._resolve_skill(combined, session_key)

            # Inject Feishu channel context + file context for Claude sessions
            if llm_config.provider == "claude-cli":
                parts = [FEISHU_SYSTEM_PROMPT]
                if llm_config.system_prompt:
                    parts.append(llm_config.system_prompt)
                file_context = self.file_store.get_context_prompt(session_key)
                if file_context:
                    parts.append(file_context)
                llm_config = LLMConfig(
                    provider=llm_config.provider,
                    model=llm_config.model,
                    timeout_seconds=llm_config.timeout_seconds,
                    system_prompt="\n\n".join(parts),
                    temperature=llm_config.temperature,
                    thinking=llm_config.thinking,
                )

            # Wrap LLM call in a tracked task for cancel support
            llm_coro = self.router.run(
                prompt=prompt, llm_config=llm_config, session_key=session_key,
            )
            llm_task = asyncio.create_task(llm_coro)
            self._running_tasks[key] = llm_task

            try:
                result = await llm_task
            except asyncio.CancelledError:
                log.info("LLM task cancelled for %s (user recalled)", key)
                # Update progress card to show cancellation
                if progress_msg_id:
                    await self.dispatcher.update_card(progress_msg_id, "⏹️ 已取消")
                return
            finally:
                self._running_tasks.pop(key, None)

            if result.cancelled:
                if progress_msg_id:
                    await self.dispatcher.update_card(progress_msg_id, "⏹️ 已取消")
                return

            if result.is_error:
                log.warning("LLM error (session=%s): %s", session_key, result.text[:200])
                self.router.clear_session(session_key)
                asyncio.create_task(self.router.save_sessions())
                reply_text = "处理出错，已重置会话。请重新发送。"
            elif result.text:
                # Check if Claude created long-task requests via task_ctl.py
                if self.task_runner:
                    await self.task_runner.check_pending_requests(
                        session_key, batch.chat_id, batch.sender_id,
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

            # Replace progress card with final reply, or send as new message
            if reply_text:
                if progress_msg_id:
                    ok = await self.dispatcher.update_card(progress_msg_id, reply_text)
                    if not ok:
                        await self.dispatcher.send_text(
                            batch.chat_id, reply_text,
                            reply_to=batch.first_message_id,
                        )
                else:
                    await self.dispatcher.send_text(
                        batch.chat_id, reply_text,
                        reply_to=batch.first_message_id,
                    )
        except Exception as e:
            log.error("Batch processing error: %s", e, exc_info=True)
        finally:
            if batch.reaction_id:
                await self.dispatcher.remove_reaction(
                    batch.first_message_id, batch.reaction_id
                )
            for mid in batch.message_ids:
                self._msg_to_key.pop(mid, None)

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
            timeout_seconds=self.default_llm.timeout_seconds,
        ), prompt

    async def _route_command(self, text: str, chat_id: str, sender_id: str) -> str | None:
        """Route #commands. Returns response text or None if not a command."""
        text = text.strip()
        if not text.startswith("#"):
            return None
        first_word = text.split(None, 1)[0].lower()
        if first_word in SKILL_ROUTES:
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd == "#help":
            return self._cmd_help()
        elif cmd == "#model":
            return await self._cmd_model(args, sender_id, chat_id)
        elif cmd == "#reset":
            return self._cmd_reset(sender_id, chat_id)
        elif cmd == "#files":
            return self._cmd_files(args, sender_id)
        elif cmd.startswith("#cron"):
            return await self._cmd_cron(cmd, args)
        elif cmd.startswith("#heartbeat"):
            return await self._cmd_heartbeat(cmd, args)
        elif cmd == "#server":
            return await self._cmd_server(args)
        elif cmd == "#task":
            return await self._cmd_task(args, chat_id, sender_id)
        else:
            # Plugin commands: check registered handlers
            for prefix, handler in self._command_handlers.items():
                if cmd.startswith(prefix):
                    return await handler(cmd, args)
        return None

    def _cmd_help(self) -> str:
        base = (
            "**nas-claude-hub commands**\n\n"
            "**Skills** (one-shot model upgrade, keeps session context)\n"
            "| Prefix | Model | Description |\n"
            "|--------|-------|-------------|\n"
            "| `#plan <text>` | Opus | Architecture/planning |\n"
            "| `#review <text>` | Opus | Code/design review |\n"
            "| `#analyze <text>` | Opus | Deep analysis |\n"
            "| `#opus <text>` | Opus | One-shot Opus |\n"
            "| `#flash <text>` | 3-Flash | Quick Gemini response |\n\n"
            "**Management**\n"
            "| Command | Description |\n"
            "|---------|-------------|\n"
            "| `#model` | Show current model |\n"
            "| `#model <provider/model>` | Switch default model |\n"
            "| `#reset` | Reset conversation session |\n"
            "| `#files` | List uploaded files in this session |\n"
            "| `#cron list` | List scheduled jobs |\n"
            "| `#cron add <name> <cron> <prompt> [--model X]` | Add job |\n"
            "| `#cron remove/run/enable/disable <id>` | Manage job |\n"
            "| `#heartbeat status/run` | Heartbeat info/trigger |\n"
            "| `#task <目标>` | Create long-running task |\n"
            "| `#task list/status/cancel <id>` | Manage tasks |\n"
            "| `#server restart` | Restart hub service |\n"
            "| `#help` | This help |"
        )
        if self._help_sections:
            base += "\n" + "\n".join(self._help_sections)
        return base

    async def _cmd_model(self, args: str, sender_id: str, chat_id: str) -> str:
        session_key = f"user:{sender_id}"
        if not args.strip():
            current = self.router.get_session_llm(session_key)
            if current:
                return f"Current model: `{current['provider']}/{current['model']}`"
            return f"Current model: `{self.default_llm.provider}/{self.default_llm.model}` (default)"

        parts = args.strip().split("/", 1)
        if len(parts) != 2:
            return "Format: `#model provider/model` (e.g. `#model claude-cli/opus`)"

        provider, model = parts
        valid_providers = ("claude-cli", "gemini-cli", "gemini-api")
        if provider not in valid_providers:
            return f"Unknown provider `{provider}`. Valid: {', '.join(valid_providers)}"

        self.router.set_session_llm(session_key, {"provider": provider, "model": model})
        asyncio.create_task(self.router.save_sessions())
        return f"Model switched to `{provider}/{model}`"

    def _cmd_reset(self, sender_id: str, chat_id: str) -> str:
        session_key = f"user:{sender_id}"
        self.router.clear_session(session_key)
        asyncio.create_task(self.router.save_sessions())
        return "Session reset. Next message starts a fresh conversation."

    def _cmd_files(self, args: str, sender_id: str) -> str:
        """List uploaded files in the current session."""
        session_key = f"user:{sender_id}"
        files = self.file_store.list_files(session_key)
        if not files:
            return "当前会话没有文件。"

        session_dir = self.file_store._session_dir(session_key)
        lines = [f"**文件列表** ({len(files)})\n"]
        for f in files:
            size = f.get("size_bytes", 0)
            size_str = f"{size / 1024:.0f}KB" if size < 1048576 else f"{size / 1048576:.1f}MB"
            analysis = ""
            if f.get("analysis"):
                preview = f["analysis"][:60].replace("\n", " ")
                analysis = f" — {preview}..."
            lines.append(
                f"- `{f['original_name']}` ({f['type']}, {size_str},"
                f" {f['timestamp'][:16]}){analysis}"
            )
        lines.append(f"\n路径: `{session_dir}/`")
        return "\n".join(lines)

    async def _cmd_cron(self, cmd: str, args: str) -> str:
        after_prefix = cmd.replace("#cron", "").strip()
        if after_prefix:
            subcmd = after_prefix.split()[0]
            rest = args
        elif args:
            parts = args.split(None, 1)
            subcmd = parts[0]
            rest = parts[1] if len(parts) > 1 else ""
        else:
            subcmd = "list"
            rest = ""

        if subcmd == "list":
            jobs = self.scheduler.list_jobs(include_disabled=True)
            if not jobs:
                return "No scheduled jobs."
            lines = ["**Scheduled Jobs**\n"]
            for j in jobs:
                status = "+" if j.enabled else "-"
                sched = j.schedule.expr or f"{j.schedule.every_seconds}s" or j.schedule.at_time
                next_run = ""
                if j.state.next_run_at:
                    from datetime import datetime
                    next_run = datetime.fromtimestamp(j.state.next_run_at).strftime(
                        "%m-%d %H:%M"
                    )
                runner = f"handler:{j.handler}" if j.handler else f"{j.llm.provider}/{j.llm.model}"
                lines.append(
                    f"| `{j.id[:8]}` | {status} | {j.name} | `{sched}` | "
                    f"{runner} | next: {next_run} |"
                )
            return "\n".join(lines)

        elif subcmd == "add":
            return await self._cron_add(rest)

        elif subcmd == "remove":
            job_id = rest.strip()
            ok = await self.scheduler.remove_job(job_id)
            return f"Job `{job_id}` removed." if ok else f"Job `{job_id}` not found."

        elif subcmd == "run":
            job_id = rest.strip()
            text = await self.scheduler.run_job(job_id)
            return f"Job result:\n\n{text[:3000]}"

        elif subcmd in ("enable", "disable"):
            job_id = rest.strip()
            enabled = subcmd == "enable"
            job = await self.scheduler.update_job(job_id, enabled=enabled)
            if job:
                return f"Job `{job_id}` {'enabled' if enabled else 'disabled'}."
            return f"Job `{job_id}` not found."

        return f"Unknown cron subcommand: `{subcmd}`. Try `#cron list`."

    async def _cron_add(self, args: str) -> str:
        # Parse --model and --handler flags
        model_override = None
        model_match = re.search(r'--model\s+(\S+)', args)
        if model_match:
            model_override = model_match.group(1)
            args = args[:model_match.start()] + args[model_match.end():]

        handler_name = ""
        handler_match = re.search(r'--handler\s+(\S+)', args)
        if handler_match:
            handler_name = handler_match.group(1)
            args = args[:handler_match.start()] + args[handler_match.end():]

        parts = self._parse_quoted_args(args.strip())

        # Handler jobs: only need name + schedule
        if handler_name:
            if len(parts) < 2:
                return (
                    "Usage: `#cron add <name> <schedule> --handler <handler_name>`\n"
                    'Example: `#cron add daily-briefing "0 8 * * *" --handler briefing`'
                )
            name, schedule = parts[0], parts[1]
            try:
                job = await self.scheduler.add_job(
                    name, schedule, handler=handler_name,
                )
                from datetime import datetime
                next_str = ""
                if job.state.next_run_at:
                    next_str = datetime.fromtimestamp(job.state.next_run_at).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                return (
                    f"Job created: `{job.id[:8]}`\n"
                    f"- Name: {job.name}\n"
                    f"- Schedule: `{schedule}`\n"
                    f"- Handler: `{handler_name}`\n"
                    f"- Next run: {next_str}"
                )
            except Exception as e:
                return f"Failed to create job: {e}"

        # Prompt jobs: need name + schedule + prompt
        if len(parts) < 3:
            return (
                "Usage: `#cron add <name> <schedule> <prompt> [--model provider/model]`\n"
                "Or: `#cron add <name> <schedule> --handler <handler_name>`\n"
                'Example: `#cron add "daily report" "0 9 * * *" "Summarize today\'s tasks"`'
            )

        name, schedule, prompt = parts[0], parts[1], " ".join(parts[2:])

        llm = None
        if model_override:
            p = model_override.split("/", 1)
            if len(p) == 2:
                llm = LLMConfig(provider=p[0], model=p[1])

        try:
            job = await self.scheduler.add_job(name, schedule, prompt, llm=llm)
            from datetime import datetime
            next_str = ""
            if job.state.next_run_at:
                next_str = datetime.fromtimestamp(job.state.next_run_at).strftime(
                    "%Y-%m-%d %H:%M"
                )
            return (
                f"Job created: `{job.id[:8]}`\n"
                f"- Name: {job.name}\n"
                f"- Schedule: `{schedule}`\n"
                f"- Model: {job.llm.provider}/{job.llm.model}\n"
                f"- Next run: {next_str}"
            )
        except Exception as e:
            return f"Failed to create job: {e}"

    async def _cmd_heartbeat(self, cmd: str, args: str) -> str:
        subcmd = cmd.replace("#heartbeat", "").strip() or (
            args.split()[0] if args else "status"
        )

        if subcmd == "status":
            s = self.heartbeat.status()
            return (
                f"**Heartbeat Status**\n"
                f"- Enabled: {s['enabled']}\n"
                f"- Interval: {s['interval_seconds']}s\n"
                f"- Model: {s['model']}\n"
                f"- Active hours: {s['active_hours']}\n"
                f"- Last sent: {s.get('last_sent_at', 'never')}\n"
                f"- Last preview: {s.get('last_text_preview', 'none')}"
            )
        elif subcmd == "run":
            result = await self.heartbeat.run_once(reason="manual-feishu")
            return f"Heartbeat result: **{result}**"

        return "Unknown heartbeat subcommand. Try `#heartbeat status` or `#heartbeat run`."

    async def _cmd_server(self, args: str) -> str:
        subcmd = args.strip().split()[0].lower() if args.strip() else "status"
        if subcmd == "restart":
            asyncio.create_task(self._do_server_restart())
            return "服务将在 3 秒后重启..."
        elif subcmd == "status":
            return "服务运行中（你能收到这条消息就说明正常）"
        return "Usage: `#server restart` or `#server status`"

    async def _do_server_restart(self):
        """Wait for reply delivery, then trigger hub restart in a detached process."""
        await asyncio.sleep(3)
        import subprocess
        hub_dir = os.path.expanduser("~/workspace/nas-claude-hub")
        log_path = os.path.join(hub_dir, "data", "restart.log")
        with open(log_path, "a") as lf:
            subprocess.Popen(
                ["/bin/sh", "-c", f"HUB_CHILD=0 {hub_dir}/hub.sh restart"],
                cwd=hub_dir,
                start_new_session=True,
                stdout=lf,
                stderr=lf,
            )

    async def _handle_task_approval(self, task, text: str) -> str | None:
        """Handle user response to a task awaiting approval.
        Returns response text, or None to fall through to normal routing.
        """
        text_lower = text.strip().lower()
        # Approve keywords
        if text_lower in ("ok", "好", "确认", "开始", "执行", "go", "yes", "可以"):
            ok = await self.task_runner.approve_task(task.task_id)
            if ok:
                log.info("Task %s approved by user", task.task_id)
                return f"任务 `{task.task_id}` 已批准，开始执行..."
            return "任务状态异常，无法批准。"
        # Cancel keywords
        if text_lower in ("取消", "cancel", "算了", "不要了"):
            await self.task_runner.cancel_task(task.task_id)
            return f"任务 `{task.task_id}` 已取消。"
        # Otherwise treat as feedback for replan
        ok = await self.task_runner.reject_task(task.task_id, feedback=text)
        if ok:
            log.info("Task %s rejected with feedback, replanning", task.task_id)
            return f"收到反馈，正在重新规划任务 `{task.task_id}`..."
        return None

    async def _cmd_task(self, args: str, chat_id: str, sender_id: str) -> str | None:
        """Handle #task command — create long-running task or manage existing ones."""
        if not self.task_runner:
            return "长程任务功能未启用。"

        if not args.strip():
            return self._cmd_task_list()

        subcmd = args.strip().split(None, 1)[0].lower()
        rest = args.strip().split(None, 1)[1] if len(args.strip().split(None, 1)) > 1 else ""

        if subcmd == "list":
            return self._cmd_task_list()
        elif subcmd == "cancel":
            task_id = rest.strip()
            ok = await self.task_runner.cancel_task(task_id)
            return f"任务 `{task_id}` 已取消。" if ok else f"任务 `{task_id}` 未找到或已结束。"
        elif subcmd == "status":
            task_id = rest.strip()
            task = self.task_runner.get_task(task_id)
            if not task:
                return f"任务 `{task_id}` 未找到。"
            return task.progress_text()
        else:
            # Treat entire args as the task goal
            session_key = (
                f"user:{sender_id}" if chat_id.startswith("oc_") is False
                else f"chat:{chat_id}"
            )
            # Check for existing active task
            existing = self.task_runner.get_awaiting_task(session_key)
            if existing:
                return (
                    f"已有等待确认的任务 `{existing.task_id}`：{existing.goal[:60]}...\n"
                    f"请先回复 **ok** 确认或 **取消** (`#task cancel {existing.task_id}`)。"
                )
            task = await self.task_runner.create_task(
                goal=args.strip(),
                session_key=session_key,
                chat_id=chat_id,
                sender_id=sender_id,
            )
            return f"任务已创建 `{task.task_id}`，正在生成执行计划..."

    def _cmd_task_list(self) -> str:
        """List all tasks."""
        tasks = self.task_runner.list_all()
        if not tasks:
            return "没有任务记录。"
        # Show most recent 10
        tasks = sorted(tasks, key=lambda t: t.created_at, reverse=True)[:10]
        lines = ["**任务列表**\n"]
        for t in tasks:
            status_icon = {
                "planning": "🔧", "awaiting_approval": "⏳",
                "executing": "🔄", "completed": ":DONE:",
                "failed": "❌",
            }.get(t.status, "?")
            done = sum(1 for s in t.steps if s.status == "completed")
            total = len(t.steps)
            progress = f" ({done}/{total})" if total > 0 else ""
            lines.append(
                f"| `{t.task_id}` | {status_icon} {t.status} | "
                f"{t.goal[:40]}{'...' if len(t.goal) > 40 else ''}{progress} |"
            )
        return "\n".join(lines)

    # ═══ Media Processing ═══

    async def _analyze_image(
        self, message_id: str, content_str: str, session_key: str,
    ) -> tuple[str | None, str]:
        """Gemini 3-Flash as vision module. Saves to FileStore. Returns (desc, footer)."""
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else {}
        except Exception:
            content = {}

        image_key = content.get("image_key", "") if isinstance(content, dict) else ""
        if not image_key:
            return None, ""

        tmp_path = None
        try:
            tmp_path = await asyncio.to_thread(
                self._download_feishu_image_sync, message_id, image_key
            )
            if not tmp_path:
                return None, ""

            # Save to FileStore permanently
            stored_path = self.file_store.save_from_path(
                session_key, tmp_path,
                original_name=f"{image_key[:16]}.webp",
                file_type="image",
            )

            # Analyze with Gemini
            vision_config = LLMConfig(provider="gemini-api", model="3-Flash")
            result = await self.router.run(
                prompt=(
                    "Describe this image in detail in Chinese. "
                    "Include: text/code content (transcribe exactly), "
                    "UI elements, layout, colors, and any notable details. "
                    "Be factual and structured."
                ),
                llm_config=vision_config,
                image_src=stored_path,
            )
            if result.is_error:
                log.warning("Vision analysis failed: %s", result.text[:200])
                return None, ""

            # Update analysis in FileStore
            self.file_store.update_analysis(
                session_key, os.path.basename(stored_path), result.text
            )

            footer = ""
            if result.cost_usd > 0:
                footer = (
                    f"\n\n`vision: gemini-api/3-Flash"
                    f" | ${result.cost_usd:.4f} | {result.duration_ms}ms`"
                )
            return result.text, footer
        except Exception as e:
            log.error("Image analysis error: %s", e)
            return None, ""
        finally:
            # Clean up temp file (stored copy is in FileStore)
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _download_feishu_image_sync(
        self, message_id: str, image_key: str,
    ) -> str | None:
        """Download image from Feishu, compress to webp (max 1024px). Returns temp path."""
        import requests
        from io import BytesIO
        try:
            r = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            token = r.json().get("tenant_access_token", "")
            if not token:
                log.error("Image download: failed to get access token")
                return None

            url = (
                f"https://open.feishu.cn/open-apis/im/v1/messages"
                f"/{message_id}/resources/{image_key}?type=image"
            )
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=30
            )
            if resp.status_code != 200:
                log.error("Image download failed: status=%d", resp.status_code)
                return None

            from PIL import Image
            img = Image.open(BytesIO(resp.content))
            max_dim = 1024
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            tmp_path = os.path.expanduser(f"~/tmp/feishu_img_{image_key[:16]}.webp")
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            img.save(tmp_path, "WEBP", quality=80)
            orig_kb = len(resp.content) / 1024
            final_kb = os.path.getsize(tmp_path) / 1024
            log.info(
                "Image compressed: %.0fKB -> %.0fKB (webp, max %dpx)",
                orig_kb, final_kb, max_dim,
            )
            return tmp_path
        except Exception as e:
            log.error("Image download error: %s", e)
            return None

    # Text-readable file extensions
    _TEXT_EXTS = {
        ".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml",
        ".toml", ".ini", ".cfg", ".conf", ".sh", ".bash", ".zsh",
        ".html", ".css", ".xml", ".csv", ".log", ".sql", ".go",
        ".rs", ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".rb",
        ".swift", ".r", ".lua", ".pl", ".php", ".env", ".gitignore",
        ".dockerfile", ".makefile",
    }

    async def _process_file(
        self, message_id: str, content_str: str, session_key: str,
    ) -> tuple[str | None, str]:
        """Download file, save to FileStore, parse content. Returns (prompt_text, footer)."""
        try:
            content = json.loads(content_str) if isinstance(content_str, str) else {}
        except Exception:
            content = {}

        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "unknown")
        if not file_key:
            return None, ""

        tmp_path = None
        try:
            tmp_path = await asyncio.to_thread(
                self._download_feishu_file_sync, message_id, file_key, file_name
            )
            if not tmp_path:
                return None, ""

            ext = os.path.splitext(file_name)[1].lower()
            file_type = (
                "pdf" if ext == ".pdf"
                else "text" if ext in self._TEXT_EXTS or ext == ""
                else "other"
            )

            # Save to FileStore permanently
            stored_path = self.file_store.save_from_path(
                session_key, tmp_path,
                original_name=file_name,
                file_type=file_type,
            )

            # PDF → Gemini Files API
            if ext == ".pdf":
                vision_config = LLMConfig(provider="gemini-api", model="3-Flash")
                result = await self.router.run(
                    prompt=(
                        f"Parse this PDF file '{file_name}' and output its full content "
                        "in structured Chinese. Preserve headings, lists, tables. "
                        "Be faithful to the original content."
                    ),
                    llm_config=vision_config,
                    files=[stored_path],
                )
                if result.is_error:
                    log.warning("PDF parse failed: %s", result.text[:200])
                    return None, ""
                self.file_store.update_analysis(
                    session_key, os.path.basename(stored_path), result.text[:500]
                )
                footer = ""
                if result.cost_usd > 0:
                    footer = (
                        f"\n\n`parse: gemini-api/3-Flash"
                        f" | ${result.cost_usd:.4f} | {result.duration_ms}ms`"
                    )
                return (
                    f"[用户发送了文件: {file_name}]\n文件内容：\n{result.text}",
                    footer,
                )

            # Text/code → read directly
            elif ext in self._TEXT_EXTS or ext == "":
                with open(stored_path, "r", encoding="utf-8", errors="replace") as f:
                    file_content = f.read()
                if len(file_content) > 10000:
                    file_content = (
                        file_content[:10000]
                        + f"\n\n... [truncated, total {len(file_content)} chars]"
                    )
                return (
                    f"[用户发送了文件: {file_name}]\n```\n{file_content}\n```",
                    "",
                )

            else:
                return (
                    f"[用户发送了文件: {file_name}] "
                    f"(不支持的格式: {ext}，支持 PDF 和文本/代码文件)",
                    "",
                )

        except Exception as e:
            log.error("File processing error: %s", e)
            return None, ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _download_feishu_file_sync(
        self, message_id: str, file_key: str, file_name: str,
    ) -> str | None:
        """Download file from Feishu API. Returns temp file path."""
        import requests
        try:
            r = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10,
            )
            token = r.json().get("tenant_access_token", "")
            if not token:
                log.error("File download: failed to get access token")
                return None

            url = (
                f"https://open.feishu.cn/open-apis/im/v1/messages"
                f"/{message_id}/resources/{file_key}?type=file"
            )
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"}, timeout=60
            )
            if resp.status_code != 200:
                log.error("File download failed: status=%d", resp.status_code)
                return None

            tmp_path = os.path.expanduser(
                f"~/tmp/feishu_file_{file_key[:16]}_{file_name}"
            )
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            log.info("File downloaded: %s (%.1fKB)", file_name, len(resp.content) / 1024)
            return tmp_path
        except Exception as e:
            log.error("File download error: %s", e)
            return None

    # ═══ Helpers ═══

    def _parse_content(self, content_str: str, msg_type: str) -> str:
        try:
            content = (
                json.loads(content_str) if isinstance(content_str, str) else content_str
            )
        except json.JSONDecodeError:
            return content_str if isinstance(content_str, str) else ""

        if msg_type == "text":
            return content.get("text", "")
        elif msg_type == "post":
            return self._parse_post_content(content)
        elif msg_type == "markdown":
            return content.get("text", "")
        return ""

    def _parse_post_content(self, content: dict) -> str:
        """Parse post message content, handling both flat and multi-language structures."""
        # Detect structure: flat {title, content: [[...]]} vs multi-lang {zh_cn: {title, content}}
        if "content" in content and isinstance(content["content"], list):
            # Flat structure (most common from Feishu client)
            return self._extract_post_body(content)
        # Multi-language structure — use first available language
        for lang_content in content.values():
            if isinstance(lang_content, dict) and "content" in lang_content:
                return self._extract_post_body(lang_content)
        return ""

    def _extract_post_body(self, post: dict) -> str:
        """Extract text from a single post body {title, content: [[elements]]}."""
        lines = []
        title = post.get("title")
        if title:
            lines.append(title)
        for para in post.get("content", []):
            if not isinstance(para, list):
                continue
            parts = []
            for elem in para:
                tag = elem.get("tag", "")
                if tag in ("text", "md"):
                    parts.append(elem.get("text", ""))
                elif tag == "a":
                    text = elem.get("text", "")
                    href = elem.get("href", "")
                    parts.append(f"[{text}]({href})" if href else text)
                elif tag == "at":
                    parts.append(elem.get("name", elem.get("key", "")))
                elif tag == "code_block":
                    lang = elem.get("language", "")
                    parts.append(f"```{lang}\n{elem.get('text', '')}\n```")
                elif tag == "emotion":
                    parts.append(f":{elem.get('emoji_type', '')}:")
                # img/media/hr — skip, no text content
            if parts:
                lines.append("".join(parts))
        return "\n".join(lines)

    def _fetch_quoted_text(self, parent_id: str) -> str:
        """Fetch the content of a quoted/replied-to message via API."""
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
            return self._parse_content(body, msg_type)
        except Exception as e:
            log.warning("Error fetching parent message %s: %s", parent_id, e)
            return ""

    def _is_duplicate(self, message_id: str) -> bool:
        now = time.time()
        if message_id in self._dedup:
            return True
        if len(self._dedup) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_TTL
            self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
        return False

    def _record_message(self, message_id: str):
        self._dedup[message_id] = time.time()

    @staticmethod
    def _parse_quoted_args(text: str) -> list[str]:
        args = []
        current = ""
        in_quote = None
        for ch in text:
            if ch in ('"', "'") and not in_quote:
                in_quote = ch
            elif ch == in_quote:
                in_quote = None
            elif ch == " " and not in_quote:
                if current:
                    args.append(current)
                    current = ""
            else:
                current += ch
        if current:
            args.append(current)
        return args

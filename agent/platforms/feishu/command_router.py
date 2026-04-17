# -*- coding: utf-8 -*-
"""Feishu command routing extracted from bot.py."""

import asyncio
from pathlib import Path

from agent.platforms.feishu.dispatcher import Dispatcher
from agent.platforms.feishu.session import SKILL_ROUTES


class CommandRouter:
    def __init__(
        self,
        router,
        scheduler,
        dispatcher: Dispatcher,
        orchestrator=None,
        user_store=None,
        admin_open_ids=frozenset(),
        project_root: str = "",
    ):
        self.router = router
        self.scheduler = scheduler
        self.dispatcher = dispatcher
        self.orchestrator = orchestrator
        self.user_store = user_store
        self._admin_open_ids = admin_open_ids
        self._project_root = project_root or str(Path(__file__).resolve().parent.parent.parent.parent)
        self._command_handlers = {}
        self._help_sections = []

    def register(self, prefix: str, handler, help_lines: str | None = None) -> None:
        """Register a plugin command handler. handler: async (cmd, args) -> str"""
        self._command_handlers[prefix] = handler
        if help_lines:
            self._help_sections.append(help_lines)

    async def route(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        session_key: str,
        *,
        on_plan_ready=None,
        on_plan_execute=None,
    ) -> str | None:
        """Route #commands. Returns response text or None if not a command."""
        del chat_type
        del on_plan_execute
        text = text.strip()
        if not text.startswith("#"):
            return None
        first_word = text.split(None, 1)[0].lower()
        if first_word in SKILL_ROUTES:
            return None

        parts = text.split(None, 1)
        cmd = parts[0].lower()

        if cmd == "#help":
            asyncio.ensure_future(self.send_menu_card(chat_id))
            return self._cmd_help()
        if cmd == "#reset":
            self._cmd_reset(session_key)
            card_json = self.dispatcher._build_card_json(
                "会话已重置，下条消息开始新对话。",
                header="Session Reset",
                color="grey",
            )
            await self.dispatcher.send_card_raw(chat_id, card_json)
            return None
        if cmd == "#usage":
            return await self._cmd_usage()
        if cmd == "#jobs":
            return self._cmd_jobs()
        if cmd == "#restart":
            is_admin = (
                (self.user_store and self.user_store.get(sender_id) and self.user_store.get(sender_id).is_admin())
                or sender_id in self._admin_open_ids
            )
            if not is_admin:
                return "权限不足，仅管理员可执行 #restart"
            return "服务将在 3 秒后重启..."
        if cmd == "#parallel":
            task_text = parts[1] if len(parts) > 1 else ""
            if not task_text:
                return "用法：`#parallel <任务描述>`"
            if not self.orchestrator:
                return "并行执行功能未启用。"
            if on_plan_ready:
                await on_plan_ready(task_text, session_key, chat_id)
            return "📋 正在分析任务…"
        if cmd == "#opus":
            return self._cmd_switch_model("opus", session_key)
        if cmd == "#sonnet":
            return self._cmd_switch_model("sonnet", session_key)
        if cmd == "#haiku":
            return self._cmd_switch_model("haiku", session_key)
        if cmd == "#think":
            return self._cmd_think(session_key)
        if cmd == "#menu":
            asyncio.ensure_future(self.send_menu_card(chat_id))
            return ""

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
            "| `#help` | 查看帮助 |\n"
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

    async def send_menu_card(self, chat_id: str) -> None:
        """Send a quick action menu card with buttons."""
        buttons = [
            {"text": "📊 服务状态", "type": "default", "value": {"action": "menu_action", "command": "#usage"}},
            {"text": "📅 今日日程", "type": "default", "value": {"action": "menu_action", "command": "今天有什么日程？"}},
            {"text": "📰 触发日报", "type": "default", "value": {"action": "menu_action", "command": "#briefing run"}},
            {"text": "🔁 重置会话", "type": "default", "value": {"action": "menu_action", "command": "#reset"}},
            {"text": "🔄 定时任务", "type": "default", "value": {"action": "menu_action", "command": "#jobs"}},
            {"text": "❓ 帮助", "type": "default", "value": {"action": "menu_action", "command": "#help"}},
        ]
        btn_group = Dispatcher.build_button_group(buttons, layout="bisected")
        elements = [{"tag": "markdown", "content": "选择一个快捷操作："}]
        if isinstance(btn_group, list):
            elements.extend(btn_group)
        else:
            elements.append(btn_group)
        card_json = Dispatcher.build_interactive_card(elements, header="快捷操作面板", color="blue")
        result = self.dispatcher.send_card_raw(chat_id, card_json)
        if asyncio.iscoroutine(result):
            await result

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
        for job in jobs:
            status = "✅" if job.enabled else "⚠️"
            sched = job.schedule.expr or f"{job.schedule.every_seconds}s"
            next_run = ""
            if job.state.next_run_at:
                next_run = datetime.fromtimestamp(job.state.next_run_at).strftime("%m-%d %H:%M")
            lines.append(f"{status} **{job.name}** `{sched}` → {next_run}")
        return "\n".join(lines)

    async def _cmd_usage(self) -> str:
        """Check Claude Max quota via API headers."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3",
                "scripts/check_quota.py",
                "--feishu",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._project_root,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode != 0:
                return f"Quota check failed: {stderr.decode().strip()}"
            return stdout.decode().strip()
        except asyncio.TimeoutError:
            return "Quota check timed out"
        except Exception as exc:
            return f"Quota check error: {exc}"

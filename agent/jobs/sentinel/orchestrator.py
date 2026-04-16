# -*- coding: utf-8 -*-
"""SentinelOrchestrator — coordinates scanners, routes signals, manages cadence."""

from __future__ import annotations

import logging
import time
from typing import Literal

from agent.jobs.sentinel.base import SentinelScanner
from agent.jobs.sentinel.models import EntropySignal, ScanContext
from agent.jobs.sentinel.store import SentinelStore

log = logging.getLogger("hub.sentinel")

# Dynamic frequency: minimum interval between scans per change-rate class
FREQUENCY_INTERVALS = {
    "hourly": 3600,       # 1 hour
    "daily": 6 * 3600,    # 6 hours (multiple scans per day)
    "weekly": 24 * 3600,  # once per day
}

# Severity → MAQS priority mapping
SEVERITY_TO_PRIORITY = {
    "critical": "P0",
    "high": "P1",
    "medium": "P2",
    "low": "P3",
}

# Category → (emoji, friendly_title, default_action)
CATEGORY_DISPLAY: dict[str, tuple[str, str, str]] = {
    "stale_doc": ("📋", "文档审计发现", "检查是否需要更新"),
    "doc_duplicate": ("📋", "文档重复提醒", "合并或删除重复文档"),
    "search_recall_degradation": ("🔍", "搜索健康告警", "检查索引和召回配置"),
    "stale_todo": ("📝", "代码 TODO 提醒", "处理或关闭过期 TODO"),
    "uncommitted_stale": ("💾", "未提交变更提醒", "提交或清理暂存变更"),
    "stale_branch": ("🌿", "分支清理提醒", "合并或删除过期分支"),
    "error_spike": ("🚨", "错误激增告警", "查看错误日志排查原因"),
    "skill_unused": ("🔧", "技能闲置提醒", "评估是否保留该技能"),
    "disk_usage_high": ("💿", "磁盘空间告警", "清理旧文件释放空间"),
}


class SentinelOrchestrator:
    """Central coordinator for scanner execution and signal routing."""

    # Categories that should route to primary bot (user-facing) instead of notifier
    USER_FACING_CATEGORIES = {"discussion_stale", "tracked_discussion"}

    def __init__(
        self,
        scanners: list[SentinelScanner],
        store: SentinelStore,
        dispatcher=None,
        user_dispatcher=None,
        workspace_dir: str = ".",
        config: dict | None = None,
        notify_open_id: str = "",
    ):
        self._scanners = list(scanners)
        self._store = store
        self._dispatcher = dispatcher
        self._user_dispatcher = user_dispatcher
        self._workspace_dir = workspace_dir
        self._config = config or {}
        self._notify_open_id = notify_open_id

    def register_scanner(self, scanner: SentinelScanner) -> None:
        self._scanners.append(scanner)

    def get_scanner(self, name: str) -> "SentinelScanner | None":
        """Return the first scanner matching the given name, or None."""
        for scanner in self._scanners:
            if scanner.name == name:
                return scanner
        return None

    async def run_cycle(
        self, trigger: Literal["idle", "cron", "manual"] = "idle"
    ) -> dict:
        """Execute a full sentinel cycle: scan → prioritize → route.

        Returns a summary dict with counts per route.
        """
        log.info("Sentinel cycle starting (trigger=%s, scanners=%d)",
                 trigger, len(self._scanners))

        # Build context
        recent = self._store.query(hours=24, unresolved_only=True)
        context = ScanContext(
            workspace_dir=self._workspace_dir,
            last_scan_at=0,  # per-scanner overrides below
            recent_signals=recent,
            user_config=self._config,
        )

        # Collect signals from due scanners
        all_signals: list[EntropySignal] = []
        for scanner in self._due_scanners(trigger):
            context.last_scan_at = self._store.get_last_scan_time(scanner.name)
            try:
                signals = await scanner.scan(context)
                log.info("Scanner [%s] produced %d signals", scanner.name, len(signals))
                all_signals.extend(signals)
            except Exception as e:
                log.error("Scanner [%s] failed: %s", scanner.name, str(e)[:200])
                # Isolation: one scanner error doesn't stop others

        # Dedup and prioritize
        deduped = self._dedup(all_signals, recent)
        prioritized = self._prioritize(deduped)

        # Persist and route
        summary = {"total": 0, "maqs": 0, "explore": 0, "notify": 0, "silent_log": 0}
        for signal in prioritized:
            self._store.append(signal)
            await self._route_signal(signal)
            summary["total"] += 1
            summary[signal.route] = summary.get(signal.route, 0) + 1

        log.info("Sentinel cycle complete: total=%d maqs=%d explore=%d notify=%d silent=%d",
                 summary["total"], summary["maqs"], summary["explore"],
                 summary["notify"], summary["silent_log"])
        summary["signals"] = prioritized
        return summary

    async def get_pending_signals(self, hours: float = 24) -> list[EntropySignal]:
        """Return unresolved signals for user review."""
        return self._store.query(hours=hours, unresolved_only=True)

    async def get_stats(self) -> dict:
        """Statistics for the Sentinel Skill to display."""
        return self._store.stats(hours=24)

    # ── Internal routing ──

    async def _route_signal(self, signal: EntropySignal) -> None:
        """Dispatch signal to the appropriate handler."""
        if signal.route == "maqs":
            await self._create_maqs_ticket(signal)
        elif signal.route == "explore":
            await self._create_exploration_task(signal)
        elif signal.route == "notify":
            await self._notify_user(signal)
        else:
            pass  # silent_log — already persisted

    async def _create_maqs_ticket(self, signal: EntropySignal) -> None:
        """Create a MAQS ticket for the signal."""
        fields = {
            "title": signal.summary[:100],
            "type": "bug",
            "complexity": "atomic",
            "source": f"sentinel:{signal.source}",
            "phenomenon": signal.summary,
            "severity": SEVERITY_TO_PRIORITY.get(signal.severity, "P2"),
            "status": "open",
            "reject_count": 0,
            "golden_data": "\n".join(signal.evidence) if signal.evidence else "",
        }

        maqs_cfg = self._config.get("maqs", {})
        app_token = maqs_cfg.get("bitable_app_token", "")
        table_id = maqs_cfg.get("bitable_table_id", "")

        if not app_token or not table_id:
            log.warning("Sentinel: cannot create MAQS Bitable ticket — bitable not configured")
            return

        from agent.jobs.mads.helpers import bitable_add

        record_id = await bitable_add(app_token, table_id, fields)
        if record_id:
            log.info("Sentinel → MAQS ticket created: %s (%s)", record_id, signal.summary)
            # Mark as resolved: signal handed off to MAQS, prevents duplicate tickets
            self._store.resolve(signal.id)
        else:
            log.error("Sentinel → MAQS ticket creation failed: %s", signal.summary)

    async def _create_exploration_task(self, signal: EntropySignal) -> None:
        """Create an exploration queue entry for deeper investigation."""
        try:
            from agent.infra.exploration import ExplorationQueue, ExplorationTask, Priority

            priority = (Priority.P1_HIGH if signal.severity in ("critical", "high")
                        else Priority.P2_NORMAL)

            task = ExplorationTask(
                title=signal.summary,
                priority=priority,
                pillar="ops",
                source=f"sentinel:{signal.source}",
                source_context="\n".join(signal.evidence) if signal.evidence else "",
                autonomy_level=0,
            )

            eq = ExplorationQueue()
            await eq.load()
            await eq.add(task)
            log.info("Sentinel → exploration task created: %s", signal.summary)
        except Exception as e:
            log.warning("Sentinel → exploration task failed: %s", str(e)[:200])

    async def _notify_user(self, signal: EntropySignal) -> None:
        """Send a user-friendly DM notification via dispatcher."""
        if not self._dispatcher:
            log.warning("Sentinel: no dispatcher for notification: %s", signal.summary)
            return

        emoji, title, default_action = CATEGORY_DISPLAY.get(
            signal.category, ("🔔", signal.category, "查看详情")
        )
        action = signal.suggested_action or default_action
        text = f"{emoji} {title}\n{signal.summary}\n\n建议：{action}"

        # User-facing categories → primary bot DM; ops alerts → notifier
        if signal.category in self.USER_FACING_CATEGORIES and self._user_dispatcher:
            if self._notify_open_id:
                await self._user_dispatcher.send_to_user(self._notify_open_id, text)
            else:
                await self._user_dispatcher.send_to_delivery_target(text)
        else:
            await self._dispatcher.send_to_delivery_target(text)

    # ── Frequency & prioritization ──

    def _due_scanners(
        self, trigger: str
    ) -> list[SentinelScanner]:
        """Return scanners that are due to run based on dynamic frequency.

        Manual trigger always runs all scanners.
        """
        if trigger == "manual":
            return list(self._scanners)

        now = time.time()
        due = []
        for scanner in self._scanners:
            rate = scanner.estimate_change_rate()
            interval = FREQUENCY_INTERVALS.get(rate, FREQUENCY_INTERVALS["daily"])
            last_run = self._store.get_last_scan_time(scanner.name)
            if now - last_run >= interval:
                due.append(scanner)

        return due

    def _dedup(
        self,
        new_signals: list[EntropySignal],
        recent: list[EntropySignal],
    ) -> list[EntropySignal]:
        """Remove signals that duplicate recent unresolved ones."""
        seen = {
            (s.source, s.category, s.summary)
            for s in recent if s.resolved_at is None
        }
        result = []
        for s in new_signals:
            key = (s.source, s.category, s.summary)
            if key not in seen:
                result.append(s)
                seen.add(key)
        return result

    def _prioritize(self, signals: list[EntropySignal]) -> list[EntropySignal]:
        """Sort signals by severity (critical first)."""
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return sorted(signals, key=lambda s: severity_order.get(s.severity, 3))

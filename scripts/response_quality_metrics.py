#!/usr/bin/env python3
"""Response Quality Proxy Metrics (P0-γ).

Three proxy metrics computed from message_store SQLite and compressed_summaries.jsonl:
  1. Conversation Continuation Rate — 区分自然结束 / 主动 reset / 沉默流失
  2. Redo Request Rate — 摘要中出现重做/纠正信号的比例
  3. Task Completion Rate — 摘要中出现完成信号的比例

Usage:
    python3 scripts/response_quality_metrics.py              # 7-day summary
    python3 scripts/response_quality_metrics.py --days 30    # 30-day summary
    python3 scripts/response_quality_metrics.py --json       # machine-readable
"""

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hub.response_quality_metrics")

BASE = Path(__file__).resolve().parent.parent
MESSAGES_DB = BASE / "data" / "messages.db"
SESSIONS_DB = BASE / "data" / "sessions.db"
SUMMARIES_JSONL = BASE / "data" / "compressed_summaries.jsonl"

# ── 沉默流失阈值 ──
SILENCE_GAP_SECONDS = 2 * 3600  # 2h
SLEEP_START_HOUR = 23
SLEEP_END_HOUR = 7

# ── Redo 关键词 ──
REDO_KEYWORDS = [
    "重新", "不对", "重来", "再来", "重做", "wrong", "redo",
    "返工", "改回", "回滚", "撤销", "不行", "有问题",
]

# ── 完成信号关键词 ──
COMPLETION_KEYWORDS = [
    "完成", "已完成", "done", "deployed", "committed", "merged",
    "已部署", "已合并", "已提交", "搞定", "上线",
]


# ─────────────────────────────────────────────
# Metric 1: Session continuation / reset / silence
# ─────────────────────────────────────────────

def _is_sleep_hour(ts: float) -> bool:
    """Check if timestamp falls in sleep window (23:00-07:00 local)."""
    dt = datetime.fromtimestamp(ts)
    return dt.hour >= SLEEP_START_HOUR or dt.hour < SLEEP_END_HOUR


def compute_reset_rate(days: int = 7) -> dict:
    """Detect reset events from session_store session_id transitions.

    每个 session_key 的 session_id 变化 = 一次 reset（或服务重启后降级）。
    这里通过 history_archive.jsonl 中 session 边界 + messages.db 时间间隔综合判断。
    """
    if not SESSIONS_DB.exists():
        log.warning("sessions.db not found, skipping reset rate")
        return {"total_sessions": 0, "resets": 0, "silence_gaps": 0,
                "natural_ends": 0, "reset_rate": 0.0, "silence_rate": 0.0}

    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400

    # 从 messages.db 获取用户会话时间线
    sessions_timeline: dict[str, list[float]] = {}
    if MESSAGES_DB.exists():
        try:
            conn = sqlite3.connect(str(MESSAGES_DB))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT batch_key, created_at FROM messages "
                "WHERE created_at > ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
            conn.close()
            for r in rows:
                bk = r["batch_key"] or "unknown"
                sessions_timeline.setdefault(bk, []).append(r["created_at"])
        except Exception as e:
            log.warning("Failed to read messages.db: %s", e)

    # 从 sessions.db 检测 session_id 变化（reset 信号）
    reset_keys: set[str] = set()
    try:
        conn = sqlite3.connect(str(SESSIONS_DB))
        rows = conn.execute(
            "SELECT session_key, session_id, updated_at FROM sessions "
            "WHERE updated_at > ?",
            (cutoff,),
        ).fetchall()
        conn.close()
        # session_id 为 None 或空 = 被 reset 过
        for key, sid, updated in rows:
            if not sid:
                reset_keys.add(key)
    except Exception as e:
        log.warning("Failed to read sessions.db: %s", e)

    # 统计三种结束模式
    total_sessions = len(sessions_timeline)
    resets = 0
    silence_gaps = 0

    for bk, timestamps in sessions_timeline.items():
        # reset 检测：batch_key 对应的 session_key
        session_key = f"user:{bk.split(':')[-1]}" if ":" in bk else bk
        if session_key in reset_keys or bk in reset_keys:
            resets += 1
            continue

        # 沉默流失检测：最后两条消息间隔 > 2h（排除睡眠时段）
        if len(timestamps) >= 2:
            last_gap = timestamps[-1] - timestamps[-2]
            if last_gap > SILENCE_GAP_SECONDS and not _is_sleep_hour(timestamps[-2]):
                silence_gaps += 1

    natural_ends = max(0, total_sessions - resets - silence_gaps)
    return {
        "total_sessions": total_sessions,
        "resets": resets,
        "silence_gaps": silence_gaps,
        "natural_ends": natural_ends,
        "reset_rate": resets / total_sessions if total_sessions else 0.0,
        "silence_rate": silence_gaps / total_sessions if total_sessions else 0.0,
    }


# ─────────────────────────────────────────────
# Helpers: load summaries
# ─────────────────────────────────────────────

def _load_summaries(days: int) -> list[dict]:
    """Load real-user summaries within date range."""
    if not SUMMARIES_JSONL.exists():
        log.warning("compressed_summaries.jsonl not found")
        return []

    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)
    records = []
    for line in SUMMARIES_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        # 过滤测试数据
        sk = rec.get("session_key", "")
        if sk.startswith("test:") or sk in ("k",):
            continue
        # 日期过滤
        ts_str = rec.get("compressed_at", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        # 过滤太短的摘要（非实质会话）
        if rec.get("summary_chars", 0) < 50:
            continue
        records.append(rec)
    return records


# ─────────────────────────────────────────────
# Metric 2: Redo Request Rate
# ─────────────────────────────────────────────

def compute_redo_rate(days: int = 7) -> dict:
    """Scan summaries for redo/rejection signals."""
    records = _load_summaries(days)
    if not records:
        return {"total_sessions": 0, "redo_sessions": 0, "redo_rate": 0.0,
                "redo_details": []}

    redo_sessions = 0
    details = []
    for rec in records:
        summary = rec.get("summary", "")
        matched = [kw for kw in REDO_KEYWORDS if kw in summary]
        if matched:
            redo_sessions += 1
            details.append({
                "session_key": rec["session_key"],
                "keywords": matched,
                "snippet": summary[:120],
            })

    total = len(records)
    return {
        "total_sessions": total,
        "redo_sessions": redo_sessions,
        "redo_rate": redo_sessions / total if total else 0.0,
        "redo_details": details,
    }


# ─────────────────────────────────────────────
# Metric 3: Task Completion Rate
# ─────────────────────────────────────────────

def compute_task_completion_rate(days: int = 7) -> dict:
    """Estimate task completion from compressed summaries."""
    records = _load_summaries(days)
    if not records:
        return {"total_sessions": 0, "completed": 0, "completion_rate": 0.0}

    completed = 0
    for rec in records:
        summary = rec.get("summary", "")
        if any(kw in summary for kw in COMPLETION_KEYWORDS):
            completed += 1

    total = len(records)
    return {
        "total_sessions": total,
        "completed": completed,
        "completion_rate": completed / total if total else 0.0,
    }


# ─────────────────────────────────────────────
# Main entry
# ─────────────────────────────────────────────

async def run_response_quality_metrics(days: int = 7) -> str:
    """Run all three proxy metrics and return formatted summary."""
    reset = compute_reset_rate(days)
    redo = compute_redo_rate(days)
    completion = compute_task_completion_rate(days)

    lines = [
        f"响应质量代理指标 (近 {days} 天)",
        "=" * 36,
        "",
        "1. 会话延续率",
        f"   总会话: {reset['total_sessions']}",
        f"   自然结束: {reset['natural_ends']}",
        f"   主动 reset: {reset['resets']} ({reset['reset_rate']:.1%})",
        f"   沉默流失: {reset['silence_gaps']} ({reset['silence_rate']:.1%})",
        "",
        "2. 重做请求率",
        f"   总会话: {redo['total_sessions']}",
        f"   重做会话: {redo['redo_sessions']} ({redo['redo_rate']:.1%})",
    ]

    if redo.get("redo_details"):
        lines.append("   详情:")
        for d in redo["redo_details"][:5]:
            lines.append(f"     - [{','.join(d['keywords'])}] {d['snippet'][:60]}...")

    lines.extend([
        "",
        "3. 任务完成率",
        f"   总会话: {completion['total_sessions']}",
        f"   已完成: {completion['completed']} ({completion['completion_rate']:.1%})",
    ])

    return "\n".join(lines)


def main():
    import asyncio
    parser = argparse.ArgumentParser(description="Response Quality Proxy Metrics")
    parser.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    if args.json:
        result = {
            "reset": compute_reset_rate(args.days),
            "redo": compute_redo_rate(args.days),
            "completion": compute_task_completion_rate(args.days),
        }
        # 移除 details 避免输出过长
        if "redo_details" in result["redo"]:
            result["redo"]["redo_details"] = result["redo"]["redo_details"][:5]
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(asyncio.run(run_response_quality_metrics(args.days)))


if __name__ == "__main__":
    main()

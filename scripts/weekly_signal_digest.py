#!/usr/bin/env python3
"""Weekly behavior signal digest — aggregates signals and detects trends."""

import asyncio
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DIGEST_DIR = BASE / "data" / "signal_digests"
EXTRACT_SCRIPT = BASE / "scripts" / "extract_behavior_signals.py"


def _current_week_label() -> str:
    """Return ISO week label like '2026-W14'."""
    now = datetime.now(timezone.utc)
    return f"{now.year}-W{now.isocalendar()[1]:02d}"


def _previous_week_label(label: str) -> str:
    """Given '2026-W14', return '2026-W13'. Handles year boundary."""
    year, week = int(label[:4]), int(label.split("W")[1])
    if week > 1:
        return f"{year}-W{week - 1:02d}"
    # Week 1 → previous year's last week (approximate: use 52)
    return f"{year - 1}-W52"


def _load_previous_digest(week_label: str) -> dict | None:
    prev_label = _previous_week_label(week_label)
    prev_path = DIGEST_DIR / f"{prev_label}.json"
    if prev_path.exists():
        return json.loads(prev_path.read_text())
    return None


def _run_extract() -> dict:
    """Run extract_behavior_signals.py --json and parse output."""
    result = subprocess.run(
        [sys.executable, str(EXTRACT_SCRIPT), "--json"],
        capture_output=True, text=True, cwd=str(BASE),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Extract script failed: {result.stderr[-300:]}")
    return json.loads(result.stdout)


def _compute_deltas(current: dict[str, int], previous: dict[str, int] | None) -> dict[str, int] | None:
    if previous is None:
        return None
    all_tags = set(current) | set(previous)
    return {tag: current.get(tag, 0) - previous.get(tag, 0) for tag in all_tags}


def _detect_noteworthy(current: dict[str, int], previous: dict[str, int] | None) -> list[str]:
    """Flag tags with >1 sigma change."""
    if previous is None:
        return []

    deltas = []
    for tag in set(current) | set(previous):
        old = previous.get(tag, 0)
        new = current.get(tag, 0)
        deltas.append((tag, old, new, new - old))

    if not deltas:
        return []

    abs_changes = [abs(d[3]) for d in deltas]
    mean = sum(abs_changes) / len(abs_changes)
    variance = sum((x - mean) ** 2 for x in abs_changes) / len(abs_changes)
    sigma = math.sqrt(variance) if variance > 0 else 0

    notes = []
    for tag, old, new, delta in deltas:
        if sigma > 0 and abs(delta) > mean + sigma:
            pct = f"+{delta / old * 100:.0f}%" if old > 0 else "new"
            notes.append(f"{tag}: {old}->{new} ({pct})")

    return notes


# ── COGNITION 候选建议映射 ──
# tag + direction → 建议文本
_COGNITION_SUGGESTIONS: dict[tuple[str, str], str] = {
    ("correction", "up"): "纠正频率上升，检查最近 feedback memory 是否有未遵循规则",
    ("correction", "down"): "纠正频率下降，当前行为模式与用户预期吻合度提升",
    ("delegation:autonomous", "up"): "委托深度增加，用户信任度提升，可考虑更新 COGNITION 委托边界",
    ("delegation:autonomous", "down"): "自主委托减少，用户可能在收紧控制边界",
    ("delegation:stepwise", "up"): "用户偏向分步确认，可能需要降低默认自主度",
    ("delegation:stepwise", "down"): "分步确认减少，用户对流程信任度提升",
    ("delegation:adaptive", "up"): "适应性干预增加，检查是否有自动化流程反复失败",
    ("style_request", "up"): "风格偏好信号增加，检查是否有未内化的输出格式要求",
    ("tool_preference", "up"): "工具偏好信号增加，检查是否有未采纳的工具选择倾向",
}


def _generate_cognition_candidates(
    noteworthy: list[str],
    current: dict[str, int],
    previous: dict[str, int] | None,
) -> list[dict]:
    """Generate COGNITION update candidates from noteworthy tag changes.

    Only tags with >1σ change (already in noteworthy) get candidates.
    Returns list of {"tag", "direction", "old", "new", "suggestion"}.
    """
    if not noteworthy or previous is None:
        return []

    candidates = []
    # 从 noteworthy 字符串解析出 tag 名
    for note in noteworthy:
        # 格式: "tag: old->new (+pct%)"
        tag = note.split(":")[0].strip()
        old = previous.get(tag, 0)
        new = current.get(tag, 0)
        direction = "up" if new > old else "down"

        # 查找建议映射
        suggestion = _COGNITION_SUGGESTIONS.get((tag, direction))
        if not suggestion:
            # 通用 fallback
            arrow = "上升" if direction == "up" else "下降"
            suggestion = f"{tag} 信号{arrow}（{old}→{new}），建议检查相关行为模式"

        candidates.append({
            "tag": tag,
            "direction": direction,
            "old": old,
            "new": new,
            "suggestion": suggestion,
        })

    return candidates


def _format_summary(digest: dict) -> str:
    """Format human-readable summary for Feishu notification."""
    week = digest["week"]
    sessions = digest["sessions_analyzed"]
    total_signals = sum(digest["tag_counts"].values())
    deltas = digest.get("deltas")

    lines = [
        f"行为信号周报 ({week})",
        f"分析 session: {sessions} | 信号总数: {total_signals}",
        "",
        "主要变化:",
    ]

    for tag, count in sorted(digest["tag_counts"].items(), key=lambda x: -x[1]):
        if deltas and tag in deltas:
            d = deltas[tag]
            old = count - d
            arrow = "+" if d >= 0 else ""
            pct = f"{d / old * 100:.0f}%" if old > 0 else "new"
            sym = "+" if d > 0 else ("-" if d < 0 else "->")
            lines.append(f"  {sym} {tag}: {old}->{count} ({arrow}{pct})")
        else:
            lines.append(f"  {tag}: {count}")

    noteworthy = digest.get("noteworthy", [])
    if noteworthy:
        lines.append("\n显著变化 (>1σ):")
        for n in noteworthy:
            lines.append(f"  ! {n}")

    candidates = digest.get("cognition_candidates", [])
    if candidates:
        lines.append(f"\nCOGNITION 更新候选: {len(candidates)} 条")
        for c in candidates:
            arrow = "↑" if c["direction"] == "up" else "↓"
            lines.append(f"  {arrow} {c['tag']}: {c['suggestion']}")

    return "\n".join(lines)


async def run_weekly_digest() -> str:
    """Main entry point — run digest and return summary text."""
    week_label = _current_week_label()

    # Extract current signals
    extracted = _run_extract()
    tag_counts = extracted.get("tag_counts", {})
    sessions = extracted.get("with_prefs", 0)

    # Load previous digest for trend comparison
    prev_digest = _load_previous_digest(week_label)
    prev_tags = prev_digest["tag_counts"] if prev_digest else None

    # Compute deltas and noteworthy changes
    deltas = _compute_deltas(tag_counts, prev_tags)
    noteworthy = _detect_noteworthy(tag_counts, prev_tags)

    # Generate COGNITION update candidates from noteworthy changes
    cognition_candidates = _generate_cognition_candidates(noteworthy, tag_counts, prev_tags)

    # Build digest
    digest = {
        "week": week_label,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sessions_analyzed": sessions,
        "tag_counts": tag_counts,
        "deltas": deltas,
        "noteworthy": noteworthy,
        "cognition_candidates": cognition_candidates,
    }

    # Save digest
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = DIGEST_DIR / f"{week_label}.json"
    digest_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False))

    return _format_summary(digest)


def main():
    print(asyncio.run(run_weekly_digest()))


if __name__ == "__main__":
    main()

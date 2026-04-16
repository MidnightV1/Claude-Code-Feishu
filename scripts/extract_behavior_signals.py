#!/usr/bin/env python3
"""Extract behavioral signals from compressed conversation summaries.

Reads compressed_summaries.jsonl, extracts structured signals from the
"用户偏好与纠正" section of each session, and outputs COGNITION.md update
candidates grouped by theme.

Phase 1: regex extraction (this script, ~70 lines core logic)
Phase 2: LLM aggregation (future — feed extracted signals to Sonnet for
         semantic clustering when data volume warrants it, est. >50 sessions)

Usage:
    python3 scripts/extract_behavior_signals.py              # show extracted signals
    python3 scripts/extract_behavior_signals.py --diff       # show diff against COGNITION.md
    python3 scripts/extract_behavior_signals.py --json       # machine-readable output
"""

import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "compressed_summaries.jsonl"
COGNITION = Path.home() / ".claude" / "COGNITION.md"

PREF_RE = re.compile(r"### 用户偏好与纠正(.*?)(?=###|\Z)", re.DOTALL)

# Keyword sets for delegation pattern classification
_AUTO_KW = ["自主执行", "高度委托", "高度自主", "一句话授权", "不需要逐步",
            "不要逐步", "直接推进", "部署吧", "执行吧", "没问题，执行",
            "继续吧", "开工吧", "都做吧", "实现吧"]
_STEP_KW = ["逐步确认", "先看看", "先设计后执行", "方案确认后再授权",
            "文档写出来", "看完再推进"]
_ADAPT_KW = ["叫停", "超出容忍", "反复失败", "手工修", "跳过自动化",
             "手动执行", "手动接管"]


def load_real_summaries():
    """Load only real user sessions with substantial summaries."""
    records = []
    with open(DATA) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("session_key", "").startswith("user:")
                    and r.get("summary_chars", 0) > 100):
                records.append(r)
    return records


def _has_any(text, keywords):
    return any(kw in text for kw in keywords)


def extract_session_signals(record):
    """Extract all behavioral signals from one session summary."""
    s = record["summary"]
    date = record["compressed_at"][:10]
    m = PREF_RE.search(s)
    if not m:
        return None

    text = m.group(1).strip()
    if len(text) < 20:
        return None

    signals = {"date": date, "raw": text, "tags": []}

    if _has_any(text, _AUTO_KW):
        signals["tags"].append("delegation:autonomous")
    if _has_any(text, _STEP_KW):
        signals["tags"].append("delegation:stepwise")
    if _has_any(text, _ADAPT_KW):
        signals["tags"].append("delegation:adaptive")
    if "纠正" in text:
        signals["tags"].append("correction")
    if any(k in text for k in ["不接受", "系统性设计", "根本问题", "头疼医头"]):
        signals["tags"].append("design:systemic")

    return signals


def aggregate(records):
    """Aggregate all sessions into signal clusters."""
    sessions = []
    tag_counts = {}
    for r in records:
        sig = extract_session_signals(r)
        if sig:
            sessions.append(sig)
            for t in sig["tags"]:
                tag_counts[t] = tag_counts.get(t, 0) + 1

    return {
        "sessions": sessions,
        "tag_counts": tag_counts,
        "total": len(records),
        "with_prefs": len(sessions),
        "date_range": (records[0]["compressed_at"][:10],
                       records[-1]["compressed_at"][:10]) if records else ("", ""),
    }


def format_report(agg):
    """Format human-readable report."""
    lines = [
        f"Sessions: {agg['total']} total, {agg['with_prefs']} with preference signals",
        f"Date range: {agg['date_range'][0]} ~ {agg['date_range'][1]}",
        "",
        "Tag distribution:",
    ]
    for tag, count in sorted(agg["tag_counts"].items(), key=lambda x: -x[1]):
        pct = count / agg["with_prefs"] * 100
        lines.append(f"  {tag}: {count}/{agg['with_prefs']} ({pct:.0f}%)")

    lines.extend(["", "=== COGNITION.md update candidates ===", ""])

    # Delegation summary
    auto = agg["tag_counts"].get("delegation:autonomous", 0)
    step = agg["tag_counts"].get("delegation:stepwise", 0)
    adapt = agg["tag_counts"].get("delegation:adaptive", 0)
    n = agg["with_prefs"]

    lines.append("## 委托边界")
    lines.append(f"  autonomous: {auto}/{n} | stepwise: {step}/{n} | adaptive: {adapt}/{n}")
    if auto > 0 and step > 0:
        lines.append(f"  → 双模式：方向确认(step) → 自主执行(auto)，非单纯自主")
    if adapt > 0:
        lines.append(f"  → [NEW] 容忍阈值：自动化连续失败 ≥3 次 → 手动接管 ({adapt} sessions)")

    # Corrections
    corrections = [s for s in agg["sessions"] if "correction" in s["tags"]]
    if corrections:
        lines.extend(["", "## 纠正信号"])
        for c in corrections:
            # Extract correction lines
            for line in c["raw"].split("\n"):
                if "纠正" in line and len(line.strip()) > 20:
                    lines.append(f"  [{c['date']}] {line.strip()[:200]}")

    # Systemic design preference
    systemic = [s for s in agg["sessions"] if "design:systemic" in s["tags"]]
    if systemic:
        lines.extend(["", "## 设计偏好（系统性解法）"])
        lines.append(f"  出现频率: {len(systemic)}/{n} sessions ({len(systemic)/n*100:.0f}%)")
        lines.append("  → 高频信号：\"头疼医头\"是反模式，要求全局视角")

    return "\n".join(lines)


def main():
    records = load_real_summaries()
    agg = aggregate(records)

    if "--json" in sys.argv:
        print(json.dumps({
            "total": agg["total"],
            "with_prefs": agg["with_prefs"],
            "date_range": agg["date_range"],
            "tag_counts": agg["tag_counts"],
        }, indent=2, ensure_ascii=False))
        return

    report = format_report(agg)

    if "--diff" in sys.argv and COGNITION.exists():
        current = COGNITION.read_text()
        print("=== Current COGNITION.md sections ===")
        for line in current.split("\n"):
            if line.startswith("## "):
                print(f"  {line}")
        print()

    print(report)


if __name__ == "__main__":
    main()

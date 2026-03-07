# -*- coding: utf-8 -*-
"""Daily error scanner — analyzes hub.log and records to Feishu Bitable.

Registered as a cron handler; runs daily via scheduler.
"""

import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta

log = logging.getLogger("hub.error_scan")

# Patterns to extract structured error info from log lines
_LOG_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+(\S+)\s+(ERROR|WARNING)\s+(.+)$"
)

# Noise filters: skip these common non-actionable warnings
_NOISE_PATTERNS = [
    "RequestsDependencyWarning",
    "Startup notification",
    "Rate limited:",
]


def _parse_log_errors(log_path: str, date_str: str) -> list[dict]:
    """Extract ERROR and WARNING lines for a specific date."""
    if not os.path.exists(log_path):
        log.warning("Log file not found: %s", log_path)
        return []

    errors = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _LOG_PATTERN.match(line.strip())
                if not m:
                    continue
                ts, source, level, message = m.groups()
                if not ts.startswith(date_str):
                    continue
                # Skip noise
                if any(n in message for n in _NOISE_PATTERNS):
                    continue
                errors.append({
                    "timestamp": ts,
                    "source": source,
                    "level": level,
                    "message": message[:500],
                })
    except Exception as e:
        log.error("Failed to parse log: %s", e)
    return errors


def _group_errors(errors: list[dict]) -> list[dict]:
    """Group similar errors and count occurrences."""
    # Group by (level, source, first 100 chars of message)
    groups: dict[tuple, dict] = {}
    for e in errors:
        key = (e["level"], e["source"], e["message"][:100])
        if key not in groups:
            groups[key] = {
                "level": e["level"],
                "source": e["source"],
                "message": e["message"],
                "count": 0,
            }
        groups[key]["count"] += 1
    # Sort by count descending, take top 30
    return sorted(groups.values(), key=lambda x: x["count"], reverse=True)[:30]


async def scan_errors(router, dispatcher, config: dict):
    """Main entry: scan yesterday's errors, analyze with Sonnet, write to bitable.

    Args:
        router: LLMRouter for Sonnet analysis
        dispatcher: Notifier dispatcher for alerts
        config: must contain 'log_path', 'bitable_app_token', 'bitable_table_id'
    """
    from agent.infra.models import LLMConfig

    log_path = config.get("log_path", "data/hub.log")
    app_token = config.get("bitable_app_token", "")
    table_id = config.get("bitable_table_id", "")

    if not app_token or not table_id:
        log.warning("Error scan skipped: bitable_app_token or bitable_table_id not configured")
        return

    # Scan yesterday's errors
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    raw_errors = _parse_log_errors(log_path, yesterday)
    if not raw_errors:
        log.info("No errors found for %s", yesterday)
        return

    grouped = _group_errors(raw_errors)
    log.info("Found %d raw errors, %d groups for %s", len(raw_errors), len(grouped), yesterday)

    # Sonnet analysis
    error_summary = json.dumps(grouped, ensure_ascii=False, indent=2)
    prompt = (
        f"以下是 claude-code-feishu 服务 {yesterday} 的错误日志摘要（已按类型分组）：\n\n"
        f"```json\n{error_summary}\n```\n\n"
        "对每个错误组，分析可能原因（一句话）。输出 JSON 数组，每项：\n"
        '{"level": "...", "error_type": "简短分类", "message": "原始消息摘要", '
        '"count": N, "cause": "可能原因", "source": "来源模块"}\n'
        "只输出 JSON，不要其他文字。"
    )

    llm_config = LLMConfig(provider="claude-cli", model="sonnet", timeout_seconds=60)
    result = await router.run(prompt=prompt, llm_config=llm_config)

    if result.is_error:
        log.warning("Sonnet analysis failed: %s", result.text[:200])
        # Fall back: write raw groups without analysis
        records = []
        for g in grouped:
            records.append({
                "日期": int(datetime.strptime(yesterday, "%Y-%m-%d").timestamp()) * 1000,
                "级别": g["level"],
                "错误类型": g["source"],
                "错误消息": g["message"][:500],
                "出现次数": g["count"],
                "可能原因": "(分析失败)",
                "状态": "待处理",
                "来源文件": g["source"],
            })
    else:
        # Parse Sonnet's JSON output
        try:
            # Strip markdown code fences if present
            text = result.text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text
                if text.endswith("```"):
                    text = text[:-3]
            analyzed = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            log.warning("Failed to parse Sonnet output: %s", e)
            analyzed = []

        records = []
        for item in analyzed:
            records.append({
                "日期": int(datetime.strptime(yesterday, "%Y-%m-%d").timestamp()) * 1000,
                "级别": item.get("level", "ERROR"),
                "错误类型": item.get("error_type", "unknown"),
                "错误消息": item.get("message", "")[:500],
                "出现次数": item.get("count", 1),
                "可能原因": item.get("cause", ""),
                "状态": "待处理",
                "来源文件": item.get("source", ""),
            })

    if not records:
        log.info("No records to write for %s", yesterday)
        return

    # Write to bitable
    import subprocess
    import sys
    script = os.path.join(
        os.path.dirname(__file__), "..", "..", ".claude", "skills",
        "feishu-bitable", "scripts", "bitable_ctl.py"
    )

    written = 0
    for rec in records:
        try:
            proc = subprocess.run(
                [sys.executable, script, "record", "add", app_token, table_id,
                 "--fields", json.dumps(rec, ensure_ascii=False)],
                capture_output=True, text=True, timeout=30,
                cwd=os.path.join(os.path.dirname(__file__), "..", ".."),
            )
            if proc.returncode == 0:
                written += 1
            else:
                log.warning("Bitable write failed: %s", proc.stderr[:200])
        except Exception as e:
            log.warning("Bitable write error: %s", e)

    log.info("Error scan complete: %d/%d records written to bitable", written, len(records))

    # Alert if high-severity errors found
    error_count = sum(1 for r in records if r["级别"] == "ERROR")
    if error_count > 0:
        try:
            await dispatcher.send_to_delivery_target(
                f"\u26a0\ufe0f **昨日错误扫描** ({yesterday})\n\n"
                f"发现 **{error_count}** 个 ERROR，共 {len(records)} 种错误类型。"
                f"\n[查看详情](https://feishu.cn/base/{app_token})"
            )
        except Exception:
            pass

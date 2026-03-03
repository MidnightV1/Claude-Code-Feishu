#!/usr/bin/env python3
"""Check Claude Max subscription quota via API response headers.

Sends a minimal API call (1 output token) with OAuth token + beta header,
reads anthropic-ratelimit-unified-* headers to get usage percentages.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def load_oauth_token() -> str:
    """Load OAuth access token from Claude credentials."""
    creds = json.loads(CREDENTIALS_PATH.read_text())
    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    if not token:
        raise RuntimeError("No OAuth access token found in credentials")

    expires_at = oauth.get("expiresAt", 0)
    if expires_at and expires_at / 1000 < time.time():
        raise RuntimeError(
            "OAuth token expired. Run `claude` to refresh."
        )
    return token


def fetch_quota() -> dict:
    """Make a minimal API call and extract ratelimit headers."""
    token = load_oauth_token()

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "User-Agent": "claude-code-quota-check/1.0",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        },
        timeout=15,
    )
    resp.raise_for_status()

    h = resp.headers
    result = {
        "status": h.get("anthropic-ratelimit-unified-status", "unknown"),
        "representative_window": h.get(
            "anthropic-ratelimit-unified-representative-claim", ""
        ),
    }

    # Parse per-window data
    for window in ["5h", "1d", "7d"]:
        util_key = f"anthropic-ratelimit-unified-{window}-utilization"
        reset_key = f"anthropic-ratelimit-unified-{window}-reset"
        status_key = f"anthropic-ratelimit-unified-{window}-status"
        if util_key in h:
            reset_ts = int(h.get(reset_key, 0))
            reset_dt = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
            result[window] = {
                "utilization": float(h[util_key]),
                "percent": round(float(h[util_key]) * 100, 1),
                "reset_at": reset_dt.isoformat(),
                "reset_in": _format_duration(reset_ts - int(time.time())),
                "status": h.get(status_key, "unknown"),
            }

    # Fallback info
    fb = h.get("anthropic-ratelimit-unified-fallback")
    if fb:
        result["fallback"] = fb
        fb_pct = h.get("anthropic-ratelimit-unified-fallback-percentage")
        if fb_pct:
            result["fallback_percentage"] = float(fb_pct)

    # Overage
    overage_status = h.get("anthropic-ratelimit-unified-overage-status")
    if overage_status:
        result["overage"] = {
            "status": overage_status,
            "reason": h.get(
                "anthropic-ratelimit-unified-overage-disabled-reason", ""
            ),
        }

    # Token cost of this check
    usage = resp.json().get("usage", {})
    result["check_cost"] = {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }

    return result


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration."""
    if seconds <= 0:
        return "now"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def format_text(data: dict) -> str:
    """Format quota data as plain text."""
    lines = [f"Status: {data['status']}"]

    for window in ["5h", "1d", "7d"]:
        if window not in data:
            continue
        w = data[window]
        is_repr = window == _window_key(data.get("representative_window", ""))
        marker = " *" if is_repr else ""
        lines.append(
            f"  {window}: {w['percent']}% used | "
            f"resets in {w['reset_in']}{marker}"
        )

    fb = data.get("fallback", "")
    if fb:
        lines.append(f"  Fallback: {fb}")

    return "\n".join(lines)


def format_feishu(data: dict) -> str:
    """Format quota data as Feishu-card markdown."""
    status_emoji = ":DONE:" if data["status"] == "allowed" else ":WARNING:"
    lines = [f"{status_emoji} **Claude Max Quota**\n"]

    for window, label in [("5h", "5 小时"), ("1d", "日"), ("7d", "7 天")]:
        if window not in data:
            continue
        w = data[window]
        is_repr = window == _window_key(data.get("representative_window", ""))
        bar = _progress_bar(w["percent"])
        marker = " (当前窗口)" if is_repr else ""
        color = "red" if w["percent"] >= 80 else "green" if w["percent"] < 50 else "grey"
        lines.append(
            f"**{label}**{marker}：<font color='{color}'>{w['percent']}%</font>\n"
            f"`{bar}` 重置于 {w['reset_in']}"
        )

    fb = data.get("fallback", "")
    if fb:
        fb_label = "可用" if fb == "available" else fb
        lines.append(f"\nFallback: {fb_label}")

    return "\n".join(lines)


def _progress_bar(percent: float, width: int = 20) -> str:
    """Generate a text progress bar."""
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def _window_key(representative: str) -> str:
    """Map representative claim name to window key."""
    mapping = {
        "five_hour": "5h",
        "daily": "1d",
        "one_day": "1d",
        "seven_day": "7d",
    }
    return mapping.get(representative, "")


def main():
    fmt = "text"
    if "--json" in sys.argv:
        fmt = "json"
    elif "--feishu" in sys.argv:
        fmt = "feishu"

    try:
        data = fetch_quota()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    elif fmt == "feishu":
        print(format_feishu(data))
    else:
        print(format_text(data))


if __name__ == "__main__":
    main()

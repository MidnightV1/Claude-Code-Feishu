#!/usr/bin/env python3
"""Scan GitHub issues and generate analysis summary.

Called by cron scheduler. Outputs analysis text to stdout for CC to relay.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

REPO = "MidnightV1/Claude-Code-Feishu"


def fetch_issues() -> list[dict]:
    """Fetch open issues from GitHub using gh CLI."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "open",
         "--json", "number,title,body,createdAt,author,labels,comments,updatedAt"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(f"Error fetching issues: {result.stderr}", file=sys.stderr)
        return []
    return json.loads(result.stdout)


def format_issue_summary(issues: list[dict]) -> str:
    """Format issues into a readable summary."""
    if not issues:
        return "GitHub 仓库当前没有 open issues。"

    now = datetime.now(timezone.utc)
    lines = [f"## GitHub Issue 扫描报告\n"]
    lines.append(f"仓库：`{REPO}`")
    lines.append(f"扫描时间：{now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Open Issues：{len(issues)} 个\n")

    for issue in sorted(issues, key=lambda x: x["number"], reverse=True):
        num = issue["number"]
        title = issue["title"]
        author = issue.get("author", {}).get("login", "unknown")
        created = issue.get("createdAt", "")[:10]
        updated = issue.get("updatedAt", "")[:10]
        labels = ", ".join(l.get("name", "") for l in issue.get("labels", [])) or "无标签"
        comment_count = len(issue.get("comments", []))
        body = (issue.get("body", "") or "")[:200]
        if len(issue.get("body", "") or "") > 200:
            body += "..."

        lines.append(f"### #{num} {title}")
        lines.append(f"- 作者：{author} | 创建：{created} | 更新：{updated}")
        lines.append(f"- 标签：{labels} | 评论：{comment_count} 条")
        if body:
            lines.append(f"- 摘要：{body}")
        lines.append("")

    # Check for new issues (created in last 24h)
    new_issues = []
    for issue in issues:
        created_str = issue.get("createdAt", "")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if (now - created_dt).total_seconds() < 86400:
                    new_issues.append(issue)
            except (ValueError, TypeError):
                pass

    if new_issues:
        lines.append(f"**新增 Issue（24h 内）：{len(new_issues)} 个**")
        for ni in new_issues:
            lines.append(f"- #{ni['number']} {ni['title']}")

    return "\n".join(lines)


if __name__ == "__main__":
    issues = fetch_issues()
    print(format_issue_summary(issues))

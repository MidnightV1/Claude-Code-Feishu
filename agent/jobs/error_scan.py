# -*- coding: utf-8 -*-
"""Daily error scanner — analyzes hub.log, auto-remediates, records to Feishu Bitable.

Registered as a cron handler; runs daily via scheduler.

Flow:
  1. Parse yesterday's errors from hub.log
  2. Sonnet analyzes causes + classifies fixability
  3. Write to Bitable (status="待处理")
  4. Auto-fix simple issues (Sonnet with tool access)
  5. Update Bitable status (已修复/待确认/仅监控)
  6. Notify user only for items needing confirmation
"""

import asyncio
import json
import logging
import os
import re
import sys
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
    "You've hit your limit",
    "you hit your limit",
    "hit your limit",
    "rate limit",
    "rate-limit",
    "overloaded",
]

# ── Analysis prompt ──

_ANALYSIS_PROMPT = """\
以下是 claude-code-feishu 服务 {date} 的错误日志摘要（已按类型分组）：

```json
{errors}
```

对每个错误组，分析：
1. 可能原因（一句话）
2. 可修复性分类：
   - "auto_fix": 简单修复，不涉及业务逻辑变更（如：添加噪声过滤、调整超时、补 try/except、修 import、改配置）
   - "confirm": 涉及业务逻辑或数据流改动，需要用户确认
   - "monitor": 瞬态问题（网络超时、第三方 API 波动），无需代码修复，仅监控
3. 修复方案（一句话描述具体做什么，auto_fix 和 confirm 必填，monitor 留空）

输出 JSON 数组，每项：
{{"level": "...", "error_type": "简短分类", "message": "原始消息摘要", \
"count": N, "cause": "可能原因", "source": "来源模块", \
"fixability": "auto_fix|confirm|monitor", "fix_plan": "修复方案"}}
只输出 JSON，不要其他文字。"""

# ── Auto-fix prompt ──

_FIX_PROMPT = """\
你是 claude-code-feishu 的运维 agent。请修复以下错误：

**错误信息**：{message}
**来源模块**：{source}
**出现次数**：{count}
**可能原因**：{cause}
**修复方案**：{fix_plan}

要求：
1. 读取相关源文件，理解上下文
2. 做最小化修改，只修复这个问题
3. 不要添加不必要的注释或重构
4. 不要 commit，只修改文件（Opus 审核后统一 commit）
5. 完成后输出一行 JSON：{{"status": "fixed", "summary": "修改了什么", "files": ["修改的文件路径"]}}
   如果判断不应该修改，输出：{{"status": "skipped", "reason": "为什么跳过"}}
只输出这一行 JSON，不要其他文字。"""

# ── Opus review prompt ──

_REVIEW_PROMPT = """\
你是 claude-code-feishu 的高级审核 agent。Sonnet 刚刚为以下错误做了自动修复，请审核修改是否合理。

**错误信息**：{message}
**修复方案**：{fix_plan}
**Sonnet 修改摘要**：{summary}
**修改的文件**：{files}

请检查 git diff（运行 `git diff` 查看未暂存变更），评估：
1. 修改是否最小化？是否引入了不必要的变更？
2. 是否可能破坏现有功能？
3. 修改是否正确解决了问题？

输出一行 JSON：
- 通过：{{"verdict": "approve", "note": "简短评语"}}
- 需要回滚+人工处理：{{"verdict": "reject", "reason": "为什么拒绝"}}
- 通过但需微调（你来调）：{{"verdict": "revise", "note": "调整了什么"}}

如果 verdict 是 approve 或 revise，请执行 `git add` 暂存变更的文件。
只输出 JSON，不要其他文字。"""


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
                message_lower = message.lower()
                if any(n.lower() in message_lower for n in _NOISE_PATTERNS):
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
    return sorted(groups.values(), key=lambda x: x["count"], reverse=True)[:30]


def _parse_json_response(text: str) -> list | dict | None:
    """Parse JSON from LLM response, stripping markdown fences."""
    text = text.strip()
    if not text:
        log.warning("LLM returned empty response, skipping parse")
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text[:-3]
    try:
        obj, _ = json.JSONDecoder().raw_decode(text.strip())
        return obj
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse JSON: %s", e)
        return None


async def _write_bitable_record(script: str, cwd: str, app_token: str,
                                 table_id: str, fields: dict) -> str | None:
    """Write a record to bitable, return record_id or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script, "record", "add", app_token, table_id,
            "--fields", json.dumps(fields, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode == 0:
            # Output format: "Created: recXXXX"
            out = stdout.decode().strip()
            if out.startswith("Created: "):
                return out.split("Created: ", 1)[1].strip()
            return "ok"
        else:
            log.warning("Bitable write failed: %s", stderr.decode()[:200])
    except asyncio.TimeoutError:
        log.warning("Bitable write timed out")
    except Exception as e:
        log.warning("Bitable write error: %s", e)
    return None


async def _update_bitable_record(script: str, cwd: str, app_token: str,
                                  table_id: str, record_id: str, fields: dict):
    """Update an existing bitable record."""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script, "record", "update", app_token, table_id,
            record_id, "--fields", json.dumps(fields, ensure_ascii=False),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as e:
        log.warning("Bitable update error: %s", e)


async def _auto_fix_error(router, error: dict) -> dict:
    """Attempt to auto-fix a single error: Sonnet fixes → Opus reviews.

    Flow: Sonnet edits files (no commit) → Opus reviews diff → approve/revise/reject.
    Returns {"status": "fixed"|"skipped"|"failed", "summary": "...", "files": [...]}.
    """
    from agent.infra.models import LLMConfig

    # Step 1: Sonnet generates the fix (edits files, no commit)
    prompt = _FIX_PROMPT.format(
        message=error.get("message", ""),
        source=error.get("source", ""),
        count=error.get("count", 1),
        cause=error.get("cause", ""),
        fix_plan=error.get("fix_plan", ""),
    )

    sonnet_cfg = LLMConfig(provider="claude-cli", model="sonnet")
    result = await router.run(prompt=prompt, llm_config=sonnet_cfg)

    if result.is_error:
        log.warning("Sonnet fix failed for %s: %s", error.get("error_type"), result.text[:200])
        return {"status": "failed", "summary": result.text[:200]}

    sonnet_result = _parse_json_response(result.text)
    if not isinstance(sonnet_result, dict):
        return {"status": "failed", "summary": "Unexpected Sonnet response"}

    # If Sonnet skipped, no review needed
    if sonnet_result.get("status") == "skipped":
        return sonnet_result

    # Step 2: Opus reviews the diff
    files_str = ", ".join(sonnet_result.get("files", []))
    review_prompt = _REVIEW_PROMPT.format(
        message=error.get("message", ""),
        fix_plan=error.get("fix_plan", ""),
        summary=sonnet_result.get("summary", ""),
        files=files_str,
    )

    opus_cfg = LLMConfig(provider="claude-cli", model="opus")
    review = await router.run(prompt=review_prompt, llm_config=opus_cfg)

    if review.is_error:
        log.warning("Opus review failed, reverting: %s", review.text[:200])
        # Revert uncommitted changes on failure
        await _git_restore(sonnet_result.get("files", []))
        return {"status": "failed", "summary": "Opus review failed, changes reverted"}

    verdict = _parse_json_response(review.text)
    if not isinstance(verdict, dict):
        await _git_restore(sonnet_result.get("files", []))
        return {"status": "failed", "summary": "Unexpected Opus response, changes reverted"}

    decision = verdict.get("verdict", "reject")

    if decision == "reject":
        log.info("Opus rejected fix: %s", verdict.get("reason", ""))
        await _git_restore(sonnet_result.get("files", []))
        return {"status": "failed", "summary": f"Opus rejected: {verdict.get('reason', '')}"}

    # approve or revise — Opus already staged files, commit
    note = verdict.get("note", "")
    log.info("Opus %s: %s", decision, note)
    return {
        "status": "fixed",
        "summary": sonnet_result.get("summary", "") + (f" (Opus: {note})" if note else ""),
        "files": sonnet_result.get("files", []),
    }


async def _git_restore(files: list[str]):
    """Revert uncommitted changes to specific files."""
    if not files:
        return
    try:
        cwd = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        proc = await asyncio.create_subprocess_exec(
            "git", "checkout", "--", *files,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=10)
    except Exception as e:
        log.warning("git restore failed: %s", e)


async def scan_errors(router, dispatcher, config: dict):
    """Main entry: scan → analyze → fix → record → notify.

    Args:
        router: LLMRouter for Sonnet analysis and auto-fix
        dispatcher: Notifier dispatcher for alerts
        config: must contain 'log_path', 'bitable_app_token', 'bitable_table_id'
    """
    from agent.infra.models import LLMConfig

    log_path = config.get("log_path", "data/hub.log")
    app_token = config.get("bitable_app_token", "")
    table_id = config.get("bitable_table_id", "")

    if not app_token or not table_id:
        log.warning("Error scan skipped: bitable not configured")
        return

    # ── Phase 1: Parse logs ──
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    raw_errors = _parse_log_errors(log_path, yesterday)
    if not raw_errors:
        log.info("No errors found for %s", yesterday)
        return

    grouped = _group_errors(raw_errors)
    log.info("Found %d raw errors, %d groups for %s", len(raw_errors), len(grouped), yesterday)

    # ── Phase 2: Sonnet analysis with fixability classification ──
    error_summary = json.dumps(grouped, ensure_ascii=False, indent=2)
    prompt = _ANALYSIS_PROMPT.format(date=yesterday, errors=error_summary)
    llm_config = LLMConfig(provider="claude-cli", model="sonnet", timeout_seconds=300)
    result = await router.run(prompt=prompt, llm_config=llm_config)

    if result.is_error:
        log.warning("Analysis failed: %s", result.text[:200])
        analyzed = []
        for g in grouped:
            analyzed.append({
                "level": g["level"], "error_type": g["source"],
                "message": g["message"][:500], "count": g["count"],
                "cause": "(分析失败)", "source": g["source"],
                "fixability": "monitor", "fix_plan": "",
            })
    else:
        parsed = _parse_json_response(result.text)
        analyzed = parsed if isinstance(parsed, list) else []

    if not analyzed:
        log.info("No analyzed records for %s", yesterday)
        return

    # ── Phase 3: Write to Bitable ──
    script = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", ".claude", "skills",
        "feishu-bitable", "scripts", "bitable_ctl.py"
    ))
    cwd = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    date_ms = int(datetime.strptime(yesterday, "%Y-%m-%d").timestamp()) * 1000

    records = []  # (bitable_fields, record_id, analyzed_item)
    for item in analyzed:
        fixability = item.get("fixability", "monitor")
        status = {"auto_fix": "修复中", "confirm": "待确认", "monitor": "仅监控"}.get(
            fixability, "待处理"
        )
        fields = {
            "日期": date_ms,
            "级别": item.get("level", "ERROR"),
            "错误类型": item.get("error_type", "unknown"),
            "错误消息": item.get("message", "")[:500],
            "出现次数": item.get("count", 1),
            "可能原因": item.get("cause", ""),
            "状态": status,
            "来源文件": item.get("source", ""),
        }
        record_id = await _write_bitable_record(script, cwd, app_token, table_id, fields)
        records.append((fields, record_id, item))

    written = sum(1 for _, rid, _ in records if rid)
    log.info("Phase 3 done: %d/%d records written", written, len(records))

    # ── Phase 4: Auto-remediation ──
    auto_fix_items = [(f, rid, item) for f, rid, item in records
                       if item.get("fixability") == "auto_fix"]
    fixed_results = []
    for fields, record_id, item in auto_fix_items:
        log.info("Auto-fixing: %s", item.get("error_type", "unknown"))
        fix_result = await _auto_fix_error(router, item)
        fix_status = fix_result.get("status", "failed")

        # Update bitable status
        new_status = {"fixed": "已修复", "skipped": "已忽略"}.get(fix_status, "待处理")
        if record_id and record_id != "ok":
            await _update_bitable_record(script, cwd, app_token, table_id, record_id, {
                "状态": new_status,
            })

        fixed_results.append({
            "error_type": item.get("error_type", "unknown"),
            "fix_status": fix_status,
            "summary": fix_result.get("summary", ""),
            "files": fix_result.get("files", []),
            "reason": fix_result.get("reason", ""),
        })
        log.info("  → %s: %s", fix_status, fix_result.get("summary", "")[:100])

    # Commit all approved fixes in one commit
    if any(r["fix_status"] == "fixed" for r in fixed_results):
        all_fixed_files = []
        for r in fixed_results:
            if r["fix_status"] == "fixed":
                all_fixed_files.extend(r.get("files", []))
        if all_fixed_files:
            # 72h reentry gate: skip auto-commit if files were recently L1-auto-modified
            try:
                from agent.infra.autonomy import check_file_reentry
                reentry = await check_file_reentry(all_fixed_files, hours=72, cwd=cwd)
                if reentry:
                    log.warning("72h reentry gate: %s recently auto-modified, skipping commit → 待确认",
                                [h["file"] for h in reentry[:5]])
                    await _git_restore(all_fixed_files)
                    for fields, record_id, item in records:
                        if item.get("fixability") == "auto_fix" and record_id and record_id != "ok":
                            await _update_bitable_record(
                                script, cwd, app_token, table_id, record_id,
                                {"状态": "待确认（72h重入）"})
                    all_fixed_files = []  # prevent commit below
            except Exception as e:
                log.debug("Reentry check failed, proceeding: %s", e)

            if all_fixed_files:
                try:
                    # Opus review already staged files; just commit
                    summaries = [r["summary"] for r in fixed_results if r["fix_status"] == "fixed"]
                    commit_msg = f"fix(auto): {yesterday} error scan — " + "; ".join(summaries)
                    if len(commit_msg) > 200:
                        commit_msg = commit_msg[:197] + "..."
                    proc = await asyncio.create_subprocess_exec(
                        "git", "commit", "-m", commit_msg,
                        cwd=cwd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
                    if proc.returncode == 0:
                        log.info("Auto-fix committed: %s", out.decode().strip()[:100])
                        # Log to autonomy audit trail
                        try:
                            from agent.infra.autonomy import AutonomousAction, log_action
                            sha_proc = await asyncio.create_subprocess_exec(
                                "git", "rev-parse", "--short", "HEAD",
                                cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            sha_out, _ = await asyncio.wait_for(sha_proc.communicate(), timeout=5)
                            commit_sha = sha_out.decode().strip() if sha_proc.returncode == 0 else ""
                            await log_action(AutonomousAction(
                                level=1,
                                category="error_scan:auto_fix",
                                summary=commit_msg[:120],
                                detail=f"Files: {', '.join(all_fixed_files[:10])}",
                                commit_sha=commit_sha,
                                rollback_cmd=f"git revert {commit_sha}" if commit_sha else "",
                                source="error_scan",
                            ))
                        except Exception as audit_err:
                            log.warning("Autonomy audit log failed: %s", audit_err)
                    else:
                        log.warning("Auto-fix commit failed: %s", err.decode()[:200])
                except Exception as e:
                    log.warning("Auto-fix commit error: %s", e)

    # Update monitor items status
    for fields, record_id, item in records:
        if item.get("fixability") == "monitor" and record_id and record_id != "ok":
            await _update_bitable_record(script, cwd, app_token, table_id, record_id, {
                "状态": "仅监控",
            })

    # ── Phase 5: Consolidated notification ──
    confirm_items = [item for _, _, item in records if item.get("fixability") == "confirm"]
    auto_fixed = [r for r in fixed_results if r["fix_status"] == "fixed"]
    auto_skipped = [r for r in fixed_results if r["fix_status"] in ("skipped", "failed")]
    monitor_items = [item for _, _, item in records if item.get("fixability") == "monitor"]

    # Only notify if there's something to report
    if not confirm_items and not auto_fixed:
        if monitor_items:
            log.info("All %d errors are transient (monitor only), no notification sent", len(monitor_items))
        return

    has_confirm = bool(confirm_items)
    scan_color = "orange" if has_confirm else "green"
    parts = [f"{{{{card:header=错误扫描报告,color={scan_color}}}}}", f"**{yesterday}**"]

    # Auto-fixed summary (FYI, no action needed)
    if auto_fixed:
        parts.append(f"\n**已自动修复** ({len(auto_fixed)} 项)")
        for r in auto_fixed:
            files_str = ", ".join(f"`{f}`" for f in r.get("files", []))
            parts.append(f"- {r['error_type']}: {r['summary']}" +
                        (f" [{files_str}]" if files_str else ""))

    # Skipped/failed
    if auto_skipped:
        parts.append(f"\n**跳过/失败** ({len(auto_skipped)} 项)")
        for r in auto_skipped:
            reason = r.get("reason") or r.get("summary", "")
            parts.append(f"- {r['error_type']}: {reason}")

    # Items needing confirmation (action required)
    if confirm_items:
        parts.append(f"\n**需要确认** ({len(confirm_items)} 项)")
        for item in confirm_items:
            parts.append(
                f"- **{item.get('error_type')}** ({item.get('count', 0)}次): "
                f"{item.get('cause', '')}\n"
                f"  方案: {item.get('fix_plan', '无')}"
            )

    # Monitor items (brief mention)
    if monitor_items:
        parts.append(f"\n*{len(monitor_items)} 项瞬态错误仅记录监控*")

    parts.append(f"\n[查看详情](https://feishu.cn/base/{app_token})")

    try:
        await dispatcher.send_to_delivery_target("\n".join(parts))
    except Exception:
        pass

    log.info("Error scan complete: %d analyzed, %d auto-fixed, %d confirm, %d monitor",
             len(records), len(auto_fixed), len(confirm_items), len(monitor_items))

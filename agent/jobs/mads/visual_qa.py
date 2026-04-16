# -*- coding: utf-8 -*-
"""MADS Visual QA gate — post-QA visual verification for client-side tickets.

Runs visual_qa_ctl.py to capture screenshots, extract accessibility trees,
and verify UI against spec. Triggers only when the ticket/contract has
visual_qa_required=true.

Integration point: after code QA PASS, before merge to dev.
Threshold: total score ≥ 80 → PASS, < 80 → iterate (max 3 rounds).
"""

import asyncio
import json
import logging
import os
import sys

from agent.jobs.mads.helpers import (
    log,
    run_agent,
    write_artifact,
    read_artifact,
    parse_json_response,
    PROJECT_ROOT,
)

VISUAL_QA_SCRIPT = os.path.join(
    PROJECT_ROOT, ".claude", "skills", "visual-qa", "scripts", "visual_qa_ctl.py"
)

VISUAL_QA_THRESHOLD = 80
VISUAL_QA_MAX_ITERATIONS = 3


def parse_visual_qa_spec(ticket: dict) -> dict | None:
    """Extract visual QA spec from ticket/contract fields.

    Returns dict with keys: url, spec, viewport, design_ref, scenarios.
    Returns None if visual_qa is not required.
    """
    # Check explicit flag
    vqa = ticket.get("visual_qa_required")
    if not vqa:
        return None

    # Parse visual_qa_spec if present (JSON string or dict)
    spec_raw = ticket.get("visual_qa_spec", "")
    if isinstance(spec_raw, str) and spec_raw.strip():
        try:
            spec = json.loads(spec_raw)
        except json.JSONDecodeError:
            spec = {"url": "", "spec": spec_raw}
    elif isinstance(spec_raw, dict):
        spec = spec_raw
    else:
        spec = {}

    # At minimum we need a URL — fallback to ticket-level field
    url = spec.get("url") or ticket.get("visual_qa_url", "")
    if not url:
        return None

    return {
        "url": url,
        "spec": spec.get("spec", ticket.get("visual_qa_assertions", "")),
        "viewport": spec.get("viewport", "1440x900"),
        "design_ref": spec.get("design_ref", ""),
        "scenarios": spec.get("scenarios", []),
    }


async def run_visual_qa(
    ticket_id: str,
    url: str,
    spec: str,
    viewport: str = "1440x900",
    design_ref: str = "",
) -> dict:
    """Run visual_qa_ctl.py verify and return parsed score dict.

    Returns: {"total": int, "scores": dict, "issues": list, "summary": str}
    or {"error": str} on failure.
    """
    cmd = [
        sys.executable, VISUAL_QA_SCRIPT, "verify",
        url, "--spec", spec, "--viewport", viewport,
    ]
    if design_ref:
        cmd.extend(["--design-ref", design_ref])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=PROJECT_ROOT,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        return {"error": "Visual QA timed out (120s)"}
    except Exception as e:
        return {"error": f"Visual QA exec failed: {e}"}

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")[:500]
        return {"error": f"Visual QA exit code {proc.returncode}: {err_text}"}

    output = stdout.decode(errors="replace").strip()
    result = parse_json_response(output)
    if not result or not isinstance(result, dict):
        return {"error": f"Cannot parse Visual QA output: {output[:300]}"}

    # Normalize nested structure: visual_qa_ctl returns {score: {total, scores, issues, summary}, ...}
    # Flatten to {total, scores, issues, summary, ...} for gate consumption
    score_block = result.get("score", {})
    if isinstance(score_block, dict) and "total" in score_block:
        return {
            "total": score_block.get("total", 0),
            "scores": score_block.get("scores", {}),
            "issues": score_block.get("issues", []),
            "summary": score_block.get("summary", ""),
            "screenshot": result.get("screenshot", ""),
            "a11y_node_count": result.get("a11y_node_count", 0),
        }

    # Already flat or unknown structure — pass through
    return result


async def visual_qa_gate(
    router,
    ticket_id: str,
    ticket: dict,
    vqa_spec: dict,
) -> tuple[str, str]:
    """Run visual QA gate with auto-iteration.

    Args:
        router: LLMRouter (for fix iteration agent calls)
        ticket_id: Short ticket ID
        ticket: Full ticket dict
        vqa_spec: Parsed visual QA spec from parse_visual_qa_spec()

    Returns:
        (verdict, report) where verdict is "PASS" or "REJECT"
    """
    url = vqa_spec["url"]
    spec = vqa_spec["spec"]
    viewport = vqa_spec.get("viewport", "1440x900")
    design_ref = vqa_spec.get("design_ref", "")

    for iteration in range(1, VISUAL_QA_MAX_ITERATIONS + 1):
        log.info("[MADS] Visual QA iteration %d/%d for %s",
                 iteration, VISUAL_QA_MAX_ITERATIONS, ticket_id)

        result = await run_visual_qa(
            ticket_id, url, spec, viewport, design_ref)

        # Write iteration artifact
        write_artifact(ticket_id, f"visual_qa_iter{iteration}.json",
                       json.dumps(result, ensure_ascii=False, indent=2))

        if "error" in result:
            log.warning("[MADS] Visual QA error for %s: %s",
                        ticket_id, result["error"])
            report = f"Visual QA 执行失败 (iter {iteration}): {result['error']}"
            # Don't iterate on infrastructure errors
            return "REJECT", report

        total = result.get("total", 0)
        summary = result.get("summary", "")
        issues = result.get("issues", [])

        report_lines = [
            f"## Visual QA 报告 (第 {iteration} 轮)",
            f"**总分: {total}/100** {'✓ PASS' if total >= VISUAL_QA_THRESHOLD else '✗ FAIL'}",
            "",
            "### 维度得分",
        ]
        for dim, score in result.get("scores", {}).items():
            report_lines.append(f"- {dim}: {score}")
        report_lines.append("")

        if issues:
            report_lines.append("### 问题清单")
            for issue in issues:
                sev = issue.get("severity", "?")
                desc = issue.get("description", "")
                sug = issue.get("suggestion", "")
                report_lines.append(f"- [{sev}] {desc}")
                if sug:
                    report_lines.append(f"  → {sug}")
            report_lines.append("")

        report_lines.append(f"### 总结\n{summary}")
        report = "\n".join(report_lines)

        write_artifact(ticket_id, f"visual_qa_report_iter{iteration}.md", report)

        if total >= VISUAL_QA_THRESHOLD:
            log.info("[MADS] Visual QA PASS for %s: %d/100", ticket_id, total)
            return "PASS", report

        # Score below threshold — attempt auto-fix if iterations remain
        if iteration < VISUAL_QA_MAX_ITERATIONS:
            log.info("[MADS] Visual QA score %d < %d, requesting fix iteration",
                     total, VISUAL_QA_THRESHOLD)

            # Build feedback for the fix agent
            high_issues = [i for i in issues if i.get("severity") == "high"]
            medium_issues = [i for i in issues if i.get("severity") == "medium"]
            fix_issues = high_issues + medium_issues

            if not fix_issues:
                # No actionable issues, don't waste an iteration
                log.info("[MADS] No high/medium issues to fix, stopping iteration")
                return "REJECT", report

            feedback = "Visual QA 发现以下问题需要修复:\n\n"
            for issue in fix_issues:
                feedback += f"- [{issue.get('severity')}] {issue.get('description', '')}\n"
                if issue.get("suggestion"):
                    feedback += f"  建议: {issue['suggestion']}\n"

            # Call fix agent to address visual issues
            fix_result = await run_agent(
                router, role="visual_fix", model="sonnet",
                prompt=f"请根据 Visual QA 反馈修复 UI 问题:\n\n{feedback}",
                system_prompt=(
                    "你是 MADS Visual Fix agent。根据 Visual QA 的反馈修复 UI/样式问题。"
                    "只修复反馈中提到的问题，不做其他改动。修复后 git add && git commit。"
                ),
            )

            if fix_result.startswith("[ERROR]"):
                log.warning("[MADS] Visual fix agent failed: %s", fix_result[:200])
                return "REJECT", report + f"\n\n自动修复失败: {fix_result[:200]}"

            write_artifact(ticket_id, f"visual_fix_iter{iteration}.md", fix_result)
            # Continue to next iteration for re-verification

    # Exhausted all iterations
    log.info("[MADS] Visual QA exhausted %d iterations for %s, final score: %d",
             VISUAL_QA_MAX_ITERATIONS, ticket_id, total)
    return "REJECT", report

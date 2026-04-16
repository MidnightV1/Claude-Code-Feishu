# -*- coding: utf-8 -*-
"""MADS Pipeline orchestrator — routes tickets through the appropriate pipeline.

State machine:
  Atomic:    open → diagnosing → diagnosed → contracting → contracted → fixing → reviewing → closed
  Composite: open → designing → awaiting_review →
               review_approved → decomposing → sub_tickets_created → closed
               review_feedback → designing (loop)

Composite tickets spawn atomic sub-tickets that go through the standard
MAQS Fix → QA path (or the new Contract → Fix → QA path).
"""

import asyncio
import json
import logging
import time

MAX_CONCURRENT_SUBTASKS = 5

from agent.jobs.mads.helpers import (
    bitable_get_status,
    bitable_query,
    bitable_update,
    git as _git,
    notify,
    parse_qa_verdict,
    send_status_card,
    update_status_card,
    write_artifact,
)
from agent.jobs.mads.contract import negotiate_contract
from agent.jobs.mads.design import generate_design, create_design_doc
from agent.jobs.mads.decompose import run_decompose_stage
from agent.jobs.mads.review import (
    check_review_status,
    create_review_task,
    send_review_reminder,
)

log = logging.getLogger("hub.mads")


async def _complete_review_task(ticket: dict) -> None:
    """Complete the Feishu review task when design is approved or feedback received."""
    task_guid = ticket.get("review_task_guid", "")
    if not task_guid:
        return
    try:
        rc, _, stderr = await task_ctl("complete", task_guid)
        if rc == 0:
            log.info("[MADS] Review task completed: %s", task_guid[:8])
        else:
            log.warning("[MADS] Review task complete failed: %s", stderr[:200])
    except Exception as e:
        log.warning("[MADS] Review task complete error: %s", e)


# ══════════════════════════════════════════════════════════════════════
#  Composite ticket processing (Design → Review → Decompose)
# ══════════════════════════════════════════════════════════════════════

async def process_composite_ticket(
    router, dispatcher, app_token: str, table_id: str,
    record_id: str, ticket: dict,
):
    """Process a composite ticket through the MADS Design pipeline.

    This is called for tickets with complexity=composite. The pipeline is
    asynchronous — it may pause at awaiting_review and resume on the next
    cron tick when comments are detected or 48h timeout fires.
    """
    ticket_id = ticket.get("title", record_id[:8])
    status = ticket.get("status", "open")

    # ── Route based on current status ──
    if status == "open":
        await _start_design(router, dispatcher, app_token, table_id,
                            record_id, ticket)
    elif status == "awaiting_review":
        await _check_review(router, dispatcher, app_token, table_id,
                            record_id, ticket)
    elif status == "review_approved":
        await _run_decompose(router, dispatcher, app_token, table_id,
                             record_id, ticket)
    elif status == "decomposing":
        # Re-entry after crash during decompose — retry
        log.info("Composite ticket %s re-entering decompose (crash recovery)", ticket_id)
        await _run_decompose(router, dispatcher, app_token, table_id,
                             record_id, ticket)
    elif status == "review_feedback":
        await _revise_design(router, dispatcher, app_token, table_id,
                             record_id, ticket)
    else:
        log.debug("Composite ticket %s in state %s — no action", ticket_id, status)


async def _start_design(router, dispatcher, app_token, table_id,
                         record_id, ticket):
    """Phase 1: Opus generates design doc."""
    ticket_id = ticket.get("title", record_id[:8])
    log.info("[MADS] Starting design for: %s", ticket_id)

    await bitable_update(app_token, table_id, record_id,
                          {"status": "designing"})

    ticket_info = (
        f"标题: {ticket.get('title', 'N/A')}\n"
        f"现象/需求: {ticket.get('phenomenon', 'N/A')}\n"
        f"类型: {ticket.get('type', 'N/A')}\n"
        f"严重度: {ticket.get('severity', 'N/A')}\n"
    )

    design_content = await generate_design(router, ticket_info)

    if design_content.startswith("[ERROR]"):
        log.error("[MADS] Design failed for %s: %s", ticket_id, design_content[:200])
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "diagnosis": design_content,
            "needs_human": True,
        })
        await notify(dispatcher, "orange",
                     f"MADS 设计失败: {ticket_id}\n需要人工介入。")
        return

    # Write design artifact
    write_artifact(record_id[:8], "design.md", design_content)

    # Create Feishu doc
    doc_id, doc_url = await create_design_doc(design_content, ticket_id)
    if not doc_id:
        log.error("[MADS] Failed to create design doc for %s", ticket_id)
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "diagnosis": design_content,
        })
        return

    # Create review task (non-blocking — failure doesn't stop the flow)
    task_guid = await create_review_task(doc_url, ticket_id)

    # Update ticket to awaiting_review
    await bitable_update(app_token, table_id, record_id, {
        "status": "awaiting_review",
        "design_doc_id": doc_id,
        "design_doc_url": doc_url,
        "review_task_guid": task_guid,
        "review_started_at": str(int(time.time())),
        "diagnosis": design_content,
    })

    # Notify user
    await notify(dispatcher, "blue",
                 f"**[MADS] 设计文档已就绪**\n\n"
                 f"「{ticket_id}」的设计方案已生成。\n"
                 f"请在文档中评论，不评论视为同意。\n\n"
                 f"[查看设计文档]({doc_url})")

    log.info("[MADS] Design doc created for %s: %s", ticket_id, doc_url)


async def _check_review(router, dispatcher, app_token, table_id,
                         record_id, ticket):
    """Phase 2: Check design review status (called each cron tick)."""
    ticket_id = ticket.get("title", record_id[:8])
    doc_id = ticket.get("design_doc_id", "")

    if not doc_id:
        log.warning("[MADS] No design_doc_id for %s, stalling", ticket_id)
        await bitable_update(app_token, table_id, record_id,
                              {"status": "stalled"})
        return

    review_started_at = float(ticket.get("review_started_at", 0))
    status, feedback = await check_review_status(doc_id, review_started_at)

    if status == "approved":
        log.info("[MADS] Design approved (silent) for %s", ticket_id)
        await _complete_review_task(ticket)
        await bitable_update(app_token, table_id, record_id,
                              {"status": "review_approved"})
        # Proceed to decompose immediately
        ticket["status"] = "review_approved"
        await _run_decompose(router, dispatcher, app_token, table_id,
                             record_id, ticket)

    elif status == "feedback":
        log.info("[MADS] Design feedback received for %s", ticket_id)
        await _complete_review_task(ticket)
        await bitable_update(app_token, table_id, record_id, {
            "status": "review_feedback",
            "review_feedback": feedback,
        })
        # Proceed to revise immediately
        ticket["status"] = "review_feedback"
        ticket["review_feedback"] = feedback
        await _revise_design(router, dispatcher, app_token, table_id,
                             record_id, ticket)

    elif status == "pending":
        # Check if 48h reminder should be sent
        elapsed = time.time() - review_started_at
        reminder_sent = ticket.get("review_reminder_sent", "")
        if elapsed > 48 * 3600 and not reminder_sent:
            doc_url = ticket.get("design_doc_url", "")
            await send_review_reminder(dispatcher, ticket_id, doc_url)
            await bitable_update(app_token, table_id, record_id,
                                  {"review_reminder_sent": "true"})
        # Otherwise just wait


async def _revise_design(router, dispatcher, app_token, table_id,
                          record_id, ticket):
    """Handle design revision based on user feedback."""
    ticket_id = ticket.get("title", record_id[:8])
    feedback = ticket.get("review_feedback", "")
    original_design = ticket.get("diagnosis", "")

    log.info("[MADS] Revising design for %s", ticket_id)
    await bitable_update(app_token, table_id, record_id,
                          {"status": "designing"})

    revision_info = (
        f"## 原始设计\n\n{original_design}\n\n"
        f"## 用户反馈\n\n{feedback}\n\n"
        f"请根据用户反馈修改设计方案。保留被认可的部分，只修改反馈指出的问题。"
    )

    revised_design = await generate_design(router, revision_info)

    if revised_design.startswith("[ERROR]"):
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": True,
        })
        return

    # Update design doc
    doc_id = ticket.get("design_doc_id", "")
    if doc_id:
        from agent.jobs.mads.helpers import doc_ctl
        rc, _, stderr = await doc_ctl("update", doc_id, revised_design, timeout=120)
        if rc != 0:
            log.warning("[MADS] doc_ctl update failed for %s (rc=%d): %s",
                        ticket_id, rc, stderr[:200])

    write_artifact(record_id[:8], "design_revised.md", revised_design)

    # Reset review cycle
    await bitable_update(app_token, table_id, record_id, {
        "status": "awaiting_review",
        "diagnosis": revised_design,
        "review_started_at": str(int(time.time())),
        "review_reminder_sent": "",
        "review_feedback": "",
    })

    doc_url = ticket.get("design_doc_url", "")
    await notify(dispatcher, "blue",
                 f"**[MADS] 设计文档已修改**\n\n"
                 f"「{ticket_id}」根据反馈修改了设计方案。\n"
                 f"[查看更新后的设计文档]({doc_url})")


async def _run_decompose(router, dispatcher, app_token, table_id,
                          record_id, ticket):
    """Phase 3: Decompose approved design into atomic sub-tickets."""
    ticket_id = ticket.get("title", record_id[:8])
    design_content = ticket.get("diagnosis", "")

    log.info("[MADS] Decomposing design for %s", ticket_id)
    await bitable_update(app_token, table_id, record_id,
                          {"status": "decomposing"})

    sub_ticket_ids = await run_decompose_stage(
        router=router,
        app_token=app_token,
        table_id=table_id,
        ticket_id=record_id[:8],
        parent_record_id=record_id,
        parent_type=ticket.get("type", "feature"),
        parent_severity=ticket.get("severity", "P2"),
        design_content=design_content,
    )

    if not sub_ticket_ids:
        log.error("[MADS] Decompose produced no sub-tickets for %s", ticket_id)
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": True,
        })
        await notify(dispatcher, "orange",
                     f"MADS 拆解失败: {ticket_id}\n"
                     f"Decompose 未产出子工单，需要人工介入。")
        return

    await bitable_update(app_token, table_id, record_id, {
        "status": "sub_tickets_created",
        "sub_ticket_ids": json.dumps(sub_ticket_ids),
    })

    await notify(dispatcher, "green",
                 f"**[MADS] 原子拆解完成**\n\n"
                 f"「{ticket_id}」已拆分为 {len(sub_ticket_ids)} 个原子工单。\n"
                 f"子工单将自动进入 Contract → Fix → QA 流水线。")

    log.info("[MADS] Decompose done for %s: %d sub-tickets created",
             ticket_id, len(sub_ticket_ids))


# ══════════════════════════════════════════════════════════════════════
#  Contract-enhanced atomic ticket processing
# ══════════════════════════════════════════════════════════════════════

async def process_atomic_with_contract(
    router, dispatcher, app_token: str, table_id: str,
    record_id: str, ticket: dict, notify_open_id: str = "",
):
    """Process an atomic ticket with Contract Negotiation before Fix.

    Flow: Contract → Fix → QA (enhanced version of MAQS process_ticket).
    Falls back to standard MAQS flow if contract negotiation fails.
    """
    from agent.jobs.maqs import process_ticket, fix_ticket, qa_review, diagnose_ticket

    ticket_id = ticket.get("title", record_id[:8])
    status = ticket.get("status", "open")

    # ── State guard: recover interrupted contracting ──
    if status == "contracting":
        log.info("[MADS] %s was contracting (likely interrupted) — resetting to diagnosed",
                 ticket_id)
        await bitable_update(app_token, table_id, record_id, {"status": "diagnosed"})
        ticket["status"] = "diagnosed"
        status = "diagnosed"
    if status == "contracted":
        log.info("[MADS] %s already contracted — proceeding to fix", ticket_id)
        await process_ticket(router, dispatcher, app_token, table_id,
                             record_id, ticket, notify_open_id,
                             skip_diagnosis=True)
        return

    # ── Status card tracking ──
    _severity = ticket.get("severity", "P1")
    _ticket_type = ticket.get("type", ticket.get("ticket_type", "bug"))
    _card_mid: str | None = None

    async def _update_card(phase: str, phases_status: dict) -> None:
        nonlocal _card_mid
        if _card_mid:
            await update_status_card(dispatcher, _card_mid, record_id[:8],
                                     ticket_id, phase, _severity, _ticket_type, phases_status)
        else:
            _card_mid = await send_status_card(dispatcher, record_id[:8], ticket_id,
                                               phase, _severity, _ticket_type, phases_status)
            if _card_mid:
                await bitable_update(app_token, table_id, record_id,
                                     {"status_card_mid": _card_mid})

    # QUEUED card — visible immediately on pipeline entry
    await _update_card("queued", {
        "diagnosing": "pending", "contracting": "pending",
        "fixing": "pending", "hardgate": "pending", "reviewing": "pending", "merging": "pending",
    })

    # ── Diagnosis (if needed) ──
    if status == "open":
        await bitable_update(app_token, table_id, record_id,
                              {"status": "diagnosing"})
        await _update_card("diagnosing", {
            "diagnosing": "running", "contracting": "pending",
            "fixing": "pending", "hardgate": "pending", "reviewing": "pending", "merging": "pending",
        })

        golden_data = ticket.get("golden_data", "") or ""
        ticket_info = (
            f"标题: {ticket.get('title', 'N/A')}\n"
            f"现象: {ticket.get('phenomenon', 'N/A')}\n"
            f"来源: {ticket.get('source', 'N/A')}\n"
            f"严重度: {ticket.get('severity', 'N/A')}\n"
        )
        if golden_data:
            ticket_info += f"金标准验证数据: {golden_data}\n"
        if ticket.get("qa_report") and ticket.get("qa_verdict") == "REJECT":
            ticket_info += f"\n上次 QA 拒绝原因:\n{ticket['qa_report']}\n"

        diagnosis = await diagnose_ticket(router, ticket_info)

        if diagnosis.startswith("[ERROR]"):
            await bitable_update(app_token, table_id, record_id, {
                "status": "stalled",
                "diagnosis": diagnosis,
                "needs_human": True,
            })
            await notify(dispatcher, "orange",
                         f"MADS 诊断失败: {ticket_id}\n需要人工介入。",
                         header="MAQS")
            return

        write_artifact(record_id[:8], "diagnosis.md", diagnosis)
        await bitable_update(app_token, table_id, record_id, {
            "status": "diagnosed",
            "diagnosis": diagnosis,
        })
        await _update_card("diagnosing", {
            "diagnosing": "done", "contracting": "pending",
            "fixing": "pending", "hardgate": "pending", "reviewing": "pending", "merging": "pending",
        })
        ticket["diagnosis"] = diagnosis
        ticket["status"] = "diagnosed"

    # ── Contract Negotiation (complexity-routed) ──
    diagnosis = ticket.get("diagnosis", "")
    if not diagnosis:
        log.warning("[MADS] No diagnosis for contract negotiation: %s", ticket_id)
        await process_ticket(router, dispatcher, app_token, table_id,
                             record_id, ticket, notify_open_id)
        return

    # Parse complexity from diagnosis
    from agent.jobs.maqs import _parse_complexity, _parse_workflow_steps
    complexity = _parse_complexity(diagnosis)
    log.info("[MADS] %s complexity=%s, routing accordingly", ticket_id, complexity)
    await bitable_update(app_token, table_id, record_id, {"complexity": complexity})

    # Check for workflow steps — when present, steps = contract (skip negotiation)
    has_workflow = bool(_parse_workflow_steps(diagnosis))

    # ── L4/L5: Must decompose, not fix directly ──
    if complexity in ("L4", "L5"):
        log.info("[MADS] %s is %s — requires decomposition, not direct fix", ticket_id, complexity)
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": complexity == "L5",
        })
        design_review = " + Design Review" if complexity == "L5" else ""
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "skipped",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })
        await notify(dispatcher, "yellow",
                     f"MADS {ticket_id} ({complexity}): 需要拆分为 L2/L3 子工单{design_review}\n"
                     f"诊断显示涉及多文件/新模块，单体修复幻觉风险高。",
                     header="MAQS")
        return

    # ── L1: Skip contract, direct fix ──
    if complexity == "L1":
        log.info("[MADS] %s is L1 — skipping contract, direct fix", ticket_id)
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "skipped",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })
        await bitable_update(app_token, table_id, record_id, {"status": "contracted"})
        if _card_mid:
            ticket["status_card_mid"] = _card_mid
        await process_ticket(router, dispatcher, app_token, table_id,
                             record_id, ticket, notify_open_id,
                             skip_diagnosis=True)
        return

    # ── Workflow-based contract: steps = contract, skip LLM negotiation ──
    if has_workflow:
        log.info("[MADS] %s has workflow steps — using steps as contract (skip %s negotiation)",
                 ticket_id, complexity)
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "done",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })
        await bitable_update(app_token, table_id, record_id, {
            "status": "contracted",
            "contract_track": "steps",
        })
        if _card_mid:
            ticket["status_card_mid"] = _card_mid
        await process_ticket(router, dispatcher, app_token, table_id,
                             record_id, ticket, notify_open_id,
                             skip_diagnosis=True)
        return

    # ── L2 (no workflow): Lightweight contract (isolated self-review, no loop) ──
    if complexity == "L2":
        log.info("[MADS] %s is L2 — lightweight contract", ticket_id)
        await bitable_update(app_token, table_id, record_id, {"status": "contracting"})
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "running",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })

        from agent.jobs.mads.contract import negotiate_contract_light
        contract = await negotiate_contract_light(
            router=router,
            ticket_id=record_id[:8],
            ticket_info=diagnosis,
        )

        write_artifact(record_id[:8], "contract.md", contract or "")
        await bitable_update(app_token, table_id, record_id, {"status": "contracted"})
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "done",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })
        if _card_mid:
            ticket["status_card_mid"] = _card_mid
        await process_ticket(router, dispatcher, app_token, table_id,
                             record_id, ticket, notify_open_id,
                             skip_diagnosis=True)
        return

    # ── L3 (no workflow): Standard contract with Sonnet reviewer, max 2 rounds ──
    log.info("[MADS] %s is L3 — standard contract (Sonnet review, max 2 rounds)", ticket_id)
    await bitable_update(app_token, table_id, record_id,
                          {"status": "contracting"})
    await _update_card("contracting", {
        "diagnosing": "done", "contracting": "running",
        "fixing": "pending", "hardgate": "pending",
        "reviewing": "pending", "merging": "pending",
    })

    contract = await negotiate_contract(
        router=router,
        ticket_id=record_id[:8],
        ticket_info=diagnosis,
        max_rounds=2,
        reviewer_model="sonnet",
    )

    if contract is None:
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": True,
        })
        await _update_card("contracting", {
            "diagnosing": "done", "contracting": "failed",
            "fixing": "pending", "hardgate": "pending",
            "reviewing": "pending", "merging": "pending",
        })
        await notify(dispatcher, "orange",
                     f"MADS Contract 未收敛: {ticket_id}\n"
                     f"Implementer 和 QA 经过 2 轮协商未达成一致，需要人工介入。")
        return

    await bitable_update(app_token, table_id, record_id, {
        "status": "contracted",
    })
    await _update_card("contracting", {
        "diagnosing": "done", "contracting": "done",
        "fixing": "pending", "hardgate": "pending",
        "reviewing": "pending", "merging": "pending",
    })

    # ── Fix + QA (delegate to existing MAQS process_ticket) ──
    if _card_mid:
        ticket["status_card_mid"] = _card_mid
    log.info("[MADS] Contract agreed for %s, proceeding to Fix/QA", ticket_id)
    await process_ticket(router, dispatcher, app_token, table_id,
                         record_id, ticket, notify_open_id,
                         skip_diagnosis=True)


# ══════════════════════════════════════════════════════════════════════
#  Integration Gate (post-merge validation for composite tickets)
# ══════════════════════════════════════════════════════════════════════

async def check_integration_gate(
    router, dispatcher, app_token: str, table_id: str,
    record_id: str, ticket: dict,
):
    """Check if all sub-tickets of a composite ticket are closed.

    When all sub-tickets are closed, run integration validation
    (smoke test + unit test) and close the parent ticket.
    """
    sub_ticket_ids_str = ticket.get("sub_ticket_ids", "[]")
    try:
        sub_ticket_ids = json.loads(sub_ticket_ids_str)
    except (json.JSONDecodeError, TypeError):
        sub_ticket_ids = []

    if not sub_ticket_ids:
        return

    ticket_id = ticket.get("title", record_id[:8])

    # Check if all sub-tickets are closed
    all_closed = True
    for sub_id in sub_ticket_ids:
        sub_status = await bitable_get_status(app_token, table_id, sub_id)
        if sub_status is None:
            # Cannot retrieve record — treat as NOT closed (safe default)
            log.warning("[MADS] Cannot get status for sub-ticket %s, skipping gate", sub_id)
            all_closed = False
            break
        if sub_status != "closed":
            all_closed = False
            break

    if not all_closed:
        return

    log.info("[MADS] All sub-tickets closed for %s, running integration gate",
             ticket_id)

    # Run smoke test as integration validation
    import asyncio
    import sys
    from agent.jobs.mads.helpers import PROJECT_ROOT
    proc = await asyncio.create_subprocess_exec(
        sys.executable, f"{PROJECT_ROOT}/scripts/smoke_test.py",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=PROJECT_ROOT,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

    if proc.returncode == 0:
        await bitable_update(app_token, table_id, record_id, {
            "status": "closed",
        })
        await notify(dispatcher, "green",
                     f"**[MADS] 集成验证通过**\n\n"
                     f"「{ticket_id}」的所有子工单已完成，集成测试通过。\n"
                     f"工单已关闭。")
    else:
        smoke_output = stdout.decode()[:500]
        await bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": True,
        })
        await notify(dispatcher, "orange",
                     f"**[MADS] 集成验证失败**\n\n"
                     f"「{ticket_id}」的子工单全部完成，但 Smoke Test 失败。\n"
                     f"需要人工检查。\n\n```\n{smoke_output}\n```")


# ══════════════════════════════════════════════════════════════════════
#  Main entry point — MADS pipeline runner
# ══════════════════════════════════════════════════════════════════════

async def run_mads_pipeline(router, dispatcher, config: dict):
    """Main MADS pipeline entry. Called by scheduler alongside MAQS.

    Handles:
    1. Composite tickets in design/review/decompose stages
    2. Integration gate checks for composite tickets with sub-tickets
    """
    app_token = config.get("bitable_app_token", "")
    table_id = config.get("bitable_table_id", "")

    if not app_token or not table_id:
        return

    # ── Process composite tickets in active MADS states ──
    mads_states = [
        "designing", "awaiting_review", "review_approved",
        "review_feedback", "decomposing",
    ]
    for state in mads_states:
        tickets = await bitable_query(
            app_token, table_id,
            filter_str=f'CurrentValue.[status]="{state}"',
            limit=10,
        )
        for ticket_data in tickets:
            rid = ticket_data.get("record_id", "")
            fields = ticket_data.get("fields", {})
            if rid:
                await process_composite_ticket(
                    router, dispatcher, app_token, table_id, rid, fields,
)

    # ── Check integration gates for composite tickets with sub-tickets ──
    sub_ticket_parents = await bitable_query(
        app_token, table_id,
        filter_str='CurrentValue.[status]="sub_tickets_created"',
        limit=10,
    )
    for ticket_data in sub_ticket_parents:
        rid = ticket_data.get("record_id", "")
        fields = ticket_data.get("fields", {})
        if rid:
            await check_integration_gate(
                router, dispatcher, app_token, table_id, rid, fields)

    # ── Route open sub-tickets (from decomposition) into contract flow ──
    # Sub-tickets have parent_ticket set; MAQS skips them, MADS owns them.
    # Process up to MAX_CONCURRENT_SUBTASKS in parallel.
    open_tickets_all = await bitable_query(
        app_token, table_id,
        filter_str='CurrentValue.[status]="open"',
        limit=20,
    )
    sem = asyncio.Semaphore(MAX_CONCURRENT_SUBTASKS)
    sub_tasks = []
    for ticket_data in open_tickets_all:
        fields = ticket_data.get("fields", {})
        if fields.get("parent_ticket"):
            rid = ticket_data.get("record_id", "")
            if rid:
                notify_open_id = fields.get("notify_open_id", "")

                async def _run_sub(r=rid, f=fields, n=notify_open_id):
                    async with sem:
                        try:
                            await process_atomic_with_contract(
                                router, dispatcher, app_token, table_id,
                                r, f, n)
                        except Exception as e:
                            log.error("Sub-ticket %s failed: %s", r[:12], e)

                sub_tasks.append(_run_sub())

    if sub_tasks:
        log.info("MADS: launching %d sub-tickets (max %d concurrent)",
                 len(sub_tasks), MAX_CONCURRENT_SUBTASKS)
        await asyncio.gather(*sub_tasks)

    # ── Route atomic tickets through contract-enhanced flow (concurrent) ──
    in_progress_tasks = []
    for state in ["diagnosed", "contracting", "contracted"]:
        tickets = await bitable_query(
            app_token, table_id,
            filter_str=f'CurrentValue.[status]="{state}"',
            limit=10,
        )
        for ticket_data in tickets:
            fields = ticket_data.get("fields", {})
            if state == "diagnosed" and fields.get("complexity", "atomic") != "atomic":
                continue
            rid = ticket_data.get("record_id", "")
            if rid:
                notify_open_id = fields.get("notify_open_id", "")

                async def _run_prog(r=rid, f=fields, n=notify_open_id):
                    async with sem:
                        try:
                            await process_atomic_with_contract(
                                router, dispatcher, app_token, table_id,
                                r, f, n)
                        except Exception as e:
                            log.error("Ticket %s failed: %s", r[:12], e)

                in_progress_tasks.append(_run_prog())

    if in_progress_tasks:
        log.info("MADS: launching %d in-progress tickets (max %d concurrent)",
                 len(in_progress_tasks), MAX_CONCURRENT_SUBTASKS)
        await asyncio.gather(*in_progress_tasks)

    # ── Route new open tickets by complexity (concurrent) ──
    open_tickets = await bitable_query(
        app_token, table_id,
        filter_str='CurrentValue.[status]="open"',
        limit=20,
    )
    from agent.jobs.maqs import process_ticket as maqs_process
    standalone_tasks = []
    for ticket_data in open_tickets:
        fields = ticket_data.get("fields", {})
        complexity = fields.get("complexity", "atomic")
        rid = ticket_data.get("record_id", "")
        if not rid:
            continue
        if complexity == "composite":
            await process_composite_ticket(
                router, dispatcher, app_token, table_id, rid, fields)
        elif not fields.get("parent_ticket"):
            notify_open_id = config.get("notify_open_id", "")

            async def _run_standalone(r=rid, f=fields, n=notify_open_id):
                async with sem:
                    try:
                        log.info("MADS routing standalone atomic ticket: %s",
                                 f.get("title", r[:8]))
                        has_diagnosis = bool(f.get("diagnosis"))
                        await maqs_process(
                            router, dispatcher, app_token, table_id, r, f,
                            n, skip_diagnosis=has_diagnosis)
                    except Exception as e:
                        log.error("Standalone ticket %s failed: %s", r[:12], e)

            standalone_tasks.append(_run_standalone())

    if standalone_tasks:
        log.info("MADS: launching %d standalone tickets (max %d concurrent)",
                 len(standalone_tasks), MAX_CONCURRENT_SUBTASKS)
        await asyncio.gather(*standalone_tasks)

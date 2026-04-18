# -*- coding: utf-8 -*-
"""MAQS — Multi-Agent Quality System.

Orchestrates isolated agents for bug diagnosis, fix, and quality control.
Flow: Signal → Triage → Diagnosis(Opus) → Atomic split → Fix(Sonnet) → QA(Opus)

All inter-agent data is structured natural language, persisted in Bitable.
Each agent runs as an isolated `claude -p` call with restricted tool access.

Shared infrastructure lives in agent.jobs.mads.helpers (extracted for MADS reuse).
"""

import asyncio
import logging
import os

from agent.infra.merge_queue import MergeQueue, MergeRequest
from agent.infra.models import LoopPhase, TicketContext
from agent.jobs.mads.helpers import (
    bitable_add as _bitable_add,
    bitable_query as _bitable_query,
    bitable_update as _bitable_update,
    git as _git,
    git_in as _git_in,
    notify as _notify_mads,
    parse_json_response as _parse_json_response,
    parse_qa_verdict as _parse_qa_verdict,
    run_agent as _run_agent,
    run_codex as _run_codex,
    send_status_card as _send_status_card,
    update_status_card as _update_status_card,
    worktree_cleanup_stale as _worktree_cleanup_stale,
    worktree_create as _worktree_create,
    worktree_merge_to_dev as _worktree_merge_to_dev,
    worktree_remove as _worktree_remove,
    write_artifact,
    WORKTREE_BASE as _WORKTREE_BASE,
)

log = logging.getLogger("hub.maqs")

MAX_PARALLEL = 5
MAX_REJECT = 3

PHASE_TIMEOUTS = {
    LoopPhase.DIAGNOSING: 900,
    LoopPhase.FIXING: 900,
    LoopPhase.REVIEWING: 900,
    LoopPhase.VISUAL_QA: 300,
}


async def _run_phase_with_timeout(phase: LoopPhase, coro, ticket_id: str = "",
                                   timeout_override: int = 0):
    """Execute a pipeline phase with timeout. Raises asyncio.TimeoutError on expiry."""
    timeout = timeout_override or PHASE_TIMEOUTS.get(phase, 900)
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        log.error("Phase %s timed out after %ds for %s", phase.name, timeout, ticket_id)
        raise


import re as _re_xml


def _parse_commit_message(fix_report: str, fallback: str) -> str:
    """Extract commit message from <fix_meta> XML block. Falls back to provided default."""
    m = _re_xml.search(r"<commit_message>(.*?)</commit_message>", fix_report, _re_xml.DOTALL)
    if m:
        msg = m.group(1).strip()
        if msg:
            return msg
    return fallback


def _parse_atomic_split(diagnosis: str) -> list[str]:
    """Extract sub-ticket titles from <atomic_split> in diagnosis_meta. Returns [] if none."""
    m = _re_xml.search(r"<atomic_split>(.*?)</atomic_split>", diagnosis, _re_xml.DOTALL)
    if not m:
        return []
    content = m.group(1).strip()
    if content == "none":
        return []
    tickets = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("- "):
            tickets.append(line[2:].strip())
    return tickets


def _parse_experience_summary(diagnosis: str) -> str:
    """Extract experience summary from <experience_summary> in diagnosis_meta."""
    m = _re_xml.search(r"<experience_summary>(.*?)</experience_summary>", diagnosis, _re_xml.DOTALL)
    if m:
        return m.group(1).strip()[:200]
    return ""


def _is_limit_banner(text: str) -> bool:
    """Detect Claude CLI rate-limit / overload banners (content-agnostic).

    Use this for non-diagnosis outputs (fix_report, design, decomposition)
    where short text is legitimate but banners must still short-circuit.
    """
    if not text:
        return False
    normalized = " ".join(text.lower().split())
    if any(pattern in normalized for pattern in (
        "you've hit your limit",
        "you hit your limit",
        "hit your limit",
        "rate limit",
        "rate-limit",
        "overloaded",
    )):
        return True
    if "503" in normalized:
        return True
    if "resets" in normalized and ("limit" in normalized or "rate" in normalized):
        return True
    return False


def _is_garbage_diagnosis(text: str) -> bool:
    """Reject known CLI garbage text while allowing minimal structured mock diagnosis."""
    if _is_limit_banner(text):
        return True
    stripped = text.strip()
    if (
        len(stripped) < 100
        and "<affected-files>" not in stripped
        and "<diagnosis_meta>" not in stripped
    ):
        return True
    # Structured diagnosis with <diagnosis_meta> but no affected_files = invalid
    if "<diagnosis_meta>" in stripped:
        from agent.jobs.hardgate import parse_affected_files
        if not parse_affected_files(text):
            return True
    return False


_VALID_COMPLEXITY = {"L1", "L2", "L3", "L4", "L5"}


# ── Workflow step parsing ──────────────────────────────────────────

def _parse_workflow_steps(diagnosis: str) -> list[dict]:
    """Parse workflow steps from <workflow_steps> in diagnosis_meta.

    Returns list of step dicts, or [] on parse failure (triggers fallback).
    """
    import json as _json
    m = _re_xml.search(r"<workflow_steps>(.*?)</workflow_steps>", diagnosis, _re_xml.DOTALL)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        log.warning("workflow_steps: empty content in tag")
        return []
    json_m = _re_xml.search(r'\[.*\]', raw, _re_xml.DOTALL)
    if json_m:
        raw = json_m.group(0)
    try:
        steps = _json.loads(raw)
        if not isinstance(steps, list):
            log.warning("workflow_steps: expected list with 2+ items, got %s", type(steps).__name__)
            return []
        if len(steps) < 2:
            log.warning("workflow_steps: expected list with 2+ items, got list with %d item(s)", len(steps))
            return []
        if len(steps) > 7:
            log.warning("workflow_steps: %d steps exceeds limit 7, truncating", len(steps))
            steps = steps[:7]
        for s in steps:
            if not isinstance(s, dict) or "content" not in s:
                log.warning("workflow_steps: invalid step format: %s", str(s)[:100])
                return []
        return steps
    except (_json.JSONDecodeError, ValueError) as e:
        log.warning("Failed to parse workflow_steps JSON: %s | raw=%r", e, raw[:200])
    return []


def _build_workflow(raw_steps: list[dict]) -> 'TicketWorkflow':
    """Build TicketWorkflow from parsed step dicts."""
    from agent.infra.models import TicketStep, TicketWorkflow, StepStatus
    steps = []
    for i, raw in enumerate(raw_steps):
        steps.append(TicketStep(
            id=f"step_{i+1:03d}",
            content=raw.get("content", ""),
            active_form=raw.get("content", "")[:50],
            status=StepStatus.PENDING,
            affected_files=raw.get("affected_files", []),
            verification=raw.get("verification", ""),
        ))
    return TicketWorkflow(steps=steps)


def _format_workflow_for_prompt(workflow: 'TicketWorkflow') -> str:
    """Format workflow steps as structured text for injection into prompts."""
    from agent.infra.models import StepStatus
    lines = []
    for s in workflow.steps:
        status_tag = ""
        if s.status == StepStatus.LOCKED:
            status_tag = " [🔒 已锁定 — 不可修改]"
        elif s.status == StepStatus.FAILED:
            status_tag = " [❌ 上轮失败 — 需要修复]"
        lines.append(
            f"- **{s.id}**: {s.content}{status_tag}\n"
            f"  affected_files: {s.affected_files}\n"
            f"  verification: {s.verification}"
        )
    return "\n".join(lines)


def _parse_step_results(fix_report: str) -> list[dict]:
    """Parse <step_results> from fixer output."""
    import json as _json
    m = _re_xml.search(r"<step_results>(.*?)</step_results>", fix_report, _re_xml.DOTALL)
    if not m:
        return []
    try:
        results = _json.loads(m.group(1).strip())
        return results if isinstance(results, list) else []
    except (_json.JSONDecodeError, ValueError):
        log.warning("Failed to parse step_results JSON")
    return []


def _parse_step_verdicts(qa_report: str) -> list[dict]:
    """Parse <step_verdicts> from QA output."""
    import json as _json
    m = _re_xml.search(r"<step_verdicts>(.*?)</step_verdicts>", qa_report, _re_xml.DOTALL)
    if not m:
        return []
    try:
        verdicts = _json.loads(m.group(1).strip())
        return verdicts if isinstance(verdicts, list) else []
    except (_json.JSONDecodeError, ValueError):
        log.warning("Failed to parse step_verdicts JSON")
    return []


def _apply_step_results(workflow: 'TicketWorkflow', results: list[dict]):
    """Apply fixer step results to workflow."""
    from agent.infra.models import StepStatus
    result_map = {r.get("step_id", ""): r for r in results}
    for step in workflow.steps:
        if step.id in result_map:
            r = result_map[step.id]
            status_str = r.get("status", "completed")
            step.status = StepStatus.FAILED if status_str == "failed" else StepStatus.COMPLETED
            step.result = r.get("result", "")


def _apply_step_verdicts(workflow: 'TicketWorkflow', verdicts: list[dict]):
    """Apply QA step verdicts to workflow."""
    verdict_map = {v.get("step_id", ""): v for v in verdicts}
    for step in workflow.steps:
        if step.id in verdict_map:
            v = verdict_map[step.id]
            step.qa_verdict = v.get("verdict", "fail")
            step.qa_reason = v.get("reason", "")


def _serialize_workflow(workflow: 'TicketWorkflow') -> str:
    """Serialize workflow to JSON string for artifact storage."""
    import json as _json
    from dataclasses import asdict
    return _json.dumps(asdict(workflow), ensure_ascii=False, indent=2)


def _parse_complexity(diagnosis: str) -> str:
    """Extract complexity level from <complexity> in diagnosis_meta.

    Returns L1-L5. Defaults to L3 (conservative — standard contract path).
    """
    m = _re_xml.search(r"<complexity>\s*(L[1-5])\s*</complexity>", diagnosis, _re_xml.IGNORECASE)
    if m:
        level = m.group(1).upper()
        if level in _VALID_COMPLEXITY:
            return level
    log.warning("Diagnosis missing valid <complexity> tag, defaulting to L3")
    return "L3"


def _parse_user_impact(diagnosis: str) -> str:
    """Extract user_impact (high/low) from <user_impact> in diagnosis_meta."""
    m = _re_xml.search(r"<user_impact>\s*(high|low)\s*</user_impact>", diagnosis, _re_xml.IGNORECASE)
    return m.group(1).lower() if m else "high"


def _select_contract_track(complexity: str, user_impact: str) -> str:
    """Route to contract track based on complexity + user_impact.

    Track A: user-facing → lightweight acceptance criteria (1 LLM call)
    Track B: internal/simple → scope declaration from diagnosis (0 LLM calls)
    """
    if complexity == "L1":
        return "B"
    if complexity == "L2":
        return "A" if user_impact == "high" else "B"
    return "A" if user_impact == "high" else "B"


CONTRACT_TRACK_A_PROMPT = """\
You are a contract agent producing a lightweight acceptance contract for a \
user-facing bug fix.

## Diagnosis
{diagnosis}

## Output format (3-7 criteria, no more)

### Acceptance Contract

**Experience promise**: One sentence — what changes for the user after this fix.

**Acceptance Criteria**
Numbered list. Each criterion:
- Written from the user's perspective (observable behavior)
- Independently verifiable without reading code
- Covers ALL root causes in the diagnosis

**Verification Method**
For each criterion:
- Trigger (what to do)
- Observe (what to check)
- PASS vs FAIL

Do NOT include: User Scenarios, Implementation Plan, Edge Cases, Assumptions. \
Keep total output under 200 lines.
"""


async def _contract_track_a(router, diagnosis: str) -> str:
    """Track A: lightweight acceptance criteria, single Sonnet call."""
    sys_prompt = CONTRACT_TRACK_A_PROMPT.format(diagnosis=_strip_task_analysis(diagnosis))
    result = await _run_agent(
        router, role="contract", model="sonnet",
        prompt="Produce the Acceptance Contract for this bug fix. "
               "Focus on what the user should experience after the fix.",
        system_prompt=sys_prompt,
    )
    return f"--- Track A: Acceptance Contract ---\n{result}\n"


def _contract_track_b(diagnosis: str) -> str:
    """Track B: scope-only contract, no LLM call."""
    from agent.jobs.hardgate import parse_affected_files
    files = parse_affected_files(diagnosis)

    experience = ""
    m = _re_xml.search(r"<experience_summary>(.*?)</experience_summary>", diagnosis, _re_xml.DOTALL)
    if m:
        experience = m.group(1).strip()

    fix_section = ""
    m = _re_xml.search(r"### 8\. Proposed fix\s*\n(.*?)(?=\n### |\n<|\Z)", diagnosis, _re_xml.DOTALL)
    if m:
        fix_section = m.group(1).strip()

    root_cause = ""
    m = _re_xml.search(r"### 6\. Root cause\s*\n(.*?)(?=\n### |\Z)", diagnosis, _re_xml.DOTALL)
    if m:
        root_cause = m.group(1).strip()

    listing = "\n".join(f"- {f}" for f in files) if files else "- (none extracted)"

    return (
        "### Scope Contract (auto-generated from diagnosis)\n\n"
        f"**Root cause**: {root_cause[:300]}\n\n"
        f"**Experience summary**: {experience}\n\n"
        f"**Affected files**:\n{listing}\n\n"
        f"**Proposed fix**:\n{fix_section[:500]}\n\n"
        "---\n"
        "*Track B scope-only contract (user_impact=low). No LLM negotiation.*"
    )


def _parse_discoveries(text: str) -> list[str]:
    """Extract discovery items from <discovery> block in fix report.

    Returns list of discovery descriptions (e.g. '[file:line] description').
    """
    m = _re_xml.search(r"<discovery>(.*?)</discovery>", text, _re_xml.DOTALL)
    if not m:
        return []
    items = []
    for line in m.group(1).strip().splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
        elif line:
            items.append(line)
    return items


def _is_gitignored_artifact(desc: str) -> bool:
    """Detect worktree-only discovery noise caused by gitignored artifacts."""
    m = _re_xml.match(r"\[([^\]:]+)(?::\d+)?\]", desc.strip())
    if not m:
        return False
    rel_path = m.group(1).strip().lstrip("./")
    if not rel_path:
        return False
    whitelist = (
        "config.yaml",
        "data/",
        "__pycache__/",
        ".worktrees/",
        ".gemini/",
    )
    if not any(rel_path == item.rstrip("/") or rel_path.startswith(item) for item in whitelist):
        return False
    module_path = os.path.abspath(__file__)
    marker = f"{os.sep}.worktrees{os.sep}"
    if marker in module_path:
        project_root = module_path.split(marker, 1)[0]
    else:
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(module_path)))
    return os.path.exists(os.path.join(project_root, rel_path))


# Hallucination signals: phrases that suggest the LLM is inventing an issue
# from stale design docs or half-remembered code. Case-insensitive, substring.
_DISCOVERY_HALLUCINATION_KEYWORDS = (
    "早已修",
    "应该存在",
    "should exist",
    "design doc",
    "设计文档",
    "todo: implement",
    "not yet implemented",
    "尚未实现",
    "未落地",
    "hypothetical",
    "推测性",
)


def _validate_discovery_premise(desc: str, wt_path: str | None) -> tuple[bool, str]:
    """Gate discovery tickets before persistence.

    Checks: file path exists in worktree HEAD, line number within bounds,
    description free of hallucination-signal keywords.

    Returns (accept, reason). Logs are the caller's responsibility.
    """
    m = _re_xml.match(r"\[([^\]:]+)(?::(\d+))?\]\s*(.*)", desc.strip(), _re_xml.DOTALL)
    if not m:
        return True, "no_file_ref"  # free-form discoveries pass; only claims-about-files get validated
    rel_path = m.group(1).strip().lstrip("./")
    line_no = int(m.group(2)) if m.group(2) else 0
    body = m.group(3) or ""

    # (a) File existence in worktree (proxy for HEAD; worktree mirrors committed state at branch point)
    if wt_path:
        fpath = os.path.join(wt_path, rel_path)
        if not os.path.exists(fpath):
            return False, "file_not_found"

        # (b) Line number within file bounds
        if line_no > 0:
            try:
                with open(fpath, encoding="utf-8", errors="replace") as _fh:
                    line_count = sum(1 for _ in _fh)
                if line_no > line_count:
                    return False, "line_out_of_bounds"
            except OSError:
                pass  # unreadable — don't block on IO errors

    # (c) Hallucination-signal keywords in description
    body_lower = body.lower()
    for kw in _DISCOVERY_HALLUCINATION_KEYWORDS:
        if kw in body_lower:
            return False, f"hallucination_signal:{kw}"

    return True, "ok"


def _parse_control_signal(text: str) -> tuple[str, str] | None:
    """Extract control signal from agent output. Returns (signal, reason) or None."""
    m = _re_xml.search(r'<control signal="(\w+)">(.*?)</control>', text, _re_xml.DOTALL)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    return None


_STALE_INTERMEDIATE_STATES = ["diagnosing", "diagnosed", "fixing", "testing", "reviewing", "visual_qa"]


async def _reset_stale_intermediate_tickets(app_token: str, table_id: str) -> int:
    """Reset orphaned intermediate-state tickets back to open.

    Since the scheduler runs one tick at a time, any ticket in an intermediate
    state at pipeline startup must be orphaned (e.g. service restart / SIGTERM
    bypassed the except block). Unconditional reset is safe here.
    """
    reset_count = 0
    for state in _STALE_INTERMEDIATE_STATES:
        tickets = await _bitable_query(
            app_token, table_id,
            filter_str=f'CurrentValue.[status]="{state}"',
            limit=20,
        )
        for t in tickets:
            rid = t.get("record_id", "")
            if not rid:
                continue
            # For "fixing" state, skip reset if the worktree directory still exists —
            # that indicates an active fix-agent subprocess is running inside it.
            # Destroying an active worktree causes FileNotFoundError in the subprocess.
            if state == "fixing":
                safe_name = f"fix_MAQS-{rid[:8]}"
                wt_path = os.path.join(_WORKTREE_BASE, safe_name)
                if os.path.exists(wt_path):
                    log.info("MAQS: skipping reset for %s — active worktree detected", rid[:8])
                    continue
            # Clean up any residual fix branch
            branch = f"fix/MAQS-{rid[:8]}"
            await _git("branch", "-D", branch)  # ignore failure; branch may not exist
            await _bitable_update(app_token, table_id, rid, {"status": "open"})
            log.info("MAQS: reset orphaned ticket %s from %s → open", rid[:8], state)
            reset_count += 1
    return reset_count

# ══════════════════════════════════════════════════════════════════════
#  Prompts
# ══════════════════════════════════════════════════════════════════════

TRIAGE_PROMPT = """\
你是 claude-code-feishu 的信号分诊器。对以下原始信号进行分类。

## 原始信号
{signal}

## 任务
1. 判断类型：
   - bug：行为异常/错误
   - feature：未实现的能力
   - refactor：代码结构/可维护性改进，不改变外部行为
   - skill：需要新增或升级 Skill（.claude/skills/）
   - config：配置项调整（config.yaml / sources.yaml 等）
   - noise：无需处理
2. 如果不是 noise，给出一句话现象描述和严重度（P0-P3）
3. 判断复杂度：
   - atomic：单文件修改，修改位置明确，无设计歧义
   - composite：涉及多文件，需要设计决策，或需要引入新抽象
4. P0=服务不可用，P1=功能受损，P2=体验问题，P3=优化建议

## 输出格式（JSON）
{{"type": "bug|feature|refactor|skill|config|noise", "complexity": "atomic|composite", "phenomenon": "一句话描述", "severity": "P0|P1|P2|P3", "reason": "分类理由"}}
只输出 JSON。"""

INVESTIGATOR_PROMPT = """\
你是 claude-code-feishu 的诊断 agent，遵循寻乌调查原则。

## 你的任务
从现象出发，沿数据流追踪根因，输出诊断报告和修复方案。
诊断不只是找技术 root cause——你必须同时做**体验归因**：用户期望什么、现实是什么、差距如何弥合。

## 工单信息
{ticket_info}

## 工作原则
- 体验视角：始终思考——用户报这个问题是为了追求什么样的体验？修复后用户应该感受到什么变化？
- 数据流追踪：从异常点逆向追踪到输入，正向追踪到输出
- 第一手数据：必须读源码确认，不能基于经验猜测
- 假设验证：每个假设必须有证据支持或反驳
- 网络搜索：涉及第三方 API / 库行为时，主动搜索确认
- 原子拆分：如果发现多个独立问题，拆分为多个原子工单
- 根因深度：停在「头痛医头」的浅层根因是不够的。例如「变量未初始化」是浅因，「为什么设计时没有 required field 约束」才是深因。至少追问两次"为什么"

## 输出格式（结构化自然语言，严格按此顺序——利于 Chain-of-Thought 推理）

**重要：输出分为两部分。<task-analysis> 内的分析过程仅存档，不传递给下游 Fixer。只有 §6-10 和 XML 控制块传递给下游。**

<task-analysis>

### 1. 体验归因
（用户期望的行为是什么？当前实际行为是什么？两者的差距在哪里？修复后用户应感受到什么变化？）

### 2. 现象描述
（起点锚定，原始现象的精确描述）

### 3. 初始假设
（基于现象的直觉判断，后续验证或推翻）

### 4. 数据流追踪
（从异常点逆向/正向追踪，标注文件:行号）

### 5. 假设验证记录
（每个假设的证据/反证，包括排除的错误假设——防止重复探索死胡同）

</task-analysis>

### 6. 根因定位
（经过验证的最终结论，附代码证据）

### 7. 影响范围
（受影响的文件列表）

### 8. 建议方案
（1-2 个方案，含 trade-off 和置信度。方案必须对齐「体验归因」中的体验承诺）

### 9. 验证方法
（如何确认修复有效——包括技术验证和体验验证）
是否需要人类协同验证：是/否
金标准数据：（如适用）

### 10. 原子工单拆分
（如果发现多个独立问题，列出每个子工单的标题和现象。单一问题写「无需拆分」）

在报告末尾**必须**追加以下 XML 控制块（供下游程序解析，格式不可变更）：

<diagnosis_meta>
<affected_files>
- path/to/file1.py
- path/to/file2.py
</affected_files>
<complexity_rationale>
先分析改动性质（值替换/逻辑变更/跨文件关联/新模块/新能力），再给出判定。
</complexity_rationale>
<complexity>L2</complexity>
<experience_summary>一句话体验归因摘要（从第1节体验归因提炼）</experience_summary>
<atomic_split>none</atomic_split>
<workflow_steps>
[
  {{"content": "更新生产代码中的目标逻辑", "affected_files": ["src/module.py"], "verification": "python3 -m pytest tests/unit/test_module.py -k target_case"}}
  {{"content": "补充覆盖该场景的单元测试", "affected_files": ["tests/unit/test_module.py"], "verification": "grep -n 'target_case' tests/unit/test_module.py"}}
]
</workflow_steps>
</diagnosis_meta>

### workflow_steps 拆分规则（最关键的智力环节）
- 每步是**一个单一、聚焦的修改动作**，不可再有意义地拆分
- 每步必须有具体的 verification（可执行的验证命令）
- 每步的 affected_files 是该步骤的修改范围（子集于总 affected_files）
- verification 中要求新增或修改的文件必须同时列入本步骤的 affected_files（典型场景：verification 要求添加测试用例时，目标测试文件必须入列）
- 步骤按依赖顺序排列
- 数量控制：2-7 步。少于 2 步说明拆分不够，超过 7 步说明工单应升级为 composite
- **[禁止]** `<workflow_steps>[]</workflow_steps>` 或空 JSON 数组无效——即使最简单的修复也需 2 步（如：修改代码 + 验证通过）
- 如果某步发现 scope 外的同类问题，该步的 content 必须写成"**检查**同类问题并上报发现"，不是"**修复**同类问题"——scope 外问题通过 `<discovery>` 提交衍生工单

### 复杂度判定规则（complexity 字段取值 L1-L5，严格按以下标准）
- **L1**：改 1 个文件，改动是值替换（数值/字符串/配置值），无逻辑分支变更，预估 ≤5 行
- **L2**：改 1 个文件，有逻辑变更（条件/数据流/函数体），但不改函数签名/接口
- **L3**：改 2-3 个文件，文件间有直接调用或数据流依赖，不新增模块
- **L4**：改 4+ 个文件，或需新增模块，但仍在同一子系统内 → 必须在 §10 拆分为 L2/L3 子工单
- **L5**：新增能力、架构变更、跨子系统协调 → 必须拆分 + 标记需要 Design Review

判定优先级：先看 affected_files 数量，再看改动性质。**宁可高估不可低估**——L2 误判为 L3 只多一轮 contract，L3 误判为 L2 可能漏掉跨文件关联。

**[强制]** `<complexity>` 标签内容只能是 `L1`/`L2`/`L3`/`L4`/`L5` 五个值之一，不可附加任何文字或标点。缺少此标签或格式错误将导致下游流水线路由失败。

如需原子拆分，<atomic_split> 格式为多行列表：
<atomic_split>
- 子工单1标题
- 子工单2标题
</atomic_split>

如果在诊断过程中遇到以下情况，在报告最后追加对应控制信号：
- 需要停止当前分析（发现问题超出范围）：`<control signal="STOP">原因</control>`
- 需要升级为人工处理（置信度不足或风险过高）：`<control signal="ESCALATE">原因</control>`
- 需要等待外部信息（如用户确认或第三方 API 响应）：`<control signal="PAUSE">等待什么</control>`

只输出上述格式的诊断报告，不要其他文字。

════════════════════════════════════════════════════════════════════
## 强制响应终结模板（必须作为回复的最后一段完整输出）

你的输出**必须**以下述 XML 块收尾，每个字段都要填入实际值（不要保留占位符，不要省略任何标签）。**缺失或格式错误将导致工单被判定为无效并需要人工介入**。

<diagnosis_meta>
<affected_files>
- path/to/file1.py
- path/to/file2.py
</affected_files>
<complexity_rationale>一句话说明复杂度判定依据</complexity_rationale>
<complexity>L2</complexity>
<experience_summary>一句话可复用经验</experience_summary>
<atomic_split>none</atomic_split>
<workflow_steps>
[
  {{"content": "步骤1：具体修改动作", "affected_files": ["path/to/file.py"], "verification": "验证命令1"}},
  {{"content": "步骤2：具体修改动作", "affected_files": ["path/to/file.py"], "verification": "验证命令2"}}
]
</workflow_steps>
</diagnosis_meta>

`<complexity>` 仅允许 `L1`/`L2`/`L3`/`L4`/`L5` 五个值之一——不要加空格、标点、注释或中文解释。
════════════════════════════════════════════════════════════════════"""

IMPLEMENTER_PROMPT = """\
你是 claude-code-feishu 的修复 agent。基于诊断报告和步骤清单实施修复。

## 诊断报告
{diagnosis}

## 步骤清单（你的执行轨道）
{workflow_steps}

## 金标准验证数据（如有）
{golden_data}

## 执行规则（硬性约束）

### 步骤驱动执行
1. **严格按步骤顺序执行**，一次只处理一个步骤
2. 每完成一步，在 `<step_results>` 中报告该步骤的 result
3. **只允许修改当前步骤的 affected_files**
4. 如果某步无法完成，标记为 failed 并说明原因，继续下一步
5. **禁止做步骤清单之外的任何修改**

### 范围闭合（最高优先级）
步骤清单的 affected_files 并集是硬边界。未列入的文件一律不动，包括：
- 其他 prompt 变量（`GATEKEEPER_PROMPT`、`DESIGNER_PROMPT` 等）
- 调用方函数（`process_ticket`、`fix_ticket` 等），除非明确列入
- 任何"顺手优化"的代码

### 禁止清单（硬性禁止，不可例外）
- **禁止创建新函数**：不得创建步骤清单未要求的新 `def` 或 `class`
- **禁止修改 prompt 变量**：`*_PROMPT` 变量只有在 affected_files 明确指向时才能改
- **禁止引入计划外 import**：不得 import 步骤清单未提及的模块
- **禁止重构**：不得改变未受影响的代码结构、变量名、注释

### 量化锚定
典型 bug fix 改动量：5-30 行。超过 50 行需自检是否越界。超过 100 行视为越界。

{retry_section}

## 输出格式

### 逐步执行报告
对每个步骤报告执行结果。

### Commit 信息草稿
（格式：fix(MAQS-{ticket_id}): 简要描述）

## 控制块（必须在报告最后输出）

<step_results>
[
  {{"step_id": "step_001", "status": "completed", "result": "做了什么"}},
  {{"step_id": "step_002", "status": "failed", "result": "失败原因"}}
]
</step_results>

<fix_meta>
<modified_files>
- path/to/file1.py
</modified_files>
<commit_message>fix(MAQS-{ticket_id}): 简要描述</commit_message>
</fix_meta>

## 发现上报（scope 外问题）

修复过程中如果发现步骤清单范围之外的问题，**不要直接修改**，用 `<discovery>` 块上报：

<discovery>
- [path/to/file.py:行号] 问题描述
</discovery>

## 诊断挑战（仅在诊断明显错误时使用）

`<control signal="CHALLENGE_DIAGNOSIS">具体理由</control>`

完成修改后，执行 `git add` 暂存变更的文件。只输出上述格式的报告。"""


IMPLEMENTER_PROMPT_LEGACY = """\
你是 claude-code-feishu 的修复 agent。基于诊断报告实施修复。

## 诊断报告
{diagnosis}

## 金标准验证数据（如有）
{golden_data}

## 执行规则（硬性约束）

### Legacy 执行模式
1. 直接根据诊断报告实施修复，不依赖步骤清单
2. affected_files 是唯一硬边界；如果未提供 affected_files，不要扩展修改范围
3. 禁止做诊断报告之外的任何修改
4. 如果上轮修复被拒绝，必须显式避免重复同类问题

### 范围闭合（最高优先级）
affected_files 是硬边界。未列入的文件一律不动，包括：
- 其他 prompt 变量（`GATEKEEPER_PROMPT`、`DESIGNER_PROMPT` 等）
- 调用方函数（`process_ticket`、`fix_ticket` 等），除非 affected_files 明确列入
- 任何"顺手优化"的代码

### 禁止清单（硬性禁止，不可例外）
- **禁止创建新函数**：不得创建诊断报告未要求的新 `def` 或 `class`
- **禁止修改 prompt 变量**：`*_PROMPT` 变量只有在 affected_files 明确指向时才能改
- **禁止引入计划外 import**：不得 import 诊断报告未提及的模块
- **禁止重构**：不得改变未受影响的代码结构、变量名、注释

### 量化锚定
典型 bug fix 改动量：5-30 行。超过 50 行需自检是否越界。超过 100 行视为越界。

{retry_section}

## 输出格式

### 执行报告
说明你如何依据诊断报告完成修复。

### Commit 信息草稿
（格式：fix(MAQS-{ticket_id}): 简要描述）

## 控制块（必须在报告最后输出）

<fix_meta>
<modified_files>
- path/to/file1.py
</modified_files>
<commit_message>fix(MAQS-{ticket_id}): 简要描述</commit_message>
</fix_meta>

## 发现上报（scope 外问题）

修复过程中如果发现诊断报告范围之外的问题，**不要直接修改**，用 `<discovery>` 块上报：

<discovery>
- [path/to/file.py:行号] 问题描述
</discovery>

## 诊断挑战（仅在诊断明显错误时使用）

`<control signal="CHALLENGE_DIAGNOSIS">具体理由</control>`

完成修改后，执行 `git add` 暂存变更的文件。只输出上述格式的报告。"""


IMPLEMENTER_RETRY_SECTION = """\
### 重试模式（本轮为第 {reject_round} 次重试）

以下步骤已通过 QA 验证并 **锁定**，你 **不得修改** 这些步骤涉及的文件中的已通过修改：
{locked_steps}

本轮只需处理以下失败步骤：
{failed_steps}

上轮 QA 反馈：
{qa_feedback}
"""

GATEKEEPER_PROMPT = """\
你是 claude-code-feishu 的质量控制 agent。你的唯一职责是**逐步骤**验证 fix 是否兑现了诊断报告的承诺。

## 核心规则
**默认 REJECT，不确定时不 PASS。**
不做 scope check（Hardgate 已做）、不跑 regression test（Hardgate 已做）。
只验证：每个步骤声称的变更是否在代码中真实存在。

## 诊断报告
{diagnosis}

## 步骤清单
{workflow_steps}

## Hardgate 报告（机器检查结果，已完成）
{hardgate_report}

## 注意事项
- Hardgate 已完成：smoke test、unit test、diff scope、prompt 变更检测、type check
- 你不需要重复这些检查，只需验证代码逻辑是否兑现每个步骤的承诺
- 如果提供了设计文档（MADS composite 工单），额外验证：实现是否符合设计文档的接口契约和架构决策

## 检查流程

### Step 1: 逐步骤验证
对步骤清单中的每个步骤：
1. 读取该步骤的 affected_files，检查修改是否符合 content 描述
2. 执行该步骤的 verification 验证命令（如可执行）
3. 给出代码证据（文件名+行号+代码片段）
4. 判定 pass 或 fail（fail 必须说明具体原因和差异）
**警告**：禁止将诊断报告的描述直接当作验证证据。证据只能来自 `git diff` 输出和实际文件内容。

### Step 2: 副作用扫描
检查 `git diff HEAD~1` 中是否有步骤清单 affected_files 并集之外的文件变动。

### Step 3: 综合判定
- 全部步骤 pass → ACCEPT
- 任一步骤 fail → PARTIAL_REJECT（通过的步骤有价值，会被锁定保留）
- 严重逻辑错误或副作用 → REJECT（全量重做）

## 输出格式

### 逐步骤验证
对每个步骤：
- step_id + content
- 代码证据
- pass 或 fail + 原因

### 副作用扫描
（超范围改动，无则写「无超范围改动」）

### 综合判定
（基于逐步验证结果，给出判定理由）

## 控制块（必须在报告最后输出）

<step_verdicts>
[
  {{"step_id": "step_001", "verdict": "pass"}},
  {{"step_id": "step_002", "verdict": "fail", "reason": "具体失败原因"}}
]
</step_verdicts>

<qa_verdict>
<result>PASS 或 PARTIAL_REJECT 或 REJECT</result>
<signals_verified>通过的步骤数</signals_verified>
<signals_failed>失败的步骤数</signals_failed>
<reason>一句话判定理由</reason>
</qa_verdict>

注意：控制块是机器解析的唯一依据，必须与你的分析结论一致。只输出上述格式的报告 + 控制块。

════════════════════════════════════════════════════════════════════
## 强制响应终结模板（必须作为回复的最后一段完整输出）

你的回复**必须**以下述 XML 块收尾，所有字段都要填入实际值。**缺少或格式错误直接判为 REJECT 并触发完整重试**。

<qa_verdict>
<result>PASS</result>
<signals_verified>3</signals_verified>
<signals_failed>0</signals_failed>
<reason>一句话判定理由</reason>
</qa_verdict>

`<result>` 仅允许 `PASS`/`PARTIAL_REJECT`/`REJECT` 三个值之一。
════════════════════════════════════════════════════════════════════"""


# ══════════════════════════════════════════════════════════════════════
#  Pipeline stages
# ══════════════════════════════════════════════════════════════════════

async def triage_signal(router, signal: dict) -> dict | None:
    """Classify a raw signal as bug/feature/noise.

    Returns dict with type, phenomenon, severity, or None on failure.
    """
    import json
    signal_text = json.dumps(signal, ensure_ascii=False, indent=2)
    prompt = TRIAGE_PROMPT.format(signal=signal_text)

    from agent.infra.models import LLMConfig
    llm_config = LLMConfig(provider="claude-cli", model="sonnet", timeout_seconds=60)
    result = await router.run(prompt=prompt, llm_config=llm_config)

    if result.is_error:
        log.warning("Triage failed: %s", result.text[:200])
        return None

    parsed = _parse_json_response(result.text)
    if not isinstance(parsed, dict):
        log.warning("Triage returned non-dict: %s", result.text[:200])
        return None

    # Validate and default complexity field (backward compatible)
    valid_complexity = {"atomic", "composite"}
    if parsed.get("complexity") not in valid_complexity:
        log.debug("Triage missing/invalid complexity, defaulting to atomic")
        parsed["complexity"] = "atomic"

    return parsed


async def diagnose_ticket(router, ticket_info: str) -> str:
    """Run Opus investigator on a ticket. Returns diagnosis report text."""
    prompt = INVESTIGATOR_PROMPT.format(ticket_info=ticket_info)
    return await _run_agent(
        router, role="investigator", model="opus",
        prompt=f"请诊断以下工单：\n\n{ticket_info}",
        system_prompt=prompt,
    )


def _strip_task_analysis(diagnosis: str) -> str:
    """Remove <task-analysis>...</task-analysis> block — only §6-10 go to Fixer."""
    return _re_xml.sub(r"<task-analysis>.*?</task-analysis>\s*", "", diagnosis, flags=_re_xml.DOTALL)


def _extract_affected_files_block(diagnosis: str) -> str:
    """Extract affected_files list as a standalone constraint block for prompt front-loading."""
    from agent.jobs.hardgate import parse_affected_files
    files = parse_affected_files(diagnosis)
    if not files:
        return ""
    listing = "\n".join(f"- {f}" for f in files)
    return (
        f"## ⚠️ 允许修改的文件（硬边界，最高优先级）\n"
        f"你 **只能** 修改以下文件，修改任何其他文件将导致工单被自动拒绝：\n{listing}\n"
        f"---\n"
    )


async def fix_ticket(router, diagnosis: str, ticket_id: str,
                     golden_data: str = "", workdir: str | None = None,
                     reject_feedback: str = "",
                     provider: str = "sonnet",
                     workflow: 'TicketWorkflow | None' = None) -> str:
    """Run implementer (Sonnet or Codex). Returns fix report text."""
    gd = golden_data or "无"
    diag = _strip_task_analysis(diagnosis)

    # Build workflow steps section
    prompt_template = IMPLEMENTER_PROMPT
    if workflow and workflow.steps:
        wf_text = _format_workflow_for_prompt(workflow)
        retry_section = ""
        if reject_feedback:
            from agent.infra.models import StepStatus
            locked = [s for s in workflow.steps if s.status == StepStatus.LOCKED]
            failed = [s for s in workflow.steps if s.status == StepStatus.FAILED]
            locked_text = "\n".join(f"- {s.id}: {s.content} (files: {s.affected_files})" for s in locked) or "（无）"
            failed_text = "\n".join(f"- {s.id}: {s.content} — 失败原因: {s.qa_reason}" for s in failed) or "（无）"
            reject_round = sum(1 for s in workflow.steps if s.qa_verdict) // max(len(workflow.steps), 1)
            retry_section = IMPLEMENTER_RETRY_SECTION.format(
                reject_round=reject_round + 1,
                locked_steps=locked_text,
                failed_steps=failed_text,
                qa_feedback=reject_feedback[:2000],
            )
    else:
        prompt_template = IMPLEMENTER_PROMPT_LEGACY
        retry_section = ""
        if reject_feedback:
            diag += f"\n\n## 上次修复被拒绝的原因（务必避免重蹈覆辙）\n{reject_feedback}"

    af_block = _extract_affected_files_block(diagnosis)
    if workflow and workflow.steps:
        prompt = af_block + prompt_template.format(
            diagnosis=diag, ticket_id=ticket_id, golden_data=gd,
            workflow_steps=wf_text, retry_section=retry_section)
    else:
        prompt = af_block + prompt_template.format(
            diagnosis=diag, ticket_id=ticket_id, golden_data=gd,
            retry_section=retry_section)

    if provider == "codex":
        combined = f"{prompt}\n\n---\n\n请基于以上诊断报告和步骤清单实施修复。直接修改文件，不要请求确认。"
        result = await _run_codex(combined, workdir=workdir)
        if result.startswith("[ERROR"):
            log.warning("Codex failed (%s), falling back to Sonnet",
                        result[:80])
            return await _run_agent(
                router, role="implementer", model="sonnet",
                prompt=f"请按步骤清单实施修复：\n\n{diag}",
                system_prompt=prompt, workdir=workdir,
            )
        return result

    return await _run_agent(
        router, role="implementer", model="sonnet",
        prompt=f"请按步骤清单实施修复：\n\n{diag}",
        system_prompt=prompt,
        workdir=workdir,
    )


async def qa_review(router, diagnosis: str, golden_data: str = "",
                    workdir: str | None = None,
                    hardgate_report: str = "",
                    design_doc: str = "",
                    workflow: 'TicketWorkflow | None' = None) -> str:
    """Run Opus gatekeeper. Returns QA report text."""
    gd = golden_data or "无"
    hr = hardgate_report or "（不可用）"
    dd = ""
    if design_doc:
        dd = f"\n\n## 设计文档（MADS composite 工单）\n{design_doc[:5000]}"

    if workflow and workflow.steps:
        wf_text = _format_workflow_for_prompt(workflow)
    else:
        wf_text = "（未提供步骤清单 — 按诊断报告整体验证）"

    prompt = GATEKEEPER_PROMPT.format(
        diagnosis=diagnosis, golden_data=gd,
        hardgate_report=hr, workflow_steps=wf_text) + dd
    result = await _run_agent(
        router, role="gatekeeper", model="opus",
        prompt="请按步骤清单逐步验证当前分支最新提交。",
        system_prompt=prompt,
        workdir=workdir,
    )
    if "<qa_verdict>" not in result:
        log.warning("QA report missing <qa_verdict>, retrying once with explicit reminder")
        result = await _run_agent(
            router, role="gatekeeper", model="opus",
            prompt="请按步骤清单逐步验证当前分支最新提交。**重要：报告末尾必须输出完整的 <qa_verdict> 控制块，否则视为无效。**",
            system_prompt=prompt,
            workdir=workdir,
        )
    return result


# ══════════════════════════════════════════════════════════════════════
#  Notification (MAQS-specific wrapper)
# ══════════════════════════════════════════════════════════════════════

async def _notify(dispatcher, color: str, message: str, open_id: str = "",
                  dm_color: str = "", dm_message: str = ""):
    """MAQS notification — delivery chat only (notifier bot)."""
    await _notify_mads(dispatcher, color, message, header="MAQS")


class MaqsTicketProcessor:
    def __init__(self, router, dispatcher, app_token, table_id, *,
                 notify_open_id: str = "", merge_queue=None):
        self.router = router
        self.dispatcher = dispatcher
        self.app_token = app_token
        self.table_id = table_id
        self.notify_open_id = notify_open_id
        self.merge_queue = merge_queue

    async def process(self, record_id, ticket, *, skip_diagnosis=False):
        ctx = TicketContext(
            record_id=record_id,
            ticket=ticket,
            ticket_id=ticket.get("title", record_id[:8]),
            severity=ticket.get("severity", "P1"),
            ticket_type=ticket.get("type", ticket.get("ticket_type", "bug")),
            card_mid=ticket.get("status_card_mid") or None,
            golden_data=ticket.get("golden_data", "") or "",
            notify_open_id=self.notify_open_id,
        )
        await self._phase_diagnosis(ctx, skip_diagnosis)
        return ctx

    async def _phase_diagnosis(self, ctx, skip):
        if skip:
            ctx.diagnosis = ctx.ticket.get("diagnosis", "")
            ctx.complexity = _parse_complexity(ctx.diagnosis)
            log.info("[MAQS] Skipping diagnosis for %s (pre-contracted)", ctx.ticket_id)
            return

        if ctx.card_mid:
            await _update_status_card(
                self.dispatcher, ctx.card_mid, ctx.record_id[:8], ctx.ticket_id,
                "queued", ctx.severity, ctx.ticket_type,
                {
                    "diagnosing": "pending", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                },
                workflow=ctx.workflow,
            )
        else:
            ctx.card_mid = await _send_status_card(
                self.dispatcher, ctx.record_id[:8], ctx.ticket_id,
                "queued", ctx.severity, ctx.ticket_type,
                {
                    "diagnosing": "pending", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                },
                workflow=ctx.workflow,
            )
            if ctx.card_mid:
                await _bitable_update(self.app_token, self.table_id, ctx.record_id,
                                      {"status_card_mid": ctx.card_mid})

        await _bitable_update(self.app_token, self.table_id, ctx.record_id,
                              {"status": "diagnosing"})
        if ctx.card_mid:
            await _update_status_card(
                self.dispatcher, ctx.card_mid, ctx.record_id[:8], ctx.ticket_id,
                "diagnosing", ctx.severity, ctx.ticket_type,
                {
                    "diagnosing": "running", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                },
                workflow=ctx.workflow,
            )
        else:
            ctx.card_mid = await _send_status_card(
                self.dispatcher, ctx.record_id[:8], ctx.ticket_id,
                "diagnosing", ctx.severity, ctx.ticket_type,
                {
                    "diagnosing": "running", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                },
                workflow=ctx.workflow,
            )
            if ctx.card_mid:
                await _bitable_update(self.app_token, self.table_id, ctx.record_id,
                                      {"status_card_mid": ctx.card_mid})

        ticket_info = (
            f"标题: {ctx.ticket.get('title', 'N/A')}\n"
            f"现象: {ctx.ticket.get('phenomenon', 'N/A')}\n"
            f"来源: {ctx.ticket.get('source', 'N/A')}\n"
            f"严重度: {ctx.ticket.get('severity', 'N/A')}\n"
        )
        if ctx.golden_data:
            ticket_info += f"金标准验证数据: {ctx.golden_data}\n"
        if ctx.ticket.get("qa_report") and ctx.ticket.get("qa_verdict") == "REJECT":
            ticket_info += f"\n上次 QA 拒绝原因:\n{ctx.ticket['qa_report']}\n"

        ctx.diagnosis = await _run_phase_with_timeout(
            LoopPhase.DIAGNOSING, diagnose_ticket(self.router, ticket_info), ctx.ticket_id)

        if ctx.diagnosis.startswith("[ERROR]"):
            log.error("Diagnosis failed for %s: %s", ctx.ticket_id, ctx.diagnosis[:200])
            await _bitable_update(self.app_token, self.table_id, ctx.record_id, {
                "status": "stalled",
                "diagnosis": ctx.diagnosis,
                "needs_human": True,
            })
            if ctx.card_mid:
                await _update_status_card(
                    self.dispatcher, ctx.card_mid, ctx.record_id[:8], ctx.ticket_id,
                    "diagnosing", ctx.severity, ctx.ticket_type,
                    {
                        "diagnosing": "failed", "fixing": "pending",
                        "testing": "pending", "reviewing": "pending",
                    },
                    workflow=ctx.workflow,
                )
            await _notify(self.dispatcher, "orange",
                          f"MAQS 诊断失败: {ctx.ticket_id}\n需要人工介入。",
                          ctx.notify_open_id)
            return

        if _is_garbage_diagnosis(ctx.diagnosis):
            log.error("Diagnosis garbage for %s: %s", ctx.ticket_id, ctx.diagnosis[:200])
            await _bitable_update(self.app_token, self.table_id, ctx.record_id, {
                "status": "stalled",
                "diagnosis": ctx.diagnosis,
                "needs_human": True,
            })
            if ctx.card_mid:
                await _update_status_card(
                    self.dispatcher, ctx.card_mid, ctx.record_id[:8], ctx.ticket_id,
                    "diagnosing", ctx.severity, ctx.ticket_type,
                    {
                        "diagnosing": "failed", "fixing": "pending",
                        "testing": "pending", "reviewing": "pending",
                    },
                    workflow=ctx.workflow,
                )
            await _notify(self.dispatcher, "orange",
                          f"MAQS 诊断失败: {ctx.ticket_id}\n需要人工介入。",
                          ctx.notify_open_id)
            return

        write_artifact(ctx.record_id[:8], "diagnosis.md", ctx.diagnosis)

        await _bitable_update(self.app_token, self.table_id, ctx.record_id, {
            "status": "diagnosed",
            "diagnosis": ctx.diagnosis,
        })
        if ctx.card_mid:
            await _update_status_card(
                self.dispatcher, ctx.card_mid, ctx.record_id[:8], ctx.ticket_id,
                "diagnosing", ctx.severity, ctx.ticket_type,
                {
                    "diagnosing": "done", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                },
                workflow=ctx.workflow,
            )
        ctx.complexity = _parse_complexity(ctx.diagnosis)


# ══════════════════════════════════════════════════════════════════════
#  Ticket processing orchestrator
# ══════════════════════════════════════════════════════════════════════

async def process_ticket(router, dispatcher, app_token: str, table_id: str,
                          record_id: str, ticket: dict, notify_open_id: str = "",
                          skip_diagnosis: bool = False, merge_queue: MergeQueue | None = None):
    """Process a single ticket through the full MAQS pipeline.

    Args:
        router: LLMRouter instance
        dispatcher: Notifier dispatcher for user notifications
        app_token: Bitable app token
        table_id: Bitable table ID
        record_id: Bitable record ID for this ticket
        ticket: dict with ticket fields (title, phenomenon, severity, etc.)
        notify_open_id: user open_id for notifications
        skip_diagnosis: If True, skip Phase 1 and use existing diagnosis from
            ticket dict. Used by MADS contract flow to preserve the contracted
            diagnosis.
    """
    ticket_id = ticket.get("title", record_id[:8])

    # ── Status card tracking ──
    _severity = ticket.get("severity", "P1")
    _title = ticket.get("title", record_id[:8])
    _ticket_type = ticket.get("type", ticket.get("ticket_type", "bug"))
    _card_mid: str | None = ticket.get("status_card_mid") or None

    _workflow_ref = [None]  # mutable ref for closure access

    async def _update_card(phase: str, phases_status: dict) -> None:
        nonlocal _card_mid
        wf = _workflow_ref[0]
        if _card_mid:
            await _update_status_card(dispatcher, _card_mid, record_id[:8],
                                       _title, phase, _severity, _ticket_type, phases_status,
                                       workflow=wf)
        else:
            _card_mid = await _send_status_card(dispatcher, record_id[:8], _title,
                                                 phase, _severity, _ticket_type, phases_status,
                                                 workflow=wf)
            if _card_mid:
                await _bitable_update(app_token, table_id, record_id,
                                       {"status_card_mid": _card_mid})

    wt_path = None
    branch = None

    try:
        golden_data = ticket.get("golden_data", "") or ""

        # ── Phase 1: Diagnosis (Opus) ──
        if skip_diagnosis:
            # MADS contract flow: diagnosis already done and contracted
            diagnosis = ticket.get("diagnosis", "")
            if _is_garbage_diagnosis(diagnosis):
                log.error("Diagnosis garbage (skip path) for %s: %s", ticket_id, diagnosis[:200])
                await _update_card("queued", {
                    "diagnosing": "pending", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                })
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "diagnosis": diagnosis,
                    "needs_human": True,
                })
                await _update_card("diagnosing", {
                    "diagnosing": "failed", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS 诊断失败: {ticket_id}\n需要人工介入。",
                              notify_open_id)
                return
            log.info("[MAQS] Skipping diagnosis for %s (pre-contracted)", ticket_id)
            if _is_garbage_diagnosis(diagnosis):
                log.error("Diagnosis garbage for %s: %s", ticket_id, diagnosis[:200])
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "diagnosis": diagnosis,
                    "needs_human": True,
                })
                await _update_card("diagnosing", {
                    "diagnosing": "failed", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS 诊断失败: {ticket_id}\n需要人工介入。",
                              notify_open_id)
                return
        else:
            await _update_card("queued", {
                "diagnosing": "pending", "fixing": "pending",
                "testing": "pending", "reviewing": "pending",
            })
            await _bitable_update(app_token, table_id, record_id,
                                   {"status": "diagnosing"})
            await _update_card("diagnosing", {
                "diagnosing": "running", "fixing": "pending",
                "testing": "pending", "reviewing": "pending",
            })

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

            diagnosis = await _run_phase_with_timeout(
                LoopPhase.DIAGNOSING, diagnose_ticket(router, ticket_info), ticket_id)

            if diagnosis.startswith("[ERROR]"):
                log.error("Diagnosis failed for %s: %s", ticket_id, diagnosis[:200])
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "diagnosis": diagnosis,
                    "needs_human": True,
                })
                await _update_card("diagnosing", {
                    "diagnosing": "failed", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS 诊断失败: {ticket_id}\n需要人工介入。",
                              notify_open_id)
                return

            if _is_garbage_diagnosis(diagnosis):
                log.error("Diagnosis garbage for %s: %s", ticket_id, diagnosis[:200])
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "diagnosis": diagnosis,
                    "needs_human": True,
                })
                await _update_card("diagnosing", {
                    "diagnosing": "failed", "fixing": "pending",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS 诊断失败: {ticket_id}\n需要人工介入。",
                              notify_open_id)
                return

            # Single retry if <complexity> tag is absent from diagnosis.
            # After retry, fail closed: stall the ticket rather than silently
            # defaulting to L3 — downstream routing (scope guard, merge path)
            # assumes a valid complexity, and a silent fallback lets bad
            # diagnoses leak into the wrong track.
            _cx_re = r"<complexity>\s*L[1-5]\s*</complexity>"
            if not _re_xml.search(_cx_re, diagnosis, _re_xml.IGNORECASE):
                log.warning("MAQS %s: <complexity> tag missing, retrying diagnosis once", ticket_id)
                retry_diag = await _run_phase_with_timeout(
                    LoopPhase.DIAGNOSING, diagnose_ticket(router, ticket_info), ticket_id)
                if not retry_diag.startswith("[ERROR]") and not _is_garbage_diagnosis(retry_diag):
                    diagnosis = retry_diag
                if not _re_xml.search(_cx_re, diagnosis, _re_xml.IGNORECASE):
                    log.error("MAQS %s: <complexity> tag still absent after retry — stalling",
                              ticket_id)
                    await _bitable_update(app_token, table_id, record_id, {
                        "status": "stalled",
                        "needs_human": True,
                        "diagnosis": diagnosis + "\n\n[PIPELINE] Stalled: <complexity> missing after retry.",
                    })
                    await _notify(dispatcher, "orange",
                                  f"MAQS {ticket_id}: 诊断缺失 <complexity> 标签（已重试），需要人工介入。",
                                  notify_open_id)
                    return

            # Single retry if workflow_steps is empty — mirrors <complexity> retry pattern.
            if not _parse_workflow_steps(diagnosis):
                log.warning("MAQS %s: workflow_steps empty, retrying diagnosis once", ticket_id)
                retry_diag = await _run_phase_with_timeout(
                    LoopPhase.DIAGNOSING, diagnose_ticket(router, ticket_info), ticket_id)
                if not retry_diag.startswith("[ERROR]") and not _is_garbage_diagnosis(retry_diag):
                    diagnosis = retry_diag
                if not _parse_workflow_steps(diagnosis):
                    log.error("MAQS %s: workflow_steps still empty after retry", ticket_id)

            # Write diagnosis artifact
            write_artifact(record_id[:8], "diagnosis.md", diagnosis)

            await _bitable_update(app_token, table_id, record_id, {
                "status": "diagnosed",
                "diagnosis": diagnosis,
            })

        # Parse diagnosis for atomic split (1:N)
        _sub_tickets = _parse_atomic_split(diagnosis)
        if _sub_tickets:
            log.info("MAQS atomic split detected for %s: %s", ticket_id, _sub_tickets)
            # TODO: auto-create sub-tickets in Bitable (P2)

        # ── Parse complexity for downstream routing ──
        complexity = _parse_complexity(diagnosis)

        # ── Parse workflow steps (TodoWrite integration) ──
        raw_steps = _parse_workflow_steps(diagnosis)
        workflow = None
        if raw_steps:
            workflow = _build_workflow(raw_steps)
            _workflow_ref[0] = workflow
            log.info("MAQS %s: workflow built with %d steps", ticket_id, len(workflow.steps))
            write_artifact(record_id[:8], "workflow.json", _serialize_workflow(workflow))
            await _bitable_update(app_token, table_id, record_id, {
                "workflow_json": _serialize_workflow(workflow),
            })
        else:
            log.info("MAQS %s: no workflow_steps in diagnosis, using legacy contract path",
                     ticket_id)

        # Restore workflow from previous run (retry scenario)
        if not workflow and ticket.get("workflow_json"):
            try:
                import json as _json
                wf_data = _json.loads(ticket["workflow_json"])
                from agent.infra.models import ticket_workflow_from_dict
                workflow = ticket_workflow_from_dict(wf_data)
                _workflow_ref[0] = workflow
                log.info("MAQS %s: restored workflow from previous run (%s)",
                         ticket_id, workflow.progress)
            except Exception as e:
                log.warning("MAQS %s: failed to restore workflow: %s", ticket_id, e)

        # ── Phase 1.5: Contract ──
        # When workflow is available, steps = contract (skip LLM contract generation)
        if workflow:
            contract_text = (
                "### Workflow Contract (auto-generated from diagnosis steps)\n\n"
                + _format_workflow_for_prompt(workflow)
                + "\n\n---\n*Steps-based contract (replaces Track A/B).*"
            )
            contract_track = "steps"
        else:
            user_impact = _parse_user_impact(diagnosis)
            contract_track = _select_contract_track(complexity, user_impact)
            log.info("MAQS %s: contract track=%s (complexity=%s, user_impact=%s)",
                     ticket_id, contract_track, complexity, user_impact)
            if contract_track == "B":
                contract_text = _contract_track_b(diagnosis)
            else:
                contract_text = await _contract_track_a(router, diagnosis)

        write_artifact(record_id[:8], "contract.md", contract_text)
        await _bitable_update(app_token, table_id, record_id, {
            "contract": contract_text,
            "contract_track": contract_track,
        })

        # ── Pre-fix gate: fail early if diagnosis lacks both affected_files and workflow ──
        from agent.jobs.hardgate import parse_affected_files as _parse_af
        _af = _parse_af(diagnosis)
        if not _af and not workflow:
            log.error("MAQS %s: diagnosis incomplete — no affected_files and no workflow_steps",
                      ticket_id)
            await _bitable_update(app_token, table_id, record_id, {
                "status": "FAILED_DIAGNOSIS_INCOMPLETE",
                "needs_human": True,
            })
            await _update_card("diagnosing", {
                "diagnosing": "failed", "fixing": "pending",
                "testing": "pending", "reviewing": "pending",
            })
            await _notify(dispatcher, "orange",
                          f"MAQS {ticket_id}: 诊断不完整，缺少 affected_files 和 workflow_steps\n"
                          f"请重新诊断后再推进。",
                          notify_open_id)
            return

        # ── Phase 2: Fix (Sonnet/Codex) in isolated worktree ──
        severity = ticket.get("severity", "P1")
        impl_provider = "codex" if severity in ("P2", "P3") else "sonnet"
        branch = f"fix/MAQS-{record_id[:8]}"
        wt_path = await _worktree_create(branch)
        if not wt_path:
            await _bitable_update(app_token, table_id, record_id,
                                   {"status": "stalled"})
            return

        await _bitable_update(app_token, table_id, record_id, {
            "status": "fixing",
            "fix_branch": branch,
        })
        await _update_card("fixing", {
            "diagnosing": "done", "fixing": "running",
            "testing": "pending", "reviewing": "pending",
        })

        reject_fb = ""
        if ticket.get("qa_verdict") == "REJECT" and ticket.get("qa_report"):
            reject_fb = ticket["qa_report"]
        fix_timeout = 1200 if complexity in ("L3", "L4") else 0

        # Guard: worktree may have been cleaned up between creation and this point
        if not os.path.exists(wt_path):
            log.warning("MAQS %s: worktree missing before fix — rebuilding %s", ticket_id, wt_path)
            wt_path = await _worktree_create(branch)
            if not wt_path:
                await _bitable_update(app_token, table_id, record_id,
                                       {"status": "stalled", "needs_human": True})
                await _update_card("fixing", {
                    "diagnosing": "done", "fixing": "failed",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS {ticket_id}: worktree 丢失且重建失败，需要人工介入。",
                              notify_open_id)
                return

        # Route L3/L4 with 2+ affected files to decomposed execution
        use_decomposed = complexity in ("L3", "L4") and len(_af) >= 2

        if use_decomposed:
            from agent.jobs.mads.fix_decomposed import fix_decomposed
            log.info("MAQS decomposed fix for %s (%d files, complexity=%s)",
                     ticket_id, len(_af), complexity)
            fix_report, decomposed_ok = await _run_phase_with_timeout(
                LoopPhase.FIXING,
                fix_decomposed(router, diagnosis, record_id[:8],
                               workdir=wt_path, branch=branch,
                               golden_data=golden_data,
                               reject_feedback=reject_fb,
                               provider=impl_provider),
                ticket_id, timeout_override=fix_timeout)

            if not decomposed_ok:
                log.warning("MAQS decomposed fix failed for %s — needs human intervention",
                            ticket_id)
                await _worktree_remove(wt_path, branch)
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "fix_plan": fix_report or "Decomposed fix failed",
                    "needs_human": True,
                })
                await _update_card("fixing", {
                    "diagnosing": "done", "fixing": "failed",
                    "testing": "pending", "reviewing": "pending",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS {ticket_id}: L{complexity[1]} 分解执行失败\n"
                              f"需要人工介入。",
                              notify_open_id)
                return
        else:
            log.info("MAQS using %s as implementer for %s (severity=%s, complexity=%s, timeout=%s)",
                     impl_provider, ticket_id, severity, complexity,
                     fix_timeout or "default")
            fix_report = await _run_phase_with_timeout(
                LoopPhase.FIXING,
                fix_ticket(router, diagnosis, record_id[:8],
                           golden_data=golden_data, workdir=wt_path,
                           reject_feedback=reject_fb,
                           provider=impl_provider,
                           workflow=workflow),
                ticket_id, timeout_override=fix_timeout)

        if not use_decomposed and (fix_report.startswith("[ERROR]") or _is_limit_banner(fix_report)):
            log.error("Fix failed for %s: %s", ticket_id, fix_report[:200])
            await _worktree_remove(wt_path, branch)
            await _bitable_update(app_token, table_id, record_id, {
                "status": "stalled",
                "fix_plan": fix_report,
            })
            await _update_card("fixing", {
                "diagnosing": "done", "fixing": "failed",
                "testing": "pending", "reviewing": "pending",
            })
            return

        # Write fix artifact
        write_artifact(record_id[:8], "fix_report.md", fix_report)

        # ── Apply step results to workflow ──
        if workflow:
            step_results = _parse_step_results(fix_report)
            if step_results:
                _apply_step_results(workflow, step_results)
                log.info("MAQS %s: step results applied — %s", ticket_id, workflow.progress)
                write_artifact(record_id[:8], "workflow.json", _serialize_workflow(workflow))
                await _bitable_update(app_token, table_id, record_id, {
                    "workflow_json": _serialize_workflow(workflow),
                })
                await _update_card("fixing", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "pending", "reviewing": "pending",
                })

        # ── Handle fix-time signals ──────────────────────────────────
        # Discovery: fixer found scope-external issues → create new tickets.
        # Each discovery goes through a premise-validation gate before
        # persistence; hallucinated claims (non-existent files, out-of-bounds
        # line numbers, design-doc-echo keywords) are rejected with structured
        # logs so hallucination rate is measurable.
        discoveries = _parse_discoveries(fix_report)
        if discoveries:
            log.info("MAQS %s: fixer discovered %d scope-external issue(s)",
                     ticket_id, len(discoveries))
            for disc in discoveries:
                if _is_gitignored_artifact(disc):
                    log.info("MAQS %s: skip gitignored artifact discovery: %s",
                             ticket_id, disc[:200])
                    continue
                accept, reason = _validate_discovery_premise(disc, wt_path)
                if not accept:
                    log.warning(
                        "MAQS %s: discovery_rejected reason=%s text=%s",
                        ticket_id, reason, disc[:200],
                    )
                    continue
                try:
                    await _bitable_add(app_token, table_id, {
                        "title": f"[Discovery] {disc[:80]}",
                        "phenomenon": disc,
                        "source": "maqs_discovery",
                        "source_ref": f"discovered during fix of {ticket_id} ({record_id[:8]})",
                        "severity": "P2",
                        "status": "open",
                        "type": "bug",
                    })
                except Exception as e:
                    log.warning("Failed to create discovery ticket: %s", e)

        # Challenge: fixer believes diagnosis is wrong → re-diagnose once
        ctrl = _parse_control_signal(fix_report)
        if ctrl and ctrl[0] == "CHALLENGE_DIAGNOSIS":
            challenge_count = ticket.get("challenge_count", 0)
            if challenge_count < 1:
                log.warning("MAQS %s: fixer challenges diagnosis: %s",
                            ticket_id, ctrl[1][:200])
                await _worktree_remove(wt_path, branch)
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "open",
                    "challenge_count": challenge_count + 1,
                    "diagnosis": "",  # clear stale diagnosis
                    "challenge_reason": ctrl[1][:500],
                })
                await _notify_mads(dispatcher, "yellow",
                    f"MAQS {ticket_id}: Fixer 挑战诊断，触发重诊断\n"
                    f"理由: {ctrl[1][:200]}",
                    notify_open_id)
                return  # will be re-diagnosed on next pipeline tick
            else:
                log.warning("MAQS %s: challenge already used, proceeding with fix",
                            ticket_id)

        # Commit the fix on the branch (in the worktree)
        # Decomposed path already committed per-sub-task
        if not use_decomposed:
            _commit_msg = _parse_commit_message(
                fix_report, f"fix(MAQS-{record_id[:8]}): auto-fix from MAQS pipeline")
            rc, _, _ = await _git_in(wt_path, "commit", "-m", _commit_msg)
            if rc != 0:
                rc2, diff_out, _ = await _git_in(wt_path, "diff", "--stat")
                if not diff_out:
                    # No file changes — retry with Sonnet if original was Codex
                    if impl_provider == "codex":
                        log.warning("No changes from Codex for %s, retrying with Sonnet", ticket_id)
                        fix_report = await _run_phase_with_timeout(
                            LoopPhase.FIXING,
                            fix_ticket(router, diagnosis, record_id[:8],
                                       golden_data=golden_data, workdir=wt_path,
                                       reject_feedback=reject_fb,
                                       provider="sonnet"),
                            ticket_id, timeout_override=fix_timeout)
                        # Banner text (Claude usage limit) bypasses [ERROR] check; empty-diff at L1232 is the real safety net.
                        if not fix_report.startswith("[ERROR]"):
                            write_artifact(record_id[:8], "fix_report.md", fix_report)
                            _commit_msg = _parse_commit_message(
                                fix_report, f"fix(MAQS-{record_id[:8]}): auto-fix from MAQS pipeline")
                            rc, _, _ = await _git_in(wt_path, "commit", "-m", _commit_msg)
                            if rc != 0:
                                _, diff_out2, _ = await _git_in(wt_path, "diff", "--stat")
                                if diff_out2:
                                    await _git_in(wt_path, "add", "-A")
                                    rc, _, _ = await _git_in(wt_path, "commit", "-m", _commit_msg)
                        # Check again after Sonnet retry
                        _, diff_final, _ = await _git_in(wt_path, "diff", "HEAD~1", "--stat")
                        rc_check, _, _ = await _git_in(wt_path, "log", "--oneline", "-1")
                        if rc != 0 or not diff_final:
                            log.warning("Sonnet retry also produced no changes for %s", ticket_id)
                            await _worktree_remove(wt_path, branch)
                            await _bitable_update(app_token, table_id, record_id, {
                                "status": "stalled",
                                "fix_plan": fix_report + "\n\n[No changes produced after Codex→Sonnet retry]",
                            })
                            await _update_card("fixing", {
                                "diagnosing": "done", "fixing": "failed",
                                "testing": "pending", "reviewing": "pending",
                            })
                            return
                    else:
                        log.warning("No changes to commit for %s", ticket_id)
                        await _worktree_remove(wt_path, branch)
                        await _bitable_update(app_token, table_id, record_id, {
                            "status": "stalled",
                            "fix_plan": fix_report + "\n\n[No changes produced]",
                        })
                        await _update_card("fixing", {
                            "diagnosing": "done", "fixing": "failed",
                            "testing": "pending", "reviewing": "pending",
                        })
                        return
                else:
                    await _git_in(wt_path, "add", "-A")
                    rc, _, _ = await _git_in(wt_path, "commit", "-m", _commit_msg)

        # Guard: verify fix branch actually diverged from dev. If every commit
        # attempt in the fix path silently failed (e.g., pre-commit hook),
        # HEAD stays at dev's tip and `rev-parse HEAD` would record dev's
        # commit as fix_commit_id — a cross-ticket pollution bug.
        _, _count, _ = await _git_in(wt_path, "rev-list", "--count", "dev..HEAD")
        if not _count.strip() or _count.strip() == "0":
            log.error("MAQS %s: fix branch %s has no commits beyond dev — "
                      "no fix landed, bailing out", ticket_id, branch)
            await _worktree_remove(wt_path, branch)
            await _bitable_update(app_token, table_id, record_id, {
                "status": "stalled",
                "fix_plan": fix_report + "\n\n[Fix branch has no commits beyond dev — commit likely failed silently]",
                "needs_human": True,
            })
            await _update_card("fixing", {
                "diagnosing": "done", "fixing": "failed",
                "testing": "pending", "reviewing": "pending",
            })
            await _notify(dispatcher, "orange",
                          f"MAQS Fix failed: {ticket_id}\n"
                          f"fix 分支 {branch} 与 dev 无差异，commit 可能因 hook 拒绝而失败。",
                          notify_open_id)
            return

        _, commit_hash, _ = await _git_in(wt_path, "rev-parse", "--short", "HEAD")

        await _bitable_update(app_token, table_id, record_id, {
            "status": "testing",
            "fix_plan": fix_report,
            "fix_commit_id": commit_hash,
        })
        await _update_card("testing", {
            "diagnosing": "done", "fixing": "done",
            "testing": "running", "reviewing": "pending",
        })

        # ── Phase 2.5: Hardgate (deterministic, no LLM) ──
        from agent.jobs.hardgate import Hardgate, parse_affected_files
        allowed_files = parse_affected_files(diagnosis)
        _locked = workflow.locked_files if workflow else None
        hardgate_result = await Hardgate().run(branch, allowed_files, workdir=wt_path,
                                                locked_files=_locked)
        write_artifact(record_id[:8], "hardgate_report.md", str(hardgate_result.details))

        if not hardgate_result.passed:
            log.warning("MAQS Hardgate REJECT for %s: %s", ticket_id, hardgate_result.details)
            reject_count = int(ticket.get("reject_count") or 0) + 1
            await _worktree_remove(wt_path, branch)

            # Scope underestimate: re-classify instead of blind retry
            diff_scope = hardgate_result.details.get("diff_scope", {})
            if diff_scope.get("scope_underestimate") and reject_count <= 1:
                related = diff_scope.get("related_files", [])
                log.info("MAQS %s: scope underestimate detected, related files: %s",
                         ticket_id, related)
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "open",
                    "reject_count": reject_count,
                    "diagnosis": "",  # clear stale diagnosis to force re-diagnosis
                    "qa_report": (f"[Hardgate REJECT — scope underestimate]\n"
                                  f"Fixer needed files beyond affected_files. "
                                  f"Related: {related}. Re-diagnosing with broader scope."),
                    "qa_verdict": "REJECT",
                })
                await _update_card("testing", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "failed", "reviewing": "pending",
                })
                await _notify(dispatcher, "yellow",
                              f"MAQS {ticket_id}: Scope 低估，触发重诊断\n"
                              f"Fixer 需要修改 affected_files 外的关联文件: {related}",
                              notify_open_id)
                return  # re-diagnosed on next tick

            await _bitable_update(app_token, table_id, record_id, {
                "status": "stalled" if reject_count >= MAX_REJECT else "open",
                "reject_count": reject_count,
                "needs_human": reject_count >= MAX_REJECT,
                "qa_report": f"[Hardgate REJECT]\n{hardgate_result.details}",
                "qa_verdict": "REJECT",
            })
            await _update_card("testing", {
                "diagnosing": "done", "fixing": "done",
                "testing": "failed", "reviewing": "pending",
            })
            failed_checks = []
            for check_name, check_result in hardgate_result.details.items():
                if not isinstance(check_result, dict) or check_result.get("ok"):
                    continue
                output = (check_result.get("output") or "").strip()
                if check_name == "diff_scope":
                    allowed = sorted(check_result.get("allowed") or [])
                    actual = sorted(check_result.get("actual") or [])
                    out_of_scope = sorted(check_result.get("out_of_scope") or [])
                    locked_violation = check_result.get("locked_violation")
                    detail = f"allowed={allowed} actual={actual}"
                    if out_of_scope:
                        detail += f" out_of_scope={out_of_scope}"
                    if locked_violation:
                        detail += f" locked_violation={sorted(locked_violation)}"
                    failed_checks.append(f"  - {check_name}: {detail}")
                else:
                    snippet = output[:300].replace("\n", " ") if output else "(no output)"
                    failed_checks.append(f"  - {check_name}: {snippet}")
            body = "\n".join(failed_checks) if failed_checks else "(no details)"
            await _notify(dispatcher, "orange",
                          f"MAQS Hardgate 未通过: {ticket_id}\n{body}",
                          notify_open_id)
            return

        await _bitable_update(app_token, table_id, record_id, {"status": "reviewing"})
        await _update_card("reviewing", {
            "diagnosing": "done", "fixing": "done",
            "testing": "done", "reviewing": "running",
        })

        # ── Phase 3: QA (Opus) ──
        _, _patch_out, _ = await _git_in(wt_path, "diff", f"dev...{branch}")
        qa_timeout = 1200 if len(_patch_out or "") > 3000 else 0
        qa_report = await _run_phase_with_timeout(
            LoopPhase.REVIEWING,
            qa_review(router, diagnosis, golden_data=golden_data,
                      workdir=wt_path, workflow=workflow),
            ticket_id, timeout_override=qa_timeout)
        verdict = _parse_qa_verdict(qa_report)

        # Apply step-level verdicts to workflow
        if workflow:
            step_verdicts = _parse_step_verdicts(qa_report)
            if step_verdicts:
                _apply_step_verdicts(workflow, step_verdicts)
                passed = sum(1 for s in workflow.steps if s.qa_verdict == "pass")
                failed = sum(1 for s in workflow.steps if s.qa_verdict == "fail")
                log.info("MAQS %s: step verdicts — %d pass, %d fail", ticket_id, passed, failed)
                write_artifact(record_id[:8], "workflow.json", _serialize_workflow(workflow))
                await _bitable_update(app_token, table_id, record_id, {
                    "workflow_json": _serialize_workflow(workflow),
                })
                await _update_card("reviewing", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "done", "reviewing": "running",
                })

            # Normalize verdict: PARTIAL_REJECT → treat as REJECT for flow control,
            # but with step-level granularity for retry
            if verdict == "PARTIAL_REJECT":
                verdict = "REJECT"

        # Write QA artifact
        write_artifact(record_id[:8], "qa_report.md", qa_report)

        await _bitable_update(app_token, table_id, record_id, {
            "qa_report": qa_report,
            "qa_verdict": verdict,
        })

        if verdict == "PASS":
            # ── Visual QA gate (optional, contract-driven) ──
            from agent.jobs.mads.visual_qa import parse_visual_qa_spec, visual_qa_gate
            vqa_spec = parse_visual_qa_spec(ticket)
            if vqa_spec:
                log.info("[MAQS] Visual QA required for %s, running gate", ticket_id)
                await _bitable_update(app_token, table_id, record_id,
                                       {"status": "visual_qa"})
                vqa_verdict, vqa_report = await _run_phase_with_timeout(
                    LoopPhase.VISUAL_QA,
                    visual_qa_gate(router, record_id[:8], ticket, vqa_spec),
                    ticket_id)
                write_artifact(record_id[:8], "visual_qa_report.md", vqa_report)
                await _bitable_update(app_token, table_id, record_id, {
                    "visual_qa_report": vqa_report[:2000],
                    "visual_qa_verdict": vqa_verdict,
                })
                if vqa_verdict != "PASS":
                    log.info("[MAQS] Visual QA REJECT for %s", ticket_id)
                    reject_count = int(ticket.get("reject_count") or 0) + 1
                    await _worktree_remove(wt_path, branch)
                    await _bitable_update(app_token, table_id, record_id, {
                        "status": "stalled" if reject_count >= MAX_REJECT else "open",
                        "reject_count": reject_count,
                        "needs_human": reject_count >= MAX_REJECT,
                    })
                    await _update_card("reviewing", {
                        "diagnosing": "done", "fixing": "done",
                        "testing": "done", "reviewing": "failed",
                    })
                    await _notify(dispatcher, "orange",
                                  f"MAQS Visual QA 未通过: {ticket_id}\n"
                                  f"{vqa_report[:300]}",
                                  notify_open_id)
                    return

            if merge_queue is not None:
                _severity_map = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
                _priority = _severity_map.get(ticket.get("severity", "P2"), 2)
                await merge_queue.enqueue(
                    MergeRequest.make(branch, wt_path, _priority, ticket_id))
                _req = await merge_queue.process_next()
                _merge_ok = _req is not None and await _worktree_merge_to_dev(
                    _req.wt_path, _req.branch)
            else:
                _merge_ok = await _worktree_merge_to_dev(wt_path, branch)
            if _merge_ok:
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "closed",
                    "workflow_json": _serialize_workflow(workflow) if workflow else (ticket.get("workflow_json") or ""),
                })
                await _update_card("closed", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "done", "reviewing": "done",
                })
                # Notify only if no status card (standalone MAQS tickets)
                # When status card exists, it already shows the final state
                if not _card_mid:
                    _diag = ticket.get("diagnosis") or ""
                    _exp_summary = _parse_experience_summary(_diag)
                    _exp_line = f"\n体验恢复: {_exp_summary}" if _exp_summary else ""

                    await _notify(dispatcher, "green",
                                  f"MAQS 修复完成: {ticket_id}\n"
                                  f"commit: `{commit_hash}`\n分支已合并到 dev。{_exp_line}",
                                  notify_open_id)
                # Log to autonomy audit trail
                try:
                    from agent.infra.autonomy import AutonomousAction, log_action
                    await log_action(AutonomousAction(
                        level=1,
                        category="maqs:auto_fix",
                        summary=f"MAQS-{record_id[:8]}: {ticket_id}",
                        detail=f"QA PASS, merged {branch} to dev. commit: {commit_hash}",
                        commit_sha=commit_hash,
                        rollback_cmd=f"git revert {commit_hash}",
                        source="maqs",
                    ))
                except Exception as audit_err:
                    log.warning("Autonomy audit log failed: %s", audit_err)
            else:
                log.error("Merge to dev failed for %s (branch kept: %s)",
                          ticket_id, branch)
                await _worktree_remove(wt_path)
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "needs_human": True,
                    "fix_branch": branch,
                })
                await _update_card("reviewing", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "done", "reviewing": "failed",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS Merge 失败: {ticket_id}\n"
                              f"QA 已通过但合并到 dev 分支失败，需要人工介入。\n"
                              f"fix 分支已保留: `{branch}` (commit {commit_hash})",
                              notify_open_id)
        else:
            reject_count = int(ticket.get("reject_count") or 0) + 1
            await _worktree_remove(wt_path, branch)

            # Lock passed steps before retry (partial retry optimization)
            workflow_json = ""
            if workflow:
                workflow.lock_passed()
                locked_count = sum(1 for s in workflow.steps
                                   if s.status.name == "LOCKED")
                failed_count = len(workflow.failed_steps)
                log.info("MAQS %s: locking %d passed steps, %d failed for retry",
                         ticket_id, locked_count, failed_count)
                workflow_json = _serialize_workflow(workflow)
                write_artifact(record_id[:8], "workflow.json", workflow_json)

            await _update_card("reviewing", {
                "diagnosing": "done", "fixing": "done",
                "testing": "done", "reviewing": "failed",
            })
            if reject_count >= MAX_REJECT:
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "stalled",
                    "reject_count": reject_count,
                    "needs_human": True,
                    "workflow_json": workflow_json,
                })
                await _update_card("stalled", {
                    "diagnosing": "done", "fixing": "done",
                    "testing": "done", "reviewing": "failed",
                })
                await _notify(dispatcher, "orange",
                              f"MAQS 连续 {reject_count} 次 QA REJECT: {ticket_id}\n"
                              f"需要人工介入。",
                              notify_open_id,
                              dm_color="red",
                              dm_message=f"工单 {ticket_id} 需要人工介入\n原因: 连续 {reject_count} 次 QA REJECT")
            else:
                await _bitable_update(app_token, table_id, record_id, {
                    "status": "open",
                    "reject_count": reject_count,
                    "workflow_json": workflow_json,
                })
                log.info("MAQS REJECT #%d for %s, will retry (partial: %s)",
                         reject_count, ticket_id,
                         f"{len(workflow.failed_steps)} failed steps" if workflow else "full")
                ticket["reject_count"] = reject_count
                ticket["qa_report"] = qa_report
                ticket["qa_verdict"] = "REJECT"
                ticket["status_card_mid"] = _card_mid
                ticket["workflow_json"] = workflow_json
                await process_ticket(router, dispatcher, app_token, table_id,
                                      record_id, ticket, notify_open_id)

    except asyncio.TimeoutError:
        log.error("MAQS phase timeout for %s", ticket_id)
        if wt_path:
            await _worktree_remove(wt_path, branch)
        await _bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "needs_human": True,
            "diagnosis": "[Phase timeout] Pipeline phase exceeded time limit",
        })
        try:
            await _notify(dispatcher, "orange",
                          f"MAQS 阶段超时: {ticket_id}\n阶段超过时间限制，需要人工介入。",
                          notify_open_id)
        except Exception:
            log.warning("Failed to notify user about phase timeout for %s", ticket_id)
    except Exception as e:
        log.exception("MAQS pipeline error for %s: %s", ticket_id, e)
        if wt_path:
            await _worktree_remove(wt_path, branch)
        await _bitable_update(app_token, table_id, record_id, {
            "status": "stalled",
            "diagnosis": f"[Pipeline error] {e}",
        })
        try:
            await _notify(dispatcher, "red",
                          f"MAQS pipeline 异常: {ticket_id}\n{e}",
                          notify_open_id)
        except Exception:
            log.warning("Failed to notify user about pipeline error for %s", ticket_id)


# ══════════════════════════════════════════════════════════════════════
#  Signal intake — create tickets from raw signals
# ══════════════════════════════════════════════════════════════════════

async def intake_signal(router, app_token: str, table_id: str,
                         signal: dict) -> str | None:
    """Process a raw signal: triage → create ticket if actionable.

    Args:
        signal: dict with keys: source, phenomenon, source_ref, raw_data

    Returns:
        record_id if ticket created, None otherwise.
    """
    triage = await triage_signal(router, signal)
    if not triage:
        log.warning("Triage failed for signal: %s", signal.get("source_ref", "?"))
        return None

    signal_type = triage.get("type", "noise")
    if signal_type == "noise":
        log.info("Signal triaged as noise, skipping: %s", triage.get("reason", ""))
        return None

    import json
    fields = {
        "title": triage.get("phenomenon", signal.get("phenomenon", ""))[:100],
        "type": signal_type,
        "complexity": triage.get("complexity", "atomic"),
        "source": signal.get("source", "unknown"),
        "source_ref": signal.get("source_ref", ""),
        "phenomenon": triage.get("phenomenon", signal.get("phenomenon", "")),
        "severity": triage.get("severity", "P2"),
        "status": "open",
        "reject_count": 0,
        "parent_signal": signal.get("signal_id", ""),
    }

    record_id = await _bitable_add(app_token, table_id, fields)
    if record_id:
        log.info("MAQS ticket created: %s (type=%s, severity=%s)",
                 fields["title"], signal_type, fields["severity"])
    return record_id


# ══════════════════════════════════════════════════════════════════════
#  Main entry point (cron handler)
# ══════════════════════════════════════════════════════════════════════

async def run_maqs_pipeline(router, dispatcher, config: dict):
    """Main MAQS pipeline entry. Called by scheduler.

    Processes all open tickets in priority order, up to MAX_PARALLEL concurrent.
    """
    app_token = config.get("bitable_app_token", "")
    table_id = config.get("bitable_table_id", "")

    if not app_token or not table_id:
        log.warning("MAQS skipped: bitable not configured")
        return

    # Reset any orphaned intermediate-state tickets before processing
    reset = await _reset_stale_intermediate_tickets(app_token, table_id)
    if reset:
        log.info("MAQS: reset %d orphaned ticket(s) to open", reset)

    tickets = await _bitable_query(
        app_token, table_id,
        filter_str='CurrentValue.[status]="open"',
        limit=MAX_PARALLEL * 2,
    )

    if not tickets:
        log.info("MAQS: no open tickets")
        return

    log.info("MAQS: found %d open tickets", len(tickets))

    severity_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    tickets.sort(key=lambda t: severity_order.get(
        t.get("fields", {}).get("severity", "P3"), 3))

    notify_open_id = config.get("notify_open_id", "")

    # Clean up any stale worktrees from crashed pipelines
    await _worktree_cleanup_stale()

    # Drain any merge requests left over from a previous crashed pipeline run
    merge_queue = MergeQueue()
    await merge_queue.load()
    while len(merge_queue) > 0:
        _leftover = await merge_queue.process_next()
        if _leftover:
            log.info("MAQS: replaying leftover merge [P%d] %s",
                     _leftover.priority, _leftover.ticket_id)
            await _worktree_merge_to_dev(_leftover.wt_path, _leftover.branch)

    processed = 0
    for ticket_data in tickets[:MAX_PARALLEL]:
        record_id = ticket_data.get("record_id", "")
        fields = ticket_data.get("fields", {})
        if not record_id:
            continue

        # Skip sub-tickets from MADS decomposition — they route through
        # MADS contract negotiation, not directly through MAQS.
        if fields.get("parent_ticket"):
            log.debug("MAQS: skipping sub-ticket %s (has parent_ticket, routed via MADS)",
                      fields.get("title", "?"))
            continue

        log.info("MAQS processing: %s (severity=%s)",
                 fields.get("title", "?"), fields.get("severity", "?"))
        skip_dx = fields.get("status") == "fixing" and bool(fields.get("diagnosis"))
        await process_ticket(
            router, dispatcher, app_token, table_id,
            record_id, fields, notify_open_id,
            skip_diagnosis=skip_dx,
            merge_queue=merge_queue,
        )
        processed += 1

    log.info("MAQS pipeline complete: %d/%d tickets processed", processed, len(tickets))

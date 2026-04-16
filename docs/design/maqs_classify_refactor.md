# [Audit-P2] maqs.py 类化重构 — 设计文档

## 1. 熵减目标（Entropy Reduction Target）

### 当前复杂度

`agent/jobs/maqs.py` 1643 行、30 个顶层函数、0 个类。核心问题不是"文件太长"，而是**状态流隐式、依赖不透明、单元不可测**：

| 维度 | 当前状态 | 目标状态 |
|------|---------|---------|
| **调用路径** | 30 个平铺函数，`process_ticket` 660 行巨型编排，内部 7 条分支路径（diagnosis/contract/fix/hardgate/QA/visual_qa/merge） | 3 个类各管一件事：解析(Parser)、阶段执行(PhaseRunner)、编排(TicketProcessor) |
| **依赖注入** | `app_token`/`table_id`/`router`/`dispatcher` 在 8 个 async 函数间逐层传递，同一组参数重复出现 15+ 次 | 构造函数一次注入，实例方法零参数传递 |
| **测试基座** | 测试需 mock 15 个 helpers 别名 + subprocess；单个 phase 无法脱离完整 pipeline 测试 | 每个 phase 可通过注入 stub 的类实例独立测试 |
| **状态管理** | `_card_mid`/`_workflow_ref` 用 mutable closure 传递，`wt_path`/`branch` 跨 600 行作用域 | 显式 `TicketContext` dataclass 携带所有 ticket-scoped 状态 |
| **解析函数** | 19 个 `_parse_*` 散落文件各处，无统一入口 | `MaqsParser` 类聚合所有解析逻辑 |

量化指标：**依赖参数传递从 ~60 次减少到 ~6 次（构造+方法签名），解析函数散落度从 19@file-scope 收敛到 1@class-scope**。

### 不消除什么

- **不改 helpers 层**：`bitable_*`/`git_*`/`run_agent` 等 shared helpers 不在此次 scope 内
- **不改 prompt 模板**：4 个 `*_PROMPT` 字符串保持原位（可考虑后续独立文件）
- **不改 MADS pipeline.py 的调用方式**：保持 `from agent.jobs.maqs import process_ticket, fix_ticket, qa_review, diagnose_ticket` 可用

---

## 2. 体验不变性契约（Experience Invariance）

以下行为**必须保持完全一致**，作为回归验收标准：

### 2.1 Cron 入口行为

| 场景 | 输入 | 预期行为 |
|------|------|---------|
| `run_maqs_pipeline(router, dispatcher, config)` | 含 `bitable_app_token`/`bitable_table_id` 的 config dict | 查询 open/fixing tickets → 按 severity 排序 → 最多并发处理 MAX_PARALLEL=5 个 → 跳过有 parent_ticket 的子工单 |
| 无 bitable 配置 | config 缺 token 或 table_id | 静默 warning 返回 |
| 孤儿工单 | 启动时存在 intermediate 状态工单 | 自动 reset 回 open |

### 2.2 MADS 集成点

| 调用方 | 调用签名 | 不变性 |
|--------|---------|--------|
| `mads/pipeline.py:325` | `process_ticket(router, dispatcher, app_token, table_id, record_id, fields, notify_open_id, skip_diagnosis=True, merge_queue=...)` | 参数列表、skip_diagnosis 语义、merge_queue 可选性 |
| `mads/pipeline.py:325` | `fix_ticket(router, diagnosis, ticket_id, ...)` | 签名和返回值（fix report text） |
| `mads/pipeline.py:325` | `qa_review(router, diagnosis, ...)` | 签名和返回值（QA report text） |
| `mads/pipeline.py:325` | `diagnose_ticket(router, ticket_info)` | 签名和返回值（diagnosis text） |
| `mads/pipeline.py:422` | `_parse_complexity(diagnosis)`, `_parse_workflow_steps(diagnosis)` | 签名和返回值 |

### 2.3 Pipeline 状态机

完整状态转换链必须不变：
```
open → diagnosing → diagnosed → fixing → testing → reviewing → closed
                                                              → stalled (任何阶段失败)
                                              → visual_qa → closed/stalled
```

### 2.4 Bitable 字段写入时机

每个 phase transition 对应的 `_bitable_update` 调用（字段集合、写入时机）不变。

### 2.5 通知行为

- 诊断失败 → orange 通知
- Hardgate reject → orange/yellow 通知
- QA reject 达 MAX_REJECT → orange + red DM
- 修复完成 → green 通知（仅无 status card 时）
- Phase timeout → orange 通知
- Pipeline 异常 → red 通知

### 2.6 测试回归清单

现有 6 个测试文件必须全部通过（import path 可变但行为不变）：
- `tests/unit/test_maqs_xml_parsers.py` — 所有 `_parse_*` 函数
- `tests/unit/test_maqs_hardgate_rollback.py` — hardgate 回滚逻辑
- `tests/unit/test_maqs_dm_notify.py` — DM 通知格式
- `tests/unit/test_maqs_crash_notify.py` — 崩溃恢复通知
- `tests/unit/test_workflow_steps.py` — workflow 步骤解析和状态
- `tests/test_maqs_stale_recovery.py` — 孤儿工单恢复

---

## 3. 用户场景

### 场景 1：Cron 触发常规 bug 修复

MAQS cron 定时触发 `run_maqs_pipeline`。3 个 P2 open tickets 在 Bitable 中。Pipeline 按 severity 排序处理，每个 ticket 走完 diagnosis → contract → fix → hardgate → QA → merge 全流程。

**验收标准**：
- Pipeline 入口调用方式不变
- 3 个 ticket 按 P2 优先级处理，最终 2 个 closed + 1 个 stalled
- 飞书状态卡片实时更新每个 phase 状态
- autonomy audit log 记录每个成功 merge 的 commit

### 场景 2：MADS 集成 — pre-contracted ticket

MADS `pipeline.py` 完成 composite ticket 的 contract 协商后，调用 `process_ticket(..., skip_diagnosis=True)` 跳过诊断直接进入 fix 阶段。

**验收标准**：
- `from agent.jobs.maqs import process_ticket` 可用且签名不变
- skip_diagnosis=True 时跳过 Phase 1，使用 ticket dict 中的 diagnosis
- 后续 fix/hardgate/QA/merge 流程正常

### 场景 3：开发者为单个 phase 编写单元测试

开发者想测试 QA review phase 的 step verdict 解析逻辑，不需要启动完整 pipeline、mock Bitable、或创建 git worktree。

**验收标准**：
- 可实例化 `MaqsParser` 并直接调用 `parse_step_verdicts(qa_report_text)` → 返回 list[dict]
- 可实例化 `MaqsPhaseRunner(router=mock_router)` 并调用 `qa_review(diagnosis, ...)` → 返回 QA report text
- 无需任何 Bitable/git/dispatcher 依赖

### 场景 4：Retry 循环 — QA REJECT 后部分重试

第一轮 QA 返回 PARTIAL_REJECT，2/4 步骤通过。Pipeline 锁定通过步骤，仅对失败步骤重试 fix+QA。

**验收标准**：
- workflow.lock_passed() 锁定已通过步骤
- 重试时 locked 步骤的 affected_files 不可修改
- 递归调用 process_ticket 时 ticket dict 携带 reject_count/qa_report/workflow_json

### 场景 5：Discovery 衍生工单创建

Fixer 在修复过程中发现 scope 外问题，通过 `<discovery>` 报告。Pipeline 自动创建 P2 衍生工单。

**验收标准**：
- `_parse_discoveries(fix_report)` 提取发现列表
- 每个 discovery 创建独立 Bitable record（title, phenomenon, source_ticket）
- 不影响当前 ticket 的处理流程

### 边缘情况

- Codex 无产出 → fallback Sonnet → 再无产出 → stalled
- Challenge diagnosis → 清除 diagnosis 重新诊断（仅限 1 次）
- Scope underestimate → hardgate 检测到后 reset + re-diagnose（仅限 1 次）
- Workflow 恢复 — 从 Bitable workflow_json 字段还原上次 run 的 workflow 状态
- Pipeline 异常 → worktree 清理 + stalled + 通知

---

## 4. 设计方案

### 4.1 架构方向

三个类 + 模块级兼容函数，拆分到同一文件（不新建模块，避免改动 import path 带来的风险）：

```
agent/jobs/maqs.py（重构后）
├── MaqsParser              — 纯函数聚合，所有 XML/JSON 解析逻辑
├── MaqsPhaseRunner         — 单 phase 执行器（diagnosis/contract/fix/QA）
├── MaqsTicketProcessor     — ticket 生命周期编排（process_ticket 逻辑）
├── 模块级兼容函数           — 保持原有 import path 可用
└── Prompt 模板             — 保持原位不动
```

### 4.2 核心抽象

#### `TicketContext` — ticket-scoped 状态容器

```python
@dataclass
class TicketContext:
    """Carries all mutable state for a single ticket processing run."""
    record_id: str
    ticket: dict
    ticket_id: str           # ticket.get("title", record_id[:8])
    severity: str
    diagnosis: str = ""
    workflow: TicketWorkflow | None = None
    contract_text: str = ""
    contract_track: str = ""
    complexity: str = ""
    wt_path: str | None = None
    branch: str | None = None
    card_mid: str | None = None
    golden_data: str = ""
    fix_report: str = ""
    commit_hash: str = ""
```

当前 `process_ticket` 内的 `_card_mid`（mutable closure）、`_workflow_ref[0]`（mutable list hack）、`wt_path`/`branch`（跨 600 行作用域）全部收入此 dataclass。消除隐式状态传递。

#### `MaqsParser` — 解析逻辑聚合

聚合 19 个 `_parse_*` / `_format_*` / `_serialize_*` / `_build_*` 函数为一个类的静态方法或类方法。关键价值：

1. **发现性**：`MaqsParser.` tab 补全即可看到所有解析能力
2. **可测试性**：纯函数，零外部依赖，直接实例化测试
3. **命名空间隔离**：消除 `_parse_commit_message` vs `_parse_complexity` 等无前缀区分的平铺

```python
class MaqsParser:
    """Stateless parser for MAQS XML/JSON blocks in agent output."""

    @staticmethod
    def commit_message(fix_report: str, fallback: str) -> str: ...

    @staticmethod
    def atomic_split(diagnosis: str) -> list[str]: ...

    @staticmethod
    def experience_summary(diagnosis: str) -> str: ...

    @staticmethod
    def complexity(diagnosis: str) -> str: ...

    @staticmethod
    def user_impact(diagnosis: str) -> str: ...

    @staticmethod
    def workflow_steps(diagnosis: str) -> list[dict]: ...

    @staticmethod
    def step_results(fix_report: str) -> list[dict]: ...

    @staticmethod
    def step_verdicts(qa_report: str) -> list[dict]: ...

    @staticmethod
    def discoveries(text: str) -> list[str]: ...

    @staticmethod
    def control_signal(text: str) -> tuple[str, str] | None: ...

    # Workflow construction
    @staticmethod
    def build_workflow(raw_steps: list[dict]) -> TicketWorkflow: ...

    @staticmethod
    def format_workflow_for_prompt(workflow: TicketWorkflow) -> str: ...

    @staticmethod
    def apply_step_results(workflow: TicketWorkflow, results: list[dict]): ...

    @staticmethod
    def apply_step_verdicts(workflow: TicketWorkflow, verdicts: list[dict]): ...

    @staticmethod
    def serialize_workflow(workflow: TicketWorkflow) -> str: ...

    # Diagnosis utilities
    @staticmethod
    def strip_task_analysis(diagnosis: str) -> str: ...

    @staticmethod
    def extract_affected_files_block(diagnosis: str) -> str: ...

    # Contract
    @staticmethod
    def select_contract_track(complexity: str, user_impact: str) -> str: ...

    @staticmethod
    def contract_track_b(diagnosis: str) -> str: ...
```

#### `MaqsPhaseRunner` — 单 phase 执行

封装 LLM 调用，依赖通过构造函数注入。每个 phase 方法独立可测。

```python
class MaqsPhaseRunner:
    """Executes individual MAQS pipeline phases. Dependencies injected at construction."""

    def __init__(self, router, dispatcher=None):
        self.router = router
        self.dispatcher = dispatcher

    async def triage(self, signal: dict) -> dict | None: ...
    async def diagnose(self, ticket_info: str) -> str: ...
    async def contract_track_a(self, diagnosis: str) -> str: ...
    async def fix(self, diagnosis: str, ticket_id: str, *,
                  golden_data: str = "", workdir: str | None = None,
                  reject_feedback: str = "", provider: str = "sonnet",
                  workflow: TicketWorkflow | None = None) -> str: ...
    async def qa_review(self, diagnosis: str, *,
                        golden_data: str = "", workdir: str | None = None,
                        hardgate_report: str = "", design_doc: str = "",
                        workflow: TicketWorkflow | None = None) -> str: ...
    async def notify(self, color: str, message: str,
                     open_id: str = "", **kwargs): ...
```

#### `MaqsTicketProcessor` — 生命周期编排

将 `process_ticket` 的 660 行逻辑拆分为明确的 phase 方法：

```python
class MaqsTicketProcessor:
    """Orchestrates a ticket through the full MAQS pipeline."""

    def __init__(self, router, dispatcher, app_token: str, table_id: str, *,
                 notify_open_id: str = "", merge_queue: MergeQueue | None = None):
        self.app_token = app_token
        self.table_id = table_id
        self.notify_open_id = notify_open_id
        self.merge_queue = merge_queue
        self.phases = MaqsPhaseRunner(router, dispatcher)
        self.parser = MaqsParser
        self._dispatcher = dispatcher

    async def process(self, record_id: str, ticket: dict, *,
                      skip_diagnosis: bool = False) -> None:
        """Full pipeline: diagnosis → contract → fix → hardgate → QA → merge."""
        ctx = TicketContext(...)
        try:
            await self._phase_diagnosis(ctx, skip_diagnosis)
            await self._phase_contract(ctx)
            await self._phase_fix(ctx)
            await self._phase_hardgate(ctx)
            await self._phase_qa(ctx)
            await self._phase_merge(ctx)
        except asyncio.TimeoutError:
            await self._handle_timeout(ctx)
        except Exception as e:
            await self._handle_error(ctx, e)

    # ── Private phase methods ──
    async def _phase_diagnosis(self, ctx: TicketContext, skip: bool) -> None: ...
    async def _phase_contract(self, ctx: TicketContext) -> None: ...
    async def _phase_fix(self, ctx: TicketContext) -> None: ...
    async def _phase_hardgate(self, ctx: TicketContext) -> None: ...
    async def _phase_qa(self, ctx: TicketContext) -> None: ...
    async def _phase_merge(self, ctx: TicketContext) -> None: ...

    # ── Helpers ──
    async def _update_card(self, ctx: TicketContext, phase: str, phases_status: dict): ...
    async def _update_bitable(self, ctx: TicketContext, fields: dict): ...
    async def _handle_timeout(self, ctx: TicketContext): ...
    async def _handle_error(self, ctx: TicketContext, error: Exception): ...
    async def _handle_reject(self, ctx: TicketContext, qa_report: str, verdict: str): ...
    async def _cleanup_worktree(self, ctx: TicketContext): ...
```

### 4.3 兼容层 — 模块级函数保留

所有现有 import path 通过薄包装保持可用。MADS pipeline.py 和测试代码**零改动**：

```python
# ── Module-level compatibility wrappers ──
# These maintain backward-compatible import paths.
# New code should use the class-based API directly.

async def process_ticket(router, dispatcher, app_token, table_id,
                          record_id, ticket, notify_open_id="",
                          skip_diagnosis=False, merge_queue=None):
    proc = MaqsTicketProcessor(
        router, dispatcher, app_token, table_id,
        notify_open_id=notify_open_id, merge_queue=merge_queue)
    await proc.process(record_id, ticket, skip_diagnosis=skip_diagnosis)

async def triage_signal(router, signal):
    return await MaqsPhaseRunner(router).triage(signal)

async def diagnose_ticket(router, ticket_info):
    return await MaqsPhaseRunner(router).diagnose(ticket_info)

async def fix_ticket(router, diagnosis, ticket_id, **kwargs):
    return await MaqsPhaseRunner(router).fix(diagnosis, ticket_id, **kwargs)

async def qa_review(router, diagnosis, **kwargs):
    return await MaqsPhaseRunner(router).qa_review(diagnosis, **kwargs)

async def intake_signal(router, app_token, table_id, signal):
    # Thin wrapper, delegates to triage + bitable_add
    ...

async def run_maqs_pipeline(router, dispatcher, config):
    # Unchanged entry point logic
    ...

# Parser compatibility — existing tests import _parse_* directly
_parse_commit_message = MaqsParser.commit_message
_parse_atomic_split = MaqsParser.atomic_split
_parse_experience_summary = MaqsParser.experience_summary
_parse_complexity = MaqsParser.complexity
_parse_user_impact = MaqsParser.user_impact
_parse_workflow_steps = MaqsParser.workflow_steps
_parse_step_results = MaqsParser.step_results
_parse_step_verdicts = MaqsParser.step_verdicts
_parse_discoveries = MaqsParser.discoveries
_parse_control_signal = MaqsParser.control_signal
_build_workflow = MaqsParser.build_workflow
_format_workflow_for_prompt = MaqsParser.format_workflow_for_prompt
_apply_step_results = MaqsParser.apply_step_results
_apply_step_verdicts = MaqsParser.apply_step_verdicts
_serialize_workflow = MaqsParser.serialize_workflow
_select_contract_track = MaqsParser.select_contract_track
_contract_track_b = MaqsParser.contract_track_b
_strip_task_analysis = MaqsParser.strip_task_analysis
_extract_affected_files_block = MaqsParser.extract_affected_files_block
```

### 4.4 Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 文件拆分 | 不拆分，保持单文件 | 避免大面积 import path 变更，兼容层成本远低于多文件方案 |
| Parser 类 | 纯 staticmethod 聚合 | 解析逻辑无状态，不需要实例化开销；但聚合提供命名空间和发现性 |
| TicketContext | dataclass 而非 dict | 类型安全、IDE 补全、消除 `ticket.get("severity", "P1")` 式防御编码 |
| 兼容函数 | 别名而非 re-export | 别名是零成本的指针，测试代码无需改动 |

### 4.5 排除的方案

**方案 A：拆分为多文件模块**（`maqs/parser.py`, `maqs/phases.py`, `maqs/processor.py`）
- 排除原因：MADS pipeline.py 有 4 处 `from agent.jobs.maqs import ...`，test 有 40+ 处。多文件方案要么改 import path（大量下游改动），要么在 `__init__.py` re-export 一切（本质上仍是单文件 + 额外文件）。收益不值得风险。

**方案 B：保持函数式，仅做参数对象化**（引入 `MaqsConfig` 替代散参数）
- 排除原因：只解决"参数传递冗余"一个问题，不解决"phase 不可独立测试"和"状态流隐式"两个更大的问题。是止痛药不是手术。

**方案 C：将 process_ticket 拆分为 Pipeline 类 + Strategy Pattern（每个 phase 一个 strategy）**
- 排除原因：过度抽象。MAQS 只有一种 pipeline 拓扑，策略模式为不存在的变体预设了扩展点。"三行类似代码好过一个早产的抽象"。

### 4.6 AI/智能注入点

本次重构不引入新的 AI 注入点（已有 4 个 LLM agent 调用）。但类化后**解锁**的未来能力：

- **Phase 级 A/B 测试**：`MaqsPhaseRunner` 可被子类化，用不同 prompt/model 替换单个 phase，对比效果。当前无法实现因为 phase 逻辑内嵌在 process_ticket 中。
- **自适应 timeout**：`_run_phase_with_timeout` 当前用硬编码 dict。类化后可从历史工单 duration 统计动态计算 timeout，替代 PHASE_TIMEOUTS 常量。
- **Pipeline 插桩**：类方法可被 decorator 包裹，注入 phase-level 度量采集（duration、token usage、verdict），供 MAQS 质量仪表盘使用。

---

## 5. 接口契约

### 5.1 TicketContext

```python
# agent/jobs/maqs.py

@dataclass
class TicketContext:
    record_id: str
    ticket: dict                        # 原始 Bitable fields
    ticket_id: str                      # display name
    severity: str                       # P0-P3
    ticket_type: str = "bug"            # bug/feature/refactor
    diagnosis: str = ""
    workflow: TicketWorkflow | None = None
    contract_text: str = ""
    contract_track: str = ""
    complexity: str = ""
    wt_path: str | None = None
    branch: str | None = None
    card_mid: str | None = None
    golden_data: str = ""
    fix_report: str = ""
    commit_hash: str = ""
    notify_open_id: str = ""
```

### 5.2 MaqsParser

```python
class MaqsParser:
    # 所有方法均为 @staticmethod
    # 入参和返回类型与当前 _parse_* 函数完全一致
    # 详见 4.2 节签名列表

    @staticmethod
    def commit_message(fix_report: str, fallback: str) -> str: ...
    @staticmethod
    def atomic_split(diagnosis: str) -> list[str]: ...
    @staticmethod
    def experience_summary(diagnosis: str) -> str: ...
    @staticmethod
    def complexity(diagnosis: str) -> str: ...
    @staticmethod
    def user_impact(diagnosis: str) -> str: ...
    @staticmethod
    def workflow_steps(diagnosis: str) -> list[dict]: ...
    @staticmethod
    def step_results(fix_report: str) -> list[dict]: ...
    @staticmethod
    def step_verdicts(qa_report: str) -> list[dict]: ...
    @staticmethod
    def discoveries(text: str) -> list[str]: ...
    @staticmethod
    def control_signal(text: str) -> tuple[str, str] | None: ...
    @staticmethod
    def build_workflow(raw_steps: list[dict]) -> TicketWorkflow: ...
    @staticmethod
    def format_workflow_for_prompt(workflow: TicketWorkflow) -> str: ...
    @staticmethod
    def apply_step_results(workflow: TicketWorkflow, results: list[dict]) -> None: ...
    @staticmethod
    def apply_step_verdicts(workflow: TicketWorkflow, verdicts: list[dict]) -> None: ...
    @staticmethod
    def serialize_workflow(workflow: TicketWorkflow) -> str: ...
    @staticmethod
    def strip_task_analysis(diagnosis: str) -> str: ...
    @staticmethod
    def extract_affected_files_block(diagnosis: str) -> str: ...
    @staticmethod
    def select_contract_track(complexity: str, user_impact: str) -> str: ...
    @staticmethod
    def contract_track_b(diagnosis: str) -> str: ...
```

### 5.3 MaqsPhaseRunner

```python
class MaqsPhaseRunner:
    def __init__(self, router, dispatcher=None): ...

    async def triage(self, signal: dict) -> dict | None: ...
    async def diagnose(self, ticket_info: str) -> str: ...
    async def contract_track_a(self, diagnosis: str) -> str: ...
    async def fix(self, diagnosis: str, ticket_id: str, *,
                  golden_data: str = "", workdir: str | None = None,
                  reject_feedback: str = "", provider: str = "sonnet",
                  workflow: TicketWorkflow | None = None) -> str: ...
    async def qa_review(self, diagnosis: str, *,
                        golden_data: str = "", workdir: str | None = None,
                        hardgate_report: str = "", design_doc: str = "",
                        workflow: TicketWorkflow | None = None) -> str: ...
    async def notify(self, color: str, message: str,
                     open_id: str = "",
                     dm_color: str = "", dm_message: str = "") -> None: ...
```

### 5.4 MaqsTicketProcessor

```python
class MaqsTicketProcessor:
    def __init__(self, router, dispatcher, app_token: str, table_id: str, *,
                 notify_open_id: str = "",
                 merge_queue: MergeQueue | None = None): ...

    async def process(self, record_id: str, ticket: dict, *,
                      skip_diagnosis: bool = False) -> None: ...

    # Private — 不属于外部接口契约，但列出以说明内部结构
    async def _phase_diagnosis(self, ctx: TicketContext, skip: bool) -> None: ...
    async def _phase_contract(self, ctx: TicketContext) -> None: ...
    async def _phase_fix(self, ctx: TicketContext) -> None: ...
    async def _phase_hardgate(self, ctx: TicketContext) -> None: ...
    async def _phase_qa(self, ctx: TicketContext) -> None: ...
    async def _phase_merge(self, ctx: TicketContext) -> None: ...
    async def _handle_reject(self, ctx: TicketContext, qa_report: str, verdict: str) -> None: ...
    async def _update_card(self, ctx: TicketContext, phase: str, phases_status: dict) -> None: ...
    async def _update_bitable(self, ctx: TicketContext, fields: dict) -> None: ...
    async def _cleanup_worktree(self, ctx: TicketContext) -> None: ...
```

### 5.5 模块级兼容函数

签名与当前完全一致，内部委托给类实例。见 4.3 节。

### 5.6 外部依赖（不变）

```python
# helpers — 从 agent.jobs.mads.helpers import（现有 15 个别名全部保留）
from agent.jobs.mads.helpers import (
    bitable_add, bitable_query, bitable_update,
    git, git_in,
    notify, parse_json_response, parse_qa_verdict,
    run_agent, run_codex,
    send_status_card, update_status_card,
    worktree_cleanup_stale, worktree_create,
    worktree_merge_to_dev, worktree_remove,
    write_artifact,
)

# 条件 import（保持 lazy 模式）
# agent.jobs.hardgate.Hardgate, parse_affected_files
# agent.jobs.mads.fix_decomposed.fix_decomposed
# agent.jobs.mads.visual_qa.parse_visual_qa_spec, visual_qa_gate
# agent.infra.models.TicketStep, TicketWorkflow, StepStatus, ticket_workflow_from_dict
# agent.infra.autonomy.AutonomousAction, log_action
```

---

## 6. 初步分解

### Atom 1：提取 `MaqsParser` 类（~80 LOC）

**内容**：将 19 个 `_parse_*`/`_build_*`/`_format_*`/`_serialize_*`/`_select_*`/`_strip_*`/`_extract_*`/`_contract_track_b` 函数包装为 `MaqsParser` 的 `@staticmethod`。在文件末尾添加别名赋值。

**测试**：运行 `test_maqs_xml_parsers.py` + `test_workflow_steps.py`，确认所有现有 import path 仍可用。

**依赖**：无前置依赖。可并行启动。

### Atom 2：创建 `TicketContext` dataclass（~30 LOC）

**内容**：在 maqs.py 顶部（类定义区域）添加 `TicketContext` dataclass。此阶段不改 process_ticket 内部逻辑，仅定义数据结构。

**测试**：实例化 `TicketContext` 的 smoke test。

**依赖**：无前置依赖。可与 Atom 1 并行。

### Atom 3：提取 `MaqsPhaseRunner` 类（~120 LOC）

**内容**：将 `triage_signal`/`diagnose_ticket`/`fix_ticket`/`qa_review`/`_contract_track_a`/`_notify` 6 个 async 函数包装为 `MaqsPhaseRunner` 的实例方法。构造函数接受 `router` 和可选 `dispatcher`。添加模块级兼容函数。

**测试**：运行 `test_maqs_dm_notify.py` + `test_maqs_crash_notify.py`，确认通知逻辑不变。新增 1 个测试：直接实例化 `MaqsPhaseRunner(mock_router)` 调用 `diagnose()`。

**依赖**：Atom 1（Parser 方法在 fix/QA prompt 构造中被调用）。

### Atom 4：提取 `MaqsTicketProcessor` 类骨架（~60 LOC）

**内容**：创建 `MaqsTicketProcessor` 类，包含 `__init__` 和空的 `process` 方法。`process` 内部仅创建 `TicketContext` 并按顺序调用 `_phase_*` 占位方法（每个方法暂时 `pass`）。保持原 `process_ticket` 函数不变。

**测试**：实例化 `MaqsTicketProcessor` 的 smoke test。

**依赖**：Atom 2（TicketContext），Atom 3（MaqsPhaseRunner）。

### Atom 5：迁移 Phase 1 — diagnosis 逻辑到 `_phase_diagnosis`（~70 LOC）

**内容**：将 `process_ticket` 中 Phase 1 逻辑（lines 904-981）提取到 `MaqsTicketProcessor._phase_diagnosis`。操作 `ctx` 而非局部变量。

**测试**：运行 `test_maqs_stale_recovery.py`（间接验证）。Mock-based 测试 diagnosis phase 独立运行。

**依赖**：Atom 4。

### Atom 6：迁移 Phase 1.5+2 — contract + fix 逻辑到 `_phase_contract` 和 `_phase_fix`（~200 LOC）

**内容**：将 Phase 1.5（lines 996-1019）和 Phase 2（lines 1021-1227）提取到两个 phase 方法。这是最大的 atom，因为 fix 逻辑包含 Codex→Sonnet fallback、decomposed 路由、discovery 处理、challenge 处理、commit 逻辑。

**测试**：运行 `test_maqs_hardgate_rollback.py`（covers fix→hardgate 交互）。

**依赖**：Atom 5。

### Atom 7：迁移 Phase 2.5+3+merge — hardgate + QA + merge + reject 逻辑（~200 LOC）

**内容**：将 Phase 2.5（lines 1240-1293）、Phase 3（lines 1301-1338）、merge（lines 1341-1435）、reject 循环（lines 1436-1488）、error handling（lines 1490-1518）提取到对应 phase 方法。

**测试**：运行全部 6 个测试文件。

**依赖**：Atom 6。

### Atom 8：切换 `process_ticket` 兼容函数指向类（~20 LOC）

**内容**：将模块级 `process_ticket` 函数改为委托到 `MaqsTicketProcessor.process`。删除原有 `process_ticket` 函数体（此时已全部迁移到类中）。

**测试**：全部 6 个测试文件 + MADS pipeline import 验证（`python -c "from agent.jobs.maqs import process_ticket, fix_ticket, qa_review, diagnose_ticket, _parse_complexity, _parse_workflow_steps"`）。

**依赖**：Atom 7。

### Atom 9：更新 `run_maqs_pipeline` 和 `intake_signal` 使用类 API（~40 LOC）

**内容**：`run_maqs_pipeline` 内部改用 `MaqsTicketProcessor` 实例。`intake_signal` 改用 `MaqsPhaseRunner` 实例。`_reset_stale_intermediate_tickets` 可作为 `MaqsTicketProcessor` 的类方法或保持独立（它只依赖 Bitable）。

**测试**：`test_maqs_stale_recovery.py` + 全量回归。

**依赖**：Atom 8。

### 依赖图

```
Atom 1 (Parser) ─────┐
                      ├──→ Atom 3 (PhaseRunner) ──→ Atom 4 (Processor skeleton) ──→ Atom 5 → Atom 6 → Atom 7 → Atom 8 → Atom 9
Atom 2 (Context) ─────┘
```

**可并行项**：Atom 1 + Atom 2（无依赖交叉）。

---

## 7. 风险评估

### 风险 1：process_ticket 递归调用在 reject 场景下的状态传递

- **描述**：当前 `process_ticket` 在 QA REJECT 时递归调用自身（line 1487），ticket dict 携带 reject_count/qa_report/workflow_json/status_card_mid。类化后 `TicketContext` 的生命周期需要跨越递归边界——如果每次 `process()` 创建新 ctx，上一轮的 card_mid 和 workflow 状态会丢失。
- **可能性**：High
- **影响**：High（silent regression——retry 看似正常但丢失 locked step 信息）
- **缓解**：reject 递归改为显式循环（`while reject_count < MAX_REJECT`），在同一个 `TicketContext` 内完成重试，消除递归。这是比忠实复制递归更优的做法——递归是历史遗留，不是有意设计。

### 风险 2：MADS pipeline.py 对 private 函数的依赖

- **描述**：`mads/pipeline.py:422` 直接 import `_parse_complexity` 和 `_parse_workflow_steps`（下划线前缀 = private convention）。兼容别名能覆盖，但如果未来有人清理"unused aliases"会破坏 MADS。
- **可能性**：Medium
- **影响**：Medium（import error 在部署时立即可见）
- **缓解**：兼容别名上方添加注释 `# MADS pipeline.py depends on these aliases — do not remove`。长期可考虑 MADS 改为 import `MaqsParser.complexity`。

### 风险 3：Atom 6（fix phase）是最大的单次迁移，内含 6 条分支路径

- **描述**：Fix phase 包含 decomposed/monolithic 路由、Codex→Sonnet fallback、discovery 处理、challenge 处理、commit+retry 逻辑。是 process_ticket 中逻辑最密集的部分。一次性迁移可能引入难以定位的 regression。
- **可能性**：Medium
- **影响**：High（fix phase 是价值产出的核心环节）
- **缓解**：Atom 6 内部进一步拆分为 `_phase_fix_route()`（选择 decomposed/monolithic）、`_phase_fix_commit()`（commit+retry）、`_phase_fix_signals()`（discovery+challenge）三个 private helper。每个 helper 可单独测试。但对外仍是一个 Atom 交付单元。

### 开放问题

1. **`_run_phase_with_timeout` 应归属哪个类？** 选项：(a) `MaqsPhaseRunner` 的方法；(b) `MaqsTicketProcessor` 的 private helper；(c) 保持独立函数。倾向 (b)——它需要 ticket_id 用于 logging，而 ticket_id 在 processor 的 ctx 中。
2. **Prompt 模板是否应移入 `MaqsPhaseRunner`？** 当前 4 个 `*_PROMPT` 是模块级常量。移入类可以更好地封装（phase runner "知道" 自己用什么 prompt），但增加了类的体积。倾向保持现状，不在本次 scope 内。
3. **`_reset_stale_intermediate_tickets` 的归属？** 它只依赖 Bitable，逻辑上属于 pipeline 启动清理而非单 ticket 处理。倾向保持为独立 async 函数或 `MaqsTicketProcessor` 的 `@staticmethod`。

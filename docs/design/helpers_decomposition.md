# [Audit-P2] helpers.py 低内聚拆分 — 设计文档

## 1. 熵减目标（Entropy Reduction Target）

### 量化复杂度

`agent/jobs/mads/helpers.py`（689 行）承载 **5 个不相关领域**：

| 领域 | 行数 | 函数数 | 职责 |
|------|------|--------|------|
| Bitable CRUD | 44–136 | 4 | 飞书多维表格读写 |
| Git 操作 | 143–337 | 12 | 分支管理 + worktree 隔离 |
| LLM agent runner | 344–475 | 4 | Claude/Codex 调用 + JSON/XML 解析 |
| 磁盘 I/O | 478–527 | 6 | 工件文件读写 + conflict prediction |
| 飞书卡片渲染 + 通知 | 530–689 | 7 | 状态卡片构建、通知发送、子进程调用 |

**核心问题：** 所有 pipeline 模块都依赖 helpers.py，但每个模块只用其中 1–3 个领域。当前是一个 33 符号的扁平命名空间，修改任何领域都触发全文件重新加载，认知负担和变更风险不成比例。

### 消除的复杂度

| 指标 | 当前 | 目标 |
|------|------|------|
| 单文件行数 | 689 | 每模块 < 200 |
| 耦合域数 | 5（全混在一起） | 1（单一职责） |
| 改 Git 逻辑影响的 import 路径 | 全部 13 个消费者 | 仅 5 个 Git 消费者 |
| 符号查找范围 | 33 个函数/常量 | 每模块 4–12 个 |

---

## 2. 体验不变性（Experience Invariance）

以下行为 **必须** 保持完全一致，作为回归验证契约：

### 功能行为

| 场景 | 输入 | 预期行为 | 验证方式 |
|------|------|----------|----------|
| Bitable 记录创建 | `bitable_add(app, table, fields)` | 返回 `record_id` 或 `None` | 单元测试 mock subprocess |
| Worktree 合并冲突恢复 | `worktree_merge_to_dev(path, branch)` — merge 失败 | 先 rebase，rebase 成功则重试 merge | `test_merge_conflict.py` 四个用例全通过 |
| LLM agent 调用 | `run_agent(router, role, model, prompt, sys)` | 返回响应文本，错误时 `[ERROR]` 前缀 | mock router 验证 |
| 状态卡片渲染 | `build_status_card_json(...)` | JSON 2.0 结构含 schema/header/body | `test_status_card.py` 三个用例全通过 |
| QA verdict 解析 | `parse_qa_verdict("<qa_verdict>...")` | 返回 "PASS" 或 "REJECT" | `test_maqs_xml_parsers.py` 五个用例全通过 |
| 工件文件读写 | `write_artifact` / `read_artifact` / `append_artifact` | 文件在 `data/mads/{ticket_id}/` 下正确创建和读取 | 集成测试 |
| Git 命令执行 | `git("status")` | 在 `PROJECT_ROOT` 下执行，返回 `(rc, stdout, stderr)` | mock subprocess |

### Import 路径兼容性

**全部** 现有 import 路径（`from agent.jobs.mads.helpers import X`）必须继续工作。通过 `helpers.py` 保留为 re-export 聚合层实现。

### 测试通过

以下测试必须零修改通过：
- `tests/unit/test_status_card.py`
- `tests/unit/test_maqs_xml_parsers.py`
- `tests/unit/test_merge_conflict.py`

`patch("agent.jobs.mads.helpers.xxx")` 形式的 mock 路径也必须继续生效。

---

## 3. 用户场景

### 场景 1：开发者修改 Git worktree 逻辑

开发者需要为 worktree 合并添加新的冲突恢复策略。

**当前**：打开 689 行的 helpers.py，在 220–337 行范围内找到 worktree 函数，同时看到无关的 Bitable 和卡片渲染代码。

**重构后**：打开 `mads/git_ops.py`（~200 行），只有 Git 相关逻辑，认知负担降低 70%。

**验收标准**：`git_ops.py` 包含所有 12 个 Git 函数，无其他领域代码混入。

### 场景 2：新增 Bitable 查询方法

需要为 MADS 添加批量 bitable 记录删除功能。

**当前**：在 helpers.py 顶部添加函数，文件进一步膨胀。

**重构后**：在 `mads/bitable_ops.py` 中添加，该文件只有 Bitable 操作，上下文清晰。

**验收标准**：`bitable_ops.py` 包含 4 个 Bitable 函数 + `run_script` 辅助（或 inline 调用）。

### 场景 3：测试 mock 路径不变

QA 通过 `patch("agent.jobs.mads.helpers.git", ...)` mock Git 调用。

**重构后**：该 patch 路径仍然有效，因为 `helpers.py` re-export 了 `git`。

**验收标准**：所有 `patch("agent.jobs.mads.helpers.xxx")` 继续生效，无需修改测试代码。

### 场景 4：Pipeline 同时使用多个领域

`maqs.py` 从 helpers 导入 17 个符号（覆盖所有 5 个领域）。

**重构后**：`maqs.py` 的 import 语句不变。长期可逐步迁移到直接 import 子模块。

**验收标准**：`maqs.py` 零修改。

---

## 4. 设计方案

### 架构方向

在现有 `agent/jobs/mads/` 目录下新增 4 个模块，`helpers.py` 降级为纯 re-export 聚合层：

```
agent/jobs/mads/
├── __init__.py          # 不变
├── helpers.py           # 降级为 re-export hub（~40 行）
├── bitable_ops.py       # NEW: Bitable CRUD（~110 行）
├── git_ops.py           # NEW: Git + worktree（~200 行）
├── agent_runner.py      # NEW: LLM 调用 + 解析（~145 行）
├── cards.py             # NEW: 通知 + 状态卡片（~155 行）
├── pipeline.py          # 不变
├── contract.py          # 不变
├── ...
```

### 核心设计决策

**决策 1：共享常量和工具函数归属**

`PROJECT_ROOT`、`ARTIFACTS_DIR`、`log`、`run_script`、`doc_ctl`、`task_ctl` 是跨领域共享的基础设施。

**方案**：保留在 `helpers.py` 中作为 "base layer"。子模块从 helpers 导入这些基础设施。这避免创建一个额外的 `_base.py` 或 `_common.py`，保持最小变更。

- `PROJECT_ROOT`, `ARTIFACTS_DIR`, `log` → 留在 `helpers.py`
- `run_script`, `doc_ctl`, `task_ctl` → 留在 `helpers.py`（这三个是薄封装，被 review.py、design.py 等直接使用）
- 工件 I/O（`_ticket_dir`, `write_artifact`, `read_artifact`, `append_artifact`, `list_artifacts`）→ 留在 `helpers.py`（6 个纯函数，28 行，依赖 `ARTIFACTS_DIR`，属于基础设施层而非独立领域）

`helpers.py` 最终 = 常量 + 基础设施工具 + 工件 I/O + re-export 聚合 ≈ 80 行。

**决策 2：`predict_conflicts` 归属**

`predict_conflicts` 是一个 6 行的纯函数（无 I/O、无 Git 调用），逻辑上服务于 worktree 并发场景。归入 `git_ops.py`。

**决策 3：re-export 策略**

`helpers.py` 使用 `from .xxx import *` 配合每个子模块的 `__all__`，确保：
- 所有现有 `from agent.jobs.mads.helpers import X` 继续工作
- `patch("agent.jobs.mads.helpers.X")` 继续指向实际实现（因为 re-export 在 helpers 的模块命名空间中创建了同名绑定）

### 排除的方案

**方案 A（排除）：直接修改所有消费者 import 路径**

修改 13 个文件的 import 语句，直接从子模块导入。

排除原因：变更面过大（13 个文件 + 4 个测试文件），且 `patch()` mock 路径也全部要改。高风险、无增量价值（功能完全相同）。re-export 层达到同样效果，零消费者修改。

**方案 B（排除）：创建 `_base.py` 抽取共享基础设施**

将 `PROJECT_ROOT`、`log`、`run_script` 等移到 `_base.py`，`helpers.py` 仅做 re-export。

排除原因：引入新的内部层级，增加了一个文件但没有减少任何复杂度。`helpers.py` 本身保留基础设施 + re-export 更直观，避免循环依赖风险。

**方案 C（排除）：按消费者而非领域拆分**

按 "pipeline 需要的" vs "maqs 需要的" 拆分。

排除原因：消费者重叠严重（`maqs.py` 用了 17/33 符号），领域拆分是正交轴，更稳定。

### AI/智能注入点

此重构是纯结构优化，不引入 AI 逻辑。但拆分后解锁的能力：

- `agent_runner.py` 独立后，可演进为支持多 provider 路由、自动 fallback、cost-aware 模型选择
- `git_ops.py` 独立后，可演进为 Git 操作的事务封装（多步 Git 操作的原子性保证）

---

## 5. 接口契约

### 模块边界

#### `bitable_ops.py`

```python
# agent/jobs/mads/bitable_ops.py
__all__ = ["bitable_add", "bitable_update", "bitable_query", "bitable_get_status"]

async def bitable_add(app_token: str, table_id: str, fields: dict) -> str | None: ...
async def bitable_update(app_token: str, table_id: str, record_id: str, fields: dict) -> None: ...
async def bitable_query(app_token: str, table_id: str, filter_str: str = "", limit: int = 50) -> list[dict]: ...
async def bitable_get_status(app_token: str, table_id: str, record_id: str) -> str | None: ...
```

内部依赖：从 `helpers` 导入 `PROJECT_ROOT`, `log`（或直接 `logging.getLogger`）。
内部常量：`_BITABLE_SCRIPT`。

#### `git_ops.py`

```python
# agent/jobs/mads/git_ops.py
__all__ = [
    "git", "git_create_branch", "git_merge_to_dev", "git_revert_last",
    "git_current_branch", "git_restore_branch", "git_in",
    "worktree_create", "worktree_remove", "worktree_merge_to_dev",
    "worktree_cleanup_stale", "predict_conflicts",
    "WORKTREE_BASE",
]

async def git(*args, timeout: int = 30) -> tuple[int, str, str]: ...
async def git_create_branch(branch: str) -> bool: ...
async def git_merge_to_dev(branch: str) -> bool: ...
async def git_revert_last() -> bool: ...
async def git_current_branch() -> str: ...
async def git_restore_branch(original: str) -> None: ...
async def git_in(cwd: str, *args, timeout: int = 30) -> tuple[int, str, str]: ...
async def worktree_create(branch: str) -> str | None: ...
async def worktree_remove(wt_path: str, branch: str | None = None) -> None: ...
async def worktree_merge_to_dev(wt_path: str, branch: str) -> bool: ...
async def worktree_cleanup_stale() -> None: ...
def predict_conflicts(modified_files: list[str], active_files: set[str]) -> list[str]: ...
```

内部依赖：从 `helpers` 导入 `PROJECT_ROOT`, `log`。
内部常量：`WORKTREE_BASE`。

#### `agent_runner.py`

```python
# agent/jobs/mads/agent_runner.py
__all__ = ["run_agent", "run_codex", "parse_json_response", "parse_qa_verdict"]

async def run_agent(router, role: str, model: str, prompt: str,
                     system_prompt: str, workdir: str | None = None) -> str: ...
async def run_codex(prompt: str, workdir: str | None = None, timeout: int = 300) -> str: ...
def parse_json_response(text: str) -> dict | list | None: ...
def parse_qa_verdict(qa_report: str) -> str: ...
```

内部依赖：从 `helpers` 导入 `PROJECT_ROOT`, `log`。
`run_agent` 内部 lazy import `agent.infra.models.LLMConfig`。

#### `cards.py`

```python
# agent/jobs/mads/cards.py
__all__ = [
    "notify", "build_status_card_json",
    "send_status_card", "update_status_card",
]

async def notify(dispatcher, color: str, message: str, header: str = "MADS") -> None: ...
def build_status_card_json(
    ticket_id: str, title: str, phase: str,
    severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> str: ...
async def send_status_card(
    dispatcher, ticket_id: str, title: str, phase: str,
    severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> str | None: ...
async def update_status_card(
    dispatcher, message_id: str, ticket_id: str, title: str,
    phase: str, severity: str, ticket_type: str, phases_status: dict,
    workflow=None,
) -> bool: ...
```

内部依赖：从 `helpers` 导入 `log`。
内部常量：`_PHASE_LABELS`, `_SEVERITY_COLORS`, `_STATUS_ICONS`, `_STEP_ICONS`。
内部函数：`_render_workflow_section`（不导出）。

#### `helpers.py`（重构后）

```python
# agent/jobs/mads/helpers.py — 降级为基础设施 + re-export hub

# 保留的基础设施
PROJECT_ROOT: str
ARTIFACTS_DIR: str
log: logging.Logger

async def run_script(script_path: str, *args, timeout: int = 60) -> tuple[int, str, str]: ...
async def doc_ctl(*args, timeout: int = 60) -> tuple[int, str, str]: ...
async def task_ctl(*args, timeout: int = 60) -> tuple[int, str, str]: ...

def write_artifact(ticket_id: str, name: str, content: str) -> None: ...
def read_artifact(ticket_id: str, name: str) -> str | None: ...
def append_artifact(ticket_id: str, name: str, content: str) -> None: ...
def list_artifacts(ticket_id: str) -> list[str]: ...

# Re-exports（从子模块聚合）
from .bitable_ops import *
from .git_ops import *
from .agent_runner import *
from .cards import *
```

---

## 6. 初步分解

### 原子任务

| # | 任务 | 预估行数 | 依赖 | 可并行 |
|---|------|----------|------|--------|
| A1 | 创建 `bitable_ops.py`：移入 4 个 Bitable 函数 + `_BITABLE_SCRIPT` 常量 | ~100 | 无 | 是 |
| A2 | 创建 `git_ops.py`：移入 12 个 Git/worktree 函数 + `WORKTREE_BASE` + `predict_conflicts` | ~200 | 无 | 是（与 A1/A3/A4） |
| A3 | 创建 `agent_runner.py`：移入 4 个 LLM 函数 | ~140 | 无 | 是 |
| A4 | 创建 `cards.py`：移入 `notify` + 4 个卡片函数 + 内部常量/渲染函数 | ~135 | 无 | 是 |
| A5 | 重写 `helpers.py`：删除已移出的代码，保留基础设施，添加 `from .xxx import *` re-export | ~80 | A1–A4 全部完成 | 否 |
| A6 | 运行全部测试套件，确认零失败 | — | A5 | 否 |

**A1–A4 完全并行**：四个模块互不依赖。
**A5 串行**：依赖全部子模块创建完成。
**A6 串行**：依赖 A5 完成后的最终状态。

### 每个原子的具体内容

**A1 `bitable_ops.py`**
- 移入：`bitable_add`, `bitable_update`, `bitable_query`, `bitable_get_status`
- 移入常量：`_BITABLE_SCRIPT`
- 新增：`__all__` 列表、模块 docstring
- 导入：`asyncio`, `json`, `sys`, `os`, `logging`；从 `.helpers` 导入 `PROJECT_ROOT`

**A2 `git_ops.py`**
- 移入：`git`, `git_create_branch`, `git_merge_to_dev`, `git_revert_last`, `git_current_branch`, `git_restore_branch`, `git_in`, `worktree_create`, `worktree_remove`, `worktree_merge_to_dev`, `worktree_cleanup_stale`, `predict_conflicts`
- 移入常量：`WORKTREE_BASE`
- 新增：`__all__` 列表、模块 docstring
- 导入：`asyncio`, `os`, `shutil`, `logging`；从 `.helpers` 导入 `PROJECT_ROOT`

**A3 `agent_runner.py`**
- 移入：`run_agent`, `run_codex`, `parse_json_response`, `parse_qa_verdict`
- 新增：`__all__` 列表、模块 docstring
- 导入：`json`, `logging`, `os`, `re`, `shutil`；从 `.helpers` 导入 `PROJECT_ROOT`
- 注意：`run_agent` 内部 lazy import `agent.infra.models.LLMConfig` 保持不变

**A4 `cards.py`**
- 移入：`notify`, `_render_workflow_section`, `build_status_card_json`, `send_status_card`, `update_status_card`
- 移入常量：`_PHASE_LABELS`, `_SEVERITY_COLORS`, `_STATUS_ICONS`, `_STEP_ICONS`
- 新增：`__all__` 列表、模块 docstring
- 导入：`json`, `logging`

**A5 `helpers.py` 瘦身**
- 删除已移出的全部函数和常量
- 保留：`PROJECT_ROOT`, `ARTIFACTS_DIR`, `log`, `_DOC_CTL_SCRIPT`, `_TASK_CTL_SCRIPT`, `run_script`, `doc_ctl`, `task_ctl`, `_ticket_dir`, `write_artifact`, `read_artifact`, `append_artifact`, `list_artifacts`
- 添加：`from .bitable_ops import *`, `from .git_ops import *`, `from .agent_runner import *`, `from .cards import *`

---

## 7. 风险评估

### Risk 1：`patch()` mock 路径失效

**可能性**：低（re-export 机制保证 `agent.jobs.mads.helpers.X` 指向正确对象）
**影响**：高（测试全面失败）
**缓解**：A6 阶段必须运行全部 3 个相关测试文件，逐个验证 patch 路径。若 `from .xxx import *` 的 re-export 不满足 `patch()` 的模块解析，则改用显式 `from .bitable_ops import bitable_add as bitable_add` 形式。

### Risk 2：循环导入

**可能性**：中（子模块从 `helpers` 导入 `PROJECT_ROOT`，`helpers` 又 `from .xxx import *`）
**影响**：高（import 时 crash）
**缓解**：`helpers.py` 的 re-export 语句 **必须放在文件末尾**（在基础设施定义之后）。Python 的模块加载顺序保证：当子模块 `import helpers` 时，`PROJECT_ROOT` 等常量已经定义完成。re-export 的 `from .xxx import *` 在子模块全部加载完成后执行。这是 Python 处理循环导入的标准模式。

### Risk 3：遗漏符号导致运行时 `ImportError`

**可能性**：低
**影响**：高（生产 crash）
**缓解**：每个子模块必须定义 `__all__`，A5 完成后立即执行验证脚本：`python -c "from agent.jobs.mads.helpers import <所有 33 个符号>"`，确认零 ImportError。

### 开放问题

1. **长期废弃 re-export？** 此设计保持 100% 向后兼容。是否在后续版本中添加 deprecation warning 引导消费者直接从子模块导入？建议暂不处理，等自然迁移。
2. **`run_script` / `doc_ctl` / `task_ctl` 是否应独立？** 这三个函数是通用子进程封装，被 review.py 和 design.py 使用。当前体量（3 函数 20 行）不值得独立文件，留在 helpers 基础设施层。

# [Audit-P2] BaseStore 抽象：LockPool + JSONLAppender

## 1. 熵减目标（Refactor）

### 消除的具体复杂度

| 重复模式 | 当前实例数 | 消除后 |
|----------|-----------|--------|
| per-key Lock 池 + LRU sweep | 2（store.py, file_store.py），逻辑完全一致 | 1 类，2 处实例化 |
| JSONL append（open + json.dumps + write + "\n"） | 5（message_store, sentinel/store, error_tracker, autonomy, exploration） | 1 方法，5 处调用 |
| JSONL read + 时间窗过滤 | 4（sentinel/store, autonomy, exploration, message_store） | 1 方法，4 处调用 |
| JSONL read-modify-write（全量读→变换→原子写回） | 4（sentinel/store.resolve, exploration.rate_log_entry, exploration.adopt_log_entry, message_store._trim_jsonl） | 1 方法 `mutate()`，4 处调用 |
| `os.makedirs` 目录初始化 | 7 处（几乎每个 Store） | 收入构造函数，调用方零行 |

**量化**：当前 9 个文件共 1800 行，提取两个工具类（~75 行）后净减 **150–200 行**，残余重复降至 0。

### 消除的调用路径

当前 JSONL 操作有 5 条独立实现路径，各自处理编码、异常、目录创建。提取后收敛为 1 条经过测试的路径，其他 Store 只传入 `path` 和 `dict`。

### 不只是"代码看起来更干净"

- **错误修复统一**：当前 sentinel/store 和 exploration 的 JSONL 写入**无锁保护**，并发写入可导致行交错损坏。LockPool + JSONLAppender 组合一次性修复所有 5 处。
- **行为一致性**：`store.py` 的原子写用 `.bak` 备份，sentinel/store 的原子写没有。统一后保证一致的容错策略。

## 2. 体验不变性（Refactor）

以下行为必须在重构前后保持完全一致：

| 场景 | 预期行为 | 验证方式 |
|------|---------|---------|
| `save_json_sync` 写入后断电 | 要么写入完整 JSON，要么回退到 `.bak` | 现有测试 `TestSaveJsonSync` |
| `update_json_key` 并发 3 key 写同一文件 | 3 个 key 全部保留，无覆盖 | 现有测试 `test_concurrent_updates_no_clobber` |
| `_get_file_lock` 超过 50 个 key 后 sweep | 未被持有的 lock 被回收，当前持有的不被回收 | 现有测试 `test_sweep_when_over_50` |
| `SentinelStore.append` → `query(hours=24)` | 刚 append 的信号立即可查 | 新增集成测试 |
| `exploration.append_log` → `read_log_sync(hours=1)` | 刚 append 的 entry 立即可读 | 新增集成测试 |
| `exploration.rate_log_entry` / `adopt_log_entry` | 只修改匹配 task_id 的行，其他行不变 | 新增单元测试 |
| `MessageStore._jsonl_append` + `_trim_jsonl` | trim 后只保留 retention_days 内的行 | 新增单元测试 |
| `ErrorTrackerHandler.emit` 失败 | 静默吞异常，不 crash 应用 | 新增单元测试 |
| `FileStore` per-session lock | 不同 session 的文件操作互不阻塞 | 新增单元测试 |
| `store.py` 公开 API（`load_json_sync`, `save_json_sync`, `update_json_key`, `delete_json_key`） | 签名和行为完全不变 | 现有测试全绿 |

**向后兼容约束**：`store.py` 的模块级公开函数（`load_json_sync`, `save_json_sync`, `load_json`, `save_json`, `update_json_key`, `delete_json_key`）必须保持原位导出，不可破坏 11 个外部导入方。

## 3. 用户场景

### 场景 1：开发者新增一个 JSONL 审计日志

**背景**：需要为新模块（如 skill_usage）添加 JSONL 审计追踪。

**当前流程**：
1. 复制 `autonomy.py` 的 `log_action()` 和 `get_recent_actions()` 逻辑
2. 手动加 `os.makedirs`、`json.dumps`、`open("a")`、异常处理
3. 如果需要 trim，再抄一遍 `_trim_jsonl`
4. 忘了加锁 → 并发写入损坏

**重构后**：
```python
appender = JSONLAppender("data/skill_usage.jsonl")
appender.append({"skill": "gemini", "ts": time.time(), "duration": 1.2})
recent = appender.query_range(hours=24, ts_field="ts")
```

**验收标准**：新增 JSONL 存储只需 3 行（实例化 + append + query），无需关心锁、目录、编码、异常。

### 场景 2：修复 JSONL 并发写入损坏

**背景**：sentinel/store.py 和 exploration.py 的 JSONL 写入无锁保护，高并发时行可能交错。

**当前状态**：autonomy `log_action()` 和 exploration `append_log()` 通过 `asyncio.to_thread` 投到线程池，多个线程可能同时写同一文件。

**重构后**：JSONLAppender 内置 `threading.Lock`，所有 append 操作串行化。

**验收标准**：100 个并发 append → 读回 100 行完整 JSON，无损坏。

### 场景 3：现有 Store 的 Lock 池行为不变

**背景**：`store.py` 用 asyncio.Lock 池保护 JSON 文件并发写入，`file_store.py` 用 threading.Lock 池保护 per-session 元数据。两者逻辑一致但类型不同。

**重构后**：两处均实例化 `LockPool`，一个传 `asyncio.Lock` factory，一个传 `threading.Lock` factory。

**验收标准**：
- `store.py` 的 `update_json_key` 并发测试继续通过
- `FileStore` 的 per-session 并发操作互不阻塞
- LRU sweep 阈值可配置（默认 50 保持不变）

### 场景 4：exploration.py 的 JSONL mutate 操作简化

**背景**：`rate_log_entry` 和 `adopt_log_entry` 各自实现 39 行几乎一样的"全量读→找匹配行→修改→全量写回"逻辑。

**重构后**：
```python
appender.mutate(
    predicate=lambda e: e.get("task_id") == task_id and not e.get("user_rating"),
    transform=lambda e: {**e, "user_rating": rating},
)
```

**验收标准**：`rate_log_entry` 和 `adopt_log_entry` 各从 39 行降到 ~10 行（保留业务判断，委托 I/O）。

### 边缘场景

| 边缘场景 | 预期行为 |
|----------|---------|
| JSONL 文件不存在时 query | 返回空列表，不抛异常 |
| JSONL 中混入损坏行 | 跳过损坏行，log warning，继续处理后续行 |
| mutate 时无匹配行 | 不执行写操作，返回 0 |
| LockPool sweep 时所有 lock 都被持有 | 不删除任何 lock，等待下次 sweep |
| JSONLAppender 路径含不存在的中间目录 | 自动创建 |

## 4. 设计方案

### 架构位置

两个新类均放入 `agent/infra/store.py`（该模块已是 JSON 持久化的 canonical 位置）。不新建文件。

```
agent/infra/store.py
├── LockPool[L]          # NEW — 泛型 per-key 锁池
├── JSONLAppender        # NEW — JSONL 文件操作封装
├── load_json_sync()     # 不变
├── save_json_sync()     # 不变
├── load_json()          # 不变
├── save_json()          # 不变
├── update_json_key()    # 内部改用 LockPool，签名不变
└── delete_json_key()    # 内部改用 LockPool，签名不变
```

### 核心抽象

**LockPool**：泛型 per-key 锁池。解决两个重复实现的锁管理 + LRU sweep 逻辑。

设计决策：
- `lock_factory` 参数决定锁类型（`asyncio.Lock` 或 `threading.Lock`）
- 内部用 `threading.Lock` 保护 dict（GIL 下安全，即使管理的是 asyncio.Lock）
- sweep 阈值可配置，默认 50 与现有行为一致
- sweep 策略保持"跳过当前 key + 跳过 locked"的现有逻辑

**JSONLAppender**：JSONL 文件操作的完整封装。同步实现，异步调用方通过 `asyncio.to_thread` 包装（与现有模式一致）。

设计决策：
- 内部持有一个 `threading.Lock` 保护文件 I/O（修复现有的无锁并发问题）
- `ts_field` 参数化：不同 Store 用不同的时间戳字段名（`ts` / `timestamp` / `created_at`）
- `mutate()` 方法用 predicate + transform 函数式接口，避免为每种修改模式写专用方法
- 原子写回用 tmp → `os.replace` 模式（与现有 sentinel/store 一致）
- 损坏行处理：跳过 + log warning（与所有现有实现一致）

### 排除的替代方案

**方案 A：BaseStore 基类继承**
- 用抽象基类让 6 个 Store 继承共享逻辑
- **排除原因**：这 6 个 Store 异构程度极高 — SessionStore/MessageStore 是 SQLite，FileStore 是文件系统 + JSON 元数据，SentinelStore 是纯 JSONL，UserStore 是 JSON dict。强制继承同一基类是把不同性质的东西硬塞进同一层次。组合优于继承，尤其在性质不同的组件间。

**方案 B：独立 `jsonl_utils.py` 模块**
- 将 JSONL 操作提取到独立模块
- **排除原因**：增加一个新文件但带来的模块化收益很小 — JSONL 操作和 JSON 原子写入是同一主题（持久化原语），放在 `store.py` 内聚性更好，也避免新增 import path。

**方案 C：通用 `FileBackend` 抽象（JSON + JSONL + SQLite 统一接口）**
- 更高层次的抽象，所有 Store 共享统一读写接口
- **排除原因**：SQLite store（SessionStore, MessageStore）的操作模式（SQL query, schema migration, WAL）和文件 store 根本不同，统一接口要么过于宽泛失去类型安全，要么强制 SQLite store 适配文件接口。ROI 不划算。

### AI/智能注入点

本次重构为纯机械性提取，无明显 LLM 注入点。但 JSONLAppender 的 `query_range` 接口为未来预留了可能性：如果 JSONL 日志量增长，可以用 LLM 对 `query_range` 结果做语义聚合（类似现有 exploration_scoring.py 的 LLM 评分），而无需改动 Store 层。

## 5. 接口契约

### LockPool

```python
# agent/infra/store.py

from typing import TypeVar, Generic, Callable
import threading

L = TypeVar("L")  # Lock 类型：asyncio.Lock 或 threading.Lock

class LockPool(Generic[L]):
    """Per-key lock pool with LRU sweep.
    
    Usage:
        async_pool = LockPool(asyncio.Lock)
        sync_pool = LockPool(threading.Lock)
        
        lock = sync_pool.get("session_123")
        with lock:
            ...
    """
    
    def __init__(self, lock_factory: Callable[[], L], max_size: int = 50) -> None:
        """
        Args:
            lock_factory: 零参数可调用，返回一个新 lock 实例。
            max_size: 超过此数量触发 sweep。
        """
        ...
    
    def get(self, key: str) -> L:
        """获取 key 对应的 lock，不存在则创建。超过 max_size 时 sweep 未被持有的 lock。"""
        ...
    
    def clear(self) -> None:
        """清空所有 lock（仅测试用）。"""
        ...
    
    def __len__(self) -> int:
        """返回当前池中 lock 数量。"""
        ...
```

### JSONLAppender

```python
# agent/infra/store.py

from pathlib import Path

class JSONLAppender:
    """Thread-safe JSONL file operations: append, query, trim, mutate.
    
    Usage:
        log = JSONLAppender("data/autonomy_log.jsonl")
        log.append({"ts": time.time(), "action": "fix"})
        recent = log.query_range(hours=24, ts_field="ts")
        log.trim(retention_days=7, ts_field="ts")
        log.mutate(
            predicate=lambda e: e["id"] == target_id,
            transform=lambda e: {**e, "resolved": True},
        )
    """
    
    def __init__(self, path: str | Path) -> None:
        """初始化。自动创建父目录。内部持有 threading.Lock。"""
        ...
    
    @property
    def path(self) -> Path:
        """返回 JSONL 文件路径。"""
        ...
    
    def append(self, entry: dict) -> None:
        """线程安全地追加一行 JSON。
        
        Args:
            entry: 可 JSON 序列化的 dict。
        Raises:
            不抛异常 — append 失败只 log warning（与现有行为一致）。
        """
        ...
    
    def query_range(
        self,
        hours: float = 24,
        ts_field: str = "ts",
        **filters: str | int | float,
    ) -> list[dict]:
        """读取时间窗口内的条目，可选 key=value 精确过滤。
        
        Args:
            hours: 回溯时长。
            ts_field: 时间戳字段名（不同 store 不同：ts/timestamp/created_at）。
            **filters: 键值精确匹配（如 source="explorer", level=1）。
        Returns:
            匹配条目列表，按 ts_field 升序。
        """
        ...
    
    def read_all(self) -> list[dict]:
        """读取文件中所有有效 JSON 行。损坏行跳过并 log warning。"""
        ...
    
    def trim(self, retention_days: int = 7, ts_field: str = "ts") -> int:
        """删除超过保留期的条目。原子写回。
        
        Returns:
            被删除的条目数。
        """
        ...
    
    def mutate(
        self,
        predicate: Callable[[dict], bool],
        transform: Callable[[dict], dict],
    ) -> int:
        """对匹配 predicate 的条目执行 transform，原子写回。
        
        无匹配时不执行写操作。
        
        Args:
            predicate: 返回 True 的条目被 transform。
            transform: 接受 dict 返回修改后的 dict。
        Returns:
            被修改的条目数。
        """
        ...
    
    def exists(self) -> bool:
        """文件是否存在。"""
        ...
```

### 模块级接口变更

`store.py` 现有公开 API 保持不变，仅内部实现改用 LockPool：

```python
# 向后兼容：保留模块级别的 _file_locks 和 _get_file_lock
# 实现改为委托 LockPool 实例
_lock_pool = LockPool(asyncio.Lock, max_size=50)
_file_locks = _lock_pool  # 兼容直接引用（test_infra_store.py 导入了 _file_locks）

def _get_file_lock(path: str) -> asyncio.Lock:
    return _lock_pool.get(path)
```

## 6. 初步分解

### 原子工作项

| # | 工作项 | 关注点 | 依赖 | 预估 LOC |
|---|--------|--------|------|----------|
| A1 | 实现 `LockPool` 类 | 泛型锁池 + LRU sweep | 无 | ~25 |
| A2 | 实现 `JSONLAppender` 类 | append / query_range / read_all / trim / mutate | 无 | ~55 |
| A3 | `LockPool` 单元测试 | threading.Lock 和 asyncio.Lock 两种 factory、sweep 行为、clear、len | A1 | ~40 |
| A4 | `JSONLAppender` 单元测试 | append / query_range / trim / mutate / 并发 / 损坏行 / 不存在文件 | A2 | ~60 |
| A5 | `store.py` 内部迁移：`_file_locks` → `LockPool` | 保持 `_file_locks` / `_get_file_lock` 导出兼容 | A1 | ~10 |
| A6 | `FileStore` 迁移：`_meta_locks` → `LockPool` | threading.Lock factory | A1 | ~15 |
| A7 | `SentinelStore` 迁移 → `JSONLAppender` | append / _read_all / _write_all / resolve 全部委托 | A2 | ~30 delta |
| A8 | `autonomy.py` 迁移：`log_action` + `get_recent_actions` → `JSONLAppender` | 模块级 appender 实例 | A2 | ~25 delta |
| A9 | `exploration.py` 迁移：`append_log` / `read_log_sync` / `rate_log_entry` / `adopt_log_entry` → `JSONLAppender` | 保留业务逻辑，委托 I/O | A2 | ~40 delta |
| A10 | `MessageStore` 迁移：`_jsonl_append` + `_trim_jsonl` → `JSONLAppender` | SQLite 主体不动，只改 JSONL 审计部分 | A2 | ~20 delta |
| A11 | `ErrorTrackerHandler` 迁移 → `JSONLAppender` | logging.Handler 内不能阻塞，保留 try/except 静默 | A2 | ~10 delta |
| A12 | 现有 `test_infra_store.py` 适配验证 | `_file_locks` 兼容性、sweep 行为 | A5 | ~5 delta |
| A13 | 集成验证：`smoke_test.py` 全绿 | 端到端 | A5–A11 | ~0 |

### 依赖图

```
A1 (LockPool) ──→ A3 (测试)
  ├──→ A5 (store.py 迁移) ──→ A12 (现有测试适配)
  └──→ A6 (FileStore 迁移)

A2 (JSONLAppender) ──→ A4 (测试)
  ├──→ A7 (SentinelStore)
  ├──→ A8 (autonomy)
  ├──→ A9 (exploration)
  ├──→ A10 (MessageStore)
  └──→ A11 (ErrorTracker)

A5–A11 ──→ A13 (集成验证)
```

### 可并行项

- **A1 与 A2**：无依赖，可并行实现
- **A3 与 A4**：分别依赖 A1/A2，但互相独立
- **A5 与 A6**：都只依赖 A1
- **A7, A8, A9, A10, A11**：都只依赖 A2，互相独立，全部可并行

### 推荐执行序列

1. **Phase 1**（基础）：A1 + A2 并行 → A3 + A4 并行
2. **Phase 2**（迁移）：A5 + A6 + A7 + A8 + A9 + A10 + A11 全部并行
3. **Phase 3**（验证）：A12 + A13

## 7. 风险评估

| 风险 | 可能性 | 影响 | 缓解策略 |
|------|--------|------|----------|
| **`_file_locks` 兼容性**：`test_infra_store.py` 直接导入 `_file_locks` dict 并调用 `.clear()`，替换为 LockPool 后接口不兼容 | 高 | 中（测试失败） | LockPool 实现 `clear()` 和 `__len__`；`_file_locks` 保留为模块级变量指向 LockPool 实例，同时提供 dict-like 的 `.clear()` 方法。或将测试改为调用 `_lock_pool.clear()` |
| **ErrorTrackerHandler 死锁**：logging.Handler.emit 在持有 logging 内部锁时调用，如果 JSONLAppender 内部也 log warning，可能形成 reentrant 死锁 | 中 | 高（整个日志系统 hang） | JSONLAppender 的 append 方法在 `ErrorTrackerHandler` 场景下必须禁止 log 输出，通过 `silent=True` 参数或在 ErrorTrackerHandler 中 catch 所有异常（保持现有行为） |
| **JSONL 写入语义微妙差异**：现有 `sentinel/store._write_all` 用 `Path.replace()`，而 `message_store._trim_jsonl` 用 `os.replace()`；Windows 上行为可能不同 | 低 | 低（仅 Mac 部署） | JSONLAppender 统一用 `os.replace()`（POSIX 原子语义），消除分歧 |

### 开放问题

1. **`_file_locks` 测试兼容方案选择**：是让 LockPool 模拟 dict 接口（实现 `__contains__`、`__delitem__` 等），还是直接修改测试？推荐修改测试 — 维护成本更低。
2. **JSONLAppender 是否需要 async 原生接口**：当前方案是纯同步实现 + 调用方用 `asyncio.to_thread` 包装（与现有模式一致）。如果未来 JSONL 文件增大，可考虑 aiofiles，但目前文件量（sentinel ~百行，exploration ~百行）不需要。
3. **`get_weekly_adoption_count` 迁移程度**：该函数有复杂的周聚合业务逻辑，I/O 部分（读 JSONL + 过滤）可委托 JSONLAppender，但业务逻辑仍留在 exploration.py。是否值得迁移？推荐迁移 I/O 部分，业务逻辑保留。

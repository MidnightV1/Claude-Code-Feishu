# [Audit-P2] FeishuBot God Object 拆分 — 设计文档

## 1. 熵减目标（Entropy Reduction Target）

### 量化现状

`bot.py` 1625 行，承载 **10 个职责域**：

| 职责域 | 行数 | 方法数 | 说明 |
|--------|------|--------|------|
| 系统提示词常量 | 120 | 0 | `FEISHU_SYSTEM_PROMPT` 占文件 7% |
| WebSocket 生命周期 | 130 | 4 | `start()`, `_start_ws()`, `stop()`, `_fetch_bot_open_id()` |
| SDK Monkey-Patch | 100 | 0 | 内嵌在 `_start_ws()` 的闭包中 |
| 健康监控 | 80 | 3 | `_ws_health_monitor()`, `_start_loop_watchdog()`, `_check_loop_alive()` |
| 卡片交互注册 | 212 | 6 | `_register_card_actions()` 含 5 个内联闭包 handler |
| 消息处理 | 320 | 2 | `_handle_message()` 单方法 320 行，`_handle_media_failure()` |
| 命令路由 | 206 | 9 | `_route_command()` + 8 个 `_cmd_*` 方法 + `_send_menu_card()` |
| Debounce 系统 | 152 | 9 | `PendingBatch` + 8 个批次管理方法 |
| Orchestration | 59 | 2 | `_orchestrate_plan()`, `_orchestrate_execute()` |
| 去重/限流 | 49 | 4 | `_is_duplicate()`, `_check_rate_limit()` 等 |

**核心问题**：FeishuBot 的 `__init__` 初始化 **30+ 实例变量**，任何职责域的变更都要求理解整个类。Mixin 模式（`SessionMixin`, `MediaMixin`）虽然拆出了 LLM 会话和媒体处理，但主类仍然是所有状态的容器和所有事件的路由器。

### 熵减指标

| 指标 | 重构前 | 重构后目标 |
|------|--------|-----------|
| `bot.py` 行数 | 1625 | ≤ 650 |
| `FeishuBot.__init__` 实例变量数 | 30+ | ≤ 15 |
| 职责域数 | 10 | 3（WebSocket 协议 + 事件分发 + 生命周期） |
| 最大单方法行数 | 320（`_handle_message`） | ≤ 80 |
| 可独立测试的模块数 | 1（FeishuBot 整体） | 5+ |

---

## 2. 体验不变性（Experience Invariance）

以下用户可观测行为 **必须保持完全一致**：

### 飞书消息行为

| 场景 | 预期行为 | 验证方式 |
|------|----------|----------|
| 发送文本消息 | debounce 合并 → thinking 卡片 → LLM 回复 | 发送两条快速消息，确认合并为一次请求 |
| 发送图片/文件 | 异步处理 → 合并到批次 → LLM 回复含附件描述 | 发送图片+文字，确认合并处理 |
| 发送语音 | 识别中提示 → 转写 → LLM 回复 | 发送语音消息，确认转写+回复流程 |
| 群聊 @Bot | 仅响应 @Bot 的消息 | 群内发消息不 @，确认无响应 |
| 消息撤回 | 取消进行中的处理 / 移除历史 | 发送后立即撤回，确认任务取消 |
| 引用回复 | 引用内容前置 + 附件处理 | 引用图片并追问，确认内容注入 |
| 合并转发 | 展开子消息内容 | 转发消息给 Bot，确认展开 |

### 命令行为

| 命令 | 预期行为 |
|------|----------|
| `#help` | 返回帮助文本 + 菜单卡片 |
| `#reset` | 清除会话，发送 Session Reset 卡片 |
| `#opus` / `#sonnet` / `#haiku` | 切换模型，返回确认文本 |
| `#think` | 切换深度推理开/关 |
| `#jobs` | 列出定时任务状态 |
| `#usage` | 调用 `check_quota.py` 返回配额 |
| `#restart` | 管理员验证 → 3 秒后 launchctl 重启 |
| `#parallel <task>` | 创建 Orchestrator 计划 → 用户确认 → 执行 |
| `#menu` | 发送快捷操作面板卡片 |
| 插件命令 | 路由到注册的 handler |

### 卡片交互行为

| 交互 | 预期行为 |
|------|----------|
| 菜单按钮点击 | 注入合成消息到对话 |
| 确认/取消按钮 | 更新卡片状态，触发/取消操作 |
| 选择按钮 | 更新卡片 + 注入选择到对话 |
| 探索反馈 (👍/👎) | 记录评分 + 调整相关任务优先级 + 更新卡片 |
| 中止按钮 | 取消运行中任务 + 更新 thinking 卡片 |

### 防护行为

| 机制 | 预期行为 |
|------|----------|
| 去重 | 同一 message_id 不重复处理 |
| 限流 | 同一用户 >10 条/分钟返回限流提示 |
| 租户隔离 | 首条消息学习 tenant_key，跨租户消息丢弃 |
| 陈旧消息 | >2 分钟的 WebSocket 重投递消息丢弃 |

### 时序保证

- Debounce 窗口：单消息 2s 等待合并，后续部分 1s 窗口
- 会话串行：同一 session_key 下的批次串行处理，后到消息显示"排队中"卡片
- 媒体等待：批次中有未完成的媒体处理时，不触发 flush

---

## 3. 用户场景

> 注：此重构不改变终端用户行为，以下场景聚焦于**开发者体验**。

### 场景 1：独立测试命令路由

**角色**：开发者需要新增一个 `#status` 命令。

**当前**：必须实例化完整 FeishuBot（需 LLMRouter、CronScheduler、HeartbeatMonitor、Dispatcher 等 10 个依赖），才能测试 `_route_command()`。

**重构后**：
1. 实例化 `CommandRouter(router, scheduler, dispatcher)`
2. 调用 `router.route("#status", chat_id, chat_type, sender_id)`
3. 断言返回值

**验收标准**：`CommandRouter` 可在不依赖 WebSocket/事件循环的情况下单独实例化和测试。

### 场景 2：调整 debounce 策略

**角色**：开发者发现语音消息的 2s debounce 窗口太长，想改为 0s。

**当前**：必须阅读 `_handle_message()` 的 320 行代码，找到语音分支中的 `debounce_seconds=0` 参数，理解它如何传递到 `_enqueue()` → `_enqueue_part()` → `_flush_after()`。

**重构后**：
1. 打开 `debounce.py`，直接看到 `DebounceManager` 的接口和默认值
2. 修改策略，运行 `DebounceManager` 的单元测试验证
3. 不需要理解消息解析或 LLM 会话逻辑

**验收标准**：`DebounceManager` 有独立的单元测试，覆盖合并、超时、串行化场景。

### 场景 3：新增卡片交互类型

**角色**：开发者要添加一种新的卡片按钮交互（如投票）。

**当前**：在 bot.py 的 `_register_card_actions()` 方法内（254-437 行）新增闭包 handler，该闭包通过 `self` 访问 bot 的大量内部状态。

**重构后**：
1. 在 `card_actions.py` 中新增 handler 函数，接收明确的依赖注入
2. 在 `register_builtin_actions()` 中注册
3. 编写独立测试

**验收标准**：新增卡片交互不需要修改 `bot.py`。

### 场景 4：排查限流误报

**角色**：用户反馈正常对话被限流。

**当前**：需要在 1625 行的 bot.py 中搜索 `_check_rate_limit`，理解它与 `_handle_message` 的交互，以及与 `_sweep_dicts` 的清理逻辑。

**重构后**：
1. 打开 `message_guard.py`（~70 行）
2. `MessageGuard` 类集中了去重、限流、租户隔离的全部逻辑
3. 可独立测试限流计数器的行为

**验收标准**：`MessageGuard` 的限流逻辑可通过单元测试直接验证边界条件。

---

## 4. 设计方案

### 架构方向

将 FeishuBot 从"万能路由器"拆分为"协议适配器 + 组件协调器"。FeishuBot 仅负责：
1. WebSocket 生命周期管理（连接、重连、SDK 补丁、健康监控）
2. 事件桥接（SDK 线程 → asyncio 事件循环）
3. 组件生命周期编排（初始化、注入、停止）

其余职责下沉到 5 个独立组件，通过构造函数注入连接。

### 提取后的文件结构

```
agent/platforms/feishu/
├── bot.py              ≤650 行  WebSocket 协议适配 + 事件分发 + 组件编排
├── prompts.py          ~130 行  (NEW) FEISHU_SYSTEM_PROMPT + 相关常量
├── command_router.py   ~220 行  (NEW) CommandRouter 类
├── card_handlers.py    ~220 行  (NEW) 内置卡片交互 handler 定义
├── debounce.py         ~170 行  (NEW) DebounceManager + PendingBatch
├── message_guard.py    ~70 行   (NEW) MessageGuard（去重/限流/租户隔离）
├── card_actions.py     229 行   (不变) CardActionStore + CardActionRouter
├── session.py          1032 行  (微调) import 路径变更
├── media.py            532 行   (不变)
├── dispatcher.py       891 行   (不变)
├── api.py              450 行   (不变)
├── utils.py            986 行   (不变)
```

### 核心抽象

#### 4.1 CommandRouter — 命令路由器

将 `#command` 的注册、解析、分发、执行从 FeishuBot 中完全剥离。

**关键决策**：命令处理器不再通过闭包访问 `self`（FeishuBot 实例），而是接收明确的依赖。这消除了命令逻辑对 bot 内部状态的隐式耦合。

```
CommandRouter
  ├── route(text, chat_id, chat_type, sender_id) → str | None
  ├── register(prefix, handler, help_lines)
  └── 内置命令: help, reset, opus/sonnet/haiku, think, jobs, usage, restart, parallel, menu
```

依赖：`LLMRouter`（会话管理）、`CronScheduler`（任务列表）、`Dispatcher`（卡片发送）、`Orchestrator`（可选，并行执行）。

#### 4.2 卡片交互 Handler 提取

当前 5 个 handler（menu, confirm, select, explore_feedback, abort）以闭包形式内联在 `_register_card_actions()` 中，通过 `self` 访问 bot 的 `_running_tasks`、`_thinking_cards`、`_loop` 等内部状态。

**提取策略**：将 handler 定义移到独立文件 `card_handlers.py`，每个 handler 是一个工厂函数，接收所需的依赖作为参数。`FeishuBot.__init__` 中调用 `register_builtin_handlers(router, deps)` 完成注册。

**`abort` handler 的特殊性**：它需要访问 `_running_tasks` 和 `_thinking_cards`——这两个字典是 debounce/session 层的状态。解决方案：abort handler 接收一个 `cancel_task(key) → bool` 回调，由 DebounceManager 提供。

#### 4.3 DebounceManager — 批次管理器

封装消息合并、定时器管理、会话串行化的全部逻辑。

**状态归属**：
- `_pending`（PendingBatch 缓冲区）→ DebounceManager 拥有
- `_msg_to_key`（message_id → debounce key 映射）→ DebounceManager 拥有
- `_session_locks`（会话锁）→ DebounceManager 拥有
- `_queued_cards`（排队指示卡片）→ DebounceManager 拥有
- `_running_tasks`（运行中的任务）→ DebounceManager 拥有，SessionMixin 通过接口写入
- `_thinking_cards`（思考卡片）→ DebounceManager 拥有，SessionMixin 通过接口写入

**回调机制**：`DebounceManager` 不知道 LLM 处理的细节。当批次就绪时，通过 `on_batch_ready: Callable[[str, PendingBatch], Awaitable[None]]` 回调通知上层。这个回调指向 `SessionMixin._process_batch`。

#### 4.4 MessageGuard — 消息防护

集中管理消息级别的防护逻辑，将分散在 `_handle_message()` 中的 4 类检查收敛为一个类：

1. **消息去重**（内存 L0 + MessageStore L1/L2）
2. **速率限制**（每用户 10 条/分钟滑动窗口）
3. **租户隔离**（自学习 + 过滤跨租户消息）
4. **陈旧消息过滤**（>2 分钟的重投递丢弃）
5. **周期性清理**（`_sweep_dicts` 逻辑）

#### 4.5 FEISHU_SYSTEM_PROMPT 外置

120 行的提示词常量移至 `prompts.py`。`session.py` 的 import 路径从 `from agent.platforms.feishu.bot import FEISHU_SYSTEM_PROMPT` 改为 `from agent.platforms.feishu.prompts import FEISHU_SYSTEM_PROMPT`。

### `_handle_message()` 的瘦身

重构后的 `_handle_message()` 变为纯分发器：

```
async def _handle_message(self, data):
    # 1. 解包事件 (10 行)
    msg, sender, message_id, chat_id, ... = self._unpack_event(data)
    
    # 2. 防护检查 (5 行)
    if not self._guard.accept(message_id, sender_id, tenant_key, create_time):
        return
    
    # 3. 用户解析 (3 行)
    user = await self._resolve_user(sender_id)
    
    # 4. 群聊 @Bot 检查 (5 行)
    if chat_type == "group" and not self._is_bot_mentioned(msg):
        return
    
    # 5. 消息解析 → 文本提取 (调用 MediaMixin 方法, 30 行)
    text, footers = await self._extract_content(msg, ...)
    
    # 6. 命令快速路径 (5 行)
    if text.startswith("#"):
        result = await self._command_router.route(text, chat_id, chat_type, sender_id)
        if result is not None: ...
    
    # 7. 入队 debounce (3 行)
    await self._debounce.enqueue(key, text, footer, ...)
```

目标：`_handle_message()` 从 320 行降至 ~80 行。消息类型特定的解析逻辑（location、audio、image、file 的分支处理）保留在 bot.py 中作为 `_extract_content()` 的子分支，因为它们与 MediaMixin 紧密耦合。

### Mixin 协议的演进

当前 `SessionMixin` 和 `MediaMixin` 通过 `self` 访问宿主类的 30+ 属性，这是 mixin 反模式的典型症状。

**本次重构不改变 mixin 架构**（那是独立的 P3 任务），但通过以下方式减少耦合面：
- `SessionMixin._process_batch()` 不再直接读写 `self._thinking_cards` / `self._running_tasks`，改为通过 `self._debounce.thinking_cards` / `self._debounce.running_tasks` 访问
- 这样 DebounceManager 成为这些状态的 single source of truth

### 排除的替代方案

#### 方案 A：完全消除 Mixin，改用组合

将 `SessionMixin` 和 `MediaMixin` 也拆为独立类，FeishuBot 通过组合持有它们。

**排除原因**：
- 两个 mixin 合计 1564 行，与 bot.py 的交互面极广（`self` 属性访问 20+）
- 风险过大，改动范围涵盖 3 个核心文件
- 应作为后续独立任务（P3），本次聚焦 bot.py 自身的拆分

#### 方案 B：将 `_handle_message()` 拆为独立的 MessageProcessor 类

**排除原因**：
- `_handle_message()` 是消息类型分发的编排逻辑，它调用 MediaMixin 的解析方法、MessageGuard 的防护方法、CommandRouter 的路由、DebounceManager 的入队——它本身就是 bot 的核心编排职责
- 强行提取会导致 MessageProcessor 需要接收与 FeishuBot 几乎相同的依赖集，产生"代理类"反模式
- 更好的做法是通过提取子组件让它自然瘦身

#### 方案 C：事件驱动架构（EventBus）

用 pub/sub 事件总线替代直接方法调用，各组件订阅事件。

**排除原因**：
- 过度工程：当前只有一个 bot 实例，事件流是线性的
- 调试困难：事件总线隐藏了调用链，与"可观测性是一等公民"的原则矛盾
- 性能开销：消息处理是热路径，不需要额外间接层

### AI/智能注入点

本次重构本身不引入新的 AI 能力，但解耦后 **解锁** 以下可能性：

1. **CommandRouter 的意图识别升级**：当前命令路由是纯前缀匹配。独立后，未来可将 `route()` 改为先尝试前缀匹配、fallback 到 LLM 意图分类——无需触及 bot.py
2. **DebounceManager 的自适应窗口**：独立后可基于用户历史消息间隔自适应调整 debounce 窗口，而非固定 2s/1s
3. **MessageGuard 的异常模式检测**：独立后可引入轻量级异常检测（如突发流量模式识别），而非简单的计数限流

---

## 5. 接口契约

### 5.1 prompts.py

```python
# agent/platforms/feishu/prompts.py

FEISHU_SYSTEM_PROMPT: str  # 完整系统提示词（当前 bot.py L49-168 的内容）
DEDUP_TTL: int = 86400
DEDUP_MAX_SIZE: int = 1000
DEBOUNCE_SECONDS: float = 0.5
```

### 5.2 CommandRouter

```python
# agent/platforms/feishu/command_router.py

class CommandRouter:
    def __init__(
        self,
        router: LLMRouter,
        scheduler: CronScheduler,
        dispatcher: Dispatcher,
        orchestrator: Orchestrator | None = None,
        user_store: UserStore | None = None,
        admin_open_ids: set[str] = frozenset(),
        project_root: str = "",
    ): ...

    def register(self, prefix: str, handler: Callable, help_lines: str | None = None) -> None:
        """注册插件命令。handler 签名: async (cmd: str, args: str) -> str"""

    async def route(
        self,
        text: str,
        chat_id: str,
        chat_type: str,
        sender_id: str,
        session_key: str,
        *,
        on_plan_ready: Callable | None = None,   # orchestration 回调
        on_plan_execute: Callable | None = None,
    ) -> str | None:
        """路由 #command。返回响应文本，None 表示非命令或 skill 路由。"""

    async def send_menu_card(self, chat_id: str) -> None:
        """发送快捷操作面板卡片。"""
```

### 5.3 card_handlers.py

```python
# agent/platforms/feishu/card_handlers.py

@dataclass
class CardHandlerDeps:
    """卡片 handler 的依赖集合。"""
    dispatcher: Dispatcher
    router: LLMRouter
    cancel_task: Callable[[str], bool]              # key → 是否成功取消
    inject_message: Callable[..., Awaitable[None]]   # 注入合成消息

def register_builtin_handlers(
    action_router: CardActionRouter,
    deps: CardHandlerDeps,
) -> None:
    """注册所有内置卡片交互 handler（menu, confirm, select, explore_feedback, abort）。"""
```

### 5.4 DebounceManager

```python
# agent/platforms/feishu/debounce.py

@dataclass
class PendingBatch:
    parts: list[str]
    footers: list[str]
    first_message_id: str
    latest_message_id: str
    message_ids: set[str]
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    timer: asyncio.Task | None
    pending_media: int
    received_at: float

class DebounceManager:
    def __init__(
        self,
        dispatcher: Dispatcher,
        on_batch_ready: Callable[[str, PendingBatch], Awaitable[None]],
        default_debounce: float = 0.5,
    ): ...

    # ── 批次操作 ──
    async def enqueue(
        self, key: str, part: str, footer: str,
        message_id: str, chat_id: str, chat_type: str,
        sender_id: str, sender_name: str = "",
        debounce_seconds: float | None = None,
    ) -> None:
        """确保批次存在并入队一个 part。"""

    async def enqueue_part(self, key: str, part: str, footer: str = "",
                           debounce_seconds: float | None = None) -> None:
        """向已存在的批次追加 part。"""

    async def ensure_batch(
        self, key: str, message_id: str, chat_id: str,
        chat_type: str, sender_id: str, sender_name: str = "",
    ) -> PendingBatch:
        """创建或获取批次。"""

    async def cancel(self, key: str) -> None:
        """取消并清理批次。"""

    def handle_media_failure(self, key: str, chat_id: str,
                             message_id: str, error_msg: str) -> None:
        """处理媒体处理失败。"""

    # ── 消息-批次映射 ──
    def key_for_message(self, message_id: str) -> str | None:
        """根据 message_id 查找所属批次 key。"""

    def unmap_message(self, message_id: str) -> str | None:
        """移除映射并返回 key。"""

    # ── 运行中任务管理 ──
    @property
    def running_tasks(self) -> dict[str, asyncio.Task]:
        """key → 运行中的 asyncio.Task。"""

    @property
    def thinking_cards(self) -> dict[str, str]:
        """key → thinking 卡片 message_id。"""

    # ── 生命周期 ──
    def sweep(self) -> None:
        """清理过期的锁、卡片、任务引用。"""
```

### 5.5 MessageGuard

```python
# agent/platforms/feishu/message_guard.py

class MessageGuard:
    def __init__(
        self,
        message_store: MessageStore | None = None,
        dedup_ttl: int = 86400,
        dedup_max_size: int = 1000,
        rate_limit_per_min: int = 10,
    ): ...

    def accept(
        self,
        message_id: str,
        sender_id: str,
        tenant_key: str,
        create_time: str | None,
    ) -> bool:
        """综合检查：去重 + 租户隔离 + 陈旧消息 + 限流。
        返回 True 表示消息可以处理。
        副作用：记录 message_id、更新 tenant_key。"""

    def check_content_dup(
        self,
        message_id: str,
        sender_id: str,
        text: str,
        category: str,    # "command" | "chat" | "image" | "file"
        debounce_key: str = "",
    ) -> bool:
        """内容级去重（依赖 MessageStore）。返回 True 表示重复。"""

    def record_content(
        self,
        message_id: str,
        sender_id: str,
        text: str,
        category: str,
        debounce_key: str = "",
    ) -> None:
        """记录内容 hash（供后续去重）。"""

    @property
    def tenant_key(self) -> str:
        """当前学习到的租户 key。"""

    def sweep(self) -> None:
        """清理过期的去重记录和限流窗口。"""
```

### 5.6 重构后的 FeishuBot

```python
# agent/platforms/feishu/bot.py（重构后）

class FeishuBot(MediaMixin, SessionMixin):
    def __init__(
        self,
        config: dict,
        router: LLMRouter,
        scheduler: CronScheduler,
        heartbeat: HeartbeatMonitor,
        dispatcher: Dispatcher,
        default_llm: LLMConfig,
        file_store: FileStore,
        user_store: UserStore | None = None,
        orchestrator: Orchestrator | None = None,
        message_store=None,
    ):
        # 身份 (4 vars)
        # 基础依赖 (7 vars: router, scheduler, heartbeat, dispatcher, ...)
        # 组件初始化 (4 vars)
        self._guard = MessageGuard(message_store, ...)
        self._debounce = DebounceManager(dispatcher, self._process_batch)
        self._command_router = CommandRouter(router, scheduler, dispatcher, ...)
        self._card_action_router = CardActionRouter(...)
        # WebSocket 状态 (3 vars: _ws_client, _loop, _bot_open_id)

    # ── WebSocket 生命周期 ──
    async def start(self): ...
    async def stop(self): ...

    # ── 事件桥接 ──
    def _on_message_event(self, data): ...     # SDK thread → asyncio
    def _on_recall_event(self, data): ...
    def _on_card_action_sync(self, data): ...  # 同步卡片回调

    # ── 消息处理（瘦身后） ──
    async def _handle_message(self, data): ...   # ≤80 行分发器
    async def _handle_recall(self, data): ...

    # ── 健康监控 ──
    async def _ws_health_monitor(self): ...
    def _start_loop_watchdog(self): ...
    def _check_loop_alive(self, caller: str): ...

    # ── 辅助 ──
    def check_idle(self) -> tuple[bool, float]: ...
    def _session_key(self, chat_type, chat_id, sender_id) -> str: ...
    def _debounce_key(self, chat_type, chat_id, sender_id) -> str: ...
```

---

## 6. 初步分解

### Atom 列表

| # | Atom | 关注点 | 预估行数 | 依赖 | 可并行 |
|---|------|--------|----------|------|--------|
| A1 | 创建 `prompts.py`，迁移 `FEISHU_SYSTEM_PROMPT` 和常量 | 数据迁移 | ~20 新 | 无 | ✅ |
| A2 | 更新 `session.py` 的 import 路径 | import 修正 | ~3 改 | A1 | — |
| A3 | 创建 `message_guard.py`，实现 `MessageGuard` 类 | 防护逻辑提取 | ~70 新 | 无 | ✅ |
| A4 | 创建 `debounce.py`，迁移 `PendingBatch` + `DebounceManager` | 批次管理提取 | ~170 新 | 无 | ✅ |
| A5 | 创建 `command_router.py`，迁移命令路由和处理器 | 命令逻辑提取 | ~220 新 | 无 | ✅ |
| A6 | 创建 `card_handlers.py`，迁移内置卡片 handler | 卡片逻辑提取 | ~220 新 | 无 | ✅ |
| A7 | 重构 `FeishuBot.__init__`：初始化 4 个新组件 | 组件编排 | ~60 改 | A3-A6 | — |
| A8 | 重构 `_handle_message()`：委托到 Guard/CommandRouter/Debounce | 消息流瘦身 | ~100 改 | A3-A5, A7 | — |
| A9 | 重构 `_handle_recall()`：委托到 DebounceManager | 撤回流适配 | ~20 改 | A4, A7 | — |
| A10 | 更新 `SessionMixin._process_batch()`：通过 `self._debounce` 访问状态 | mixin 适配 | ~15 改 | A4, A7 | — |
| A11 | 移除 bot.py 中已迁移的代码，验证行数 ≤ 800 | 清理 | ~删除 800 行 | A7-A10 | — |
| A12 | 为 `CommandRouter` 编写单元测试 | 测试 | ~80 新 | A5 | ✅ |
| A13 | 为 `MessageGuard` 编写单元测试 | 测试 | ~60 新 | A3 | ✅ |
| A14 | 为 `DebounceManager` 编写单元测试 | 测试 | ~100 新 | A4 | ✅ |
| A15 | 集成 smoke test（`smoke_test.py` 通过 + import 链完整） | 验证 | ~20 改 | A11 | — |

### 依赖图

```
A1 ──→ A2
A3 ─┐
A4 ─┤
A5 ─┼──→ A7 ──→ A8 ──→ A11 ──→ A15
A6 ─┘         ├──→ A9
              └──→ A10

A3 ──→ A13  (并行)
A4 ──→ A14  (并行)
A5 ──→ A12  (并行)
```

### 执行顺序建议

**Phase 1 — 并行创建新文件**（A1, A3, A4, A5, A6 可并行）：
各 atom 独立创建新文件，包含完整的类实现。此阶段 bot.py 不变。

**Phase 2 — 集成重构**（A2, A7, A8, A9, A10 串行）：
修改 bot.py 和 session.py，将调用链接到新组件。

**Phase 3 — 清理 + 验证**（A11, A15 串行）：
删除 bot.py 中的旧代码，运行 smoke test。

**Phase 4 — 测试补充**（A12, A13, A14 可并行）：
为新组件编写独立测试。

---

## 7. 风险评估

### 风险 1：SessionMixin 隐式 self 访问断裂

**描述**：`SessionMixin._process_batch()` 通过 `self._thinking_cards`、`self._running_tasks` 等属性名访问宿主类的状态。将这些状态迁移到 `DebounceManager` 后，SessionMixin 需要改为通过 `self._debounce.thinking_cards` 访问。如果遗漏任何访问点，运行时会出现 `AttributeError`。

**可能性**：中  
**影响**：高（消息处理完全中断）  
**缓解**：
- 在 session.py 中 grep 所有 `self._thinking_cards`、`self._running_tasks`、`self._pending`、`self._queued_cards` 引用，列出完整清单后再动手
- 在 `FeishuBot.__init__` 中保留代理属性（`@property`），重构期间作为安全网，稳定后删除
- smoke_test.py 覆盖完整的消息→LLM→回复链路

### 风险 2：卡片 handler 闭包的 `self` 引用丢失

**描述**：当前 5 个 handler 闭包通过 `self`（FeishuBot 实例）访问 `_running_tasks`、`_thinking_cards`、`_loop`、`dispatcher`、`router` 等。提取为独立函数后，必须通过依赖注入传递所有必要引用。`_loop`（asyncio 事件循环）的引用尤其微妙——当前 `_handle_menu` 中有 `asyncio.run_coroutine_threadsafe(..., self._loop)` 的条件分支。

**可能性**：中  
**影响**：中（卡片交互失效，但不影响核心消息处理）  
**缓解**：
- `CardHandlerDeps` 数据类显式声明全部依赖，编译期可检查
- 提取前逐个 handler 标注其 `self` 访问列表
- 集成测试：发送菜单卡片 → 点击按钮 → 验证注入消息

### 风险 3：Debounce 时序行为回归

**描述**：debounce 系统的正确性依赖于精确的异步时序——timer 创建/取消、pending_media 计数器、session lock 的 acquire/release 顺序。提取为独立类时，如果回调链的 await 点发生变化，可能引入竞态条件。

**可能性**：低（逻辑不变，只是搬家）  
**影响**：高（消息丢失或重复处理）  
**缓解**：
- Phase 1 的 atom 是纯"复制"——新文件的逻辑完全照搬现有代码
- DebounceManager 单元测试必须覆盖：单消息 flush、多部分合并、media 等待、session 串行化、cancel 时序
- 上线前在 dev 分支运行 24h 真实流量观察

### 开放问题

1. **`_handle_message()` 中的消息类型分支**（text/location/audio/image/file）是否值得进一步提取为策略模式（`MessageTypeHandler`）？当前设计将其保留在 bot.py 中。如果未来新增消息类型（如视频），该方法会重新膨胀。——**建议**：本次保留，留作 P3 优化项。
2. **WebSocket SDK monkey-patch**（`_start_ws()` 内 100 行）是否应提取到 `ws_adapter.py`？这些补丁与 lark_oapi 版本强耦合，单独文件有利于版本升级时的隔离审查。——**建议**：本次保留在 bot.py 中，因为它与 `start()` 的生命周期紧密绑定，且不影响 800 行目标。
3. **Orchestration 方法**（`_orchestrate_plan`、`_orchestrate_execute`）是否应移入 SessionMixin 或独立模块？当前只有 59 行，且仅由 `#parallel` 命令触发。——**建议**：移入 `CommandRouter` 作为 `#parallel` 的实现细节，或保留在 bot.py 中。由 Decompose 阶段决定。

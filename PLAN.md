# nas-claude-hub 实现方案

## Context

OpenClaw 过重但定时任务/心跳/飞书集成有价值。NAS 已有 Claude Code CLI + Gemini API SDK + Node.js 20，目标：轻量 Python 服务复刻三个能力 + 多模型路由（含 Gemini CLI），全用户态，无 sudo。

---

## 模型路由策略

按任务复杂度分层，原则：**理解/设计/审查用 Pro 级，执行/编码/解析用 Flash/Sonnet 级**。

| 任务类型 | 模型 | Provider | 理由 |
|----------|------|----------|------|
| 飞书日常对话（理解需求、设计、规划） | Claude Opus | claude-cli | 准确理解意图，有工具能力 |
| 编码、改 bug | Claude Sonnet | claude-cli | token 多但复杂度低，有工具能力 |
| Review、检查 | Claude Opus / Gemini 3.1-Pro | claude-cli / gemini-api | 需要深度推理 |
| 搜索结果解析、看网页 | Gemini 3-Flash | gemini-cli / gemini-api | 高 token 低复杂度，便宜 |
| 多模态（图片分析） | Gemini 3-Flash | gemini-api | 3-Flash 多模态强 |
| 大型文档/PDF 解析 | Gemini 3.1-Pro | gemini-api | 1M 上下文，Files API 支持 |
| 心跳检查 | Gemini 2.5-Flash-Lite | gemini-api（必须） | $0.1/M，极低成本 |
| Cron 任务（视具体需求） | per-job 配置 | 任意 | 灵活指定 |

> **备注**：除心跳必须走 gemini-api 外，其他表中标注 gemini-api 的任务均可用 gemini-cli 平替（免费，有工具能力）。初版先按上表实现，后续根据 Gemini CLI 的稳定性逐步迁移。

### 三个 Provider

| Provider | 调用方式 | 成本 | 能力 | 适用场景 |
|----------|----------|------|------|----------|
| `claude-cli` | `claude -p --output-format json` | 订阅制 | 工具使用、文件读写、shell | 需要执行操作的任务 |
| `gemini-cli` | `gemini --output-format json` | 免费（Google 账户） | 工具使用、文件管理、shell、web | 不想花 API 费的工具任务 |
| `gemini-api` | `google-genai` Python SDK | 按 token 计费 | 多模态、Files API、1M 上下文 | 多模态/大文档/心跳等轻量调用 |

---

## 架构

```
飞书 WebSocket ──→ FeishuBot ──→ LLMRouter ─┬→ claude -p       (claude-cli)
                      │              ↑       ├→ gemini headless  (gemini-cli)
                      │    ┌─────────┘       └→ google-genai     (gemini-api)
                      ├──→ CronScheduler (per-job 模型)
                      └──→ HeartbeatMonitor (gemini-api/2.5-Flash-Lite)
                                │
                         Dispatcher ──→ 飞书 API 回传
```

---

## 文件结构

```
~/workspace/nas-claude-hub/
├── main.py               # 入口
├── llm_router.py          # 多模型路由（3 provider 统一接口）
├── claude_cli.py          # Claude Code CLI 封装
├── gemini_cli.py          # Gemini CLI 封装（headless 模式）
├── gemini_api.py          # Gemini API 封装（多模态 + Files API）
├── scheduler.py           # 进程内 cron 调度器
├── heartbeat.py           # 心跳监控
├── feishu_bot.py          # 飞书 WebSocket bot
├── dispatcher.py          # 飞书消息发送
├── store.py               # JSON 原子持久化
├── models.py              # 共享数据结构
├── config.yaml            # 配置
├── HEARTBEAT.md           # 心跳任务清单
└── data/                  # 运行时状态（自动创建）
    ├── jobs.json
    ├── sessions.json
    └── nas-claude-hub.log
```

---

## 实现顺序（11 步）

### 1. models.py — 数据结构

```python
@dataclass
class LLMConfig:
    provider: str = "claude-cli"      # "claude-cli" | "gemini-cli" | "gemini-api"
    model: str = "opus"               # claude: opus/sonnet/haiku
                                      # gemini: 3.1-Pro/3-Flash/2.5-Flash/2.5-Flash-Lite
    timeout_seconds: int = 600
    system_prompt: str | None = None
    temperature: float = 1.0          # gemini 默认 1.0（官方建议）
    thinking: str | None = None       # gemini thinking level: minimal/low/medium/high

@dataclass
class CronJob:
    id: str; name: str; enabled: bool
    schedule: CronSchedule; prompt: str
    llm: LLMConfig                    # per-job 模型
    deliver_to_feishu: bool = True
    one_shot: bool = False
    state: CronJobState               # next_run_at, last_status, consecutive_errors...
```

`LLMResult`：统一返回 `text, session_id, duration_ms, is_error, cost_usd, input_tokens, output_tokens`。

### 2. store.py — JSON 原子持久化

原子写入（临时文件 → `os.replace` → `.bak`），参考 OpenClaw `src/cron/service/store.ts`。

### 3. claude_cli.py — Claude Code CLI

```
claude -p --output-format json --dangerously-skip-permissions \
  [--resume {session_id}] [--model {model}] [--append-system-prompt "..."]
```

- prompt 通过 stdin 传入
- 解析 JSONL 取最后一条 `type=result`
- Session 策略：DM 按 open_id 续接，群聊按 chat_id，cron 隔离，心跳续接

### 4. gemini_cli.py — Gemini CLI

```
gemini --output-format json [--model {model}] "prompt"
```

- Headless 模式：非 TTY 自动激活，或传位置参数
- JSONL 事件流：init → message → tool_use → tool_result → result
- 解析逻辑同 claude_cli.py（取最后 `result` 事件）
- 退出码：0 成功，1 错误，42 输入错误，53 轮次超限
- 有工具能力（文件读写、shell、web 搜索）
- Session 管理：Gemini CLI 自带 session/checkpoint，但 cron 任务不 resume

### 5. gemini_api.py — Gemini API（多模态 + Files API）

复用 `work_functions.py`（`YOUR_LOCAL_WORKSPACE/work_functions.py:229`）调用模式：

```python
class GeminiAPI:
    MODELS = {
        "3.1-Pro":        "gemini-3.1-pro-preview",
        "3-Flash":        "gemini-3-flash-preview",
        "2.5-Flash":      "gemini-2.5-flash",
        "2.5-Pro":        "gemini-2.5-pro",
        "2.5-Flash-Lite": "gemini-2.5-flash-lite-preview-06-17",
    }
    # 注意：3-Pro 已于 2026.3.9 下架，不纳入
```

Thinking 配置（源自 `work_functions.py:234-250`）：
- 3 系列 → `ThinkingConfig(thinking_level=level)`
  - 3.1-Pro：low / medium / high
  - 3-Flash：minimal / low / medium / high
- 2.5 系列 → `ThinkingConfig(thinking_budget=-1)` 或 `thinking_budget=0`
- **不能同时用 thinking_level 和 thinking_budget**

多模态支持：
- 图片：`types.Part.from_bytes(data, mime_type)`（同 `work_functions.py:253-270`）
- 文件上传：`client.files.upload(file=path)` → 返回 file reference → 传入 contents
- PDF：通过 Files API 上传，支持大文件（1M 上下文）
- 视频/音频：同样通过 Files API

方法：
```python
async def run(self, prompt, system_prompt=None, model="2.5-Flash",
              thinking=None, temperature=1.0, timeout_seconds=120,
              files=None, image_src=None) -> LLMResult
```

- `asyncio.to_thread` 包装同步 SDK 调用
- cost 计算复用 `work_functions.py` 的 PRICING

### 6. llm_router.py — 统一路由

```python
class LLMRouter:
    async def run(self, prompt, llm_config: LLMConfig,
                  session_key=None, files=None, image_src=None) -> LLMResult:
        match llm_config.provider:
            case "claude-cli":  # → claude_cli.run()
            case "gemini-cli":  # → gemini_cli.run()
            case "gemini-api":  # → gemini_api.run()
```

- Claude CLI：有 session 续接
- Gemini CLI：有工具能力，但无 session 续接（cron 场景适用）
- Gemini API：无状态，但支持多模态/文件

### 7. dispatcher.py — 飞书消息发送

- `lark_oapi.Client` + markdown post（schema 2.0）
- 长文本 > 4000 字符分块
- 重试 3 次指数退避

### 8. scheduler.py — 进程内调度器

参考 OpenClaw `src/cron/service/timer.ts`：
- `croniter` + asyncio 定时器，最大延迟 60s
- per-job `LLMConfig`，通过 `LLMRouter` 路由
- 退避 30s → 1m → 5m → 15m → 60m
- 启动补执行 missed jobs

### 9. heartbeat.py — 心跳监控

- **默认模型：`gemini-api/2.5-Flash-Lite`**
- 30 分钟间隔，活跃时段控制
- HEARTBEAT_OK 抑制 + 24h 去重
- 通过 `LLMRouter` 调用

### 10. feishu_bot.py — 飞书 Bot

`lark_oapi.ws.Client` WebSocket，无需公网 IP。

指令系统：
```
#cron list                                              — 列出所有任务
#cron add <名称> <cron表达式> <prompt> [--model X]       — 添加（可指定 provider/model）
#cron remove <id>                                       — 删除
#cron run <id>                                          — 立即触发
#cron enable/disable <id>                               — 启停
#heartbeat status                                       — 心跳状态
#heartbeat run                                          — 立即触发
#model [provider/model]                                 — 查看/切换当前对话模型
#reset                                                  — 重置 session
#help                                                   — 帮助
```

`#model` 支持格式：
- `#model claude-cli/opus` — Claude Opus（理解/设计）
- `#model claude-cli/sonnet` — Claude Sonnet（编码）
- `#model gemini-cli/3-Flash` — Gemini CLI 3-Flash（免费工具任务）
- `#model gemini-api/3.1-Pro` — Gemini API 3.1-Pro（大文档/PDF）

**默认：`claude-cli/opus`**（准确理解任务需求）

### 11. main.py — 入口

初始化顺序：GeminiAPI → ClaudeCli → GeminiCli → LLMRouter → Dispatcher → Scheduler → Heartbeat → FeishuBot

---

## 配置格式

```yaml
feishu:
  app_id: "cli_xxx"
  app_secret: "xxx"
  domain: "https://open.feishu.cn"
  delivery_chat_id: "oc_xxx"

llm:
  default:
    provider: "claude-cli"
    model: "opus"

  claude-cli:
    path: "~/.npm-global/bin/claude"
    timeout_seconds: 600
    workspace_dir: "~/workspace/nas-claude-hub"

  gemini-cli:
    path: "~/.npm-global/bin/gemini"   # npm install -g @google/gemini-cli
    timeout_seconds: 300

  gemini-api:
    api_key: "AIzaSy..."
    timeout_seconds: 120
    temperature: 1.0

scheduler:
  enabled: true
  store_path: "data/jobs.json"

heartbeat:
  enabled: true
  interval_seconds: 1800
  llm:
    provider: "gemini-api"
    model: "2.5-Flash-Lite"
  active_hours:
    start: "08:00"
    end: "23:59"
    timezone: "Asia/Shanghai"

logging:
  level: "INFO"
  file: "data/nas-claude-hub.log"
```

---

## 依赖安装

```bash
ssh Midnight@YOUR_SERVER_IP
# Python 包
TMPDIR=~/tmp python3 -m pip install lark-oapi croniter pyyaml
# Gemini CLI（Node.js 20 已有）
npm install -g @google/gemini-cli
```

NAS 已有：`google-genai` 1.62.0、`claude` 2.1.63、Node.js 20.18.3。

## 前置条件

1. 飞书自建应用已创建 ✓，确保 WebSocket 事件订阅 + 机器人能力
2. Gemini API key 就绪（`llm_keys.yaml` 已有）
3. Gemini CLI 安装后需登录 Google 账户（`gemini auth login`）

## 运行

```bash
cd ~/workspace/nas-claude-hub && screen -S claude-hub && python3 main.py
```

## 验证步骤

1. **gemini_api.py**：测 2.5-Flash-Lite 和 3.1-Pro 调用，含多模态（传图片）
2. **gemini_cli.py**：测 headless 模式 JSON 输出解析
3. **claude_cli.py**：测 `-p` 模式 + session resume
4. **llm_router.py**：三个 provider 各跑一次
5. **dispatcher.py**：飞书发测试消息
6. **feishu_bot.py**：DM 发 "hello" → 确认 Opus 回复
7. **#model 切换**：`#model gemini-cli/3-Flash` → 发问题 → 确认走 Gemini CLI
8. **Cron**：`#cron add test "*/5 * * * *" "say hi" --model gemini-api/2.5-Flash` → 验证
9. **心跳**：HEARTBEAT.md 写入任务 → `#heartbeat run` → 确认走 Flash-Lite 且投递
10. **心跳抑制**：清空 HEARTBEAT.md → 确认跳过

---

## Long Task：可观测的长程任务执行

### 问题

当前架构：用户消息 → 单次 Claude CLI 调用（最长 10min）→ 一次性返回。中间完全黑盒——无进度、不知卡没卡、crash 也无感知。

### 三层架构

设计目标：**Skill 可移植、Orchestrator 平台无关、Adapter 可替换**。整个 long-task/ skill 目录可独立发布。

```
┌──────────────────────────────────────────────┐
│  Layer 1: Skill (.claude/skills/long-task/)  │  ← 纯 Claude Code，可移植
│  SKILL.md: plan 生成协议 + 步骤执行协议       │
│  scripts/task_ctl.py: 任务状态查询 CLI        │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────┴───────────────────────┐
│  Layer 2: Orchestrator (task_runner.py)       │  ← 纯 Python，零平台 import
│  状态机 + 持久化 + 逐步执行                    │
│  通过 ProgressReporter 接口上报进度            │
└──────────────────────┬───────────────────────┘
                       │  ProgressReporter 接口
┌──────────────────────┴───────────────────────┐
│  Layer 3: Notification Adapter               │  ← 平台特定，可替换
│  feishu_reporter.py: 卡片更新 + 任务 API      │
│  (slack/discord/webhook: 未来扩展)            │
└──────────────────────────────────────────────┘
```

| 层 | 职责边界 | 可移植性 | 开源价值 |
|---|---------|---------|---------|
| **Skill** | 告诉 Claude **怎么规划和执行** | 任何有 CC 的环境 | 最高——独立可用 |
| **Orchestrator** | 管理**任务生命周期**（状态机+持久化+执行） | 任何 Python 环境 | 高——通用编排器 |
| **Adapter** | 把进度**推到用户眼前** | 平台绑定 | 中——参考实现 |

关键约束：**Orchestrator 不 import 任何平台模块**，只调 ProgressReporter 接口。

### 流程：Plan → Approve → Execute → Report

```
#task <目标描述>
    ↓
1. Plan — Skill 约束 Claude 输出 JSON 计划
    ↓
2. Approve — Reporter 展示计划，用户确认/修改
    ↓
3. Execute — Orchestrator 逐步调 Claude CLI
    ├→ Reporter.on_step_start/done（实时进度）
    └→ 异常/超时 → Reporter.on_failed
    ↓
4. Report — Reporter.on_completed（完成摘要）
```

### 数据结构

核心模型不含任何平台字段。平台特定的状态（message_id、task_guid）由 Adapter 自己维护。

```python
# ── Orchestrator 核心（task_runner.py）──

@dataclass
class TaskPlan:
    task_id: str                    # UUID[:12]
    session_key: str                # "user:ou_xxx" / "chat:oc_xxx"
    status: str                     # planning → awaiting_approval → executing → completed → failed
    goal: str                       # 用户原始请求
    steps: list[TaskStep]           # 结构化计划
    current_step: int               # 当前执行到第几步（0-indexed）
    cli_session_id: str | None      # Claude CLI session（跨步骤复用上下文）
    created_at: float
    updated_at: float
    error: str | None

@dataclass
class TaskStep:
    name: str                       # 步骤名称
    description: str                # 具体做什么
    acceptance: str                 # 验收标准
    status: str                     # pending → running → completed → failed
    result: str | None


# ── ProgressReporter 接口（task_runner.py 内定义）──

class ProgressReporter(Protocol):
    async def on_plan_ready(self, task: TaskPlan) -> None: ...
    async def on_step_start(self, task: TaskPlan, step_index: int) -> None: ...
    async def on_step_done(self, task: TaskPlan, step_index: int) -> None: ...
    async def on_completed(self, task: TaskPlan) -> None: ...
    async def on_failed(self, task: TaskPlan, error: str) -> None: ...


# ── 飞书 Adapter（feishu_reporter.py）──
# 实现 ProgressReporter，内部维护 message_id / task_guid 等平台状态
```

### 状态机

```
        #task 触发
            ↓
       ┌─ planning ──────→ awaiting_approval ─┐
       │    (Claude 生成 plan)    (发卡片等确认)   │
       │                                        ↓
       │                    用户 "ok" ────→ executing
       │                    用户修改 ────→ planning（replan）
       │                                    │
       │                         ┌──────────┤
       │                         ↓          ↓
       │                    step N done   step N fail
       │                         │          │
       │                         ↓          ↓
       │                   N < total?    failed
       │                    yes → next step
       │                    no  ↓
       │                   completed
       └───────────────────────────────────┘
              任何阶段 crash → heartbeat 检测
```

### 消息路由变更

`feishu_bot.py` 的消息处理增加任务状态检查：

```
消息到达
  ↓
是 #task 命令？→ 创建 TaskPlan，进入 planning
  ↓ 否
当前用户有 awaiting_approval 的任务？→ 路由到 task_runner（处理确认/修改）
  ↓ 否
正常 LLM 路由（现有逻辑不变）
```

### 飞书 Adapter：双通道进度（feishu_reporter.py）

| 通道 | 机制 | 时效 | 持久 |
|------|------|------|------|
| **卡片更新** | 发卡片 → 拿 message_id → PATCH 更新内容 | 实时 | 低 |
| **飞书任务** | 创建 Task + 子任务 → 逐步 complete（P1） | 事件 | 高 |

卡片渲染示例（执行中）：
```
📋 重构 briefing 模块

✅ 1. 分析现有 briefing.py 结构
✅ 2. 设计 Collector 接口
🔄 3. 实现 collector.py        ← 当前
⬜ 4. 重构 briefing.py 调用
⬜ 5. 运行测试

> Step 3/5 执行中...
```

其他平台只需实现同一个 `ProgressReporter` 接口即可接入。

### 心跳集成

心跳快照增加任务池状态，注入 HEARTBEAT.md 内容之后、发给 LLM 之前：

```python
# heartbeat.py — 扩展 snapshot
def _collect_task_snapshot(self) -> str:
    tasks = task_store.list_active()
    if not tasks:
        return ""
    lines = ["## 活跃任务"]
    for t in tasks:
        age = format_duration(time.time() - t.updated_at)
        lines.append(f"- [{t.status}] {t.goal} (Step {t.current_step+1}/{len(t.steps)}, {age}前更新)")
    return "\n".join(lines)
```

心跳 LLM 判断规则（自然语言，写入 HEARTBEAT.md 或 prompt）：
- 执行中任务超过 15min 无更新 → 可能卡住
- awaiting_approval 超过 2h → 提醒用户确认
- completed 超过 24h 未查看 → 提醒

### Skill 定义

```
.claude/skills/long-task/
├── SKILL.md              # Claude 的行为协议（plan 格式、执行约束、输出规范）
└── scripts/
    └── task_ctl.py       # 任务状态查询/管理 CLI（类似 hub_ctl.py）
```

**SKILL.md 定义协议而非代码**——告诉 Claude：
- 收到任务目标时输出 JSON plan（schema 固定）
- 执行每步时遵守验收标准检查
- 失败时结构化报告 error
- 换平台只要把 `#task` 路由到 Claude CLI + 这个 skill，整个流程就能跑

**Plan 生成协议**（写入 SKILL.md）：
```
输出格式（严格 JSON，不要 markdown 包裹）：
{
  "steps": [
    {
      "name": "步骤简称",
      "description": "具体做什么",
      "acceptance": "怎么算完成"
    }
  ]
}

约束：
- 3-8 个步骤，太细增加协调成本，太粗失去可观测性
- 每步应产出可验证的结果（文件变更、测试通过、配置生效等）
- 步骤间依赖关系用执行顺序隐含表达
- 不要包含"确认需求"类步骤——用户已确认目标
```

### 分阶段实现

#### P0：核心管道（Skill + Orchestrator + 飞书卡片 Adapter）

**新增文件：**

| 文件 | 层 | 职责 |
|------|---|------|
| `.claude/skills/long-task/SKILL.md` | Skill | Claude 的 plan/execute 协议 |
| `.claude/skills/long-task/scripts/task_ctl.py` | Skill | 任务状态查询 CLI |
| `task_runner.py` | Orchestrator | 状态机 + 持久化 + 逐步执行 + ProgressReporter 接口 |
| `feishu_reporter.py` | Adapter | 飞书卡片更新实现 ProgressReporter |

**修改文件：**

| 文件 | 改动 |
|------|------|
| `dispatcher.py` | 增加 `update_card(message_id, text)` — PATCH `/im/v1/messages/:id` |
| `feishu_bot.py` | `#task` 命令路由 + awaiting_approval 拦截 |
| `main.py` | 初始化 TaskRunner(reporter=FeishuReporter) 并注入 bot |

**实现步骤：**
1. `long-task/SKILL.md` — plan 生成 + 步骤执行协议
2. `task_runner.py` — TaskPlan/TaskStep 模型 + ProgressReporter Protocol + 状态机 + 持久化
3. `dispatcher.py` — `update_card()` 方法
4. `feishu_reporter.py` — 实现 ProgressReporter（发卡片、更新卡片、渲染进度）
5. `feishu_bot.py` — `#task` 触发 + awaiting_approval 消息拦截
6. `task_ctl.py` — 任务列表/详情/取消 CLI
7. `main.py` — 组装注入

#### P1：飞书任务 API 集成

**前置：** 飞书应用开通 `task:task:write` + `task:tasklist:write` 权限

- `feishu_reporter.py` 增加飞书任务双写（create_task + subtasks + complete）
- `feishu_api.py` 增加任务 API
- 创建 "Hub Agent Tasks" Tasklist
- Activity Subscription 推送到通知群

#### P2：心跳集成

- `heartbeat.py` — `run_once()` 前收集任务快照，注入 prompt
- `task_runner.py` — 暴露 `list_active()` 给心跳查询
- 心跳判断规则：执行中超 15min 无更新→可能卡住，awaiting_approval 超 2h→提醒确认

#### P3：韧性增强

- subprocess 输出心跳（检测 stdout 停滞）
- crash 后从 checkpoint 恢复（读取 `data/tasks/` 中未完成任务）
- 执行中 replan（用户中途发消息修改方向）

### 验证步骤

1. `#task 给 dispatcher 加个 update_card 方法` → 确认 Skill 生成 plan
2. 回复 "ok" → 确认 Orchestrator 开始执行
3. 观察卡片实时更新（Step 1/N → 2/N → ...）→ 确认 Adapter 工作
4. 故意给一个会失败的任务 → 确认 on_failed 处理和通知
5. 执行中杀 Claude CLI 进程 → 确认超时检测
6. 心跳周期到达 → 确认任务状态出现在心跳快照中
7. `task_ctl.py list` → 确认 CLI 能查询任务状态

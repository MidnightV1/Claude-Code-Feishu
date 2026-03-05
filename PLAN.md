# Architecture Plan

## Context

Goal: lightweight Python service with scheduled tasks, heartbeat monitoring, and deep Feishu integration + multi-model routing (Claude CLI / Gemini CLI / Gemini API), fully userland, no sudo.

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

> **备注**：除心跳必须走 gemini-api 外，其他表中标注 gemini-api 的任务均可用 gemini-cli 平替（免费，有工具能力）。初版先按上表实现，后续根据 Gemini CLI 在 NAS 上的稳定性逐步迁移。

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
claude-code-lark/
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
    └── hub.log
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

原子写入（临时文件 → `os.replace` → `.bak`）。

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

调用模式：

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

Thinking 配置：
- 3 系列 → `ThinkingConfig(thinking_level=level)`
  - 3.1-Pro：low / medium / high
  - 3-Flash：minimal / low / medium / high
- 2.5 系列 → `ThinkingConfig(thinking_budget=-1)` 或 `thinking_budget=0`
- **不能同时用 thinking_level 和 thinking_budget**

多模态支持：
- 图片：`types.Part.from_bytes(data, mime_type)`
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
- cost 计算基于内置 PRICING 表

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
    workspace_dir: "."

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
  file: "data/hub.log"
```

---

## 依赖安装

```bash
pip install -r requirements.txt
# Optional: Gemini CLI for document co-pilot
npm install -g @google/gemini-cli
```

## 前置条件

1. 飞书自建应用已创建，确保 WebSocket 事件订阅 + 机器人能力
2. Gemini API key（用于心跳、简报、多模态）
3. （可选）Gemini CLI 安装后需登录 Google 账户（`gemini auth login`）

## 运行

```bash
./hub.sh start
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



> **Note**: The Long Task orchestrator design was replaced by native Claude Code TodoWrite event streaming — ~50 lines instead of ~550 lines. See `claude_cli.py` for the implementation.

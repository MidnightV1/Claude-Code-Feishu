# claude-code-lark

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

[English](README.md) | 中文

让 Claude Code 通过飞书/Lark 与你对话 — 集成日历、文档、任务、知识库、每日简报和自主健康监控。

## 这是什么

一个轻量 Python 服务，通过 WebSocket 将 Claude Code CLI 连接到飞书。用户在飞书私聊或群聊中与 Claude 对话，Claude 拥有完整的工具能力（文件读写、Shell、网页搜索等）。

**不是简单的 API 封装。** Bot 为每个用户运行真正的 Claude Code CLI 会话，具备持久对话上下文、工具调用和所有 Claude Code 能力。

## 为什么做这个

市面上飞书 AI Bot 不少，但大多是 LLM API 的聊天封装。这个项目走了不同的路：

- **Claude Code CLI 原生集成** — 不是 API 封装。每个用户运行真正的 `claude -p` 子进程，拥有完整工具链：文件读写、Shell 执行、代码编辑、网页搜索、子 Agent 协作。这不是一个调用 Claude 的聊天机器人，而是 Claude Code 本身。
- **飞书深度集成** — 7 个专属 Skill（日历、文档、任务、知识库、每日简报、心跳监控、文档助手）。不只是消息转发 — Claude 能直接创建日历事件、撰写飞书文档、管理任务、浏览知识库。
- **自进化的每日简报** — 自动化新闻 pipeline，内置关键词进化：每次运行后 LLM 自动分析覆盖盲区并优化搜索关键词，简报质量随时间持续提升，无需手动调优。
- **多 LLM 路由 + 降级链** — Claude CLI 负责对话，Gemini CLI 负责文档分析，Gemini API 负责多模态。每项能力都有降级路径（如 PDF：Gemini CLI → Gemini API → Claude Read）。
- **会话连续性** — `--resume` 保持完整 CLI 上下文；失败时 Sonnet 将历史压缩为结构化摘要，无缝恢复。
- **低功耗自部署** — 为 NAS/低功耗服务器设计。单 Python 进程，无需 Docker/Redis/数据库。

## 架构

```
飞书 WebSocket ──> FeishuBot ──> LLMRouter ─┬─> claude -p       (claude-cli)
                       │              │      ├─> gemini cli       (gemini-cli)
                       │              │      └─> google-genai     (gemini-api)
                       ├──> CronScheduler（按任务路由模型）
                       └──> HeartbeatMonitor（两层研判 → 行动）
                                 │
                          Dispatcher ──> 飞书卡片 JSON 2.0
```

核心组件：

| 组件 | 职责 |
|------|------|
| `feishu_bot.py` | WebSocket 事件处理、消息防抖、多模态输入 |
| `llm_router.py` | 会话管理、resume-or-fallback、历史压缩 |
| `dispatcher.py` | 飞书卡片渲染、分块、重试、实时更新 |
| `claude_cli.py` | Claude CLI 封装，流式 TodoWrite 进度 |
| `scheduler.py` | 进程内 cron 调度器（croniter + asyncio） |
| `heartbeat.py` | LLM 驱动的系统健康监控 |

## 功能

- **对话** — 飞书私聊或群聊 @Bot 进行完整 Claude Code 对话
- **多模态** — 图片理解（Claude 原生视觉）、PDF/文件分析（Gemini CLI → API → Claude 降级链）
- **日历** — 飞书日历事件的增删改查、联系人管理
- **文档** — 飞书文档的创建、阅读、搜索、评论；所有权转移
- **任务** — 飞书任务管理，支持负责人、截止日期和心跳截止日期监控
- **知识库** — 浏览知识库空间，创建/移动/读写知识库页面
- **每日简报** — 自动化多领域新闻摘要（详见下方）
- **文档助手** — 通过 Gemini CLI 深度分析文档，不污染聊天上下文
- **进度追踪** — 复杂任务通过 TodoWrite 实时展示思考卡片进度
- **定时任务** — 支持热加载的 cron 任务调度（无需重启）
- **心跳监控** — 两层 LLM 研判式健康检查，异常通知到私聊
- **会话连续性** — 优先 `--resume` 恢复，失败时降级为压缩历史注入

## 每日简报

在 cron 计划上运行的自动化新闻摘要 pipeline：

```
Brave Search → 采集文章 → Gemini 生成草稿 → Claude 审稿 → 通过邮件/飞书投递
```

- **多领域**：独立配置各领域（如「科技」「金融」），各有专属关键词、提示词和投递目标
- **关键词进化**：每次运行后，LLM 分析覆盖盲区并建议新搜索关键词——关键词库持续优化
- **审稿层**：可选的 Claude 审稿步骤，捕捉幻觉并在投递前提升质量
- **灵活投递**：邮件（SMTP）、飞书 IM 卡片、飞书文档，或任意组合

每个领域是 `~/briefing/domains/<name>/` 下的一个目录，包含 `sources.yaml`（关键词）、`domain.yaml`（模型 + 投递配置）和提示词模板。详见 `.claude/skills/briefing/SKILL.md`。

## 前置要求

- Python 3.10+ 及 pip
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并认证
- 启用了 Bot 能力和 WebSocket 的飞书自建应用
- Google AI Studio API Key（用于 Gemini — 多模态、心跳、简报）
- （可选）Gemini CLI，用于文档分析助手

## 快速开始

```bash
# 克隆
git clone <repo-url> && cd claude-code-lark

# 安装依赖
pip install -r requirements.txt

# 配置
cp config.yaml.example config.yaml
# 编辑 config.yaml — 填入飞书应用凭据、Gemini API Key 等

# 启动
./hub.sh start

# 检查状态
./hub.sh status

# 查看日志
tail -f data/hub.log
```

如需 AI agent 引导你完成完整配置，请参阅 [SETUP.md](SETUP.md)。

## 飞书应用配置

建议创建**两个**飞书应用 — 一个用于交互对话，一个用于通知推送：

| 应用 | 用途 | 为何分开 |
|------|------|---------|
| **对话 Bot** | 用户对话、工具调用 | 交互式会话，按用户隔离上下文 |
| **通知器** | 心跳告警、简报投递、定时消息 | 后台任务，无需用户会话 |

这种分离让通知投递独立于对话会话。单个应用也能工作 — 跳过 config 中的 `notify` 部分即可。

### 每个应用：

1. 前往 [飞书开放平台](https://open.feishu.cn/app) → 创建自建应用
2. 启用 **机器人** 能力
3. 启用 **WebSocket** 连接模式（非 HTTP 回调）— *仅对话 Bot*
4. 订阅事件（仅对话 Bot）：
   - `im.message.receive_v1` — 接收消息
   - `im.message.recalled_v1` — 处理消息撤回
5. 授权权限 — 导入 [`docs/feishu_scopes.json`](docs/feishu_scopes.json) 获取完整权限集，或按 Skill 逐项授权
6. 发布版本以激活 Bot
7. 将 App ID 和 App Secret 填入 `config.yaml`

## Skills

Skills 是 `.claude/skills/` 下的模块化能力。每个 Skill 都有 `SKILL.md` 提供用法文档和配置指南。

| Skill | 用途 | 关键配置 |
|-------|------|---------|
| `hub-ops` | 定时任务、服务状态、热加载 | （内置） |
| `briefing` | 每日新闻简报 pipeline | Gemini API key、领域配置 |
| `feishu-cal` | 日历事件增删改查、联系人 | `feishu.calendar.calendar_id` |
| `feishu-doc` | 文档增删改查、搜索、评论 | `feishu.docs.shared_folders` |
| `feishu-task` | 任务管理、截止日期监控 | `feishu.tasks.tasklist_guid` |
| `feishu-wiki` | 知识库空间和页面管理 | （将 Bot 加入知识库空间） |
| `gemini-doc` | 文档分析助手 | 安装 Gemini CLI |

每个 Skill 按需启用。

## 配置

所有选项参见 `config.yaml.example`。关键配置项：

| 配置段 | 用途 |
|--------|------|
| `feishu` | 应用凭据、日历、文档、任务、联系人 |
| `llm` | 默认 provider/模型、CLI 路径、超时 |
| `gemini-api` | Google AI Studio API Key |
| `briefing` | 简报 pipeline 模型配置 |
| `scheduler` | 启用/禁用 cron、存储路径 |
| `heartbeat` | 间隔、活跃时段、LLM 模型 |
| `notify` | 可选的第二个飞书应用（通知/告警） |

## 服务管理

```bash
./hub.sh start       # 后台启动
./hub.sh stop        # 优雅停止
./hub.sh restart     # 重启
./hub.sh status      # 检查运行状态
./hub.sh watchdog    # 未运行则启动（用于 cron 看门狗）
```

## AI Agent 指南

如果你是首次部署此服务的 Claude Code 实例，请阅读 [`SETUP.md`](SETUP.md) — 这是一份逐步引导指南，帮助你交互式地带用户完成全部配置。

## 许可证

[MIT](LICENSE)

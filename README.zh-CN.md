# claude-code-lark

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

[English](README.md) | 中文

让 Claude Code 通过飞书/Lark 与你对话 — 集成自主开发流水线、质量保障、25 个模块化 Skill 和多 LLM 编排。

## 这是什么

一个 Python 服务，通过 WebSocket 将 Claude Code CLI 连接到飞书。用户在飞书私聊或群聊中与 Claude 对话，Claude 拥有完整的工具能力（文件读写、Shell、网页搜索等）。

**不是简单的 API 封装。** Bot 为每个用户运行真正的 Claude Code CLI 会话，具备持久对话上下文、工具调用和所有 Claude Code 能力。

## 为什么做这个

市面上飞书 AI Bot 不少，但大多是 LLM API 的聊天封装。这个项目走了不同的路：

- **Claude Code CLI 原生集成** — 不是 API 封装。每个用户运行真正的 `claude -p` 子进程，拥有完整工具链：文件读写、Shell 执行、代码编辑、网页搜索、子 Agent 协作。这不是一个调用 Claude 的聊天机器人，而是 Claude Code 本身。
- **自主开发流水线（MADS）** — 多 Agent 开发系统，包含诊断、合同协商、修复、QA、合并五阶段。复杂度路由（L1–L5）将简单 bug 分配给 Sonnet，架构级变更交给 Opus。Scope 守卫防止漂移；Git worktree 隔离支持并行工单。
- **自动化质量保障（MAQS）** — 多 Agent 质量系统从错误日志中自动发现 bug、创建工单并接入 MADS 流水线。停滞工单自动恢复，QA 判定循环闭环反馈。
- **飞书深度集成** — 25 个专属 Skill，覆盖日历、文档、任务、知识库、多维表格、云盘、画板、电子表格、权限管理等。不只是消息转发 — Claude 能直接创建日历事件、撰写飞书文档、管理任务、浏览知识库。
- **自进化的每日简报** — 自动化新闻 pipeline，内置关键词进化：每次运行后 LLM 自动分析覆盖盲区并优化搜索关键词，简报质量随时间持续提升，无需手动调优。
- **Sentinel 自主巡检** — 健康脉搏、代码质量、文档审计三维扫描器，信号聚合在用户察觉前检测系统退化。
- **多 LLM 路由 + 降级链** — Claude CLI 负责对话，Gemini CLI 负责网页搜索和文档分析，Gemini API 负责多模态。每项能力都有降级路径（如 PDF：Gemini CLI → Gemini API → Claude Read）。
- **会话连续性** — `--resume` 保持完整 CLI 上下文；失败时 Sonnet 将历史压缩为结构化摘要，无缝恢复。
- **低功耗自部署** — 适用于任何常驻机器（家用服务器、迷你主机、云 VM）。单 Python 进程，无需 Docker/Redis/数据库。

## 架构

```
飞书 WebSocket ──> FeishuBot ──> LLMRouter ─┬─> claude -p       (claude-cli)
                       │              │      ├─> gemini cli       (gemini-cli)
                       │              │      └─> google-genai     (gemini-api)
                       ├──> CronScheduler（按任务路由模型）
                       ├──> LoopExecutor（MADS 工单生命周期）
                       ├──> Sentinel（健康 / 代码 / 文档扫描）
                       └──> HeartbeatMonitor（两层研判 → 行动）
                                 │
                          Dispatcher ──> 飞书卡片 JSON 2.0
```

核心组件：

| 组件 | 职责 |
|------|------|
| `feishu_bot.py` | WebSocket 事件处理、消息防抖、多模态输入 |
| `llm_router.py` | 会话管理、resume-or-fallback、历史压缩 |
| `dispatcher.py` | 飞书卡片渲染、分块、密钥扫描、重试 |
| `claude_cli.py` | Claude CLI 封装，流式输出，空闲超时 |
| `loop_executor.py` | MADS 工单编排，优先级队列与抢占 |
| `worker.py` | 并发 Worker，Git worktree 隔离 |
| `sentinel/` | 自主扫描器：健康脉搏、代码质量、文档审计 |
| `scheduler.py` | 进程内 cron 调度器，热加载（SIGUSR1） |
| `heartbeat.py` | LLM 驱动的系统健康监控 |

## 功能

### 核心
- **对话** — 飞书私聊或群聊 @Bot 进行完整 Claude Code 对话
- **多模态** — 图片理解（Claude 原生视觉）、PDF/文件分析（Gemini CLI → API → Claude 降级链）
- **语音合成** — 通过 Fish.audio S2-Pro 生成语音，以飞书语音消息投递
- **进度追踪** — 复杂任务通过 TodoWrite 实时展示思考卡片进度
- **会话连续性** — 优先 `--resume` 恢复，失败时降级为压缩历史注入

### 开发流水线（MADS）
- **诊断** — Opus 驱动的根因分析，配合代码库探索
- **合同** — Scope 协商，包含受影响文件、验收标准、复杂度路由
- **修复** — Sonnet 实现，Scope 守卫（Hardgate）防止漂移
- **QA** — 自动化测试验证，reject → retry 循环（最多 3 轮）
- **合并** — 合并队列，rebase-on-conflict 冲突恢复
- **设计** — 复合任务（L4+）的 Opus 设计文档
- **分解** — 将复杂修复拆分为原子化、可独立测试的工作项

### 质量保障（MAQS）
- **错误追踪器** — 聚合运行时错误，驱动自动 bug 发现
- **自动建单** — 从错误日志发现 bug，创建 MADS 工单
- **停滞恢复** — 检测并恢复被遗弃的工单

### 自主运维
- **Sentinel** — 健康脉搏、代码质量、文档审计扫描器，信号聚合
- **心跳监控** — 两层 LLM 研判式健康检查，异常通知到私聊
- **探索引擎** — 从对话、任务、错误中发现研究方向
- **定时任务** — 支持热加载的 cron 任务调度（无需重启）
- **每日简报** — 自动化多领域新闻摘要（详见下方）

### 飞书集成
- **日历** — 事件增删改查、联系人管理
- **文档** — 创建、阅读、搜索、评论、所有权转移；Block 树遍历
- **任务** — 管理负责人、截止日期、心跳截止日期监控
- **知识库** — 浏览空间，创建/移动/读写页面
- **多维表格** — 表格增删改查、记录查询/筛选
- **云盘** — 文件/文件夹管理、搜索、发送媒体到群聊
- **画板** — 白板创建、流程图绘制、节点读取
- **电子表格** — 单元格读写、工作表管理
- **权限** — 文档分享、协作者管理、公开链接

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
- （可选）Gemini CLI，用于网页搜索和文档分析
- （可选）Fish.audio API Key，用于语音合成

## 快速开始

```bash
# 克隆
git clone https://github.com/MidnightV1/claude-code-lark.git && cd claude-code-lark

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

如需 AI agent 引导你完成完整配置，请参阅 [SETUP.md](docs/SETUP.md)。

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

Skills 是 `.claude/skills/` 下的模块化能力。每个 Skill 都有 `SKILL.md` 提供用法文档和配置指南。共 25 个 Skill：

### 飞书平台
| Skill | 用途 |
|-------|------|
| `feishu-cal` | 日历事件增删改查、联系人 |
| `feishu-doc` | 文档增删改查、搜索、评论、Block 树读取 |
| `feishu-task` | 任务管理、截止日期监控 |
| `feishu-wiki` | 知识库空间和页面管理 |
| `feishu-bitable` | 多维表格增删改查、记录查询/筛选 |
| `feishu-drive` | 云盘文件/文件夹管理、搜索、媒体发送 |
| `feishu-board` | 画板/白板创建、流程图绘制 |
| `feishu-sheet` | 电子表格读写、工作表管理 |
| `feishu-perm` | 文档权限管理、分享 |

### 开发与质量
| Skill | 用途 |
|-------|------|
| `dev-pipeline` | 统一 MADS/MAQS 入口：建单、状态查询、阶段推进 |
| `sentinel` | 自主健康/代码/文档扫描器，信号聚合 |
| `visual-qa` | CDP 截图、可访问性树、五维度评分 |
| `codex` | OpenAI Codex CLI 集成，代码审查与任务交接 |
| `skill-creator` | Skill 开发框架：创建、测试、评估、迭代 |

### 搜索与智能
| Skill | 用途 |
|-------|------|
| `gemini` | 网页搜索、URL 阅读、文件分析、摘要（零 API 成本） |
| `brave-web-search` | Brave 英文网页搜索 |
| `brave-news-search` | Brave 新闻搜索，支持时效过滤 |
| `arxiv-tracker` | ArXiv 论文追踪，关键词预筛 + LLM 评估 |

### 运维与工具
| Skill | 用途 |
|-------|------|
| `hub-ops` | 定时任务、服务状态、热加载 |
| `briefing` | 每日新闻简报 pipeline 管理 |
| `weather` | 天气查询，位置持久化 |
| `plan-review` | CEO/创始人模式方案审查（4 种 scope 模式） |

### 社交媒体
| Skill | 用途 |
|-------|------|
| `twitter-cli` | Twitter/X 读搜发、书签 |
| `xiaohongshu-cli` | 小红书笔记、热门、发帖 |
| `bilibili-cli` | B 站视频、热门、字幕、AI 摘要 |

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

如果你是首次部署此服务的 Claude Code 实例，请阅读 [`SETUP.md`](docs/SETUP.md) — 这是一份逐步引导指南，帮助你交互式地带用户完成全部配置。

## 许可证

[MIT](LICENSE)

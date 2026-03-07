# Claude Code Feishu

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://python.org)

[English](README.en.md) | 中文

Claude Code Feishu，让 AI 真正与你协同起来。

## 从工具到同事

Claude Code 在终端里是强大的编程工具。但编程只是工作的一部分——你还需要排日程、写方案、跟任务、做调研、盯进度。

这些事情发生在飞书里，不在 IDE 里。

这个项目把 Claude Code 带进飞书，但不只是「消息转发」。它让 Claude 拥有飞书的全部协作能力：日历、文档、任务、知识库、多维表格、云盘。加上 Claude Code 原生的文件读写、代码编辑、命令执行、子 Agent 协作——你得到的是一个能力边界远超 CLI 的 AI 协作者。

## 三个场景

**场景一：早上打开飞书**

你还没发消息，AI 已经在工作了。每日简报自动送达——关键词会自动进化，覆盖面越来越精准。过期任务的提醒出现在私聊里。这不是被动响应，是主动协作。

**场景二：一句话拆成三个人的活**

你说「帮我修日报的链接 bug，顺便把 feishu-doc skill 加个 replace 功能」。Opus 调研完两个问题，设计方案，拆成独立子任务交给 Sonnet 并行执行。你继续和 Opus 聊别的事，执行完了它自动验收汇报。

**场景三：方案讨论全程在飞书**

不需要在 CLI 和飞书之间来回跳。Claude 直接创建飞书文档写方案，你在文档里加评论，Claude 读取评论逐条回应，修改后更新同一份文档。任务、日程、知识库都是同一个对话的延伸。

## CLI vs 飞书：不只是界面不同

| 维度 | CLI / IDE | 飞书协作 |
|------|-----------|----------|
| 交互方式 | 你主动发指令 | 它也会主动找你（日报、DDL 提醒、异常告警） |
| 能力边界 | 代码 + 文件 + Shell | + 日历 + 文档 + 任务 + 知识库 + 多维表格 |
| 协作模式 | 1:1 同步等待 | 异步——你可以离开，它继续干，干完飞书通知你 |
| 身份感知 | 匿名 session | 识别每个用户，维护独立上下文和认知档案 |
| 任务编排 | 你拆任务分配 | Opus 调研设计 → Sonnet 并行执行 → Opus 验收 |
| 多人协作 | 单人使用 | 多用户各自会话，互不干扰 |
| 上下文恢复 | 关掉就没了 | resume 恢复完整上下文，失败时压缩降级 |
| 多 Bot 支持 | 单实例 | 多 Bot 实例，独立人设 / 模型 / 会话，支持工作目录隔离 |

## 架构

单 Python 进程，无需 Docker / Redis / 数据库。

```
飞书 WebSocket → FeishuBot → LLMRouter ─┬→ claude -p    (对话/工具)
                     │            │      ├→ gemini cli   (搜索/文档)
                     │            │      └→ gemini api   (大文档/fallback)
                     ├→ Orchestrator（Opus 编排 + Sonnet 工作池）
                     ├→ CronScheduler（定时任务）
                     ├→ HeartbeatMonitor（心跳监控）
                     └→ Dispatcher → 飞书卡片 JSON 2.0
```

核心设计：

- **会话隔离**：每个用户独立 CLI session，per-user 原子持久化
- **上下文韧性**：优先 `--resume` 恢复完整上下文；失败时 Sonnet 压缩历史为结构化摘要注入新 session，无感降级
- **多模型协作**：Claude CLI 对话 + Gemini CLI 搜索与文档分析，各取所长
- **Token 节省**：用 Gemini CLI（订阅制零成本）处理大文件读取、文档分析等高 token 消耗任务，将 Claude 上下文留给需要深度推理的工作
- **任务编排**：Opus 调研拆分 → 用户确认 → Sonnet 并行执行 → Opus 验收

## 能力一览

**对话与理解**

- 飞书私聊的完整 Claude Code 对话
- 图片理解（Claude 原生视觉）
- PDF / 文件分析（多模型降级链）
- 实时进度卡片（TodoWrite 流式更新）

**飞书深度集成**

| Skill | 能力 |
|-------|------|
| `feishu-cal` | 日历事件增删改查、参会人管理、联系人 |
| `feishu-doc` | 文档创建 / 阅读 / 更新 / 按章节替换 / 评论分析 |
| `feishu-task` | 任务管理 + 心跳截止日期监控 |
| `feishu-wiki` | 知识库空间浏览、页面增删改查 |
| `feishu-bitable` | 多维表格记录查询 / 筛选 / 增删改 |
| `feishu-drive` | 云盘文件与文件夹管理 |
| `feishu-perm` | 文档权限管理、协作者增删 |
| `hub-ops` | 定时任务 CRUD、服务状态、热加载 |
| `briefing` | 每日简报 pipeline、自定义数据源、多域管理、关键词自进化 |
| `gemini` | 搜索 / 网页 / 文件分析 / 摘要（订阅制零成本） |

每个 Skill 独立启用，按需配置。

**自主行为**

- **每日简报**：多源采集 → LLM 生成 → 审稿 → 投递，关键词自动进化
- **心跳监控**：两层 Sonnet 研判（triage → action），异常自动 DM 通知
- **任务到期提醒**：心跳读取任务快照，逾期 / 即将到期主动提醒

**任务编排**

Opus 负责思考，Sonnet 负责动手：

1. 用户提出复杂任务
2. Opus 调研分析，设计方案，拆解为独立子任务
3. 用户在飞书确认计划
4. Sonnet 工作进程并行执行（Opus 释放，可继续对话）
5. Opus 验收结果，处理冲突，汇报

## 快速开始

**前置要求**

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 已安装并认证
- 飞书自建应用（启用 Bot 能力 + WebSocket 连接模式）
- （推荐）Claude Code 使用订阅模式；安装 [Gemini CLI](https://github.com/google-gemini/gemini-cli) 并订阅 Gemini 基础会员，最大化节省 token 成本

```bash
git clone https://github.com/MidnightV1/Claude-Code-Feishu.git
cd Claude-Code-Feishu
pip install -r requirements.txt
cp config.yaml.example config.yaml
# 编辑 config.yaml 填入飞书凭据
./hub.sh start
```

详细部署指南见 [SETUP.md](docs/SETUP.md)（[中文](docs/SETUP.zh-CN.md)）。

---

*以下内容面向 AI Agent（Claude Code 或其他 agent）——如果你是首次部署此服务的 agent，继续阅读。*

## 给 AI Agent 的说明

如果你是 Claude Code 或其他 agent 首次阅读这个项目：

- `SETUP.md` 是逐步部署指南，直接跟着做
- `.claude/skills/` 下每个 `SKILL.md` 都有 Onboarding 段落，首次加载时引导用户完成配置
- `config.yaml.example` 有所有配置项的注释说明
- 用户说「定时任务」→ `hub-ops`，说「日报」→ `briefing`，说「文档」→ `feishu-doc`

## 许可证

[MIT](LICENSE)

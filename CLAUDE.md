# nas-claude-hub

## 身份

你是 Claude Code，运行在 server 上的 `nas-claude-hub` 服务内，作为 Claude CLI 子进程。你与用户所有的Claude Code共享灵魂（`~/.claude/CLAUDE.md`）和认知（`~/.claude/COGNITION.md`），遵循相同的原则和工作方式。

你的消息来自两个通道，通道决定场景：

| 通道 | 触发方式 | 特征 |
|------|----------|------|
| **飞书** | 用户在飞书 DM / 群聊 @Bot | 经 `feishu_bot.py` 路由，支持图片/文件/多模态。消息通过**飞书卡片 JSON 2.0** 的 markdown 组件渲染。详见飞书系统提示词 |
| **SSH CLI** | 用户 SSH 到 NAS 直接 `claude` | 完整 CLI 能力，本地文件系统，无飞书能力限制 |

---

## 运行约束

- **禁止 `hub.sh restart/stop`** — 你是 hub 的子进程，执行等于自杀。`HUB_CHILD` 环境变量已拦截此操作
- **禁止 sudo** — 全用户态运行
- 需要重启时，告诉用户：「服务需要重启，因为 [原因]。请在飞书发送 `#restart` 或在服务端执行 `hub.sh restart`」

### 热加载 vs 重启

| 变更 | 需要重启？ |
|------|-----------|
| 定时任务增删改 | **不需要** — hub_ctl.py 自动热加载（SIGUSR1） |
| `sources.yaml`（日报搜索词） | **不需要** — collector 每次运行时读取 |
| 飞书任务变更 | **不需要** — 心跳每周期重新拉取快照 |
| `config.yaml`（凭据、模型默认值） | **需要** |
| Hub Python 代码（main.py 等） | **需要** |
| Python 依赖 | **需要** |

### 对话上下文保持

两层架构保持对话连续性：

| 层 | 触发条件 | 上下文量 |
|---|---------|---------|
| **主路径** | `--resume` 成功 | Claude CLI 完整上下文窗口 |
| **降级路径** | resume 失败/`#reset`/重启 | 历史注入 system prompt（Sonnet 压缩 + 近期原文） |

关键参数（`llm_router.py`）：

| 参数 | 值 | 含义 |
|------|---|------|
| `HISTORY_ROUNDS` | 8 | 保留最近 N 轮对话 |
| `HISTORY_TRUNCATE` | 2000 字 | 单条消息截断长度 |
| `SUMMARY_THRESHOLD` | 4 轮 | 超过此阈值触发摘要压缩 |

上下文策略特点：
- **无 TTL 限制** — 永远尝试 `--resume`，让 CLI 自行判断 session 可用性
- **Retry 在 router 层** — resume 失败后带上下文重试，claude_cli 只做执行
- **上下文注入走 system prompt** — 不污染 user message，CC 能区分历史和当前指令
- **Sonnet 压缩（Gemini fallback）** — 结构化摘要保留决策、文件路径、任务状态
- **恢复通知** — 降级时告知 CC 工具调用记录不可访问，需重新读取文件

降级触发场景：CLI session 文件丢失/损坏、LLM 报错 → resume 失败自动降级、`#reset` → 主动清除、服务重启 → CLI session 失效。

---

## Skills 优先

**新增运维能力必须优先创建 Skill（`.claude/skills/<name>/SKILL.md` + `scripts/`），而非在 `feishu_bot.py` 里硬编码 `#command`。**

| Skill | 位置 | 用途 |
|-------|------|------|
| `hub-ops` | `.claude/skills/hub-ops/` | 定时任务 CRUD、服务状态、热加载 |
| `briefing` | `.claude/skills/briefing/` | 日报 pipeline 管理、域管理、关键词进化 |
| `feishu-cal` | `.claude/skills/feishu-cal/` | 日历日程 CRUD、参会人管理、联系人 |
| `feishu-doc` | `.claude/skills/feishu-doc/` | 文档 CRUD、搜索、评论分析、所有权转移 |
| `feishu-task` | `.claude/skills/feishu-task/` | 任务 CRUD、截止日期管理、心跳联动 |

参考：https://code.claude.com/docs/en/skills

---

## 协作者

你不是唯一的 agent。用户同时与以下端点协作：

- **本机 CC**（Windows）— 主要开发环境，跑在 `YOUR_LOCAL_WORKSPACE/`
- **NAS CC（你）** — 运行 hub 服务，管理 NAS 上的任务
- **飞书 Bot** — 你的对外通信界面

原则：
- 修改了代码/配置后**同步更新文档**（CLAUDE.md、PLAN.md、nas_env.md），其他 agent 依赖文档对齐
- 不假设其他端点的状态，看到不认识的改动先查 git log 再行动
- 环境变更（装包、改配置、加目录）必须更新 `nas_env.md`

### Git 协作

代码通过 NAS 上的 bare repo 同步，**不经 GitHub**。

| 角色 | 仓库路径 |
|------|----------|
| **Bare repo（canonical）** | `~/repos/YOUR_PROJECT.git` |
| **NAS 工作目录** | `~/workspace/nas-claude-hub`（remote: `origin`） |
| **Windows 工作目录** | `YOUR_LOCAL_WORKSPACE/nas-claude-hub`（remote: `origin` → `ssh://USER@YOUR_SERVER_IP/~/repos/YOUR_PROJECT.git`） |

工作流：
1. **修改代码后** → `git add` + `git commit` + `git push origin master`
2. **push 触发 post-receive hook** → 自动 `git reset --hard` 到 NAS 工作目录
3. **Python 文件变更** → hook 会记录到 `data/deploy.log`，需手动重启服务
4. **拉取对方变更** → `git pull origin master`（先 pull 再改，避免冲突）

规则：
- **先 pull 后 push** — 修改前先 `git pull` 确认是否有对方变更
- **commit 粒度** — 一个功能/修复一个 commit，message 说清 what 和 why
- **不要 force push** — 两端共享 master，force push 会丢对方的提交
- `config.yaml`、`data/`、`__pycache__/` 已在 `.gitignore` 中排除

---

## LLM Providers

| Provider | 调用方式 | 适用场景 |
|----------|----------|----------|
| `claude-cli` | `claude -p` subprocess | 需要工具能力的任务 |
| `gemini-api` | `google-genai` SDK | 多模态、大文档 |

`gemini-cli` 接口已预留，因 tree-sitter 编译问题on the server暂不可用。

心跳使用两层 Claude 模型：Haiku（triage）→ Sonnet（action，仅异常时触发）。

---

## 文件目录

| 文件/目录 | 职责 |
|-----------|------|
| `main.py` | 入口，PID 文件写入，SIGUSR1 热加载信号 |
| `feishu_bot.py` | 飞书 WebSocket Bot，消息路由，debounce，多模态处理 |
| `briefing_plugin.py` | 日报 thin shim（subprocess launcher，60 行，不含逻辑） |
| `scripts/briefing_run.py` | 日报 pipeline 独立脚本（采集→生成→审稿→邮件→关键词进化） |
| `scheduler.py` | 进程内 cron 调度器（croniter + asyncio timer） |
| `heartbeat.py` | 心跳监控（两层架构：Haiku triage → Sonnet action） |
| `llm_router.py` | 多模型路由（claude-cli / gemini-api） |
| `dispatcher.py` | 飞书消息发送（卡片 JSON 2.0 markdown，分块，重试，卡片更新） |
| `file_store.py` | 会话级文件存储（图片/文件持久化 + 上下文注入） |
| `claude_cli.py` | Claude CLI subprocess 封装（stream-json + TodoWrite 进度流） |
| `gemini_api.py` | Gemini API SDK 封装（多模态 + Files API） |
| `models.py` | 共享数据结构 |
| `feishu_api.py` | 飞书 API 客户端（token 缓存 + HTTP + 联系人映射） |
| `store.py` | JSON 原子持久化 |
| `config.yaml` | 配置（凭据、模型、心跳、日报、飞书） |
| `hub.sh` | 服务管理脚本（start/stop/restart/status/watchdog） |
| `.claude/skills/` | Claude Code Skills |
| `data/` | 运行时状态（jobs.json, sessions.json, hub.pid, logs） |
| `PLAN.md` | 架构设计文档 |

---

## 服务管理

`hub.sh` 仅限用户或外部 SSH 执行：

```
./hub.sh start | stop | restart | status | watchdog
```

两个 Dispatcher（独立飞书应用）：
- `YOUR_CHAT_APP_ID` — 聊天回复（FeishuBot）
- `YOUR_NOTIFY_APP_ID` — 通知/告警/日报（Notifier）

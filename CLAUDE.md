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
- 需要重启时，告诉用户：「服务需要重启，因为 [原因]。请执行：`hub.sh restart`」

### 热加载 vs 重启

| 变更 | 需要重启？ |
|------|-----------|
| 定时任务增删改 | **不需要** — hub_ctl.py 自动热加载（SIGUSR1） |
| `sources.yaml`（日报搜索词） | **不需要** — collector 每次运行时读取 |
| `HEARTBEAT.md` | **不需要** — 每个心跳周期重读 |
| `config.yaml`（凭据、模型默认值） | **需要** |
| Hub Python 代码（main.py 等） | **需要** |
| Python 依赖 | **需要** |

---

## Skills 优先

**新增运维能力必须优先创建 Skill（`.claude/skills/<name>/SKILL.md` + `scripts/`），而非在 `feishu_bot.py` 里硬编码 `#command`。**

| Skill | 位置 | 用途 |
|-------|------|------|
| `hub-ops` | `.claude/skills/hub-ops/` | 定时任务 CRUD、服务状态、热加载 |
| `feishu-cal` | `.claude/skills/feishu-cal/` | 日历日程 CRUD、参会人管理、联系人 |
| `feishu-doc` | `.claude/skills/feishu-doc/` | 飞书文档创建、读取、追加内容 |
| `feishu-task` | `.claude/skills/feishu-task/` | 飞书任务 CRUD、子任务、任务列表 |
| `long-task` | `.claude/skills/long-task/` | 长程任务 plan→execute→report 协议 |

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
| `gemini-api` | `google-genai` SDK | 多模态、大文档、心跳 |

`gemini-cli` 接口已预留，因 tree-sitter 编译问题on the server暂不可用。

心跳固定使用 `gemini-api/2.5-Flash-Lite`（最低成本）。

---

## 文件目录

| 文件/目录 | 职责 |
|-----------|------|
| `main.py` | 入口，PID 文件写入，SIGUSR1 热加载信号 |
| `feishu_bot.py` | 飞书 WebSocket Bot，消息路由，debounce，多模态处理 |
| `briefing.py` | 日报 pipeline（采集→Gemini 生成→Claude 审稿→邮件→飞书） |
| `scheduler.py` | 进程内 cron 调度器（croniter + asyncio timer） |
| `heartbeat.py` | 心跳监控（系统快照→LLM 判断→异常投递） |
| `llm_router.py` | 多模型路由（claude-cli / gemini-api） |
| `dispatcher.py` | 飞书消息发送（卡片 JSON 2.0 markdown，分块，重试，卡片更新） |
| `task_runner.py` | 长程任务编排器（状态机 + 持久化 + ProgressReporter 协议） |
| `feishu_reporter.py` | 飞书进度通知适配器（实现 ProgressReporter，卡片实时更新） |
| `file_store.py` | 会话级文件存储（图片/文件持久化 + 上下文注入） |
| `claude_cli.py` | Claude CLI subprocess 封装 |
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

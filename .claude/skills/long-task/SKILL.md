# Long Task — 可观测的长程任务执行

## 什么时候使用

**核心原则：用户不应该干等。** 当任务需要多个步骤且执行时间较长时，使用长程任务模式让用户实时看到进度。

### 必须使用的场景

- 涉及 **2+ 工具调用**的复合任务（调研+分析+输出、读取+整理+创建文档等）
- 预计执行时间 **超过 30 秒**
- 跨文件/跨模块的修改
- 需要先分析再设计再实现的任务

### 不要使用的场景

- 简单问答、闲聊
- 单个文件的小修改
- 一步能完成的操作

## 如何触发

### 你主动触发（推荐）

对话中发现任务符合上述条件时，执行：

```bash
python3 .claude/skills/long-task/scripts/task_ctl.py create \
  --goal "任务目标描述" \
  --plan '{"steps": [{"name": "步骤名", "description": "做什么", "acceptance": "怎么算完成"}, ...]}'
```

脚本写入请求到 `data/task_requests/`。Hub 自动读取后发送进度卡片到对话框，供用户确认。

调用后告诉用户：「已创建任务计划，请在进度卡片中确认后开始执行。」

### 用户显式触发

用户发送 `#task <目标描述>` 时，Hub 直接创建任务并调用你生成计划。

## Plan JSON 格式

```json
{
  "steps": [
    {
      "name": "步骤简称",
      "description": "具体做什么",
      "acceptance": "怎么算完成"
    }
  ]
}
```

### 约束

- **3-8 个步骤** — 太细增加协调成本，太粗失去可观测性
- 每步应产出**可验证的结果**（文件变更、测试通过、文档产出等）
- 步骤间依赖关系用**执行顺序**隐含表达
- **不要**包含「确认需求」类步骤 — 用户已确认目标
- **不要**包含「总结汇报」类步骤 — 系统自动生成完成报告

## 步骤执行协议

计划获批后，Hub 逐步调用你执行。每步你会收到：

- 任务总目标
- 当前步骤名称、描述、验收标准
- 之前步骤的上下文（通过 CLI session 保持）

### 执行要求

- 专注当前步骤，不要跳步
- 完成后简要说明**做了什么**和**验收结果**
- 如果步骤无法完成，明确说明原因
- 利用 Claude Code 的工具能力（读写文件、运行命令等）

## 进度可视化

任务进度通过飞书卡片在对话框中实时展示：
- 计划提交 → 卡片展示步骤列表，等待用户 ok
- 用户确认 → 逐步执行，卡片原地更新（✅ / 🔄 / ⬜ 状态图标）
- 完成/失败 → 卡片最终状态 + 结果摘要

## 跨 Session 上下文

每次 CLI 调用是独立 session。查看任务状态：

```bash
# 查看所有活跃任务
python3 .claude/skills/long-task/scripts/task_ctl.py active

# 查看特定任务详情（包含每步结果）
python3 .claude/skills/long-task/scripts/task_ctl.py status <task_id>

# 查看所有任务（含已完成）
python3 .claude/skills/long-task/scripts/task_ctl.py list
```

任务数据持久化在 `data/tasks.json`，重启不丢失。

## 示例

用户说「整理下环境信息和项目情况，写个介绍文档发我」：

```bash
python3 .claude/skills/long-task/scripts/task_ctl.py create \
  --goal "整理 NAS 环境和项目信息，创建个人介绍飞书文档" \
  --plan '{"steps": [
    {"name": "采集环境信息", "description": "读取 nas_env.md、硬件配置、存储布局", "acceptance": "环境数据整理完成"},
    {"name": "梳理项目清单", "description": "扫描 workspace 目录，读取各项目 CLAUDE.md", "acceptance": "项目清单和状态整理完成"},
    {"name": "读取认知画像", "description": "读取 COGNITION.md 和灵魂文件", "acceptance": "身份和认知信息提取完成"},
    {"name": "创建飞书文档", "description": "整合所有信息，创建并分享飞书文档", "acceptance": "文档创建成功并发送给用户"}
  ]}'
```

# Long Task — 可观测的长程任务执行

## 什么时候使用

当你判断用户的请求**需要 3 个以上独立步骤**才能完成时，主动使用此技能。典型场景：

- 涉及多个文件的功能开发或重构
- 需要先分析、再设计、再实现的复杂任务
- 跨模块修改（改代码 + 改配置 + 改文档 + 测试）

**不要用于**：简单问答、单文件修改、信息查询、一步能完成的操作。

## 如何触发

### 你主动触发（推荐）

对话中发现任务复杂时，调用 `task_ctl.py create` 创建任务请求：

```bash
python3 .claude/skills/long-task/scripts/task_ctl.py create \
  --goal "任务目标描述" \
  --plan '{"steps": [{"name": "步骤名", "description": "做什么", "acceptance": "怎么算完成"}, ...]}'
```

脚本会写入请求文件到 `data/task_requests/`。Hub 在你的响应返回后自动读取，用当前会话上下文创建 TaskPlan，发送飞书卡片供用户确认。

调用后告诉用户你创建了一个长程任务，计划已发送待确认。

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
- 每步应产出**可验证的结果**（文件变更、测试通过、配置生效等）
- 步骤间依赖关系用**执行顺序**隐含表达
- **不要**包含「确认需求」类步骤 — 用户已确认目标
- **不要**包含「总结汇报」类步骤 — 系统自动生成完成报告

## 步骤执行协议

计划获批后，Hub 会逐步调用你执行。每步你会收到：

- 任务总目标
- 当前步骤名称、描述、验收标准
- 之前步骤的上下文（通过 CLI session 保持）

### 执行要求

- 专注当前步骤，不要跳步
- 完成后简要说明**做了什么**和**验收结果**
- 如果步骤无法完成，明确说明原因
- 利用 Claude Code 的工具能力（读写文件、运行命令等）

## 跨 Session 上下文

每次 CLI 调用是独立 session。你可以随时查看任务状态以获取上下文：

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

用户说「帮我重构 briefing 模块，把采集和生成逻辑拆开」，你判断这需要多步：

```bash
python3 .claude/skills/long-task/scripts/task_ctl.py create \
  --goal "重构 briefing 模块，拆分采集和生成逻辑" \
  --plan '{"steps": [
    {"name": "分析现有结构", "description": "阅读 briefing.py，梳理数据流和模块边界", "acceptance": "输出职责清单和依赖关系"},
    {"name": "设计 Collector 接口", "description": "定义采集器输入输出协议", "acceptance": "接口定义写入代码"},
    {"name": "实现 collector.py", "description": "将采集逻辑抽取到独立模块", "acceptance": "collector.py 可独立运行"},
    {"name": "重构 briefing.py", "description": "调用 collector 接口获取数据", "acceptance": "日报生成正常工作"},
    {"name": "端到端测试", "description": "手动触发日报生成验证", "acceptance": "日报正常生成并投递"}
  ]}'
```

然后回复用户：「这个重构涉及 5 个步骤，我已创建长程任务计划，请在飞书确认后开始执行。」

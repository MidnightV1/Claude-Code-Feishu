# Dev Pipeline — 统一开发流水线入口

对话中检测到任何开发相关信号（bug、功能需求、重构、优化、架构调整、链路改造、流程改进、Skill 变更）时触发。CC 作为 LLM 分类器，基于对话上下文判断信号类型和复杂度，路由到 MADS（复合任务）或 MAQS（原子任务）标准流水线并立即启动。

触发场景（CC 自主判断，以下为参考）：
- **Bug/问题**：报错、异常、失败、不工作、不触发、丢失、遗漏、crash
- **功能/需求**：新增、实现、加一个、做一个、支持、接入、对接、打通
- **优化/改造**：优化、整合、合并、改造、升级、提升、增强、链路优化、策略优化、流程优化
- **重构/整理**：重构、整理、清理、拆分、简化、迁移、统一
- **架构/设计**：架构调整、系统设计、方案设计、重新设计、框架改造
- **Skill**：新 Skill、Skill 改进、能力扩展
- **配置**：配置调整、参数修改、阈值变更

> CC 不依赖关键词硬匹配。当对话语义包含「对现有系统的变更意图」时，即应触发本 Skill。

## Tool

```
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py <command> [args]
```

## Commands

### 信号入口（Intake）

```bash
# 从对话创建工单（CC 提供分类参数）
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py intake \
  --phenomenon "描述" \
  --type feature \
  --complexity composite \
  --severity P2 \
  --immediate              # 立即触发 pipeline（对话场景默认使用）

# 干跑分类（不创建工单，仅规则引擎参考）
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py classify --phenomenon "描述"
```

### 工单管理（Ops）

```bash
# 查看活跃工单
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py status

# 带筛选列出工单
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py list --status open --severity P1
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py list --type bug --json

# 查看工单详情
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py show <record_id>

# 手动触发工单流水线推进
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py run <record_id>
```

### 模拟测试

```bash
# 批量场景验证分类准确性
python3 .claude/skills/dev-pipeline/scripts/dev_ctl.py simulate --file .claude/skills/dev-pipeline/tests/scenarios.json
```

## Behavior Notes

### 分类器：CC 即分类器

CC 基于对话上下文判断信号的 type、complexity、severity。规则引擎（classify）仅作为参考和 fallback。

CC 在 `intake` 时 **必须显式传入** `--type`、`--complexity`、`--severity` 参数，基于自身对对话语境的理解。不要依赖规则引擎的默认分类。

### 对话场景：即时执行

**核心原则：对话场景中发现 MADS/MAQS 信号后，应立刻创建工单并启动流水线执行。**

流程：
1. **识别信号**：CC 从对话语义判断是否存在开发信号（不依赖关键词）
2. **分类**：CC 基于上下文确定 type / complexity / severity
3. **创建工单**：`intake --immediate` — 创建 Bitable 工单并立即触发 pipeline
4. **即时反馈**：告知用户工单已创建、pipeline 已启动，预计何时收到设计文档

`--immediate` 会将 mads-pipeline cron 的下次运行时间设为当前时间并触发 scheduler reload，pipeline 在当前对话结束后数秒内自动启动。

### 路由逻辑

| 复杂度 | 流水线 | 适用场景 |
|--------|--------|----------|
| atomic | MAQS | 单文件 bug 修复、小改动、配置调整 |
| composite | MADS | 多文件特性、架构变更、新 Skill 创建 |

### 与 Sentinel 的关系

Dev Pipeline 和 Sentinel 是**并行入口**，共写同一张 Bitable 表：
- Sentinel：自动巡检发现的问题（cron 驱动，source=sentinel:*）
- Dev Pipeline：对话中人工发现的问题（用户驱动，source=chat）

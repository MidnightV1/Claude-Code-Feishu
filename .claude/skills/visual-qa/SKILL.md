# Visual QA — UI 感知与自主验证

客户端 UI 的截图采集、多模态视觉验证、五维度评分。集成到 MADS pipeline 作为 Post-QA 可选阶段。

## Tool

```bash
python3 .claude/skills/visual-qa/scripts/visual_qa_ctl.py <command> [args]
```

## Commands

```bash
# 截图采集（Chrome DevTools Protocol）
visual_qa_ctl.py capture <url> [--viewport 1440x900] [--mobile] [--output DIR]

# 获取 Accessibility Tree
visual_qa_ctl.py a11y <url> [--viewport 1440x900]

# 视觉验证（截图 + A11y Tree + 规约 → 五维度评分）
visual_qa_ctl.py verify <url> --spec "SPEC_TEXT" [--design-ref PATH] [--viewport 1440x900] [--mobile]

# 交互流验证（多步骤截图 + 状态断言）
visual_qa_ctl.py flow <url> --steps "STEPS_JSON" [--viewport 1440x900]

# 完整 QA 报告（capture + a11y + verify，输出 report.md + 截图）
visual_qa_ctl.py report <url> --spec "SPEC_TEXT" --output-dir DIR [--ticket-id ID]

# 状态检查
visual_qa_ctl.py status
```

## When to Use

| 场景 | 命令 | 说明 |
|------|------|------|
| MADS QA 后的视觉验证 | `report` | Contract 中 `visual_qa_required: true` 时自动触发 |
| 开发时预览检查 | `capture` | 快速截图看渲染结果 |
| 无障碍/结构检查 | `a11y` | 获取页面语义结构 |
| 用户交互流测试 | `flow` | 模拟多步操作并截图验证 |
| 设计稿对标 | `verify --design-ref` | 截图 vs 设计稿的视觉+语义 diff |

## 五维度评分

| 维度 | 权重 | 检查点 |
|------|------|--------|
| 功能正确性 | 30% | 元素存在、交互可用、状态正确、Console 无错 |
| 设计还原度 | 25% | 与设计稿/规范一致性，像素级还原 |
| 反 AI 化 | 15% | 不使用默认 AI 模板、有设计意图、平台原生风格 |
| 文案与可读性 | 15% | 文案质量、信息层级、空间节奏、留白 |
| 操作便捷性 | 15% | 视觉动线、反馈即时性、容错设计、触达距离 |

**阈值**：≥ 80 PASS，< 80 FAIL → 自主迭代（最多 3 轮）

## 感知层

**主路径：Chrome DevTools Protocol（CDP）**
- Headless Chrome 实例（`--remote-debugging-port=9223`），launchd 保活
- 端口 9223（9222 被 Claude Code Chrome native host 占用）
- 支持 headless 模式，独立 user-data-dir（`~/.chrome-cdp`）

**备选：Playwright CLI**
- CI 批量截图、多视口并行
- 跨浏览器测试（Firefox、WebKit）

## 输出产物

```
data/mads/{ticket_id}/visual_qa/
├── report.md                    # 文字报告（五维度评分 + 问题清单）
├── score.json                   # 机器可读评分
├── screenshots/                 # 截图证据
│   ├── 01_initial_load.png
│   ├── 02_after_interaction.png
│   ├── 03_mobile_375px.png
│   └── diff_vs_design.png       # 设计稿 diff（如有）
└── a11y_tree.yaml               # Accessibility Tree 快照
```

## 依赖

- Chrome（已安装，146+）
- `websockets` Python 包（已安装）
- 多模态 LLM：Claude（截图分析）或 Gemini（备选）

## MADS 集成

Contract 扩展字段：
```yaml
visual_qa_required: true
visual_qa_spec:
  scenarios:
    - name: "初始加载"
      url: "http://localhost:3000"
      assertions: ["登录按钮可见", "导航栏完整"]
    - name: "移动端"
      viewport: "375x812"
      assertions: ["底部导航不被截断"]
  design_ref: "path/to/design.png"  # 可选
  style_guide: "遵循 Material Design 3"  # 可选
```

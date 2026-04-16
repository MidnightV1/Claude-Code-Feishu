---
name: feishu-board
description: Create and read Feishu boards (画板/白板/流程图) — create boards in documents, draw flowcharts from structured steps, read board content, add shapes and connectors. Use when the user wants to create a flowchart (流程图), diagram (图示/示意图), whiteboard (画板/白板), or read visual content from a Feishu board. Also trigger when converting ASCII/text flowcharts to visual format.
---

# Feishu Boards (画板)

在飞书文档中创建画板，绘制流程图，读取画板内容。

## Tool

```
python3 .claude/skills/feishu-board/scripts/board_ctl.py <command> [args]
```

## Commands

```bash
# Create a board in a document
python3 .claude/skills/feishu-board/scripts/board_ctl.py create --doc <doc_id_or_url>

# Read all nodes from a board
python3 .claude/skills/feishu-board/scripts/board_ctl.py read <board_token>

# Add raw nodes (advanced — see Node Structure below)
python3 .claude/skills/feishu-board/scripts/board_ctl.py add <board_token> --nodes '[{"type":"composite_shape","x":0,"y":0,"width":180,"height":60,"composite_shape":{"type":"rect"},"text":{"text":"Hello"}}]'

# Create a flowchart from steps
python3 .claude/skills/feishu-board/scripts/board_ctl.py flowchart <board_token> --steps '[
  {"text": "开始", "type": "start"},
  {"text": "处理数据", "type": "process"},
  {"text": "是否通过?", "type": "decision", "yes": "输出结果", "no": "错误处理"},
  {"text": "输出结果", "type": "process"},
  {"text": "错误处理", "type": "process"},
  {"text": "结束", "type": "end"}
]'

# Delete a node
python3 .claude/skills/feishu-board/scripts/board_ctl.py delete_node <board_token> <node_id>
```

## Flowchart Step Types

| type | 含义 | 形状 |
|------|------|------|
| `start` | 开始节点 | 圆角矩形（蓝色） |
| `end` | 结束节点 | 圆角矩形（蓝色） |
| `process` | 处理步骤 | 矩形 |
| `decision` | 判断节点 | 菱形（黄色） |
| `io` | 输入/输出 | 平行四边形 |
| `sub` | 子流程 | 双边框矩形 |
| `delay` | 延迟 | 延迟形状 |
| `database` | 数据库 | 圆柱体 |

### Decision 分支

决策节点用 `yes` / `no` 字段指定分支目标（按文本匹配）：
```json
{"text": "审批通过?", "type": "decision", "yes": "执行", "no": "退回修改"}
```

## Workflow

典型使用流程：
1. `create --doc <doc_id>` → 获得 board token
2. `flowchart <token> --steps '[...]'` → 绘制流程图
3. `read <token>` → 验证内容

## Reading Boards from Documents

文档中的画板（block_type=43）通过 Docx API 只能获取 token，需要用 Board API 读取内容：
1. `doc_ctl.py read <doc_id>` → 找到画板 block，获取 token
2. `board_ctl.py read <token>` → 读取画板节点和连线

## Node Structure (API 实测)

飞书画板 API 的节点结构中，**style 和 text 是顶层字段**，不在子类型对象内：

```json
{
  "type": "composite_shape",
  "x": 0, "y": 0, "width": 180, "height": 60,
  "composite_shape": {"type": "rect"},
  "style": {"fill_color": "#3370FF", "border_color": "#3370FF", "border_width": "narrow", "border_style": "solid"},
  "text": {"text": "节点文本", "font_size": 14, "text_color": "#FFFFFF", "horizontal_align": "center", "vertical_align": "mid"}
}
```

## Coordinate System

- 原点 (0,0) 在画布中心
- x 向右为正，y 向下为正
- 节点坐标指向几何中心（不是左上角）

## Valid Node Types (API 验证)

`image`, `text_shape`, `group`, `composite_shape`, `svg`, `connector`, `table`, `life_line`, `activation`, `section`, `table_uml`, `table_er`, `sticky_note`, `mind_map`, `paint`, `combined_fragment`

## Available Shape Types (composite_shape.type)

基础：`rect`, `round_rect`, `ellipse`, `diamond`, `triangle`, `parallelogram`, `trapezoid`, `pentagon`, `hexagon`, `octagon`, `star`, `cross`, `cloud`

流程图：`flow_chart_round_rect`, `flow_chart_diamond`, `flow_chart_parallelogram`, `flow_chart_trapezoid`, `flow_chart_hexagon`, `flow_chart_cylinder`, `flow_chart_mq`

UML：`class_interface`, `classifier`, `actor`, `note_shape`

其他：`document_shape`, `predefined_process`, `manual_input`, `delay_shape`, `off_page_connector`, `cylinder`, `cube`

## Known Limitations

- **Connector（连线）创建 API 未解决**：测试了 30+ 种字段组合，均返回 `4005072: connector info empty`。当前使用文本箭头（↓/→）作为视觉替代。读取已有的 connector 节点可以正常工作
- 布局引擎为简单自上而下排列，复杂布局需手动指定坐标（`add` 命令）
- 单次 API 调用最多 3000 节点
- `text_shape` 工作正常，`sticky_note` 需进一步调试

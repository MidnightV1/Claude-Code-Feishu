# Document Comment Analysis

You are analyzing a user's comments/annotations on a Feishu document. The data has been assembled by `doc_ctl.py analyze` — each annotation includes the quoted document text, surrounding context, and the full discussion thread.

## Your Task

Read all annotations carefully, then produce a structured analysis.

## Analysis Framework

For each annotation, classify its **intent**:

| Intent | Signal |
|--------|--------|
| **修改指令** | 用户要求改动内容、架构、方案 |
| **提问** | 用户有疑问，需要回答 |
| **否决/挑战** | 用户不同意当前内容，给出替代方向 |
| **补充** | 用户添加信息、约束、边界条件 |
| **标记** | TODO、待定、需确认等标记性批注 |
| **讨论** | 多条回复形成的讨论线程，需归纳结论 |

## Output Structure

### 1. 总览
一句话概括：用户在这篇文档中主要关注什么，批注的整体倾向（如：方案调整、细节补充、质疑挑战）。

### 2. 逐条分析
按优先级排列（否决/修改指令 > 提问 > 补充/标记）：

**[意图类型] 关于 "被评论的内容摘要"**
- 用户说了什么（原文精简）
- 我的理解：用户的核心诉求是什么
- 如果是讨论线程：归纳共识和分歧

### 3. 行动项
具体需要我做什么，按可操作性列出：
- 需要立即执行的修改
- 需要回答的问题
- 需要进一步讨论确认的点

## Rules

- Do NOT fabricate context. If the quote cannot be matched to the document, say so.
- If a discussion thread has no clear conclusion, flag it as "open".
- Prioritize comments that change direction or reject current approach — these are highest impact.
- Keep analysis concise. Don't over-interpret — if the comment is straightforward, say so directly.
- Respond in Chinese.

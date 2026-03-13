---
name: arxiv-tracker
description: Track and filter arXiv papers by research topics and keywords. Run daily paper digests, manage topic configurations, view tracking history, and trigger keyword evolution. Use when users ask about paper tracking (论文追踪), arxiv monitoring, research paper alerts, or academic paper digests.
---

# ArXiv Paper Tracker

按领域和关键词追踪 arXiv 新论文，两阶段漏斗筛选高价值论文。

## Tool

```
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py <command> [args]
```

## Commands

```bash
# Run daily paper tracking (default: yesterday's papers)
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py run
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py run --date 2026-03-10

# List current topic configuration
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py topics

# View tracking history
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py history
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py history --days 14

# Trigger keyword evolution analysis
python3 .claude/skills/arxiv-tracker/scripts/arxiv_ctl.py evolve
```

## Architecture

Two-stage funnel:
1. **Keyword pre-filter**: arXiv API → keyword match on title + abstract (~200 → ~30 papers)
2. **LLM deep evaluation**: Gemini 3.1 Pro → 4-dimension score (novelty × 0.25 + rigor × 0.15 + relevance × 0.35 + collaboration × 0.25) → threshold ≥ 3.5

Output → Two Feishu documents:
- **业务日报**: 平铺 + 多标签（无分类重复），含实操建议 + 趋势雷达
- **你可能感兴趣的**: 个人兴趣论文（仅在有 personal 标记时生成）

3. **趋势雷达**（双模型交叉分析）:
   - Round 1: Gemini Pro + Opus 并行独立分析
   - Round 2: 交叉校准（各看对方 R1 结论后修正）
   - Opus 综合渲染最终报告（共识/争议/知乎价值/趋势变化）
   - 持久化 `trend_state.json`：信号强化/衰减/淡出机制

完成后自动通过 Notifier 发送文档链接通知。

## Configuration

Topic and keyword config: `config/topics.yaml`
- Topics are data-driven — change topics by editing YAML, no code changes needed
- Keyword evolution: automatic suggestions based on hit rate analysis

## Schedule

Cron: `0 9 * * 2-6` (Tue-Sat 9:00 AM, covering Mon-Fri arXiv publications)
Handler: `arxiv:daily`

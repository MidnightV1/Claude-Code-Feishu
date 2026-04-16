# -*- coding: utf-8 -*-
"""Auto-scoring for exploration outputs — four-dimensional quality assessment.

Dimensions (calibrated weights from 20-entry analysis):
  - novelty (0.30): New insights vs restating known state
  - depth (0.30): Evidence-backed, data-driven analysis
  - actionability (0.25): Concrete, implementable recommendations
  - efficiency (0.15): Value per resource spent

Tiers (calibrated from 128-entry backtest, target ~25-30% HIGH):
  - HIGH: weighted >= 4.20  (genuinely novel + data-backed + actionable)
  - MED:  weighted 2.5-4.20 (useful investigation, no major surprise)
  - LOW:  weighted < 2.5    (trivial, no analysis depth)

Two scoring modes:
  - rule_score(): Zero-cost heuristic from summary text patterns
  - llm_score(): Sonnet one-shot (~$0.003/call, v4 depth-independent calibrated)
"""

import json
import logging
import math
import re

log = logging.getLogger("hub.scoring")

# ── Weights (from calibration on 20 entries) ──
WEIGHTS = {"novelty": 0.30, "depth": 0.30, "actionability": 0.25, "efficiency": 0.15}
TIER_HIGH = 4.20
TIER_LOW = 2.50


def weighted_score(scores: dict) -> float:
    """Compute weighted average from dimension scores."""
    total = sum(scores.get(dim, 3) * w for dim, w in WEIGHTS.items())
    return round(total, 2)


def tier_label(w: float) -> str:
    if w >= TIER_HIGH:
        return "HIGH"
    if w >= TIER_LOW:
        return "MED"
    return "LOW"


# ═══ Rule-based scoring (zero cost) ═══

# Novelty signals
_NOVEL_POS = re.compile(
    r"发现|首次|意外|实际上|根因|真正的|揭示|盲区|gap|缺失|"
    r"not\s+track|identity\s+crisis|missing|hidden|blind\s+spot|reveals",
    re.IGNORECASE,
)
_NOVEL_NEG = re.compile(
    r"现状|已知|描述|当前状态|status\s+quo|as\s+expected|straightforward",
    re.IGNORECASE,
)

# Actionability signals
_ACTION_POS = re.compile(
    r"推荐行动|Action\s+\d|具体.*步骤|代码改动|实施.*成本|"
    r"行代码|commit|PR|修复方案|implementation|fix:|patch",
    re.IGNORECASE,
)

# Depth signals
_DEPTH_SECTIONS = ["## 结论", "## 关键发现", "## 推荐行动", "## 目标树更新", "## 后续方向"]


def rule_score(summary: str, duration_seconds: int = 0,
               messages_used: int = 1) -> dict:
    """Score an exploration summary using text heuristics.

    Returns dict with novelty/actionability/depth/efficiency (1-5),
    weighted score, and tier.
    """
    if not summary or len(summary) < 50:
        return _build_result(1, 1, 1, 1)

    # ── Novelty (1-5) ──
    pos_hits = len(_NOVEL_POS.findall(summary))
    neg_hits = len(_NOVEL_NEG.findall(summary))
    novelty = _clamp(2 + pos_hits * 0.5 - neg_hits * 0.7)

    # ── Depth (1-5) ──
    # Template sections (结论/关键发现/推荐行动/目标树更新/后续方向) are present in ~80%
    # of explorations — they indicate format compliance, not analytical depth.
    # Cap section bonus and weight substantive signals higher.
    sections_found = sum(1 for s in _DEPTH_SECTIONS if s in summary)
    has_tables = summary.count("|") > 10
    has_code = "```" in summary
    data_points = len(re.findall(r"\d+%|\d+\.\d+|\d+ 条|\d+ 个", summary))
    file_refs = len(re.findall(r"\w+\.(py|yaml|json|md|js|ts)", summary))
    # Cross-referencing: mentions of comparing/correlating multiple sources
    cross_ref = len(re.findall(
        r"vs|对比|相关性|cross.*ref|一致|diverge|偏差|calibrat", summary, re.IGNORECASE
    ))

    depth_base = 1.5
    depth_base += min(sections_found * 0.3, 0.9)  # cap at +0.9 (was +2.5)
    depth_base += 0.4 if has_tables else 0
    depth_base += 0.2 if has_code else 0
    depth_base += min(data_points * 0.15, 1.0)    # quantitative evidence (new)
    depth_base += min(file_refs * 0.08, 0.4)
    depth_base += min(cross_ref * 0.3, 0.6)       # cross-referencing bonus (new)
    depth = _clamp(depth_base)

    # ── Actionability (1-5) ──
    # Previous regex over-triggers on "commit"/"PR" in code references (not actual
    # actions taken). Weight L1 evidence (actual commits) higher than mentions.
    action_hits = len(_ACTION_POS.findall(summary))
    bullet_count = summary.count("\n- ") + summary.count("\n  - ")
    has_l1_commit = bool(re.search(r"\[L1-auto\]|已.*commit|committed|已提交", summary))
    has_file_line = bool(re.search(r"\w+\.py:\d+|line\s+\d+", summary, re.IGNORECASE))
    action_base = 1.5
    action_base += min(action_hits * 0.3, 1.5)   # halved per-hit weight (was 0.6)
    action_base += min(bullet_count * 0.04, 0.6)
    action_base += 0.8 if has_l1_commit else 0    # actual code change (new)
    action_base += 0.4 if has_file_line else 0    # specific file:line references (new)
    actionability = _clamp(action_base)

    # ── Efficiency (1-5) ──
    # Insight density: how much signal per unit of text.
    # Uses continuous scoring to avoid step-function degeneracy.
    slen = len(summary)

    # Numbered findings as primary signal
    numbered_findings = len(re.findall(r"\n\d+\.\s+\*?\*?", summary))
    # Data density: quantitative claims per 1000 chars
    data_mentions = len(re.findall(r"\d+%|\d+\.\d+|\d+ 条|\d+ 个", summary))
    density_per_k = (numbered_findings + data_mentions * 0.5) / max(slen / 1000, 1)
    # Map density to 1-5 continuous: 0 → 1.5, 2 → 2.5, 6+ → 3.8
    # Slower climb (0.5 vs 0.75) and lower cap (3.8 vs 4.5) to prevent
    # ceiling clustering — explorations are data-heavy (density ~6-11),
    # so higher cap saturates the dimension.
    density_score = min(1.5 + density_per_k * 0.5, 3.8)

    # Length penalty: very short = shallow, very long = verbose
    # Optimal range ~1500-3000 chars; log-normal penalty outside
    len_center = 2000
    len_spread = 1.2  # how forgiving
    if slen > 0:
        len_ratio = math.log(slen / len_center)
        len_penalty = -(len_ratio ** 2) / (2 * len_spread ** 2)
        len_adj = max(len_penalty, -1.5)  # cap penalty
    else:
        len_adj = -2.0

    # Duration efficiency: reward fast-and-deep, penalize slow-and-shallow
    if duration_seconds > 0:
        if duration_seconds < 600 and depth >= 3:
            duration_bonus = 0.4
        elif duration_seconds < 1200:
            duration_bonus = 0.2
        elif duration_seconds > 2700 and depth < 3:
            duration_bonus = -0.8
        else:
            duration_bonus = 0.0
    else:
        duration_bonus = 0.0

    efficiency = _clamp(density_score + len_adj + duration_bonus)

    return _build_result(novelty, actionability, depth, efficiency)


def _clamp(v: float, lo: float = 1.0, hi: float = 5.0) -> float:
    return round(max(lo, min(hi, v)), 1)


def _build_result(novelty: float, actionability: float,
                  depth: float, efficiency: float) -> dict:
    scores = {
        "novelty": novelty,
        "actionability": actionability,
        "depth": depth,
        "efficiency": efficiency,
    }
    w = weighted_score(scores)
    scores["weighted"] = w
    scores["tier"] = tier_label(w)
    return scores


# ═══ LLM-based scoring (optional, ~$0.003/call) ═══

_SCORE_PROMPT = """Score this exploration output on 4 dimensions (1-5 integer).
Be critical and use the FULL range 1-5. Most explorations deserve 2-3 on each dimension.

CRITICAL: 76% of past LLM scores gave depth=4.0. This is wrong — depth=4 requires
cross-referencing 3+ independent data sources with quantitative comparison. Reading code
and listing findings is depth=2-3, not 4. Score depth INDEPENDENTLY of other dimensions.

Target distribution per 10 explorations: 2-3 HIGH (weighted>=4.20), 5-6 MED, 1-2 LOW.

Dimensions:
1. novelty (weight 0.30): Did it reveal something the team didn't know?
   1=restated known facts or code comments, 2=confirmed existing hypothesis with data, 3=new data point but expected direction, 4=unexpected finding that changes approach, 5=fundamental hidden bug or design flaw discovered
2. depth (weight 0.30): How rigorous is the evidence? Score this INDEPENDENTLY.
   1=surface scan, no numbers, 2=read code and described what it does, 3=quantified with metrics from one source (e.g. log counts, code grep), 4=cross-referenced 3+ independent sources with quantitative comparison (e.g. log data vs config vs git history, with tables showing discrepancies), 5=statistical analysis with baselines, confidence measures, or systematic audit across full dataset
3. actionability (weight 0.25): How concrete are the recommendations?
   1=vague "should optimize", 2=identified problem but no solution, 3=specific suggestion with rationale, 4=exact code changes with file/line, 5=code changes done + committed in the exploration
4. efficiency (weight 0.15): Value density — insight per resource spent?
   1=trivial finding after long exploration, 2=one useful finding in a long session, 3=reasonable ratio of findings to effort, 4=multiple findings efficiently, 5=high-impact discovery in minimal time

Calibration examples (use these as anchors — note depth varies independently):
- "Discovered Sentinel→MAQS pipeline never connected — traced through config routing, verified 0 tickets in MAQS log, cross-checked Sentinel output format vs MAQS intake schema" → {{"novelty":5,"depth":4,"actionability":4,"efficiency":4}} (HIGH: hidden bug + multi-source evidence)
- "Quantified 48 real timeout cases from production logs, computed latency percentiles, compared against 3 config thresholds, debunked assumed root cause with statistical evidence" → {{"novelty":4,"depth":5,"actionability":4,"efficiency":4}} (HIGH: rigorous statistical depth)
- "Read exploration_scoring.py, listed the 4 dimensions and their weights, noted the tier thresholds" → {{"novelty":1,"depth":2,"actionability":2,"efficiency":2}} (LOW: described code, no analysis)
- "Checked error tracker, found 12 errors/hour spike, identified it as API outage from log timestamps" → {{"novelty":3,"depth":3,"actionability":3,"efficiency":3}} (MED: single-source quantified)
- "personal-intel domain already activated 9 days ago, exploration premise was stale" → {{"novelty":1,"depth":2,"actionability":1,"efficiency":1}} (LOW: wasted effort on outdated premise)

Exploration title: {title}
Exploration output (truncated):
{summary}

Return ONLY a JSON object, no other text:
{{"novelty":N,"depth":N,"actionability":N,"efficiency":N,"reasoning":"one sentence"}}
Respond with JSON only, no other text."""


async def llm_score(title: str, summary: str, router) -> dict | None:
    """Score using Sonnet — more accurate but costs ~$0.003.

    Args:
        title: Exploration task title
        summary: Exploration output text
        router: LLMRouter instance for making the call

    Returns:
        Score dict or None if LLM call fails.
    """
    from agent.infra.models import LLMConfig

    prompt = _SCORE_PROMPT.format(
        title=title,
        summary=summary[:3000],  # truncate to keep cost low
    )

    try:
        result = await router.run(
            prompt=prompt,
            llm_config=LLMConfig(
                provider="claude-cli",
                model="sonnet",
                effort="low",
                timeout_seconds=30,
            ),
            session_key=None,
        )

        if result.is_error or not result.text:
            log.warning("LLM scoring failed: %s", result.text[:200] if result.text else "empty")
            return None

        # Extract JSON from response (may have markdown wrapping)
        text = result.text.strip()
        # Try ```json ... ``` block first
        block_match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if block_match:
            json_match_text = block_match.group(1)
        else:
            json_match = re.search(r"\{.*?\}", text, re.DOTALL)
            if not json_match:
                log.warning("LLM scoring: no JSON found in response")
                return None
            json_match_text = json_match.group()

        data = json.loads(json_match_text)

        # Validate dimensions present and in range
        dims = ["novelty", "depth", "actionability", "efficiency"]
        for dim in dims:
            if dim not in data or not isinstance(data[dim], (int, float)):
                log.warning("LLM scoring: missing dimension %s", dim)
                return None
            data[dim] = _clamp(float(data[dim]))

        w = weighted_score(data)
        data["weighted"] = w
        data["tier"] = tier_label(w)
        data["method"] = "llm"
        data["prompt_version"] = 5  # v5: TIER_HIGH 3.75→4.20 (128-entry backtest calibration)
        return data

    except Exception as e:
        log.warning("LLM scoring exception: %s", e)
        return None


async def score_exploration(title: str, summary: str,
                            duration_seconds: int = 0,
                            messages_used: int = 1,
                            router=None,
                            use_llm: bool = False) -> dict:
    """Score an exploration output, preferring LLM if available.

    Falls back to rule-based scoring if LLM is unavailable or fails.
    """
    # Always compute rule-based as baseline
    rules = rule_score(summary, duration_seconds, messages_used)
    rules["method"] = "rule"

    if use_llm and router:
        llm = await llm_score(title, summary, router)
        if llm:
            # Reject degenerate all-4.0 scores — LLM clusters at 4.0 in 38% of cases
            dims_vals = [llm.get(d) for d in ("novelty", "depth", "actionability", "efficiency")]
            if all(v == 4.0 for v in dims_vals):
                log.info("LLM scored all-4.0 (degenerate), falling back to rule scores")
                rules["llm_rejected"] = "all-4.0-cluster"
                return rules

            llm["rule_baseline"] = {
                "novelty": rules["novelty"],
                "depth": rules["depth"],
                "actionability": rules["actionability"],
                "efficiency": rules["efficiency"],
                "weighted": rules["weighted"],
                "tier": rules["tier"],
            }
            return llm

    return rules

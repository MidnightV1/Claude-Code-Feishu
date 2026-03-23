#!/usr/bin/env python3
"""Award screening script for 「让 AI 在我的专业领域里让 GPU 冒烟的顶级难题」.

Reads candidate entries from Feishu sheet, evaluates each via Gemini 3.1 Pro
with multimodal input (text + images), and writes scores back.

Usage:
    # Sample 20 entries
    python3 scripts/award_screen.py --sample 20

    # Full run (all A-高质量 + B-可用)
    python3 scripts/award_screen.py

    # Resume from a specific row
    python3 scripts/award_screen.py --resume-from 100
"""

import argparse
import asyncio
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import yaml
from agent.platforms.feishu.api import FeishuAPI

# ── Config ──────────────────────────────────────────────
SHEET_TOKEN = "HkkSsnNB1hJBLQtjmKzc04MMnrb"
SHEET_ID = "39553c"
MAX_CONCURRENCY = 20

# Column mapping (0-indexed from A) — updated after sheet restructure
COL = {
    "human_review": 0,  # A: 人类复筛
    "award": 1,         # B: 🤖获奖筛选 (output)
    "note": 2,          # C: 🤖备注 (output)
    "yuegao": 3,        # D: 约稿
    "fullname": 4,      # E: fullname
    "content_id": 5,    # F: content_id
    "type": 6,          # G: 内容类型
    "title": 7,         # H: 内容标题
    "content": 8,       # I: content
    "image_urls": 9,    # J: image_urls
    "link": 10,         # K: 内容链接
    "upvotes": 11,      # L: 赞同数
    "comments": 12,     # M: 评论数
    "collects": 13,     # N: 收藏数
    "pv": 14,           # O: pv
    "badges": 28,       # AC: 黄标+蓝标
    "filter": 29,       # AD: 初筛结果
    "value_tier": 30,   # AE: 内容价值分级
    "quiz": 31,         # AF: quiz content
}

EVAL_PROMPT = """你是一个严格的专业评审，需要完成两项独立评估任务。请依次完成，将结果合并在一个 JSON 中输出。

═══════════════════════════════════════════
PART 1: 获奖筛选
═══════════════════════════════════════════

为知乎「让 AI 在我的专业领域里让 GPU 冒烟的顶级难题」奖项筛选获奖内容。

### 奖项定位

寻找能够在特定专业领域持续产出高质量 AI 评测题目/rubrics 的专家型用户。获奖内容必须同时满足：
1. **指向明确的学科场景**：内容聚焦于某个具体专业领域（如医学影像诊断、结构工程计算、古典文献校勘等），而非泛泛的日常观察
2. **揭示具体的模型能力缺陷**：不是"AI不行"的笼统吐槽，而是精准定位了模型在该领域的具体薄弱环节，且该缺陷具有可复现性和可评测性
3. **体现答主的专业判断力**：内容展现出答主具备该领域的专业训练或深度实践经验，能够设计出有明确质量评判标准的评测题目

### 严格排除标准（命中任一条直接判为不合格）

- 纯日常常识测试（如洗车/走路/雨天浇花等），除非从认知科学/语言学等学术角度做了深入的专业分析
- 仅列举多个模型的测试截图做横评对比，但缺乏专业分析和深度见解
- 脑筋急转弯、文字游戏、绕口令等娱乐性测试
- 内容主要是产品评测/使用体验分享，而非专业领域的能力评估
- 泛泛的"AI编故事不行/AI数学不好"等无具体场景的笼统评论
- 内容过短（实质性分析不足200字）或主要是转述他人观点
- **自媒体/KOL风格内容**：行文以吸引流量为导向（夸张标题、情绪化表述、过度使用排版技巧），即使涉及专业话题，若核心是"展示见解"而非"提出可复现的评测场景"，应判为不合格
- **纯深度分析无场景价值**：对AI能力做了详尽分析但未提出具体的、可操作的评测问题/场景，本质是评论文章而非出题素材

### 评分维度

1. **professional_depth** (1-5): 专业深度
   - 5: 明确的学科背景+深厚的领域知识，内容直指该领域核心难题
   - 4: 清晰的专业视角，有实际领域经验支撑
   - 3: 有一定专业性但不够深入，或领域不够明确
   - 2: 泛技术讨论，无特定学科聚焦
   - 1: 纯日常观察，无专业门槛

2. **deficiency_quality** (1-5): 场景的生产价值（核心维度）
   - 5: 提出了明确的专业场景，可直接转化为有标准答案/评判rubric的评测题目，具有实际生产价值
   - 4: 场景具体且有生产潜力，稍加提炼即可成为评测题
   - 3: 有场景但不够具体，或场景本身缺乏评测价值（如纯展示型分析）
   - 2: 场景模糊或实质是体验报告/观点输出，无法转化为可评测题目
   - 1: 无实质场景，纯观点/吐槽/横评

3. **expert_potential** (1-5): 持续产出潜力
   - 5: 明显具备持续出题/设计评测 rubric 的能力，内容体现了系统性思考
   - 4: 有较好的出题潜力，分析方法可迁移
   - 3: 有一定潜力但不确定是否能持续产出
   - 2: 可能只是偶然发现，持续性存疑
   - 1: 一次性内容，无持续产出迹象

### 评分规则

composite_score = professional_depth × 0.30 + deficiency_quality × 0.40 + expert_potential × 0.30
verdict: composite_score >= 3.0 且三个分项都 >= 2 → PASS，否则 FAIL
P1条件: composite_score >= 3.5 且 deficiency_quality >= 4（场景必须有实际生产价值）
P2条件: 其余PASS（即使总分很高，deficiency_quality < 4 不得判为P1）

═══════════════════════════════════════════
PART 2: 复合学科冲突特征识别
═══════════════════════════════════════════

独立于 PART 1，判断该内容中提出的问题/场景是否包含"静态知识在动态约束下的博弈"。识别是否存在多套规则体系的冲突、长链条的因果推演或跨维度的逻辑重构。

### 六维冲突特征

逐一检查以下特征，仅标记**确实存在**的（不要勉强匹配）：

1. **dimensional_intrusion**（跨维变量引入）：在既有封闭系统（如古代、某IP世界、纯物理环境）中强行引入异质变量（如现代科技、算力），要求推演系统重构路径
2. **boundary_arbitrage**（规则边界博弈）：同一物理事实在不同社会属性（如法律 vs 医学、道德 vs 商业）下的定性分歧与利益权衡
3. **counterfactual_network**（反事实网络重建）：剔除现实/历史中的某个核心节点，基于剩余规则重新推演庞大且自洽的新系统
4. **cross_context_encoding**（跨语境知识编码）：将硬核理工科参数或计算指令深度嵌套在人文、艺术、历史等非结构化感性表达中
5. **causal_cascading**（长链条因果传导）：A 领域的微观震荡，跨越多个中间领域，最终导致远端 Z 领域的具体变化
6. **spurious_correlation**（伪相关逻辑陷阱）：刻意将两个无真实逻辑关联的实体或现象强行绑定，测试解答者是否会陷入强行论证

### 判定规则

- compound_discipline: 命中至少1个特征 → true，否则 → false
- 简单的知识检索、单线程逻辑、无意义的文字游戏不算复合学科
- 日常常识问题即使涉及多个生活场景也不算

═══════════════════════════════════════════
输出格式（严格 JSON，不要有任何其他文字）
═══════════════════════════════════════════

{
  "content_id": "原始ID",
  "professional_depth": 1-5,
  "deficiency_quality": 1-5,
  "expert_potential": 1-5,
  "composite_score": 加权分数（保留1位小数）,
  "field": "专业领域（尽量具体）",
  "reasoning": "一句话评分理由（50字以内，若为自媒体风格请明确指出）",
  "verdict": "PASS 或 FAIL",
  "compound_discipline": true/false,
  "conflict_features": ["命中的特征名称列表，如 boundary_arbitrage, causal_cascading"],
  "compound_note": "一句话说明为何判定为复合/非复合（30字以内）"
}

═══════════════════════════════════════════
待评审内容
═══════════════════════════════════════════

**内容ID**: $CONTENT_ID
**标题**: $TITLE
**作者**: $FULLNAME
**作者标签**: $BADGES
**是否约稿**: $YUEGAO
**互动数据**: 赞同 $UPVOTES | 评论 $COMMENTS | 收藏 $COLLECTS | 浏览 $PV

**正文内容**:
$CONTENT

**提炼的测试题（如有）**:
$QUIZ
"""


def load_config():
    with open(BASE / "config.yaml") as f:
        return yaml.safe_load(f)


def get_gemini_client(cfg):
    """Initialize Gemini API client."""
    from google import genai
    api_key = cfg["llm"]["gemini-api"]["api_key"]
    client = genai.Client(api_key=api_key)
    return client


def read_all_candidates(api, skip_existing: bool = False):
    """Read all rows and filter to A-高质量 + B-可用."""
    import urllib.parse as _up
    range_spec = f"{SHEET_ID}!A2:AK1692"
    url = f"/open-apis/sheets/v2/spreadsheets/{SHEET_TOKEN}/values/{_up.quote(range_spec, safe='')}"
    resp = api.get(url, params={"valueRenderOption": "ToString"})
    if resp.get("code") != 0:
        print(f"ERROR reading sheet: {resp.get('msg')}", file=sys.stderr)
        sys.exit(1)

    rows = resp.get("data", {}).get("valueRange", {}).get("values", [])
    candidates = []
    skipped_existing = 0
    for i, row in enumerate(rows):
        row_num = i + 2  # 1-indexed, skip header
        row = row + [None] * (37 - len(row))

        # Skip rows that already have bot results
        if skip_existing:
            award_val = str(row[COL["award"]] or "").strip()
            if award_val:
                skipped_existing += 1
                continue

        filter_val = str(row[COL["filter"]] or "")
        if filter_val not in ("A-高质量", "B-可用"):
            continue

        content = str(row[COL["content"]] or "")
        quiz = str(row[COL["quiz"]] or "")
        image_urls = str(row[COL["image_urls"]] or "")

        # Accept if: content >= 50 chars, OR has quiz content, OR has images
        has_content = content and content != "#UNSUPPORT VALUE" and len(content) >= 50
        has_quiz = quiz and quiz != "解析失败"
        has_images = "http" in image_urls

        if not (has_content or has_quiz or has_images):
            continue

        candidates.append({
            "row_num": row_num,
            "content_id": str(row[COL["content_id"]] or ""),
            "title": str(row[COL["title"]] or ""),
            "content": content,
            "quiz": quiz,
            "image_urls": image_urls,
            "fullname": str(row[COL["fullname"]] or ""),
            "badges": str(row[COL["badges"]] or ""),
            "yuegao": "是" if str(row[COL["yuegao"]] or "") == "约稿" else "否",
            "upvotes": str(row[COL["upvotes"]] or "0"),
            "comments": str(row[COL["comments"]] or "0"),
            "collects": str(row[COL["collects"]] or "0"),
            "pv": str(row[COL["pv"]] or "0"),
            "filter": filter_val,
            "value_tier": str(row[COL["value_tier"]] or ""),
        })

    if skip_existing:
        print(f"Skipped {skipped_existing} rows with existing results")
    return candidates


def parse_image_urls(url_str: str) -> list[str]:
    """Extract image URLs from the string representation."""
    if not url_str or url_str in ("[]", "None", "#UNSUPPORT VALUE"):
        return []
    urls = re.findall(r"https?://[^\s',\]\)]+", url_str)
    return urls[:5]  # Limit to 5 images max


def download_image(url: str, timeout: int = 10) -> tuple[bytes, str] | None:
    """Download image and return (bytes, mime_type)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            ct = resp.headers.get("Content-Type", "image/jpeg")
            if "png" in ct:
                mime = "image/png"
            elif "gif" in ct:
                mime = "image/gif"
            elif "webp" in ct:
                mime = "image/webp"
            else:
                mime = "image/jpeg"
            return data, mime
    except Exception:
        return None


def build_prompt_parts(entry: dict):
    """Build multimodal content parts for Gemini."""
    from google.genai import types

    content_text = entry["content"][:8000]
    quiz_text = entry.get("quiz", "")[:2000] or "（无）"
    prompt_text = (EVAL_PROMPT
        .replace("$CONTENT_ID", entry["content_id"])
        .replace("$TITLE", entry["title"])
        .replace("$FULLNAME", entry["fullname"])
        .replace("$BADGES", entry["badges"])
        .replace("$YUEGAO", entry["yuegao"])
        .replace("$UPVOTES", entry["upvotes"])
        .replace("$COMMENTS", entry["comments"])
        .replace("$COLLECTS", entry["collects"])
        .replace("$PV", entry["pv"])
        .replace("$CONTENT", content_text)
        .replace("$QUIZ", quiz_text)
    )

    parts = [types.Part.from_text(text=prompt_text)]

    image_urls = parse_image_urls(entry["image_urls"])
    for url in image_urls:
        img_data = download_image(url)
        if img_data:
            data, mime = img_data
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))

    return parts


async def evaluate_entry_async(client, entry: dict, semaphore: asyncio.Semaphore,
                                progress: dict) -> dict | None:
    """Evaluate a single entry using Gemini 3.1 Pro (async with semaphore)."""
    from google.genai import types

    async with semaphore:
        idx = progress["done"] + 1
        total = progress["total"]
        cid = entry["content_id"]
        name = entry["fullname"]
        title = entry["title"][:40]

        # Build parts in thread pool (image downloads are blocking)
        loop = asyncio.get_event_loop()
        parts = await loop.run_in_executor(None, build_prompt_parts, entry)

        # Call Gemini API
        try:
            response = await client.aio.models.generate_content(
                model="gemini-3.1-pro-preview",
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_budget=2048),
                    temperature=0.3,
                    response_mime_type="application/json",
                ),
            )
            text = response.text.strip()
            result = json.loads(text)
            if isinstance(result, list):
                result = result[0] if result else None
        except json.JSONDecodeError:
            match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
            if match:
                try:
                    result = json.loads(match.group())
                except json.JSONDecodeError:
                    result = None
            else:
                result = None
            if not result:
                progress["done"] += 1
                progress["errors"] += 1
                print(f"[{idx}/{total}] ERROR parse: {cid} ({name})")
                return None
        except Exception as e:
            progress["done"] += 1
            progress["errors"] += 1
            print(f"[{idx}/{total}] ERROR api: {cid} ({name}) - {e}")
            return None

        if result:
            result["row_num"] = entry["row_num"]
            result["fullname"] = name
            result["yuegao"] = entry["yuegao"]

            verdict = result.get("verdict", "?")
            score = result.get("composite_score", 0)
            field = result.get("field", "?")

            progress["done"] += 1
            if verdict == "PASS":
                progress["passed"] += 1

            print(f"[{progress['done']}/{total}] {verdict} {score:.1f} | {cid} ({name}) | {field}")
        else:
            progress["done"] += 1
            progress["errors"] += 1

        return result


def write_results_to_sheet(api, results: list[dict]):
    """Write evaluation results back to sheet: B:C (award) + AL:AM (compound)."""
    import urllib.parse as _up
    results_sorted = sorted(results, key=lambda r: r["row_num"])

    written = 0
    errors = 0
    for r in results_sorted:
        row = r["row_num"]
        score = r.get("composite_score", 0)
        verdict = r.get("verdict", "FAIL")
        field = r.get("field", "")
        reasoning = r.get("reasoning", "")

        if verdict == "PASS":
            dq = r.get("deficiency_quality", 0)
            award_val = "P1" if score >= 3.5 and dq >= 4 else "P2"
        else:
            award_val = "FAIL"

        note = f"分数{score:.1f} | {field} | {reasoning}"

        # Compound discipline fields
        compound = r.get("compound_discipline", False)
        features = r.get("conflict_features", [])
        compound_note = r.get("compound_note", "")
        compound_val = "是" if compound else "否"
        features_val = ", ".join(features) if features else ""
        if compound_note:
            features_val = f"{features_val} | {compound_note}" if features_val else compound_note

        # Write B:C (award + note)
        range_bc = f"{SHEET_ID}!B{row}:C{row}"
        body_bc = {"valueRange": {"range": range_bc, "values": [[award_val, note]]}}
        resp1 = api.put(
            f"/open-apis/sheets/v2/spreadsheets/{SHEET_TOKEN}/values",
            body=body_bc,
        )

        # Write AL:AM (compound + features)
        range_al = f"{SHEET_ID}!AL{row}:AM{row}"
        body_al = {"valueRange": {"range": range_al, "values": [[compound_val, features_val]]}}
        resp2 = api.put(
            f"/open-apis/sheets/v2/spreadsheets/{SHEET_TOKEN}/values",
            body=body_al,
        )

        if resp1.get("code") != 0 or resp2.get("code") != 0:
            errors += 1
            if resp1.get("code") != 0:
                print(f"  [ERROR] B:C write failed for row {row}: {resp1.get('msg')}")
            if resp2.get("code") != 0:
                print(f"  [ERROR] AL:AM write failed for row {row}: {resp2.get('msg')}")
        else:
            written += 1

    print(f"Written {written}/{len(results_sorted)} rows to sheet ({errors} errors).")


async def run_evaluation(client, candidates: list[dict], concurrency: int = 20) -> list[dict]:
    """Run evaluation on all candidates with concurrency control."""
    semaphore = asyncio.Semaphore(concurrency)
    progress = {"done": 0, "total": len(candidates), "passed": 0, "errors": 0}

    tasks = [
        evaluate_entry_async(client, entry, semaphore, progress)
        for entry in candidates
    ]

    results_raw = await asyncio.gather(*tasks)
    results = [r for r in results_raw if r is not None]

    return results


def main():
    parser = argparse.ArgumentParser(description="Award screening for GPU冒烟 category")
    parser.add_argument("--sample", type=int, default=0,
                        help="Sample N entries for testing (0 = all)")
    parser.add_argument("--resume-from", type=int, default=0,
                        help="Resume from row number (skip earlier rows)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't write results back to sheet")
    parser.add_argument("--concurrency", type=int, default=MAX_CONCURRENCY,
                        help=f"Max concurrent API calls (default: {MAX_CONCURRENCY})")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Only process rows without existing bot results")
    args = parser.parse_args()

    concurrency = args.concurrency

    cfg = load_config()
    api = FeishuAPI.from_config()
    client = get_gemini_client(cfg)

    print("Reading candidates from sheet...")
    candidates = read_all_candidates(api, skip_existing=args.skip_existing)
    print(f"Found {len(candidates)} candidates (A-高质量 + B-可用, content >= 50 chars)")

    if args.resume_from:
        candidates = [c for c in candidates if c["row_num"] >= args.resume_from]
        print(f"Resuming from row {args.resume_from}: {len(candidates)} remaining")

    if args.sample:
        high_q = [c for c in candidates if c["filter"] == "A-高质量"]
        usable = [c for c in candidates if c["filter"] == "B-可用"]
        n_high = min(args.sample * 2 // 3, len(high_q))
        n_usable = min(args.sample - n_high, len(usable))
        random.seed(42)
        candidates = random.sample(high_q, n_high) + random.sample(usable, n_usable)
        print(f"Sampled {len(candidates)} entries ({n_high} A-高质量, {n_usable} B-可用)")

    print(f"\nStarting evaluation with {concurrency} concurrent workers...")
    results = asyncio.run(run_evaluation(client, candidates, concurrency))

    # Summary
    print(f"\n{'='*60}")
    print(f"Evaluated: {len(results)}/{len(candidates)}")
    passed = [r for r in results if r.get("verdict") == "PASS"]
    p1 = [r for r in passed if r.get("composite_score", 0) >= 3.5 and r.get("deficiency_quality", 0) >= 4]
    p2 = [r for r in passed if not (r.get("composite_score", 0) >= 3.5 and r.get("deficiency_quality", 0) >= 4)]
    compound = [r for r in results if r.get("compound_discipline")]
    print(f"PASS: {len(passed)} (P1: {len(p1)}, P2: {len(p2)})")
    print(f"FAIL: {len(results) - len(passed)}")
    print(f"Compound discipline: {len(compound)}/{len(results)}")

    if passed:
        fields = {}
        for r in passed:
            f = r.get("field", "unknown")
            fields[f] = fields.get(f, 0) + 1
        print(f"\nField distribution:")
        for f, c in sorted(fields.items(), key=lambda x: -x[1]):
            print(f"  [{c}] {f}")

    if compound:
        feat_counts = {}
        for r in compound:
            for feat in r.get("conflict_features", []):
                feat_counts[feat] = feat_counts.get(feat, 0) + 1
        print(f"\nConflict feature distribution:")
        for f, c in sorted(feat_counts.items(), key=lambda x: -x[1]):
            print(f"  [{c}] {f}")

    # Write back
    if not args.dry_run and results:
        print(f"\nWriting results to sheet...")
        write_results_to_sheet(api, results)
        print("Done!")
    elif args.dry_run:
        print("\n[DRY RUN] Results not written to sheet.")

    # Save raw results to file (append mode: merge with existing)
    out_path = BASE / "data" / "award_screen_results.json"
    out_path.parent.mkdir(exist_ok=True)
    existing = []
    if out_path.exists():
        with open(out_path) as f:
            existing = json.load(f)
    # Merge: new results override by row_num
    existing_by_row = {r["row_num"]: r for r in existing}
    for r in results:
        existing_by_row[r["row_num"]] = r
    merged = sorted(existing_by_row.values(), key=lambda r: r["row_num"])
    with open(out_path, "w") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"Raw results saved to {out_path} ({len(merged)} total, {len(results)} new)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Benchmark: Gemini 3 Flash (CLI) vs 3.1 Flash-Lite (API) — search quality comparison.

Measures: latency, citation count, output length, source diversity.
- Gemini 3 Flash: via CLI (production path, google_web_search built-in)
- Gemini 3.1 Flash-Lite: via Python SDK + Google Search Grounding tool

Usage:
    python3 scripts/bench_gemini_search.py
    python3 scripts/bench_gemini_search.py --cases zh-01 en-05
    python3 scripts/bench_gemini_search.py --output data/bench_gemini_search.json
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import yaml
from dataclasses import dataclass, asdict
from pathlib import Path

GEMINI_PATH = shutil.which("gemini") or os.path.expanduser("~/.npm-global/bin/gemini")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

MODELS = {
    "gemini-3-flash-preview": "cli",
    "gemini-3.1-flash-lite-preview": "api",
}

# ── Test cases: 12 zh + 12 en = 24 per model, 48 total ────

CASES = [
    # Chinese (12)
    {"id": "zh-01", "lang": "zh", "query": "2026年AI Agent最新进展和趋势"},
    {"id": "zh-02", "lang": "zh", "query": "苹果M5芯片性能评测和对比"},
    {"id": "zh-03", "lang": "zh", "query": "中国新能源汽车2026年出口数据"},
    {"id": "zh-04", "lang": "zh", "query": "大语言模型幻觉问题最新解决方案"},
    {"id": "zh-05", "lang": "zh", "query": "2026年全球半导体产业格局变化"},
    {"id": "zh-06", "lang": "zh", "query": "量子计算最新突破 2026"},
    {"id": "zh-07", "lang": "zh", "query": "日本核污水排放最新监测数据"},
    {"id": "zh-08", "lang": "zh", "query": "OpenAI最新模型发布和技术细节"},
    {"id": "zh-09", "lang": "zh", "query": "中美科技竞争最新动态 芯片"},
    {"id": "zh-10", "lang": "zh", "query": "飞书多维表格和Notion对比 2026"},
    {"id": "zh-11", "lang": "zh", "query": "马斯克Neuralink脑机接口最新进展"},
    {"id": "zh-12", "lang": "zh", "query": "Rust语言在嵌入式开发中的应用现状"},
    # English (12)
    {"id": "en-01", "lang": "en", "query": "latest breakthroughs in nuclear fusion energy 2026"},
    {"id": "en-02", "lang": "en", "query": "Claude Code vs Cursor vs GitHub Copilot comparison 2026"},
    {"id": "en-03", "lang": "en", "query": "SpaceX Starship latest launch results"},
    {"id": "en-04", "lang": "en", "query": "NVIDIA Blackwell GPU benchmarks and availability"},
    {"id": "en-05", "lang": "en", "query": "state of WebAssembly adoption 2026"},
    {"id": "en-06", "lang": "en", "query": "EU AI Act implementation progress and impact"},
    {"id": "en-07", "lang": "en", "query": "latest research on protein folding prediction accuracy"},
    {"id": "en-08", "lang": "en", "query": "Anthropic Claude 4 model capabilities and benchmarks"},
    {"id": "en-09", "lang": "en", "query": "global inflation trends and central bank policies 2026"},
    {"id": "en-10", "lang": "en", "query": "Apple Vision Pro 2 rumors and release date"},
    {"id": "en-11", "lang": "en", "query": "Kubernetes vs serverless architecture trade-offs 2026"},
    {"id": "en-12", "lang": "en", "query": "CRISPR gene therapy approved treatments 2026"},
]

# Prompt for CLI mode (same as production gemini_ctl.py)
CLI_PROMPT = """\
You MUST use your google_web_search tool to search for: {query}

Requirements:
- Search thoroughly, use multiple relevant queries if needed
- Synthesize information from multiple sources
- Cite sources with URLs
- Be concise but comprehensive — target under 2000 characters
- Output language: {lang}\
"""

# Prompt for API mode (Google Search Grounding handles search automatically)
API_PROMPT = """\
Search for and provide comprehensive information about: {query}

Requirements:
- Synthesize information from multiple sources
- Cite sources with URLs where available
- Be concise but comprehensive — target under 2000 characters
- Output language: {lang}\
"""

# ── Metrics ────────────────────────────────────────────────

URL_RE = re.compile(r'https?://[^\s\)\]>"\'，。）]+')
DOMAIN_RE = re.compile(r'https?://(?:www\.)?([^/\s]+)')


@dataclass
class Result:
    case_id: str
    model: str
    mode: str  # "cli" or "api"
    lang: str
    query: str
    latency_s: float
    output_len: int
    url_count: int
    unique_domains: int
    domain_list: list
    grounding_chunks: int  # API-only: number of grounding source chunks
    output_text: str
    error: str | None = None


def extract_metrics(text: str) -> tuple[int, int, list[str]]:
    urls = URL_RE.findall(text)
    domains = []
    for url in urls:
        m = DOMAIN_RE.match(url)
        if m:
            domains.append(m.group(1))
    unique = list(dict.fromkeys(domains))
    return len(urls), len(unique), unique


# ── CLI Runner (Gemini 3 Flash) ───────────────────────────

def run_cli(case: dict, model: str) -> Result:
    lang_map = {"zh": "Chinese", "en": "English"}
    prompt = CLI_PROMPT.format(query=case["query"], lang=lang_map[case["lang"]])
    args = [GEMINI_PATH, "--model", model]

    start = time.monotonic()
    try:
        proc = subprocess.run(
            args, input=prompt, capture_output=True, text=True, timeout=90
        )
        elapsed = round(time.monotonic() - start, 2)

        if proc.returncode != 0:
            return Result(
                case_id=case["id"], model=model, mode="cli", lang=case["lang"],
                query=case["query"], latency_s=elapsed, output_len=0,
                url_count=0, unique_domains=0, domain_list=[], grounding_chunks=0,
                output_text="", error=proc.stderr[:300]
            )

        text = proc.stdout.strip()
        url_count, unique_domains, domain_list = extract_metrics(text)
        return Result(
            case_id=case["id"], model=model, mode="cli", lang=case["lang"],
            query=case["query"], latency_s=elapsed, output_len=len(text),
            url_count=url_count, unique_domains=unique_domains,
            domain_list=domain_list, grounding_chunks=0, output_text=text
        )

    except subprocess.TimeoutExpired:
        elapsed = round(time.monotonic() - start, 2)
        return Result(
            case_id=case["id"], model=model, mode="cli", lang=case["lang"],
            query=case["query"], latency_s=elapsed, output_len=0,
            url_count=0, unique_domains=0, domain_list=[], grounding_chunks=0,
            output_text="", error="TIMEOUT (90s)"
        )


# ── API Runner (Gemini 3.1 Flash-Lite + Search Grounding) ─

_api_client = None


def _get_api_client():
    global _api_client
    if _api_client is None:
        from google import genai
        config_path = PROJECT_ROOT / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        api_key = cfg["llm"]["gemini-api"]["api_key"]
        _api_client = genai.Client(api_key=api_key)
    return _api_client


def run_api(case: dict, model: str) -> Result:
    from google.genai import types

    lang_map = {"zh": "Chinese", "en": "English"}
    prompt = API_PROMPT.format(query=case["query"], lang=lang_map[case["lang"]])
    client = _get_api_client()

    start = time.monotonic()
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            )
        )
        elapsed = round(time.monotonic() - start, 2)

        text = response.text.strip() if response.text else ""

        # Extract grounding metadata
        grounding_chunks = 0
        grounding_domains = []
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if gm.grounding_chunks:
                grounding_chunks = len(gm.grounding_chunks)
                for chunk in gm.grounding_chunks:
                    if chunk.web and chunk.web.uri:
                        dm = DOMAIN_RE.match(chunk.web.uri)
                        if dm:
                            grounding_domains.append(dm.group(1))

        # Also extract inline URLs from text
        url_count, unique_domains, domain_list = extract_metrics(text)
        # Merge grounding domains
        for d in grounding_domains:
            if d not in domain_list:
                domain_list.append(d)
        unique_domains = len(domain_list)

        return Result(
            case_id=case["id"], model=model, mode="api", lang=case["lang"],
            query=case["query"], latency_s=elapsed, output_len=len(text),
            url_count=url_count, unique_domains=unique_domains,
            domain_list=domain_list, grounding_chunks=grounding_chunks,
            output_text=text
        )

    except Exception as e:
        elapsed = round(time.monotonic() - start, 2)
        err_msg = str(e)[:300]
        if elapsed >= 89:
            err_msg = f"TIMEOUT (~{elapsed:.0f}s)"
        return Result(
            case_id=case["id"], model=model, mode="api", lang=case["lang"],
            query=case["query"], latency_s=elapsed, output_len=0,
            url_count=0, unique_domains=0, domain_list=[], grounding_chunks=0,
            output_text="", error=err_msg
        )


# ── Main ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=list(MODELS.keys()))
    parser.add_argument("--output", default="data/bench_gemini_search.json")
    parser.add_argument("--log", default="data/bench_gemini_search.log")
    parser.add_argument("--cases", nargs="*", help="Filter case IDs")
    args = parser.parse_args()

    cases = CASES
    if args.cases:
        cases = [c for c in CASES if c["id"] in args.cases]

    # Resolve model→mode mapping
    model_modes = {}
    for m in args.models:
        if m in MODELS:
            model_modes[m] = MODELS[m]
        else:
            print(f"WARNING: Unknown model {m}, defaulting to cli mode")
            model_modes[m] = "cli"

    total = len(cases) * len(model_modes)
    results: list[Result] = []
    idx = 0

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "w", encoding="utf-8")

    for model, mode in model_modes.items():
        header = f"\n{'='*60}\nModel: {model} (mode: {mode})\n{'='*60}"
        print(header)
        log_f.write(header + "\n")
        log_f.flush()

        for case in cases:
            idx += 1
            q_short = case["query"][:40]
            print(f"  [{idx}/{total}] {case['id']}: {q_short}...", end=" ", flush=True)

            if mode == "cli":
                r = run_cli(case, model)
            else:
                r = run_api(case, model)

            if r.error:
                line = f"  [{idx}/{total}] {case['id']}: {q_short}... ERR: {r.error[:50]}"
                print(f"ERR: {r.error[:50]}")
            else:
                gc_info = f" | {r.grounding_chunks}gc" if mode == "api" else ""
                line = (f"  [{idx}/{total}] {case['id']}: {q_short}... "
                        f"{r.latency_s}s | {r.output_len}ch | {r.url_count}urls | "
                        f"{r.unique_domains}doms{gc_info}")
                print(f"{r.latency_s}s | {r.output_len}ch | {r.url_count}urls | "
                      f"{r.unique_domains}doms{gc_info}")

            log_f.write(line + "\n")
            log_f.flush()
            results.append(r)
            time.sleep(1)  # rate limit

    log_f.close()

    # ── Save ──
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
    print(f"\nRaw results → {out_path}")
    print(f"Log → {log_path}")

    # ── Summary ──
    print_summary(results, list(model_modes.keys()))


def print_summary(results: list[Result], models: list[str]):
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")

    for lang in ["zh", "en"]:
        label = "中文" if lang == "zh" else "English"
        print(f"\n## {label}")
        print(f"{'Model':<30} {'Mode':<5} {'Avg(s)':<8} {'Med(s)':<8} "
              f"{'AvgLen':<8} {'AvgURLs':<9} {'AvgDoms':<9} {'AvgGC':<7} {'Errs':<5}")
        print("-" * 90)

        for model in models:
            subset = [r for r in results if r.model == model and r.lang == lang]
            ok = [r for r in subset if not r.error]
            errs = len(subset) - len(ok)

            if not ok:
                short = model.replace("gemini-", "")
                mode = subset[0].mode if subset else "?"
                print(f"{short:<30} {mode:<5} {'N/A':<8} {'N/A':<8} "
                      f"{'N/A':<8} {'N/A':<9} {'N/A':<9} {'N/A':<7} {errs}")
                continue

            lats = sorted(r.latency_s for r in ok)
            avg_lat = sum(lats) / len(lats)
            med_lat = lats[len(lats) // 2]
            avg_len = sum(r.output_len for r in ok) / len(ok)
            avg_urls = sum(r.url_count for r in ok) / len(ok)
            avg_doms = sum(r.unique_domains for r in ok) / len(ok)
            avg_gc = sum(r.grounding_chunks for r in ok) / len(ok)

            short = model.replace("gemini-", "")
            mode = ok[0].mode
            print(f"{short:<30} {mode:<5} {avg_lat:<8.1f} {med_lat:<8.1f} "
                  f"{avg_len:<8.0f} {avg_urls:<9.1f} {avg_doms:<9.1f} "
                  f"{avg_gc:<7.1f} {errs}")

    # Per-case
    print(f"\n{'='*90}")
    print("PER-CASE")
    print(f"{'='*90}")
    print(f"{'Case':<8} {'Query':<32} {'Model':<18} {'Time':<7} "
          f"{'Len':<6} {'URLs':<5} {'Doms':<5} {'GC':<4}")
    print("-" * 85)

    case_ids = list(dict.fromkeys(r.case_id for r in results))
    for cid in case_ids:
        cr = [r for r in results if r.case_id == cid]
        for i, r in enumerate(cr):
            q = r.query[:30] + ".." if len(r.query) > 32 else r.query
            m = r.model.replace("gemini-", "")[:16]
            cid_s = cid if i == 0 else ""
            q_s = q if i == 0 else ""
            err = " ERR" if r.error else ""
            print(f"{cid_s:<8} {q_s:<32} {m:<18} {r.latency_s:<7.1f} "
                  f"{r.output_len:<6} {r.url_count:<5} {r.unique_domains:<5} "
                  f"{r.grounding_chunks:<4}{err}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Gemini CLI unified interface — search, web, analyze, summarize.

Standalone script (no hub imports). Invokes gemini CLI directly via subprocess.

Usage:
    gemini_ctl.py search <query> [--lang zh|en|auto] [--model MODEL]
    gemini_ctl.py web <url> [--prompt TEXT] [--model MODEL]
    gemini_ctl.py analyze <file_path> [--prompt TEXT] [--model MODEL]
    gemini_ctl.py summarize <file_or_url> [--prompt TEXT] [--max-chars N] [--model MODEL]
    gemini_ctl.py status
"""

import argparse
import os
import shutil
import subprocess
import sys

GEMINI_PATH = shutil.which("gemini") or os.path.expanduser("~/.npm-global/bin/gemini")

TIMEOUTS = {"search": 120, "web": 120, "analyze": 300, "summarize": 300}

# ── Prompt templates ────────────────────────────────────────

SEARCH_PROMPT = """\
You MUST use your google_web_search tool to search for: {query}

Requirements:
- Search thoroughly, use multiple relevant queries if needed
- Synthesize information from multiple sources
- Cite sources with URLs
- Be concise but comprehensive — target under 2000 characters
- Output language: {lang}\
"""

WEB_PROMPT = """\
You MUST use your web_fetch tool to read this URL: {url}

Then: {instruction}

Be concise. Output in Chinese unless the content is in another language.\
"""

ANALYZE_PROMPT = """\
Analyze this file. Output a structured analysis in Chinese:
1. 文件类型和主题（一句话）
2. 核心内容摘要（3-5 要点）
3. 关键数据/结论（如有）
4. 文档结构概览
Be concise. Total output under 800 chars.\
"""

SUMMARIZE_PROMPT_FILE = """\
Summarize this content concisely in Chinese.
Focus on: {focus}
Target length: under {max_chars} characters.
Structure: key takeaways first, then supporting details.\
"""

SUMMARIZE_PROMPT_URL = """\
You MUST use your web_fetch tool to read: {url}
Then summarize the content concisely in Chinese.
Focus on: {focus}
Target length: under {max_chars} characters.
Structure: key takeaways first, then supporting details.\
"""


# ── Core runner ─────────────────────────────────────────────

def _run_gemini(prompt: str, file_path: str | None = None,
                model: str | None = None, timeout: int = 120) -> str:
    """Run Gemini CLI via stdin pipe. Returns output text."""
    if not shutil.which(GEMINI_PATH):
        print(f"ERROR: Gemini CLI not found at {GEMINI_PATH}", file=sys.stderr)
        sys.exit(1)

    if file_path and not os.path.isfile(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    full_prompt = f"{prompt} @{file_path}" if file_path else prompt
    args = [GEMINI_PATH]
    if model:
        args.extend(["--model", model])

    try:
        result = subprocess.run(
            args,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"ERROR: Gemini CLI timed out after {timeout}s", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        err = result.stderr[:500] if result.stderr else "unknown error"
        print(f"ERROR: Gemini CLI exit {result.returncode}: {err}", file=sys.stderr)
        sys.exit(1)

    return result.stdout.strip()


# ── Commands ────────────────────────────────────────────────

def cmd_search(args):
    lang_map = {"zh": "Chinese", "en": "English", "auto": "same language as the query"}
    lang = lang_map.get(args.lang, "same language as the query")
    prompt = SEARCH_PROMPT.format(query=args.query, lang=lang)
    timeout = args.timeout or TIMEOUTS["search"]
    print(_run_gemini(prompt, model=args.model, timeout=timeout))


def cmd_web(args):
    instruction = args.prompt or "summarize and extract key information"
    prompt = WEB_PROMPT.format(url=args.url, instruction=instruction)
    timeout = args.timeout or TIMEOUTS["web"]
    print(_run_gemini(prompt, model=args.model, timeout=timeout))


def cmd_analyze(args):
    prompt = args.prompt or ANALYZE_PROMPT
    timeout = args.timeout or TIMEOUTS["analyze"]
    print(_run_gemini(prompt, file_path=args.file_path, model=args.model,
                      timeout=timeout))


def cmd_summarize(args):
    target = args.target
    focus = args.prompt or "key points and conclusions"
    max_chars = args.max_chars

    is_url = target.startswith("http://") or target.startswith("https://")
    timeout = args.timeout or TIMEOUTS["summarize"]

    if is_url:
        prompt = SUMMARIZE_PROMPT_URL.format(url=target, focus=focus,
                                             max_chars=max_chars)
        print(_run_gemini(prompt, model=args.model, timeout=timeout))
    else:
        prompt = SUMMARIZE_PROMPT_FILE.format(focus=focus, max_chars=max_chars)
        print(_run_gemini(prompt, file_path=target, model=args.model,
                          timeout=timeout))


def cmd_status(_args):
    path = shutil.which(GEMINI_PATH)
    if path:
        print(f"Gemini CLI: available ({path})")
    else:
        print(f"Gemini CLI: NOT FOUND ({GEMINI_PATH})")
        sys.exit(1)


# ── Main ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gemini CLI unified interface")
    sub = parser.add_subparsers(dest="command", required=True)

    # search
    p = sub.add_parser("search", help="Web search via Google Search Grounding")
    p.add_argument("query")
    p.add_argument("--lang", choices=["zh", "en", "auto"], default="auto")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.set_defaults(func=cmd_search)

    # web
    p = sub.add_parser("web", help="Read and process a URL")
    p.add_argument("url")
    p.add_argument("--prompt", default=None, help="Custom processing instruction")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.set_defaults(func=cmd_web)

    # analyze
    p = sub.add_parser("analyze", help="Analyze a file (image/PDF/code/text)")
    p.add_argument("file_path")
    p.add_argument("--prompt", default=None, help="Custom analysis prompt")
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.set_defaults(func=cmd_analyze)

    # summarize
    p = sub.add_parser("summarize", help="Summarize long content (file or URL)")
    p.add_argument("target", help="File path or URL")
    p.add_argument("--prompt", default=None, help="Summary focus")
    p.add_argument("--max-chars", type=int, default=1500)
    p.add_argument("--model", default=None)
    p.add_argument("--timeout", type=int, default=None)
    p.set_defaults(func=cmd_summarize)

    # status
    p = sub.add_parser("status", help="Check Gemini CLI availability")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Gemini CLI document analysis — analyze and query documents.

Standalone script (no hub imports). Invokes gemini CLI directly via subprocess.

Usage:
    gemini_doc_ctl.py analyze <file_path> [--prompt TEXT] [--model MODEL]
    gemini_doc_ctl.py query <file_path> "question" [--model MODEL]
    gemini_doc_ctl.py status
"""

import argparse
import os
import shutil
import subprocess
import sys

GEMINI_PATH = shutil.which("gemini") or os.path.expanduser("~/.npm-global/bin/gemini")
DEFAULT_TIMEOUT = 300  # 5 minutes for large documents

ANALYZE_PROMPT = (
    "Analyze this document. Output a structured summary in Chinese:\n"
    "1. 文档类型和主题（一句话）\n"
    "2. 核心内容摘要（3-5 要点）\n"
    "3. 关键数据/结论（如有）\n"
    "4. 文档结构（章节列表）\n"
    "Be concise. Total output under 800 chars."
)


def _run_gemini(prompt: str, file_path: str, model: str | None = None,
                timeout: int = DEFAULT_TIMEOUT) -> str:
    """Run Gemini CLI with file via stdin pipe. Returns output text."""
    if not shutil.which(GEMINI_PATH):
        print(f"ERROR: Gemini CLI not found at {GEMINI_PATH}", file=sys.stderr)
        sys.exit(1)

    if not os.path.isfile(file_path):
        print(f"ERROR: File not found: {file_path}", file=sys.stderr)
        sys.exit(1)

    full_prompt = f"{prompt} @{file_path}"
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


def cmd_analyze(args):
    prompt = args.prompt or ANALYZE_PROMPT
    print(_run_gemini(prompt, args.file_path, model=args.model))


def cmd_query(args):
    prompt = (
        "Based on this document, answer the following question in Chinese:\n"
        f"{args.question}"
    )
    print(_run_gemini(prompt, args.file_path, model=args.model))


def cmd_status(_args):
    if shutil.which(GEMINI_PATH):
        print(f"Gemini CLI: available ({GEMINI_PATH})")
    else:
        print(f"Gemini CLI: NOT FOUND ({GEMINI_PATH})")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Gemini document analysis")
    sub = parser.add_subparsers(dest="command", required=True)

    p_analyze = sub.add_parser("analyze", help="Summarize/analyze a document")
    p_analyze.add_argument("file_path")
    p_analyze.add_argument("--prompt", default=None, help="Custom analysis prompt")
    p_analyze.add_argument("--model", default=None)
    p_analyze.set_defaults(func=cmd_analyze)

    p_query = sub.add_parser("query", help="Ask a question about a document")
    p_query.add_argument("file_path")
    p_query.add_argument("question")
    p_query.add_argument("--model", default=None)
    p_query.set_defaults(func=cmd_query)

    p_status = sub.add_parser("status", help="Check Gemini CLI availability")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

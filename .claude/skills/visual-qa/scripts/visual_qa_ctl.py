#!/usr/bin/env python3
"""Visual QA — UI capture, accessibility tree, and visual verification.

Standalone script (no hub imports). Uses Chrome DevTools Protocol (CDP)
for screenshots and accessibility tree, multimodal LLM for verification.

Usage:
    visual_qa_ctl.py capture <url> [--viewport WxH] [--mobile] [--output DIR]
    visual_qa_ctl.py a11y <url> [--viewport WxH]
    visual_qa_ctl.py verify <url> --spec TEXT [--design-ref PATH] [--viewport WxH]
    visual_qa_ctl.py flow <url> --steps JSON [--viewport WxH]
    visual_qa_ctl.py report <url> --spec TEXT --output-dir DIR [--ticket-id ID]
    visual_qa_ctl.py status
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    print("ERROR: websockets package required. Install: pip3 install websockets",
          file=sys.stderr)
    sys.exit(1)

# ── CDP Client ─────────────────────────────────────────────

CDP_DEFAULT_PORT = 9223
CDP_DEFAULT_HOST = "127.0.0.1"

# Mobile device presets
MOBILE_PRESETS = {
    "iphone14": {"width": 390, "height": 844, "deviceScaleFactor": 3, "mobile": True,
                 "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
    "iphone_se": {"width": 375, "height": 667, "deviceScaleFactor": 2, "mobile": True,
                  "userAgent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
    "pixel7": {"width": 412, "height": 915, "deviceScaleFactor": 2.625, "mobile": True,
               "userAgent": "Mozilla/5.0 (Linux; Android 14)"},
    "ipad": {"width": 820, "height": 1180, "deviceScaleFactor": 2, "mobile": True,
             "userAgent": "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)"},
}


class CDPClient:
    """Minimal Chrome DevTools Protocol client via WebSocket."""

    def __init__(self, host: str = CDP_DEFAULT_HOST, port: int = CDP_DEFAULT_PORT):
        self.host = host
        self.port = port
        self._ws = None
        self._msg_id = 0

    async def connect(self, target_url: str | None = None):
        """Connect to Chrome debug WebSocket."""
        import urllib.request
        tabs_url = f"http://{self.host}:{self.port}/json"
        try:
            with urllib.request.urlopen(tabs_url, timeout=5) as resp:
                tabs = json.loads(resp.read())
        except Exception as e:
            raise ConnectionError(
                f"Cannot connect to Chrome at {self.host}:{self.port}. "
                f"Start Chrome with --remote-debugging-port={self.port}\n"
                f"Error: {e}"
            )

        # Find target tab or use first page
        ws_url = None
        for tab in tabs:
            if tab.get("type") == "page":
                if target_url and target_url in tab.get("url", ""):
                    ws_url = tab["webSocketDebuggerUrl"]
                    break
                if not ws_url:
                    ws_url = tab.get("webSocketDebuggerUrl")

        if not ws_url:
            raise ConnectionError("No debuggable page found in Chrome")

        self._ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send CDP command and wait for response."""
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method}
        if params:
            msg["params"] = params
        await self._ws.send(json.dumps(msg))

        while True:
            resp = json.loads(await self._ws.recv())
            if resp.get("id") == self._msg_id:
                if "error" in resp:
                    raise RuntimeError(f"CDP error: {resp['error']}")
                return resp.get("result", {})

    async def close(self):
        if self._ws:
            await self._ws.close()


# ── Core Functions ─────────────────────────────────────────

async def capture_screenshot(url: str, viewport: str = "1440x900",
                             mobile: str | None = None,
                             output_dir: str = ".",
                             cdp_port: int = CDP_DEFAULT_PORT) -> str:
    """Navigate to URL, capture screenshot, return file path."""
    cdp = CDPClient(port=cdp_port)
    await cdp.connect()

    try:
        # Set viewport
        w, h = _parse_viewport(viewport)
        device_metrics = {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False}

        if mobile:
            preset = MOBILE_PRESETS.get(mobile, MOBILE_PRESETS["iphone14"])
            device_metrics.update(preset)

        await cdp.send("Emulation.setDeviceMetricsOverride", device_metrics)

        if mobile and device_metrics.get("userAgent"):
            await cdp.send("Emulation.setUserAgentOverride",
                           {"userAgent": device_metrics["userAgent"]})

        # Navigate
        await cdp.send("Page.enable")
        result = await cdp.send("Page.navigate", {"url": url})
        if "errorText" in result:
            raise RuntimeError(f"Navigation failed: {result['errorText']}")

        # Wait for load
        await asyncio.sleep(2)
        await cdp.send("Page.stopLoading")

        # Capture
        screenshot = await cdp.send("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": False,
        })

        # Save
        os.makedirs(output_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        suffix = f"_{mobile}" if mobile else f"_{w}x{h}"
        filename = f"screenshot_{timestamp}{suffix}.png"
        filepath = os.path.join(output_dir, filename)

        with open(filepath, "wb") as f:
            f.write(base64.b64decode(screenshot["data"]))

        return filepath
    finally:
        await cdp.close()


async def get_a11y_tree(url: str, viewport: str = "1440x900",
                        cdp_port: int = CDP_DEFAULT_PORT) -> dict:
    """Get accessibility tree for the page."""
    cdp = CDPClient(port=cdp_port)
    await cdp.connect()

    try:
        w, h = _parse_viewport(viewport)
        await cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": w, "height": h, "deviceScaleFactor": 1, "mobile": False,
        })

        await cdp.send("Page.enable")
        await cdp.send("Page.navigate", {"url": url})
        await asyncio.sleep(2)

        await cdp.send("Accessibility.enable")
        tree = await cdp.send("Accessibility.getFullAXTree")

        # Also get console errors
        await cdp.send("Runtime.enable")
        console_result = await cdp.send("Runtime.evaluate", {
            "expression": "JSON.stringify(window.__console_errors || [])",
            "returnByValue": True,
        })

        return {
            "url": url,
            "viewport": viewport,
            "nodes": _simplify_a11y_tree(tree.get("nodes", [])),
            "console_errors": console_result.get("result", {}).get("value", "[]"),
        }
    finally:
        await cdp.close()


async def verify_url(url: str, spec: str, design_ref: str | None = None,
                     viewport: str = "1440x900", mobile: str | None = None,
                     cdp_port: int = CDP_DEFAULT_PORT) -> dict:
    """Capture screenshot + a11y tree, send to LLM for 5-dimension scoring."""
    import subprocess
    import shutil

    # Capture screenshot
    tmp_dir = f"/tmp/visual_qa_{int(time.time())}"
    screenshot_path = await capture_screenshot(
        url, viewport=viewport, mobile=mobile,
        output_dir=tmp_dir, cdp_port=cdp_port
    )

    # Get a11y tree
    a11y = await get_a11y_tree(url, viewport=viewport, cdp_port=cdp_port)

    # Build verification prompt
    prompt_path = Path(__file__).parent.parent / "prompts" / "verify.md"
    if prompt_path.exists():
        prompt_template = prompt_path.read_text()
    else:
        prompt_template = DEFAULT_VERIFY_PROMPT

    a11y_summary = _format_a11y_summary(a11y["nodes"][:100])  # Cap for context

    prompt = prompt_template.format(
        url=url,
        viewport=viewport,
        spec=spec,
        a11y_tree=a11y_summary,
        console_errors=a11y.get("console_errors", "[]"),
        design_ref_note=f"设计参考图已提供，请对比截图与设计稿的差异。" if design_ref else "无设计参考图。",
    )

    # Use Claude to analyze screenshot
    claude_path = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if not shutil.which(claude_path):
        raise RuntimeError("Claude CLI not found")

    # Build command: pipe prompt + screenshot to Claude
    cmd = [claude_path, "-p", prompt, "--allowedTools", ""]

    # For now, use text-only analysis with a11y tree
    # TODO: Add screenshot as image input when Claude CLI supports it
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
        llm_output = result.stdout.strip()
    except subprocess.TimeoutExpired:
        llm_output = '{"error": "LLM analysis timed out"}'

    # Parse score from LLM output
    score = _parse_score(llm_output)

    return {
        "url": url,
        "viewport": viewport,
        "screenshot": screenshot_path,
        "a11y_node_count": len(a11y["nodes"]),
        "score": score,
        "llm_analysis": llm_output,
        "verdict": "PASS" if score.get("total", 0) >= 80 else "FAIL",
    }


async def generate_report(url: str, spec: str, output_dir: str,
                          ticket_id: str | None = None,
                          design_ref: str | None = None,
                          cdp_port: int = CDP_DEFAULT_PORT) -> str:
    """Full QA report: screenshots (desktop + mobile) + a11y + verification."""
    os.makedirs(output_dir, exist_ok=True)
    screenshots_dir = os.path.join(output_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)

    results = []

    # Desktop screenshot + verify
    desktop = await verify_url(url, spec, design_ref=design_ref,
                               viewport="1440x900", cdp_port=cdp_port)
    results.append(("desktop_1440x900", desktop))

    # Mobile screenshot
    mobile = await capture_screenshot(url, mobile="iphone14",
                                      output_dir=screenshots_dir,
                                      cdp_port=cdp_port)
    results.append(("mobile_iphone14", {"screenshot": mobile}))

    # Copy desktop screenshot to output
    if desktop.get("screenshot"):
        import shutil
        dest = os.path.join(screenshots_dir, "desktop_1440x900.png")
        shutil.copy2(desktop["screenshot"], dest)

    # Get a11y tree
    a11y = await get_a11y_tree(url, cdp_port=cdp_port)
    a11y_path = os.path.join(output_dir, "a11y_tree.json")
    with open(a11y_path, "w") as f:
        json.dump(a11y, f, ensure_ascii=False, indent=2)

    # Write score.json
    score = desktop.get("score", {})
    score_path = os.path.join(output_dir, "score.json")
    with open(score_path, "w") as f:
        json.dump({
            "ticket_id": ticket_id,
            "url": url,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "score": score,
            "verdict": desktop.get("verdict", "UNKNOWN"),
        }, f, ensure_ascii=False, indent=2)

    # Write report.md
    report = _generate_report_md(url, spec, ticket_id, results, score,
                                 desktop.get("verdict", "UNKNOWN"),
                                 desktop.get("llm_analysis", ""))
    report_path = os.path.join(output_dir, "report.md")
    with open(report_path, "w") as f:
        f.write(report)

    print(f"Report: {report_path}")
    print(f"Score: {json.dumps(score)}")
    print(f"Verdict: {desktop.get('verdict', 'UNKNOWN')}")

    return report_path


# ── Helpers ────────────────────────────────────────────────

def _parse_viewport(viewport: str) -> tuple[int, int]:
    parts = viewport.lower().split("x")
    return int(parts[0]), int(parts[1])


def _simplify_a11y_tree(nodes: list) -> list:
    """Extract essential info from raw CDP accessibility nodes."""
    simplified = []
    for node in nodes:
        role = node.get("role", {}).get("value", "")
        name = node.get("name", {}).get("value", "")
        if not role or role in ("none", "generic", "InlineTextBox"):
            continue
        entry = {"role": role}
        if name:
            entry["name"] = name[:100]
        props = {}
        for prop in node.get("properties", []):
            pname = prop.get("name", "")
            pval = prop.get("value", {}).get("value")
            if pname in ("focused", "disabled", "checked", "expanded", "required"):
                props[pname] = pval
        if props:
            entry["properties"] = props
        simplified.append(entry)
    return simplified


def _format_a11y_summary(nodes: list) -> str:
    """Format a11y nodes as readable text for LLM."""
    lines = []
    for n in nodes:
        role = n.get("role", "?")
        name = n.get("name", "")
        props = n.get("properties", {})
        prop_str = ", ".join(f"{k}={v}" for k, v in props.items()) if props else ""
        line = f"[{role}] {name}"
        if prop_str:
            line += f" ({prop_str})"
        lines.append(line)
    return "\n".join(lines)


def _parse_score(llm_output: str) -> dict:
    """Extract 5-dimension score from LLM output."""
    try:
        # Try to find JSON block in output
        start = llm_output.find("{")
        end = llm_output.rfind("}") + 1
        if start >= 0 and end > start:
            candidate = llm_output[start:end]
            data = json.loads(candidate)
            if "total" in data or "scores" in data:
                return data
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: return unknown
    return {"total": 0, "error": "Could not parse score from LLM output"}


def _generate_report_md(url, spec, ticket_id, results, score, verdict, analysis):
    """Generate markdown report."""
    tid = ticket_id or "unknown"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")

    sections = [
        f"# Visual QA Report — {tid}\n",
        f"## 概览\n",
        f"- **验证时间**：{ts}",
        f"- **URL**：{url}",
        f"- **验证规约**：{spec[:200]}",
        f"- **最终判定**：**{verdict}**\n",
        f"## 截图证据\n",
    ]

    for name, data in results:
        screenshot = data.get("screenshot", "N/A")
        sections.append(f"### {name}\n")
        sections.append(f"截图路径：`{screenshot}`\n")

    sections.append(f"## 五维度评分\n")
    if "scores" in score:
        sections.append("| 维度 | 得分 | 满分 |")
        sections.append("|------|------|------|")
        for dim, val in score["scores"].items():
            sections.append(f"| {dim} | {val} | — |")
    sections.append(f"\n**总分：{score.get('total', 'N/A')}** / 100\n")

    sections.append(f"## 详细分析\n")
    sections.append(analysis[:3000])

    return "\n".join(sections)


def check_status():
    """Check Chrome debug port and dependencies."""
    import urllib.request
    try:
        url = f"http://{CDP_DEFAULT_HOST}:{CDP_DEFAULT_PORT}/json/version"
        with urllib.request.urlopen(url, timeout=3) as resp:
            info = json.loads(resp.read())
            print(f"Chrome: connected ({info.get('Browser', 'unknown')})")
            print(f"  WebSocket: {info.get('webSocketDebuggerUrl', 'N/A')}")
            print(f"  Protocol: {info.get('Protocol-Version', 'N/A')}")
    except Exception:
        print(f"Chrome: NOT CONNECTED at {CDP_DEFAULT_HOST}:{CDP_DEFAULT_PORT}")
        print(f"  Start Chrome with: --remote-debugging-port={CDP_DEFAULT_PORT}")
        sys.exit(1)

    # Check websockets
    try:
        import websockets  # noqa: F811
        print(f"websockets: {websockets.__version__}")
    except ImportError:
        print("websockets: NOT INSTALLED")
        sys.exit(1)

    # Check Claude CLI
    import shutil
    claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if shutil.which(claude):
        print(f"Claude CLI: available ({claude})")
    else:
        print("Claude CLI: NOT FOUND")


# ── Default verify prompt ──────────────────────────────────

DEFAULT_VERIFY_PROMPT = """\
You are a Visual QA engineer. Analyze the UI based on the accessibility tree and spec below.

URL: {url}
Viewport: {viewport}
{design_ref_note}

## Spec (what the UI should look like/do):
{spec}

## Accessibility Tree:
{a11y_tree}

## Console Errors:
{console_errors}

## Instructions:
Score the UI on 5 dimensions. Output ONLY a JSON object:

{{
  "scores": {{
    "functional_correctness": <0-30>,
    "design_fidelity": <0-25>,
    "anti_ai_aesthetic": <0-15>,
    "copy_readability": <0-15>,
    "interaction_convenience": <0-15>
  }},
  "total": <sum>,
  "issues": [
    {{"dimension": "...", "severity": "high|medium|low", "description": "..."}}
  ],
  "summary": "One paragraph overall assessment in Chinese"
}}

Scoring guidelines:
- functional_correctness (30): Elements exist, interactive, correct state, no console errors
- design_fidelity (25): Matches design spec/reference, consistent styling
- anti_ai_aesthetic (15): No default AI templates (rounded card grids, purple gradients, generic illustrations). Uses platform-native components. Looks "paid and handcrafted"
- copy_readability (15): Copy is clear, natural, proper information hierarchy, good whitespace
- interaction_convenience (15): Visual flow (F/Z pattern), single CTA per screen, immediate feedback, error tolerance
"""


# ── CLI ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Visual QA — UI capture and verification")
    sub = parser.add_subparsers(dest="command", required=True)

    # capture
    p = sub.add_parser("capture", help="Screenshot a URL")
    p.add_argument("url")
    p.add_argument("--viewport", default="1440x900")
    p.add_argument("--mobile", choices=list(MOBILE_PRESETS.keys()), default=None)
    p.add_argument("--output", default=".")
    p.add_argument("--port", type=int, default=CDP_DEFAULT_PORT)

    # a11y
    p = sub.add_parser("a11y", help="Get accessibility tree")
    p.add_argument("url")
    p.add_argument("--viewport", default="1440x900")
    p.add_argument("--port", type=int, default=CDP_DEFAULT_PORT)

    # verify
    p = sub.add_parser("verify", help="Visual verification with 5-dimension scoring")
    p.add_argument("url")
    p.add_argument("--spec", required=True, help="What the UI should look like")
    p.add_argument("--design-ref", default=None, help="Path to design reference image")
    p.add_argument("--viewport", default="1440x900")
    p.add_argument("--mobile", choices=list(MOBILE_PRESETS.keys()), default=None)
    p.add_argument("--port", type=int, default=CDP_DEFAULT_PORT)

    # flow
    p = sub.add_parser("flow", help="Multi-step interaction flow verification")
    p.add_argument("url")
    p.add_argument("--steps", required=True, help="JSON array of interaction steps")
    p.add_argument("--viewport", default="1440x900")
    p.add_argument("--port", type=int, default=CDP_DEFAULT_PORT)

    # report
    p = sub.add_parser("report", help="Full QA report")
    p.add_argument("url")
    p.add_argument("--spec", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--ticket-id", default=None)
    p.add_argument("--design-ref", default=None)
    p.add_argument("--port", type=int, default=CDP_DEFAULT_PORT)

    # status
    sub.add_parser("status", help="Check dependencies")

    args = parser.parse_args()

    if args.command == "status":
        check_status()
    elif args.command == "capture":
        path = asyncio.run(capture_screenshot(
            args.url, viewport=args.viewport, mobile=args.mobile,
            output_dir=args.output, cdp_port=args.port))
        print(f"Screenshot saved: {path}")
    elif args.command == "a11y":
        tree = asyncio.run(get_a11y_tree(
            args.url, viewport=args.viewport, cdp_port=args.port))
        print(_format_a11y_summary(tree["nodes"]))
        print(f"\n--- {len(tree['nodes'])} nodes ---")
    elif args.command == "verify":
        result = asyncio.run(verify_url(
            args.url, spec=args.spec, design_ref=args.design_ref,
            viewport=args.viewport, mobile=args.mobile, cdp_port=args.port))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "flow":
        print("ERROR: flow command not yet implemented", file=sys.stderr)
        sys.exit(1)
    elif args.command == "report":
        asyncio.run(generate_report(
            args.url, spec=args.spec, output_dir=args.output_dir,
            ticket_id=args.ticket_id, design_ref=args.design_ref,
            cdp_port=args.port))


if __name__ == "__main__":
    main()

"""Entry point wrapper for the feishu-sheet CLI command.

Bridges the installed console_script entry point to the actual sheet_ctl.py
implementation inside .claude/skills/feishu-sheet/scripts/.
"""
from pathlib import Path
import sys

# Project root = two levels up from this file (feishu_cli/sheet.py → project root)
_root = Path(__file__).resolve().parent.parent

# Ensure agent package is importable (needed by sheet_ctl.py's module-level imports)
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

# Add the skill scripts directory so `import sheet_ctl` resolves
_scripts = _root / ".claude" / "skills" / "feishu-sheet" / "scripts"
if str(_scripts) not in sys.path:
    sys.path.insert(0, str(_scripts))

# Import main from the real implementation (module-level imports in sheet_ctl.py
# run after the sys.path setup above, so FeishuAPI import succeeds)
from sheet_ctl import main  # noqa: E402


def main_cli():
    """Console script entry point: feishu-sheet <command> [args]"""
    main()


if __name__ == "__main__":
    main_cli()

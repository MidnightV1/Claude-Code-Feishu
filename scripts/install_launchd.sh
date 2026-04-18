#!/bin/bash
# install_launchd.sh — install claude-code-feishu as a macOS LaunchAgent
#
# What this does:
#   1. Verifies macOS and prerequisites (python3, repo paths)
#   2. Checks auto-login is enabled (required for Mac-as-server deployments)
#   3. Generates a plist from docs/launchd.plist.template with real paths
#   4. Installs to ~/Library/LaunchAgents/ and launchctl loads it
#
# Re-run safely: it unloads first, then reloads.
#
# For Linux, use systemd — see docs/SETUP.md Phase 5.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
LABEL="com.claude-code-feishu"
TEMPLATE="$REPO/docs/launchd.plist.template"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"

# ── 1. OS check ────────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
    echo "❌ This script is macOS-only. For Linux use systemd (see docs/SETUP.md)."
    exit 1
fi

# ── 2. Prerequisite: template exists ───────────────────────────────────────
if [[ ! -f "$TEMPLATE" ]]; then
    echo "❌ Template not found: $TEMPLATE"
    exit 1
fi

# ── 3. Resolve python3 ─────────────────────────────────────────────────────
PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
    echo "❌ python3 not found in PATH. Install Python 3.10+ and re-run."
    exit 1
fi
echo "✓ Python: $PYTHON"
echo "✓ Repo:   $REPO"

# ── 4. Auto-login check (Mac mini-as-server requirement) ───────────────────
AUTO_LOGIN=""
if [[ -r /Library/Preferences/com.apple.loginwindow.plist ]]; then
    AUTO_LOGIN=$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || echo "")
fi

if [[ -z "$AUTO_LOGIN" ]]; then
    cat <<'EOF'

⚠️  Auto-login is NOT enabled.

Why this matters: LaunchAgents only run inside an active user session.
On a Mac mini acting as a server, if no user logs in after reboot,
the bot will never start — the plist loads but nothing executes.

Enable auto-login now:
  System Settings → Users & Groups → Login Options
  → "Automatically log in as" → select your user → enter password

(Requires FileVault disabled, or login password stored.)

Continue installing plist anyway? [y/N]
EOF
    read -r ans
    if [[ "$ans" != "y" && "$ans" != "Y" ]]; then
        echo "Aborted. Enable auto-login first, then re-run this script."
        exit 1
    fi
else
    echo "✓ Auto-login enabled (user: $AUTO_LOGIN)"
fi

# ── 5. Generate plist from template ────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"

# Escape paths for sed (handle spaces, slashes)
esc() { printf '%s' "$1" | sed 's/[&/\]/\\&/g'; }

sed \
    -e "s/{{PYTHON}}/$(esc "$PYTHON")/g" \
    -e "s/{{REPO}}/$(esc "$REPO")/g" \
    -e "s/{{HOME}}/$(esc "$HOME")/g" \
    "$TEMPLATE" > "$TARGET"

echo "✓ Plist written: $TARGET"

# ── 6. Load (idempotent) ───────────────────────────────────────────────────
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

sleep 2
if launchctl list | grep -q "$LABEL"; then
    echo "✓ Service loaded:"
    launchctl list | grep "$LABEL"
    cat <<EOF

Logs:
  $REPO/data/launchd-stdout.log
  $REPO/data/launchd-stderr.log
  $REPO/data/hub.log  (from agent itself)

To disable:
  launchctl unload $TARGET

Recommended config.yaml entry for #restart (via Feishu):
  hub:
    restart_command: "launchctl kickstart -k gui/\$(id -u)/$LABEL"
EOF
else
    echo "❌ Service failed to load. Recent launchd errors:"
    log show --predicate 'process == "launchd"' --last 1m --style compact 2>/dev/null | tail -20
    exit 1
fi

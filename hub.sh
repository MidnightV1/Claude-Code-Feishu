#!/bin/bash
# claude-code-feishu service manager
# Usage: hub.sh {start|stop|restart|status|check|watchdog}

HUB_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${BRIEFING_PYTHON:-python3}"
SCREEN_NAME="claude-hub"
WATCHDOG_SCREEN="hub-watchdog"
WATCHDOG_LOG="$HUB_DIR/data/watchdog.log"
WATCHDOG_INTERVAL=300  # 5 minutes

# Env vars that must be stripped to avoid "nested session" error
STRIP_VARS="CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"

is_running() {
    screen -ls 2>/dev/null | grep -q "$SCREEN_NAME"
}

do_start() {
    if is_running; then
        echo "Already running"
        screen -ls | grep "$SCREEN_NAME"
        return 0
    fi
    cd "$HUB_DIR" || exit 1
    env $(for v in $STRIP_VARS; do echo "-u $v"; done) \
        HUB_CHILD=1 \
        screen -dmS "$SCREEN_NAME" "$PYTHON" main.py
    sleep 2
    if is_running; then
        echo "Started OK"
        screen -ls | grep "$SCREEN_NAME"
    else
        echo "Failed to start — check logs: $HUB_DIR/data/hub.log"
        return 1
    fi
}

do_stop() {
    # Stop watchdog first to prevent auto-restart
    if screen -ls 2>/dev/null | grep -q "$WATCHDOG_SCREEN"; then
        screen -S "$WATCHDOG_SCREEN" -X quit 2>/dev/null
        echo "Watchdog stopped"
    fi
    if ! is_running; then
        echo "Not running"
        return 0
    fi
    screen -S "$SCREEN_NAME" -X quit
    sleep 2
    if is_running; then
        screen -S "$SCREEN_NAME" -X kill
        sleep 1
    fi
    echo "Stopped"
}

do_check() {
    # Watchdog: restart if dead, silent if alive
    if ! is_running; then
        do_start
        echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog restarted service" >> "$WATCHDOG_LOG"
    fi
}

# ── Child process guard ──
# When Claude CLI (spawned by hub) tries to restart/stop, refuse with guidance.
if [ "$HUB_CHILD" = "1" ] && [ "${1:-}" = "restart" -o "${1:-}" = "stop" ]; then
    cat <<'GUARD'
[BLOCKED] You are running INSIDE claude-code-feishu as a subprocess.
Running hub.sh restart/stop would kill your own parent process and yourself.

When restart is actually needed (rare):
- main.py code changed (new features, bug fixes in hub logic)
- config.yaml changed (feishu credentials, llm defaults, heartbeat settings)
- Python dependencies updated

When restart is NOT needed:
- briefing sources.yaml changed (collector reads it fresh each run)
- briefing prompt changed in briefing.py (requires restart, but tell the user)
- HEARTBEAT.md changed (read fresh each cycle)
- cron jobs added/removed via #cron commands (live-updated)

If you believe a restart is needed, tell the user:
"Service restart needed because [reason]. Please run: hub.sh restart"
GUARD
    exit 1
fi

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; do_start ;;
    status)
        if is_running; then
            echo "RUNNING"
            screen -ls | grep "$SCREEN_NAME"
        else
            echo "STOPPED"
        fi
        ;;
    check)   do_check ;;
    watchdog)
        # Run watchdog in a screen session, checking every 5 min
        if screen -ls 2>/dev/null | grep -q "$WATCHDOG_SCREEN"; then
            echo "Watchdog already running"
            exit 0
        fi
        screen -dmS "$WATCHDOG_SCREEN" /bin/bash -c "
            while true; do
                $0 check
                sleep $WATCHDOG_INTERVAL
            done
        "
        echo "Watchdog started (interval=${WATCHDOG_INTERVAL}s)"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|check|watchdog}"
        exit 1
        ;;
esac

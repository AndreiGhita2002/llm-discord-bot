#!/bin/bash

# Discord Bot Daemon Setup Script for macOS
# This script sets up the bot to:
# 1. Run as a launchd daemon on startup
# 2. Automatically restart on crashes
# 3. Check for GitHub updates and restart when changes are detected
#
# Usage: Run this script from the bot directory, or pass the directory as an argument
#   ./setup-daemon.sh
#   ./setup-daemon.sh /path/to/bot

set -e

# Configuration - detect bot directory dynamically. BOT_DIR is the PROJECT ROOT (the git repo,
# holding kronk_config.yaml and all runtime state); the Python code lives in its src/ subdir.
if [ -n "$1" ]; then
    BOT_DIR="$(cd "$1" && pwd)"
elif [ -f "$(dirname "$0")/src/main.py" ]; then
    BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
elif [ -f "./src/main.py" ]; then
    BOT_DIR="$(pwd)"
else
    echo "Error: Could not find src/main.py. Run this script from the bot directory or pass the path as an argument."
    exit 1
fi

# Generate a unique plist name based on directory name
BOT_NAME="$(basename "$BOT_DIR")"
PLIST_NAME="com.${USER}.${BOT_NAME}"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$BOT_DIR/logs"
UPDATE_CHECK_INTERVAL=300  # Check for updates every 5 minutes

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Discord Bot Daemon Setup ===${NC}"
echo "Bot directory: $BOT_DIR"
echo "Plist name: $PLIST_NAME"
echo ""

# Create LaunchAgents directory if it doesn't exist
mkdir -p "$HOME/Library/LaunchAgents"

# Create logs directory
mkdir -p "$LOG_DIR"

# Check for required tools
if ! command -v git &> /dev/null; then
    echo -e "${RED}Error: git is not installed${NC}"
    exit 1
fi

if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}Warning: uv is not installed. Installing via curl...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Check if DISCORD_BOT_TOKEN is set
if [ -z "$DISCORD_BOT_TOKEN" ]; then
    echo -e "${YELLOW}Warning: DISCORD_BOT_TOKEN environment variable is not set.${NC}"
    echo "You'll need to set it in the plist file or export it before loading the daemon."
    read -p "Enter your DISCORD_BOT_TOKEN (or press Enter to skip): " TOKEN_INPUT
    if [ -n "$TOKEN_INPUT" ]; then
        DISCORD_BOT_TOKEN="$TOKEN_INPUT"
    fi
fi

# Optionally capture OLLAMA_API_KEY (needed for the web_search / web_fetch tools). launchd
# does not inherit your shell env, so it must be baked into the plist to reach the bot.
if [ -z "$OLLAMA_API_KEY" ]; then
    read -p "Enter your OLLAMA_API_KEY for web search (or press Enter to skip): " OLLAMA_KEY_INPUT
    if [ -n "$OLLAMA_KEY_INPUT" ]; then
        OLLAMA_API_KEY="$OLLAMA_KEY_INPUT"
    fi
fi

# Create the runner script that handles updates
echo -e "${GREEN}Creating bot runner script...${NC}"
cat > "$BOT_DIR/run-bot.sh" << 'RUNNER_EOF'
#!/bin/bash

# Determine bot directory from script location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
LOG_FILE="$BOT_DIR/logs/bot.log"
UPDATE_CHECK_INTERVAL="${UPDATE_CHECK_INTERVAL:-300}"   # how often to check git for updates
HEALTH_CHECK_INTERVAL="${HEALTH_CHECK_INTERVAL:-15}"    # how often to check the bot is alive+responsive
HEARTBEAT_FILE="$BOT_DIR/bot.heartbeat"                 # bot rewrites this every few seconds
HEARTBEAT_MAX_AGE="${HEARTBEAT_MAX_AGE:-90}"            # stale heartbeat older than this = hung -> restart
BRANCH="${GIT_BRANCH:-main}"

# Ensure logs directory exists
mkdir -p "$BOT_DIR/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# Run a command with a hard timeout so a network stall (git fetch / uv sync) can never wedge
# the whole guard loop. Uses coreutils timeout/gtimeout when present, else a pure-bash fallback.
if command -v timeout >/dev/null 2>&1; then _TIMEOUT_BIN=timeout
elif command -v gtimeout >/dev/null 2>&1; then _TIMEOUT_BIN=gtimeout
else _TIMEOUT_BIN=""; fi

run_timeout() {
    local secs="$1"; shift
    if [ -n "$_TIMEOUT_BIN" ]; then
        "$_TIMEOUT_BIN" "$secs" "$@"
        return $?
    fi
    # Fallback: run in background, kill it if it overruns.
    "$@" &
    local cmd_pid=$!
    ( sleep "$secs"; kill -9 "$cmd_pid" 2>/dev/null ) &
    local killer=$!
    wait "$cmd_pid" 2>/dev/null
    local rc=$?
    kill "$killer" 2>/dev/null
    return $rc
}

# Seconds since the bot last wrote its heartbeat file (echoes a huge number if missing).
heartbeat_age() {
    if [ ! -f "$HEARTBEAT_FILE" ]; then echo 999999; return; fi
    local now hb
    now=$(date +%s)
    hb=$(stat -f %m "$HEARTBEAT_FILE" 2>/dev/null || stat -c %Y "$HEARTBEAT_FILE" 2>/dev/null)
    [ -z "$hb" ] && { echo 999999; return; }
    echo $(( now - hb ))
}

check_for_updates() {
    cd "$BOT_DIR"

    # Fetch latest changes from remote (bounded - a stalled fetch must not wedge the loop).
    if ! run_timeout 60 git fetch origin "$BRANCH" 2>/dev/null; then
        log "git fetch timed out or failed; will retry next cycle."
        return 1
    fi

    # Compare local and remote
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse "origin/$BRANCH")

    if [ "$LOCAL" != "$REMOTE" ]; then
        log "Update detected! Local: $LOCAL, Remote: $REMOTE"
        return 0
    fi
    return 1
}

pull_updates() {
    cd "$BOT_DIR"
    log "Applying latest changes (hard reset to origin/$BRANCH)..."
    # Hard reset instead of 'git pull': a dirty tree or diverged local commit can never block a
    # deploy/reset. Runtime files (heartbeat, pid, logs, config.yaml) are gitignored, so nothing
    # of value is discarded. This is what makes the 'push .deploy-trigger to force a reset' work
    # reliably every time.
    run_timeout 60 git fetch origin "$BRANCH" 2>/dev/null
    git reset --hard "origin/$BRANCH"

    # Reinstall dependencies if they changed in the new revision.
    if git diff HEAD@{1} HEAD --name-only 2>/dev/null | grep -q "pyproject.toml\|uv.lock"; then
        log "Dependencies changed, running uv sync..."
        run_timeout 300 uv sync
    fi
}

run_bot() {
    cd "$BOT_DIR"
    log "Starting bot..."

    # Remove any stale heartbeat from a previous run so the new process gets a clean grace
    # period (an absent heartbeat is treated as 'starting up', not 'hung') until it writes one.
    rm -f "$HEARTBEAT_FILE"

    # Run the bot using uv. Redirect straight to the log file (NO 'tee' pipe) so $! is the
    # uv/python pid, not a pipeline wrapper - otherwise stop_bot kills the wrong process and
    # orphaned bots pile up across restarts.
    uv run python src/main.py >> "$LOG_FILE" 2>&1 &
    BOT_PID=$!
    echo $BOT_PID > "$BOT_DIR/bot.pid"
    log "Bot started with PID: $BOT_PID"
}

stop_bot() {
    if [ -f "$BOT_DIR/bot.pid" ]; then
        PID=$(cat "$BOT_DIR/bot.pid")
        if kill -0 "$PID" 2>/dev/null; then
            log "Stopping bot (PID: $PID)..."
            kill "$PID" 2>/dev/null
            sleep 2
            if kill -0 "$PID" 2>/dev/null; then
                kill -9 "$PID" 2>/dev/null
            fi
        fi
        rm -f "$BOT_DIR/bot.pid"
    fi

    # Belt-and-braces: kill any bot python still running from THIS directory (catches orphans
    # left by earlier restarts). Dir-specific via the venv path, so other bots are untouched.
    pkill -f "${BOT_DIR}/.venv/bin/python3 src/main.py" 2>/dev/null
    pkill -f "${BOT_DIR}/.venv/bin/python src/main.py" 2>/dev/null
    sleep 1
}

cleanup() {
    log "Received shutdown signal, cleaning up..."
    stop_bot
    exit 0
}

# Handle signals
trap cleanup SIGTERM SIGINT SIGHUP

# Main loop
log "=== Bot Runner Started ==="
log "Bot directory: $BOT_DIR"
log "Branch: $BRANCH"
log "Update check interval: ${UPDATE_CHECK_INTERVAL}s"

# True if the bot process is both alive AND responsive (heartbeat fresh).
bot_is_healthy() {
    # Dead / no pid file?
    if [ ! -f "$BOT_DIR/bot.pid" ]; then return 1; fi
    local pid
    pid=$(cat "$BOT_DIR/bot.pid")
    if ! kill -0 "$pid" 2>/dev/null; then
        log "Bot process (PID $pid) is not running."
        return 1
    fi
    # Alive but hung? Stale heartbeat means the event loop is wedged. Only judge staleness once
    # the bot has written a heartbeat at least once (fresh starts get a grace period until then).
    if [ -f "$HEARTBEAT_FILE" ]; then
        local age
        age=$(heartbeat_age)
        if [ "$age" -gt "$HEARTBEAT_MAX_AGE" ]; then
            log "Bot heartbeat stale (${age}s > ${HEARTBEAT_MAX_AGE}s) - loop is hung."
            return 1
        fi
    fi
    return 0
}

# Initial start
run_bot

# Guard loop: fast health checks (dead OR hung) on every tick; slower update checks.
SECONDS_SINCE_UPDATE_CHECK=0
while true; do
    sleep "$HEALTH_CHECK_INTERVAL"
    SECONDS_SINCE_UPDATE_CHECK=$((SECONDS_SINCE_UPDATE_CHECK + HEALTH_CHECK_INTERVAL))

    # 1) Health: restart a dead or hung bot immediately.
    if ! bot_is_healthy; then
        log "Bot unhealthy - restarting."
        stop_bot
        run_bot
        continue
    fi

    # 2) Updates: only every UPDATE_CHECK_INTERVAL seconds.
    if [ "$SECONDS_SINCE_UPDATE_CHECK" -ge "$UPDATE_CHECK_INTERVAL" ]; then
        SECONDS_SINCE_UPDATE_CHECK=0
        if check_for_updates; then
            stop_bot
            pull_updates
            run_bot
        fi
    fi
done
RUNNER_EOF

chmod +x "$BOT_DIR/run-bot.sh"

# Create the launchd plist
echo -e "${GREEN}Creating launchd plist...${NC}"

# Expand $HOME for the PATH in plist
EXPANDED_HOME="$HOME"

cat > "$PLIST_PATH" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${BOT_DIR}/run-bot.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${BOT_DIR}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:${EXPANDED_HOME}/.local/bin:${EXPANDED_HOME}/.cargo/bin</string>
        <key>HOME</key>
        <string>${EXPANDED_HOME}</string>
        <key>DISCORD_BOT_TOKEN</key>
        <string>${DISCORD_BOT_TOKEN:-YOUR_TOKEN_HERE}</string>
        <key>OLLAMA_API_KEY</key>
        <string>${OLLAMA_API_KEY:-}</string>
        <key>UPDATE_CHECK_INTERVAL</key>
        <string>${UPDATE_CHECK_INTERVAL}</string>
        <key>GIT_BRANCH</key>
        <string>main</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd-stderr.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
PLIST_EOF

echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo -e "${YELLOW}=== Important Notes ===${NC}"
echo ""

if [ -z "$DISCORD_BOT_TOKEN" ] || [ "$DISCORD_BOT_TOKEN" = "YOUR_TOKEN_HERE" ]; then
    echo -e "${RED}1. You MUST edit the plist to add your DISCORD_BOT_TOKEN:${NC}"
    echo "   nano $PLIST_PATH"
    echo "   (Replace YOUR_TOKEN_HERE with your actual token)"
    echo ""
fi

echo "2. To load and start the daemon now:"
echo "   launchctl load $PLIST_PATH"
echo ""
echo "3. To stop the daemon:"
echo "   launchctl unload $PLIST_PATH"
echo ""
echo "4. To check status:"
echo "   launchctl list | grep $PLIST_NAME"
echo ""
echo "5. To view logs:"
echo "   tail -f $LOG_DIR/bot.log"
echo ""
echo "6. The bot will automatically:"
echo "   - Start on login"
echo "   - Restart if it crashes"
echo "   - Check for GitHub updates every 5 minutes"
echo "   - Pull updates and restart when changes are detected"
echo ""
echo -e "${GREEN}To start now, run:${NC}"
echo "   launchctl load $PLIST_PATH"

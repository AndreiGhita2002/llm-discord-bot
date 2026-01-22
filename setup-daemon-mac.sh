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

# Configuration - detect bot directory dynamically
if [ -n "$1" ]; then
    BOT_DIR="$(cd "$1" && pwd)"
elif [ -f "$(dirname "$0")/main.py" ]; then
    BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
elif [ -f "./main.py" ]; then
    BOT_DIR="$(pwd)"
else
    echo "Error: Could not find main.py. Run this script from the bot directory or pass the path as an argument."
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

# Create the runner script that handles updates
echo -e "${GREEN}Creating bot runner script...${NC}"
cat > "$BOT_DIR/run-bot.sh" << 'RUNNER_EOF'
#!/bin/bash

# Determine bot directory from script location
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
LOG_FILE="$BOT_DIR/logs/bot.log"
UPDATE_CHECK_INTERVAL="${UPDATE_CHECK_INTERVAL:-300}"
BRANCH="${GIT_BRANCH:-main}"

# Ensure logs directory exists
mkdir -p "$BOT_DIR/logs"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

check_for_updates() {
    cd "$BOT_DIR"

    # Fetch latest changes from remote
    git fetch origin "$BRANCH" 2>/dev/null

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
    log "Pulling latest changes..."
    git pull origin "$BRANCH"

    # Reinstall dependencies if pyproject.toml changed
    if git diff HEAD~1 --name-only | grep -q "pyproject.toml\|uv.lock"; then
        log "Dependencies changed, running uv sync..."
        uv sync
    fi
}

run_bot() {
    cd "$BOT_DIR"
    log "Starting bot..."

    # Run the bot using uv
    uv run python main.py 2>&1 | tee -a "$LOG_FILE" &
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
            # Force kill if still running
            if kill -0 "$PID" 2>/dev/null; then
                kill -9 "$PID" 2>/dev/null
            fi
        fi
        rm -f "$BOT_DIR/bot.pid"
    fi
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

# Initial start
run_bot

# Update check loop
while true; do
    sleep "$UPDATE_CHECK_INTERVAL"

    # Check if bot is still running
    if [ -f "$BOT_DIR/bot.pid" ]; then
        PID=$(cat "$BOT_DIR/bot.pid")
        if ! kill -0 "$PID" 2>/dev/null; then
            log "Bot process died, restarting..."
            run_bot
            continue
        fi
    fi

    # Check for updates
    if check_for_updates; then
        stop_bot
        pull_updates
        run_bot
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

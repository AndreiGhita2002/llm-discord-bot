#!/bin/bash
set -e

BOT_SESSION="bot"
BOT_USER="botuser"
BOT_CMD="cd /app && uv run python main.py"

# --- Signal handling ---
cleanup() {
    echo "[entrypoint] Shutting down..."
    # Kill bot tmux session
    su - "$BOT_USER" -c "tmux kill-session -t $BOT_SESSION 2>/dev/null" || true
    # Stop sshd
    kill "$(cat /run/sshd.pid 2>/dev/null)" 2>/dev/null || true
    exit 0
}
trap cleanup SIGTERM SIGINT

# --- Start sshd ---
echo "[entrypoint] Starting sshd..."
# Use persistent host keys (mounted volume)
mkdir -p /etc/ssh/ssh_host_keys
if [ ! -f /etc/ssh/ssh_host_keys/ssh_host_ed25519_key ]; then
    echo "[entrypoint] Generating SSH host keys..."
    ssh-keygen -t ed25519 -f /etc/ssh/ssh_host_keys/ssh_host_ed25519_key -N ""
    ssh-keygen -t rsa -b 4096 -f /etc/ssh/ssh_host_keys/ssh_host_rsa_key -N ""
fi
/usr/sbin/sshd

# --- Sync dependencies ---
echo "[entrypoint] Syncing dependencies..."
cd /app
uv sync

# --- Initialize bot_memory if missing ---
if [ ! -d /app/bot_memory ]; then
    echo "[entrypoint] Initializing bot_memory directory..."
    mkdir -p /app/bot_memory
fi

# --- Fix permissions ---
chown -R "$BOT_USER:$BOT_USER" /app/bot_memory
chown -R "$BOT_USER:$BOT_USER" /opt/bot-venv 2>/dev/null || true

# --- Start bot in tmux ---
start_bot() {
    echo "[entrypoint] Starting bot in tmux session '$BOT_SESSION'..."
    su - "$BOT_USER" -c "cd /app && tmux new-session -d -s $BOT_SESSION '$BOT_CMD'"
}

start_bot
echo "[entrypoint] Container ready."

# --- Monitor loop: restart bot if tmux session dies ---
while true; do
    sleep 10
    if ! su - "$BOT_USER" -c "tmux has-session -t $BOT_SESSION 2>/dev/null"; then
        echo "[entrypoint] Bot session died, restarting..."
        sleep 2
        start_bot
    fi
done

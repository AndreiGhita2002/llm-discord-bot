#!/bin/bash
#
# botctl - manage the Discord bot process
#
# Usage: botctl {start|stop|restart|status|attach|logs}
#

BOT_SESSION="bot"
BOT_CMD="cd /app && uv run python main.py"

case "$1" in
    start)
        if tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Bot is already running."
            exit 1
        fi
        echo "Starting bot..."
        cd /app && tmux new-session -d -s "$BOT_SESSION" "$BOT_CMD"
        echo "Bot started. Use 'botctl attach' to view output."
        ;;

    stop)
        if ! tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Bot is not running."
            exit 1
        fi
        echo "Stopping bot..."
        tmux kill-session -t "$BOT_SESSION"
        echo "Bot stopped."
        ;;

    restart)
        if tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Stopping bot..."
            tmux kill-session -t "$BOT_SESSION"
            sleep 1
        fi
        echo "Starting bot..."
        cd /app && tmux new-session -d -s "$BOT_SESSION" "$BOT_CMD"
        echo "Bot restarted. Use 'botctl attach' to view output."
        ;;

    status)
        if tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Bot is running."
        else
            echo "Bot is not running."
        fi
        ;;

    attach)
        if ! tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Bot is not running. Start it with 'botctl start'."
            exit 1
        fi
        echo "Attaching to bot session (Ctrl+B, D to detach)..."
        tmux attach-session -t "$BOT_SESSION"
        ;;

    logs)
        if ! tmux has-session -t "$BOT_SESSION" 2>/dev/null; then
            echo "Bot is not running."
            exit 1
        fi
        # Capture and display recent tmux pane output
        tmux capture-pane -t "$BOT_SESSION" -p -S -100
        ;;

    *)
        echo "Usage: botctl {start|stop|restart|status|attach|logs}"
        echo ""
        echo "Commands:"
        echo "  start    Start the bot in a tmux session"
        echo "  stop     Stop the bot"
        echo "  restart  Restart the bot"
        echo "  status   Check if the bot is running"
        echo "  attach   Attach to the bot's tmux session (Ctrl+B, D to detach)"
        echo "  logs     Show recent bot output"
        exit 1
        ;;
esac

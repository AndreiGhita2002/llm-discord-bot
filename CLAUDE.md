# Discord LLM Bot

A Discord bot powered by a local Ollama LLM with optional web search capabilities.

**Default persona: Kronk** - The default configuration ships with "Kronk" (from Emperor's New Groove) as the bot personality, but this is fully customizable via `config.yaml`.

## AI Instructions

When working on this project, keep the "Known Issues / TODOs" section below up to date:
- Mark items as complete `[x]` or remove them when fixed
- Add new TODOs when you discover issues or leave something incomplete
- Add notes about non-obvious implementation details

When updating the changelog in README.md:
- Group all changes from the same day under one version
- Check the date of the last commit (`git log -1 --format=%cd --date=short`) - if it's today, add to that version
- Only increment the patch version (smallest digit) if last commit was a different day
- Only increment minor/major version if the user explicitly says it's a major change
- Today's date is available in the environment info at the start of the conversation

## Project Structure

- `main.py` - Main bot code
- `kronk_config.yaml` - Default config (Kronk persona), always loaded as base
- `config.yaml` - User overrides (gitignored, optional). Only needs fields you want to change.
- `memory.py` - Lightweight memory system (user summaries + conversation recall)
- `pyproject.toml` - Dependencies (uses uv)
- `setup-daemon-mac.sh` - macOS daemon setup script
- `setup-memory.sh` - Initializes memory directory and pulls embedding model
- `bot_memory/` - Created by setup script, stores user summaries and conversation embeddings (gitignored)
- `Dockerfile` - Container image for Docker deployment
- `docker-compose.yml` - Docker Compose service definition
- `docker/` - Docker support files (entrypoint, botctl, authorized_keys)

## Key Configuration

Configuration uses `kronk_config.yaml` as defaults, with `config.yaml` providing overrides. The configs are deep-merged, so `config.yaml` only needs fields you want to change (nested fields like `memory.do_memory` work too).
- **Model**: Configurable in `config.yaml` (default: `gemma3:27b`)
- **System prompt**: Customizable personality/behavior in `config.yaml`. Supports placeholders:
  - `{{discord_display_name}}`: Bot's display name (replaced at runtime)
  - `{{discord_user_id}}`: Bot's user ID (replaced at runtime)
  - `{{github_url}}`: GitHub URL from config (replaced at load time)
- **Message history**: Short-term memory settings in `config.yaml`:
  - `limit`: Number of recent messages to fetch (default: 10)
  - `max_age_minutes`: Ignore messages older than this (default: 60, set to 0 to disable)
- **Memory settings**: Long-term memory with granular toggles:
  - `do_memory`: Master toggle for all memory features
  - `user_memory`: Toggle user-specific summaries
  - `conversation_memory`: Toggle conversation recall
  - `user_summary_update_chance`: Probability of updating user summary (0.0-1.0)
  - `max_stored_conversations`: Maximum conversations to store
- **Tools**: TODO - placeholder in `config.yaml` for future implementation

Environment variables:
- **Discord token**: `DISCORD_BOT_TOKEN` env var (falls back to `KRONK_TOKEN` for backward compatibility)
- **Ollama API key**: `OLLAMA_API_KEY` env var (needed for web search - the search itself uses Ollama's cloud)

## How It Works

1. Bot responds when @mentioned or when someone replies to its message
2. When triggered, fetches recent messages from that channel (configurable limit and max age)
3. Referenced messages are inserted *before* the user's current message in context (so model responds to user, not the reference)
4. Uses `ollama.AsyncClient()` for async LLM calls

## Web Search Feature

Web search uses a **two-model architecture** to work with models that don't support tool calling:
1. **Function model** (e.g., `functionary`): Decides if/which tools to call based on the user's message
2. **Main model** (e.g., `gemma3:27b`): Generates the conversational response with tool results injected into context

This approach keeps the personality model (gemma3) for all user-facing responses while using a specialized model for tool decisions.

**Requirements**:
- `function_model` in config (default: `functionary`) - pull with `ollama pull functionary`
- `web_search: true` in config
- `OLLAMA_API_KEY` env var (the actual web search/fetch uses Ollama's cloud API)

## Memory System

The bot has a lightweight memory system (`memory.py`) that provides:

1. **User Summaries** (`user_memory`): LLM-generated summaries of each user (personality, interests, facts). Updated probabilistically (configurable chance) to avoid overhead.

2. **Conversation Recall** (`conversation_memory`): Stores conversation snippets with embeddings for semantic search. When a user sends a message, relevant past conversations are retrieved and injected into context.

Both features can be independently toggled via config. The `do_memory` flag is a master switch that disables all memory features when false.

**Requirements**:
- Needs `nomic-embed-text` model in Ollama: `ollama pull nomic-embed-text`
- Data stored in `./bot_memory/` directory (configurable via `memory_dir` in config.yaml)
- Backward compatible: falls back to `./kronk_memory/` if it exists
- Keeps last N conversations (configurable via `max_stored_conversations`, default 500) to prevent unbounded growth

## Docker Deployment

The bot can be deployed in a Docker container with SSH access for collaborators.

### Architecture
- Single container: sshd + bot in tmux, managed by `botctl`
- Bind-mounted project dir at `/app` - collaborators can edit code, git pull, etc.
- Connects to Ollama on the host via `OLLAMA_HOST=http://host.docker.internal:11434`
- `UV_PROJECT_ENVIRONMENT=/opt/bot-venv` avoids conflict with host's `.venv`
- `restart: unless-stopped` for auto-restart

### Files
- `Dockerfile` - Container image definition
- `docker-compose.yml` - Service configuration
- `docker/entrypoint.sh` - Container startup (sshd + bot + monitor loop)
- `docker/botctl.sh` - Bot management script (installed to `/usr/local/bin/botctl`)
- `docker/authorized_keys` - SSH public keys for collaborators (gitignored)

### Quick Start
```bash
# Add collaborator SSH keys
echo "ssh-ed25519 AAAA... user@host" >> docker/authorized_keys

# Copy existing config/memory if you have them
cp /path/to/config.yaml .
cp -r /path/to/bot_memory/ ./bot_memory/

# Set env vars (or use .env file)
export DISCORD_BOT_TOKEN="..."

# Build and run
docker compose up -d

# SSH in
ssh -p 2222 botuser@localhost
botctl status
```

**Note**: The project dir is bind-mounted at `/app`, so `config.yaml` and `bot_memory/` just need to exist in the project root on the host. No `docker cp` needed.

### Collaborator Commands
```bash
botctl start|stop|restart  # manage bot
botctl status              # check if running
botctl attach              # view live output (Ctrl+B, D to detach)
botctl logs                # show recent output
```

## Known Issues / TODOs

[ ] Websearch: implemented two-model architecture, needs testing.
[ ] Log channel feature: the user can set a channel for bot logs, and the bot will announce when it's turning on or off.
[ ] Implement configurable tools system in config.yaml.

## Running

```bash
# Install dependencies
uv sync

# (Optional) Create config overrides - only add fields you want to change
# If not created, defaults from kronk_config.yaml are used
echo 'model: "llama3.1:8b"' > config.yaml  # example: just override the model

# Setup memory system (creates directory + pulls embedding model)
./setup-memory.sh

# Set environment variables
export DISCORD_BOT_TOKEN="your-discord-token"
export OLLAMA_API_KEY="your-ollama-key"  # optional, for web search

# Run
uv run python main.py
```

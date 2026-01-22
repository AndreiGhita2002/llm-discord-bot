# Discord LLM Bot

A Discord bot powered by a local Ollama LLM with optional web search capabilities.

**Default persona: Kronk** - The default configuration ships with "Kronk" (from Emperor's New Groove) as the bot personality, but this is fully customizable via `config.yaml`.

## AI Instructions

When working on this project, keep the "Known Issues / TODOs" section below up to date:
- Mark items as complete `[x]` or remove them when fixed
- Add new TODOs when you discover issues or leave something incomplete
- Add notes about non-obvious implementation details

## Project Structure

- `main.py` - Main bot code
- `kronk_config.yaml` - Default config template (Kronk persona)
- `config.yaml` - User's local config (gitignored, created by copying kronk_config.yaml)
- `memory.py` - Lightweight memory system (user summaries + conversation recall)
- `pyproject.toml` - Dependencies (uses uv)
- `setup-daemon-mac.sh` - macOS daemon setup script
- `setup-memory.sh` - Initializes memory directory and pulls embedding model
- `bot_memory/` - Created by setup script, stores user summaries and conversation embeddings (gitignored)

## Key Configuration

Configuration is loaded from `config.yaml`:
- **Model**: Configurable in `config.yaml` (default: `gemma3:27b`)
- **System prompt**: Customizable personality/behavior in `config.yaml`
- **Message history**: Limit and settings in `config.yaml`
- **Memory settings**: User summary chance, max conversations in `config.yaml`
- **Tools**: TODO - placeholder in `config.yaml` for future implementation

Environment variables:
- **Discord token**: `DISCORD_BOT_TOKEN` env var (falls back to `KRONK_TOKEN` for backward compatibility)
- **Ollama API key**: `OLLAMA_API_KEY` env var (only needed for web search)
- **Web search**: Only enabled if `OLLAMA_API_KEY` is found

## How It Works

1. Bot responds when @mentioned or when someone replies to its message
2. When triggered, fetches the last 20 messages from that channel via `channel.history()`
3. Referenced messages are inserted *before* the user's current message in context (so model responds to user, not the reference)
4. Uses `ollama.AsyncClient()` for async LLM calls

## Web Search Feature

Web search uses Ollama's cloud API (not local), so it requires:
- An API key from https://ollama.com/settings/keys
- `OLLAMA_API_KEY` environment variable set
- A model that supports tool calling (llama3.1+, qwen3, etc.)

Automatically enabled when `OLLAMA_API_KEY` is set.

## Memory System

The bot has a lightweight memory system (`memory.py`) that provides:

1. **User Summaries**: LLM-generated summaries of each user (personality, interests, facts). Updated probabilistically (20% chance after each interaction) to avoid overhead.

2. **Conversation Recall**: Stores conversation snippets with embeddings for semantic search. When a user sends a message, relevant past conversations are retrieved and injected into context.

**Requirements**:
- Needs `nomic-embed-text` model in Ollama: `ollama pull nomic-embed-text`
- Data stored in `./bot_memory/` directory (configurable via `memory_dir` in config.yaml)
- Backward compatible: falls back to `./kronk_memory/` if it exists
- Keeps last 500 conversations max to prevent unbounded growth

## Known Issues / TODOs

[ ] Websearch not working.
[ ] Log channel feature: the user can set a channel for bot logs, and the bot will announce when it's turning on or off.
[ ] Implement configurable tools system in config.yaml.

## Running

```bash
# Install dependencies
uv sync

# Create your config (won't be overwritten by git updates)
cp kronk_config.yaml config.yaml

# Setup memory system (creates directory + pulls embedding model)
./setup-memory.sh

# Set environment variables
export DISCORD_BOT_TOKEN="your-discord-token"
export OLLAMA_API_KEY="your-ollama-key"  # optional, for web search

# Run
uv run python main.py
```

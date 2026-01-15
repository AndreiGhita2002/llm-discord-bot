# Kronk Discord Bot

A Discord bot powered by a local Ollama LLM with optional web search capabilities.

## AI Instructions

When working on this project, keep the "Known Issues / TODOs" section below up to date:
- Mark items as complete `[x]` or remove them when fixed
- Add new TODOs when you discover issues or leave something incomplete
- Add notes about non-obvious implementation details

## Project Structure

- `main.py` - Main bot code
- `pyproject.toml` - Dependencies (uses uv)
- `setup-daemon-mac.sh` - macOS daemon setup script

## Key Configuration

- **Model**: `gpt-oss:20b` (local Ollama model, but it might change)
- **Discord token**: `KRONK_TOKEN` env var
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

## Known Issues / TODOs

[ ] Websearch not working.  
[ ] Log channel feature: the user can set a channel to be Kronk's logs, and Kronk will say when he's turning on or off in those channels.

## Running

```bash
# Install dependencies
uv sync

# Set environment variables
export KRONK_TOKEN="your-discord-token"
export OLLAMA_API_KEY="your-ollama-key"  # optional, for web search

# Run
uv run python main.py
```

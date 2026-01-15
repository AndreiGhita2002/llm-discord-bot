# Kronk Discord Bot

A Discord bot powered by a local Ollama LLM with optional web search capabilities.

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

1. Bot listens to all messages and stores last 20 in `message_history` deque
2. Responds when @mentioned or when someone replies to its message
3. Referenced messages are inserted *before* the user's current message in context (so model responds to user, not the reference)
4. Uses `ollama.AsyncClient()` for async LLM calls

## Web Search Feature

Web search uses Ollama's cloud API (not local), so it requires:
- An API key from https://ollama.com/settings/keys
- `OLLAMA_API_KEY` environment variable set
- A model that supports tool calling (llama3.1+, qwen3, etc.)

Enable by setting `do_websearch = True` in main.py.

## Known Issues / TODOs

- [ ] Message history is global - should be split by channel/server
- [ ] Empty responses sometimes occur (failsafe exists but root cause unknown)
- [ ] PyCharm shows warning on `ollama.AsyncClient().chat()` await - works fine at runtime

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

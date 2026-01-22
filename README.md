# Discord LLM Bot

A Discord bot powered by a local Ollama LLM with a configurable personality. Ships with **Kronk** (from Emperor's New Groove) as the default persona - a fun, conversational member of your server who can also help with fact-checking.

All bot settings (model, personality, memory) are customizable via `config.yaml`.

## Features

- Runs on a local LLM via Ollama (no cloud API costs for basic usage)
- Responds when @mentioned or when you reply to its messages
- Remembers the last 20 messages for context
- Optional web search capability for up-to-date information

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com/) running locally
- A Discord bot token

## Setup

1. **Clone the repo**
   ```bash
   git clone https://github.com/AndreiGhita2002/llm-discord-bot.git
   cd llm-discord-bot
   ```

2. **Install dependencies** (using [uv](https://github.com/astral-sh/uv))
   ```bash
   uv sync
   ```

3. **Set up Ollama**

   Install Ollama and pull the model:
   ```bash
   ollama pull gpt-oss:20b
   ```

4. **Configure environment variables**
   ```bash
   export DISCORD_BOT_TOKEN="your-discord-bot-token"
   ```

5. **Run the bot**
   ```bash
   uv run python main.py
   ```

## Web Search (Optional)

The bot can search the web for current information. This feature uses Ollama's cloud API.

1. Get a free API key from https://ollama.com/settings/keys
2. Set the environment variable:
   ```bash
   export OLLAMA_API_KEY="your-api-key"
   ```

Web search is automatically enabled when the API key is present.

## Running as a Service (macOS)

Use the included script to set up the bot as a launchd daemon:

```bash
./setup-daemon-mac.sh
```

## Usage

- **@mention** the bot to get a response
- **Reply** to any of the bot's messages to continue the conversation

## Changelog

### v0.2.0

- **Configuration file**: All bot settings now live in `config.yaml` (model, system prompt, message history, memory settings)
- **Bot-agnostic codebase**: Code no longer hardcodes "Kronk" - personality is fully configurable
- **Renamed env var**: `KRONK_TOKEN` → `DISCORD_BOT_TOKEN` (old name still works for backward compatibility)
- **Renamed memory directory**: `kronk_memory/` → `bot_memory/` (old directory still works for backward compatibility)
- **Added PyYAML dependency** for config loading

### v0.1.0

- Initial release
- Basic Discord bot with Ollama integration
- Memory system with user summaries and conversation recall
- Optional web search via Ollama cloud API

## License

MIT

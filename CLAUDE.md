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
- `tools.py` - Tool registry (schemas + handlers + config gating) for native tool calling
- `kronk_config.yaml` - Default config (Kronk persona), always loaded as base
- `config.yaml` - User overrides (gitignored, optional). Only needs fields you want to change.
- `memory.py` - Lightweight memory system (user summaries + conversation recall), SQLite + sqlite-vec backed
- `pyproject.toml` - Dependencies (uses uv)
- `setup-daemon-mac.sh` - macOS daemon setup script
- `setup-memory.sh` - Initializes memory directory and pulls embedding model
- `bot_memory/` - Holds `memory.db` (SQLite + sqlite-vec: user summaries + conversation embeddings), created on first run (gitignored)
- `bot_reminders.json` - Persisted pending reminders, rescheduled on startup (gitignored)

## Key Configuration

Configuration uses `kronk_config.yaml` as defaults, with `config.yaml` providing overrides. The configs are deep-merged, so `config.yaml` only needs fields you want to change (nested fields like `memory.do_memory` work too).
- **Model**: Configurable in `config.yaml` (default: `qwen3.5:35b-a3b`; must support native tool calling)
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

## Web Search / Tool Calling

The bot uses a **single tool-calling model** (default `qwen3.5:35b-a3b`) that natively decides
when to call tools. `query_ollama()` runs an agentic loop: call the model with the available tools →
if it requests tools, execute them and feed results back as `role: "tool"` messages → repeat until
the model returns a final answer (bounded by `max_tool_rounds`, default 5). This supports multi-step
tool use, e.g. `web_search` → `web_fetch` → answer.

Tools live in `tools.py` as a registry of `Tool(name, schema, handler, ...)` entries.
`configure()` reads the `tools:` config section (+ capability checks) to decide which are active;
`get_schemas()` feeds the model; `execute()` dispatches a call. Handlers get `(args, ctx)` where
`ctx: ToolContext` carries the Discord `message`/`client` so tools can act on the server.

**Adding a tool**: write an `async def _handler(args, ctx) -> str`, add one `Tool(...)` entry to
`_REGISTRY`, and add its default to the `tools:` block in `kronk_config.yaml`. If it's a lookup,
add a `DEFAULT_ANNOUNCEMENTS` entry (list of variants) so the bot posts "Looking up …" before it runs.

**Available tools** (each toggled in `tools:` config):
- Web (need `OLLAMA_API_KEY`): `web_search`, `web_fetch`
- Fun/social: `roll_dice`, `flip_coin`, `random_choice`, `set_reminder`, `create_poll`, `start_thread`
- Knowledge: `wikipedia` (no key needed), `calculator` (safe AST eval), `get_time`
- Embodiment: `add_reaction`, `set_status`, `set_nickname`, `get_user_info`
- Memory (need `memory.do_memory`): `remember_fact`, `recall`

**Gating**: web tools require `OLLAMA_API_KEY`; memory tools require `do_memory`; Discord action
tools require the bot's role/permissions at runtime. The `model` must support native tool calling
(gemma3 does NOT; qwen3.5, llama3.1, mistral do).

**Tool announcements** (`announce_tools: true`): before a *lookup* tool runs, the bot posts a short
"🔎 Searching…"/"📖 Looking up… on Wikipedia" line so users know the reply draws from a real source.
Action tools (reactions, status…) don't announce - the action is self-evident. Each tool has a list
of variants (one picked at random for variety) with `{placeholder}` fields from the tool's args.
Neutral defaults live in `tools.py` (`DEFAULT_ANNOUNCEMENTS`); per-persona overrides go in the
`tool_announcements:` config block (kronk_config.yaml ships Kronk-voiced ones). Loaded via
`tools.configure_announcements()`.

## Memory System

The bot has a lightweight memory system (`memory.py`) that provides:

1. **User Summaries** (`user_memory`): LLM-generated summaries of each user (personality, interests, facts). Updated probabilistically (configurable chance) to avoid overhead.

2. **Conversation Recall** (`conversation_memory`): Stores conversation snippets with embeddings for semantic search. When a user sends a message, relevant past conversations are retrieved and injected into context.

Both features can be independently toggled via config. The `do_memory` flag is a master switch that disables all memory features when false.

**Storage** (`memory.db`, SQLite + sqlite-vec):
- Embeddings are stored as packed **float32 blobs** and indexed for KNN via the `sqlite-vec` vec0 virtual table (`distance_metric=cosine`) - far more space/CPU efficient than the old JSON-load-and-linear-scan approach.
- User summaries and conversation documents are stored **zlib-compressed** - smaller on disk and not casually human-readable (the whole `.db` is binary anyway). This is obfuscation, not encryption: the goal was efficiency + no plaintext, not confidentiality (the host holds no key to protect).
- Two tables: `conversations` (metadata + compressed `document`) joined by rowid to `conversations_vec` (the vectors); plus `user_summaries`. Access is serialized by a `threading.Lock` since recall runs in a worker thread.
- Channel filtering over-fetches KNN then filters in Python (fine at this scale).

**Requirements**:
- Needs `nomic-embed-text` model in Ollama: `ollama pull nomic-embed-text` (768-dim; `EMBEDDING_DIM` in memory.py must match if the model changes)
- `sqlite-vec` Python package (in pyproject); the host Python's `sqlite3` must allow loadable extensions
- Data stored in `./bot_memory/memory.db` (dir configurable via `memory_dir` in config.yaml), created on first run
- Backward compatible: falls back to `./kronk_memory/` dir if it exists (old `*.json` files are ignored, not migrated)
- Keeps last N conversations (configurable via `max_stored_conversations`, default 500) to prevent unbounded growth

## Known Issues / TODOs

[x] Websearch: migrated from two-model hack to single-model native tool-calling loop (smoke-tested with llama3.1:8b; verify web_search/web_fetch on the deployed qwen3.5 model with a real OLLAMA_API_KEY).
[x] Configurable tools system: `tools:` config section with per-tool on/off (see `tools.py`).
[x] Expanded tool set: web, fun/social, knowledge, embodiment, and memory tools (moderation intentionally skipped for now).
[x] Tool-call announcements: bot posts "Looking up …" before lookups (`announce_tools`).
[ ] Live-verify Discord-action tools (add_reaction, set_status, set_nickname, get_user_info, create_poll, start_thread) on the deployed bot - unit + mock-integration tested, but not yet run against real Discord. create_poll needs discord.py 2.4+.
[x] Persist reminders: stored in `reminders_file` (default `./bot_reminders.json`); rescheduled on startup via `tools.reschedule_reminders()` in on_ready, overdue ones fire immediately.
[x] Memory storage: migrated JSON -> SQLite + sqlite-vec (binary float32 vectors, zlib-compressed text). More efficient + not plaintext on disk. Verified with a fake embedding; run once against real `nomic-embed-text` on the deployed bot.
[x] Latency + hallucination fixes: `use_thinking: false` disables the reasoning pass (faster, no reasoning leak); system prompt makes Kronk tool-shy (answer from own knowledge, offer to look up rather than auto-searching, never fabricate); per-channel asyncio lock serializes concurrent messages; tool-announcement messages are excluded from fetched history so the model doesn't "continue" a half-started answer.
[ ] If double-replies persist after these fixes, suspect TWO bot processes on the host (check `ps aux | grep '[m]ain.py'`) - the per-channel lock only serializes within one process. A single-instance guard was tried once (commit 0393d45) and reverted (91def21).
[ ] Log channel feature: the user can set a channel for bot logs, and the bot will announce when it's turning on or off.
[ ] Optional: moderation tools (timeout/role/pin) with a permission/allowlist model - deferred by choice.

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

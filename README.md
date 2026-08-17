# Discord LLM Bot

A Discord bot powered by a local Ollama LLM with a configurable personality. Ships with **Kronk** (from Emperor's New Groove) as the default persona - a fun, conversational member of your server who can also help with fact-checking.

All bot settings (model, personality, memory) are customizable via `config.yaml`.

## Features

- Runs on a local LLM via Ollama (no cloud API costs for basic usage)
- Responds when @mentioned or when you reply to its messages
- Configurable message history for conversation context
- **Memory system** with user summaries and conversation recall (can be toggled on/off)
- Optional web search capability for up-to-date information
- **YouTube audio in voice channels** — `!play <song name or link>` and the bot joins your voice channel

## Requirements

- Python 3.12+
- [Ollama](https://ollama.com/) running locally
- A Discord bot token
- `ffmpeg` (+ libopus) on the host, only if you want voice playback — `brew install ffmpeg`

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

3. **Create your config file**
   ```bash
   cp kronk_config.yaml config.yaml
   ```
   Edit `config.yaml` to customize the bot's personality, model, and other settings. Your local config won't be overwritten by updates.

4. **Set up Ollama**

   Install Ollama and pull the model:
   ```bash
   ollama pull gemma3:27b
   ```

5. **Configure environment variables**
   ```bash
   export DISCORD_BOT_TOKEN="your-discord-bot-token"
   ```

6. **Run the bot**
   ```bash
   uv run python src/main.py
   ```
   Run it from the project root. The bot resolves its config and data files relative to the
   repo (not your working directory), so it also works if you launch it from elsewhere.

## Web Search (Optional)

The bot can search the web for current information using a **two-model architecture**:
1. A **function-calling model** (e.g., `functiongemma`) decides when to search
2. The **main model** (e.g., `gemma3:27b`) generates the response with search results

This lets you use conversation models that don't natively support tools.

**Setup**:
1. Pull the function-calling model:
   ```bash
   ollama pull functiongemma
   ```
2. Get a free API key from https://ollama.com/settings/keys (the search itself uses Ollama's cloud)
3. Set the environment variable:
   ```bash
   export OLLAMA_API_KEY="your-api-key"
   ```
4. Enable in config:
   ```yaml
   web_search: true
   function_model: "functiongemma"  # or another function-calling model
   ```

## Memory System

The bot has a lightweight long-term memory system that can be configured in `config.yaml`:

```yaml
memory:
  do_memory: true              # Master toggle for all memory features
  user_memory: true            # Remember facts about individual users
  conversation_memory: true    # Recall relevant past conversations
  user_summary_update_chance: 0.2  # Probability of updating user summary (0.0-1.0)
  max_stored_conversations: 500    # Maximum conversations to store
```

**Setup**: Run `./setup-memory.sh` to initialize the memory directory and pull the embedding model.

When enabled, the bot will:
- Build summaries of users based on their messages (personality, interests, facts)
- Store conversation snippets with semantic embeddings
- Recall relevant past conversations when responding

## Voice Playback (YouTube)

The bot can join a voice channel and stream audio from YouTube. Audio is streamed, never
downloaded: `yt-dlp` resolves a direct media URL and `ffmpeg` pipes it into Discord.

**Setup**:
1. Install the system dependencies (this also provides libopus, which Discord needs to encode
   the audio):
   ```bash
   brew install ffmpeg          # macOS; on Debian/Ubuntu: apt install ffmpeg
   ```
2. `uv sync` installs the Python side (`yt-dlp`, `PyNaCl`, `davey`).

   `davey` is not optional: Discord enforced end-to-end voice encryption (DAVE) on
   2026-03-02, and without it the voice gateway rejects the bot with close code 4017.
3. Make sure the bot has the **Connect** and **Speak** permissions in your voice channels.

**Commands** — typed as normal chat messages, not Discord slash commands. They use a `!`
prefix so typing one doesn't pop up Discord's slash-command autocomplete:

| Command | What it does |
| --- | --- |
| `!play <search terms>` | Joins your voice channel and plays the top YouTube result |
| `!play <url>` | Same, but for a direct link |
| `!skip` | Skip the current track |
| `!stop` | Stop and clear the queue (stays connected) |
| `!leave` | Stop and disconnect |
| `!queue` | Show what's lined up |
| `!np` | Show the current track |
| `!pause`, `!resume` | Pause / resume playback |

`!help` lists these plus every other command the bot knows.

**Config** (`voice:` block — see `kronk_config.yaml` for all options and Kronk-voiced message
templates):

```yaml
voice:
  enabled: true
  volume: 1.0
  max_duration_minutes: 180     # refuse anything longer (0 = no limit)
  max_queue: 20                 # tracks waiting per server
  idle_timeout_seconds: 300     # leave after this long with an empty queue
  leave_when_alone: true        # leave when the last human leaves the channel
  cookies_file: null            # yt-dlp cookies, if YouTube starts asking
```

If `ffmpeg`, `yt-dlp`, `PyNaCl` or `davey` is missing, the feature reports itself unavailable at
startup and the commands explain what's missing in chat instead of erroring out.

## Running as a Service (macOS)

Use the included script to set up the bot as a launchd daemon:

```bash
./setup-daemon-mac.sh
```

### Stopping the bot

`kill-bot.py` stops the bot at every layer. This matters because the layers restart each
other: the launchd agent restarts `run-bot.sh`, which restarts the bot within ~15 seconds if
it looks dead or hung — so killing the Python process alone just brings it back.

```bash
./kill-bot.py                 # stop the bot AND unload the daemon
./kill-bot.py --keep-daemon   # kill just the bot; the guard restarts it (i.e. a restart)
./kill-bot.py --dry-run       # show what would be killed
./kill-bot.py --all           # also hunt bot processes from other checkouts (asks first)
```

It's stdlib-only, so it still works if the venv is broken. It matches both the current
(`src/main.py`) and older (`main.py` at the root) layouts, cleans up the stale `bot.pid` and
`bot.heartbeat`, and exits non-zero if anything survived.

## Usage

- **@mention** the bot to get a response
- **Reply** to any of the bot's messages to continue the conversation
- **`!play <song>`** to have the bot play YouTube audio in your voice channel (see above)
- **`!help`** to list every command

## Changelog

### v0.2.3

- **`!help`**: lists every command the bot knows, grouped into music / server admin / chat.
  Each feature module owns its own help entries next to the code that implements them, so the
  listing can't drift out of sync. Voice commands still appear when playback is unavailable,
  annotated with the reason, rather than silently vanishing.
- **Fixed voice close code 4017**: Discord's end-to-end voice encryption (DAVE) became
  mandatory on 2026-03-02. discord.py implements it but leaves the `davey` package optional, so
  without it the bot advertised no DAVE support and Discord refused every voice connection —
  visible only as `Failed to connect to voice... Retrying in 1.0s` on repeat. `davey` is now a
  required dependency, and the voice commands check for it so a missing install explains itself
  in chat rather than retry-looping.
- **`kill-bot.py`**: stops the bot at every layer (launchd agent → `run-bot.sh` guard → bot
  process), so it stays stopped instead of being restarted by the layer above. `--keep-daemon`
  turns it into a restart, `--dry-run` shows what it would kill. Stdlib-only, so it works even
  if the venv is broken.
- **Source moved to `src/`**: all Python modules now live in `src/`, with configs, scripts and
  runtime state (memory db, reminders, heartbeat, logs) staying in the project root. The entry
  point is `uv run python src/main.py`. Config paths are now resolved relative to the project
  root instead of the working directory, so the bot runs identically from anywhere; the daemon
  scripts were updated to match.
- **YouTube voice playback** (`voice.py`): `!play <search terms or url>` makes the bot join your
  voice channel and stream the top YouTube result, plus `!skip`, `!stop`, `!leave`, `!queue`,
  `!np`, `!pause` and `!resume`. Tracks queue up per server and play in order; the bot leaves
  after `idle_timeout_seconds` with nothing to play, or as soon as the last human leaves the
  channel. Audio is streamed rather than downloaded (yt-dlp resolves a media URL, ffmpeg pipes
  it into Discord), and every yt-dlp call runs off the event loop with a timeout so a slow
  YouTube lookup can't freeze the bot. New `voice:` config block with per-persona message
  templates. Adds the `yt-dlp`, `PyNaCl` and `davey` dependencies; `ffmpeg` + libopus are system
  requirements (`brew install ffmpeg`). Commands use a `!` prefix rather than `/` so typing one
  doesn't trigger Discord's native slash-command autocomplete.

### v0.2.2

- **Single-model native tool calling**: Replaced the two-model web-search architecture with a single tool-calling model that runs a proper agentic loop (call tool → read result → optionally call again → answer). This enables multi-step tool use like search → fetch → respond.
- **New default model**: `qwen3.5:35b-a3b` — a MoE model with strong native tool calling and expressive, in-character output that fits comfortably on 64GB Apple Silicon.
- **Removed** `function_model` config option (no longer needed).
- **New config option**: `max_tool_rounds` (default 5) caps tool-call rounds to prevent runaway loops.
- **Tool registry** (`tools.py`): a config-driven registry with a per-tool `tools:` on/off section in config. Adding a tool is a handler + one registry entry.
- **New tools** across five groups: web (`web_search`, `web_fetch`), fun/social (`roll_dice`, `flip_coin`, `random_choice`, `set_reminder`, `create_poll`, `start_thread`), knowledge (`wikipedia`, `calculator`, `get_time`), embodiment (`add_reaction`, `set_status`, `set_nickname`, `get_user_info`), and memory (`remember_fact`, `recall`).
- **Tool-call announcements** (`announce_tools`, default on): the bot posts a short "🔎 Looking up… " line before running a lookup tool, so users know the reply draws from a real/deterministic source. Each tool has several variants (picked at random for variety), and they're fully customizable per-persona via the `tool_announcements:` config block — the default Kronk config ships in-character ones.
- **Persistent reminders**: `set_reminder` now survives restarts. Pending reminders are saved to `reminders_file` (default `./bot_reminders.json`) and rescheduled on startup; any that came due while the bot was offline fire immediately.
- **SQLite-backed memory**: memory moved from JSON files to a `sqlite-vec` database (`bot_memory/memory.db`). Embeddings are stored as packed binary vectors with indexed nearest-neighbor search (much more efficient than the old load-everything-and-scan approach), and summaries/conversations are zlib-compressed — smaller on disk and no longer plaintext. Adds the `sqlite-vec` dependency; existing JSON memories are not migrated (starts fresh).
- **Faster, less erratic replies**: new `use_thinking` option (default `false`) turns off the model's slow internal reasoning pass — quicker responses and no reasoning traces leaking into chat. The bot is now tool-shy by default: it answers conversationally from its own knowledge and *offers* to look things up rather than reflexively searching, and won't fabricate facts a tool would provide.
- **No more double / cross-wired replies**: messages in a channel are now handled one at a time (per-channel lock), so overlapping questions from multiple people no longer cause duplicate answers or Kronk responding on someone else's behalf. The bot's own "looking it up…" status messages are also excluded from its context so it stops "continuing" half-finished answers.
- **Self-recovery watchdog**: the bot now survives hangs without manual intervention. An in-process watchdog thread force-exits if the asyncio event loop stops advancing (e.g. a blocking call froze it), and the daemon runner restarts a fresh process. The runner also detects a *hung* (not just dead) bot via a heartbeat file, restarts unhealthy processes within seconds instead of minutes, and wraps all git operations in timeouts using `git reset --hard` — so a bad network or dirty tree can never wedge the guard loop. New (optional) config: `watchdog_timeout`, `watchdog_interval`, `heartbeat_file`.
- **Web tools can't freeze the bot**: `web_search`/`web_fetch` (synchronous Ollama-cloud calls) are now offloaded to a thread with a hard timeout, so a stalled search can no longer block the whole event loop. They also degrade to a clear message when no `OLLAMA_API_KEY` is set, so the tools can stay enabled in config for whenever a key is added.

### v0.2.1

- **Two-model web search**: New architecture using a function-calling model (e.g., `functiongemma`) to decide when to search, while the main model handles conversation. This enables web search with models that don't support tools natively.
- **New config option**: `function_model` to specify which model handles tool decisions

### v0.2.0

- **Configuration file**: All bot settings now live in `config.yaml` (model, system prompt, message history, memory settings)
- **User-local config**: `config.yaml` is gitignored; copy from `kronk_config.yaml` template. Your config won't be overwritten by updates.
- **Bot-agnostic codebase**: Code no longer hardcodes "Kronk" - personality is fully configurable
- **Renamed env var**: `KRONK_TOKEN` → `DISCORD_BOT_TOKEN` (old name still works for backward compatibility)
- **Renamed memory directory**: `kronk_memory/` → `bot_memory/` (old directory still works for backward compatibility)
- **Added PyYAML dependency** for config loading
- **Granular memory controls**: New config options to toggle memory features independently (`do_memory`, `user_memory`, `conversation_memory`)
- **Configurable conversation limit**: `max_stored_conversations` now read from config
- **System prompt placeholders**: Use `{{discord_display_name}}`, `{{discord_user_id}}`, and `{{github_url}}` in your system prompt
- **Message age filtering**: New `max_age_minutes` setting to ignore old messages from context

### v0.1.0

- Initial release
- Basic Discord bot with Ollama integration
- Memory system with user summaries and conversation recall
- Optional web search via Ollama cloud API

## License

MIT

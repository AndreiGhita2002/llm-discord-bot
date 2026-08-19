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

All Python code lives in `src/`; configs, scripts and runtime state live in the project root.

- `src/main.py` - Main bot code (entry point: `uv run python src/main.py`)
- `src/tools.py` - Tool registry (schemas + handlers + config gating) for native tool calling
- `src/voice.py` - YouTube audio playback in voice channels (`!play` and friends)
- `src/memory.py` - Lightweight memory system (user summaries + conversation recall), SQLite + sqlite-vec backed
- `src/discord_logging.py` - Optional per-server Discord log channel (`/setlogchannel`)
- `kronk_config.yaml` - Default config (Kronk persona), always loaded as base
- `config.yaml` - User overrides (gitignored, optional). Only needs fields you want to change.
- `pyproject.toml` - Dependencies (uses uv)
- `setup-daemon-mac.sh` - macOS daemon setup script
- `setup-memory.sh` - Initializes memory directory and pulls embedding model
- `kill-bot.py` - Stops the bot at every layer (launchd agent, run-bot.sh guard, bot process)
- `bot_memory/` - Holds `memory.db` (SQLite + sqlite-vec: user summaries + conversation embeddings), created on first run (gitignored)
- `bot_reminders.json` - Persisted pending reminders, rescheduled on startup (gitignored)

## Paths

Code is in `src/`, but **every config and runtime file lives in the project root** (the git
repo): `kronk_config.yaml`, `config.yaml`, `bot_memory/`, `bot_reminders.json`,
`log_channels.json`, `bot.heartbeat`, `.bot_version`, `logs/`.

`main.py` defines `PROJECT_ROOT` (the parent of `src/`) and `project_path()`, and every
relative path from config is resolved through it - never against the current working
directory. So the bot behaves identically started from the repo root, from `src/`, or by the
daemon. Absolute paths in config are used as-is. **When adding a new config path, run it
through `project_path()`** or it will silently land wherever the process happened to start -
which for `bot.heartbeat` would make run-bot.sh think the bot is hung and restart-loop it.

Process matching (`kill_other_instances()` in main.py, `pkill` in run-bot.sh) anchors on the
project root, not `src/`: the command line is `<root>/.venv/bin/python3 src/main.py`, so the
interpreter path and script path are separated and `src` never sits next to `main.py`.

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
- Voice (needs voice playback working): `queue_song`
- Knowledge: `wikipedia` (no key needed), `calculator` (safe AST eval), `get_time`
- Embodiment: `add_reaction`, `set_status`, `set_nickname`, `get_user_info`
- Memory (need `memory.do_memory`): `remember_fact`, `recall`

**Gating**: web tools require `OLLAMA_API_KEY`; memory tools require `do_memory`; `queue_song`
requires `needs_voice` (i.e. `VOICE_READY` - so `voice.configure()` MUST run *before*
`tools.configure()` in main.py, or the tool silently never registers); Discord action tools
require the bot's role/permissions at runtime. The `model` must support native tool calling
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

## Commands and `!help`

`!help` (in `main.py`) prints every command, grouped into Music / Server admin / Chat. It does
**not** hold the command list itself: each feature module exports a `COMMAND_HELP` list of
`(usage, description)` tuples sitting directly beside the regexes that implement those commands,
so the two can't drift apart. **Adding a command means adding one entry to its module's
`COMMAND_HELP`** - never edit the help text in main.py.

Voice commands are listed even when playback is unavailable, annotated with
`voice.unavailable_reason()` - omitting them entirely would look like the feature doesn't exist.

`_HELP_RE` is anchored (`^!help\s*$`) so "!help me pick a song" falls through to the LLM as a
normal question rather than being answered with a command dump.

## Voice Playback (`voice.py`)

`!play <search terms | url>` makes the bot join the caller's voice channel and stream the top
YouTube hit. Also `!skip`, `!stop`, `!leave`, `!queue`, `!np`, `!pause`, `!resume`.

These are **text commands matched by regex**, not Discord's slash-command API - same pattern as
`discord_logging.handle_command`, because the bot runs on a bare `discord.Client` (no
`CommandTree`). `voice.handle_command(message)` is called early in `on_message`, before any LLM
handling, and returns True when it consumed the message.

**How it plays**: audio is streamed, never downloaded. `yt-dlp` resolves a direct media URL
(`ytsearch1:<phrase>` for a search, the URL as-is for a link) and `discord.FFmpegPCMAudio` pipes
it into the voice channel. yt-dlp does blocking network I/O, so every resolve goes through
`asyncio.to_thread` + `asyncio.wait_for(RESOLVE_TIMEOUT)` - the same discipline as the web tools.

**State**: one `GuildPlayer` per guild in `_players`. It owns a queue and a single background
task (`_run`) that pops tracks, starts ffmpeg, and waits on an `asyncio.Event` that the ffmpeg
`after` callback sets (via `call_soon_threadsafe` - that callback runs on ffmpeg's thread).
Commands only mutate the queue or call into the voice client, so "what plays next" is decided in
one place. The loop disconnects after `idle_timeout_seconds` with an empty queue; `_wind_down()`
re-checks the queue first so a `!play` landing exactly on the idle timer doesn't make the bot
join and immediately leave.

Queued tracks re-resolve their media URL if it's older than `STREAM_URL_TTL` (30 min), because
YouTube's direct URLs expire.

**Track links in LLM replies**: when `queue_song` runs, it records `(title, url)` on
`ToolContext.queued_songs`, and `main.format_queued_songs()` appends a
`🎶 Now playing: <title> <url>` line to Kronk's reply after the model has written it. Done in
code deliberately - a model instructed to end with an exact line will drop it, paraphrase it or
invent a URL often enough to matter, and "always" has to mean always. The tool result tells the
model the link is added for it, so it doesn't repeat it. The line is still posted when the model
answers `<ignore>`. (A bare URL, not `[text](url)`: masked links only render inside embeds.)

**One entry point**: `enqueue_request(message, query, source=...) -> PlayResult` is the single path for
"resolve, join, queue", used by BOTH `!play` and the `queue_song` tool - so limits and checks
can't drift between them. `play_precheck()` holds the cheap checks (voice available, in a
server, requester in a channel, queue not full) so callers can bail before paying for a yt-dlp
lookup. `PlayResult.started` says whether playback began immediately, which tells the caller
NOT to post its own line - the player loop already announces "now playing". `source`
("command"/"tool") tags every log line, so when one route works and the other doesn't the logs
say which was running - and every rejection (not in voice, no permission, handshake timeout) is
logged at WARNING/ERROR so it reaches the Discord log channel instead of being paraphrased away
by the model. `connect()` is bounded by `CONNECT_TIMEOUT`; on the tool path a hanging handshake
would otherwise stall the whole agentic loop.

**YouTube player client (READ THIS WHEN PLAYBACK BREAKS)**: `voice.player_clients` (default
`["android"]`) controls which YouTube client yt-dlp impersonates. YouTube ties the media URL it
hands out to the requesting client, and ffmpeg fetches that URL *separately* with its own
headers - so most clients give ffmpeg a **403 Forbidden**. yt-dlp's own default (currently
`ANDROID_VR`) is one of the broken ones. Passing yt-dlp's `http_headers` through to ffmpeg does
NOT fix it (tested). The symptom is nasty: the bot joins, announces the track, then it vanishes
from the queue instantly with no audio - because ffmpeg dying on the input looks to discord.py
like an ordinary end-of-track. If playback breaks after a YouTube change, try other clients
(`ios`, `tv`, `web_safari`, `mweb`, `web`) - it's config, no code change needed.

**Never read `raw._process` after playback**: discord.py's `AudioPlayer.run()` calls the `after`
callback and THEN `source.cleanup()`, which resets `_process` to `discord.utils.MISSING`. Our
callback only *schedules* the event set, so by the time `_play()` resumes, `_process` is the
sentinel - and `MISSING is not None` is True, so a None-guard does NOT save you (it has no
`.poll()`; this shipped once as `'_MissingSentinel' object has no attribute 'poll'` and broke
every track it was meant to report on). `_play()` therefore keeps its own reference to the Popen
before starting, and all post-playback diagnostics run inside `_report_playback()` wrapped in
try/except - instrumentation must never be able to break playback.

**Silent-failure detection**: `_play()` times each track and captures ffmpeg's stderr (to a temp
FILE, not a PIPE - nothing drains a pipe mid-song, so a chatty stream would fill the 64KB buffer
and wedge ffmpeg). A track that ends in under `MIN_PLAYBACK_SECONDS` without a deliberate
skip/stop is logged at ERROR (so it reaches the Discord log channel) and reported in chat.
`_skip_requested` distinguishes a real failure from `!skip`/`!stop`/`!leave`.

**YouTube error classification**: `classify_youtube_error()` maps yt-dlp errors to a kind
(`bot-check`, `rate-limited`, `no-format`, `extractor-broken`, `unavailable`, …) plus a chat
explanation, because each needs a different fix (cookies file / wait / change player_clients /
upgrade yt-dlp). Anything unrecognised is logged as `unexpected` at ERROR - that's the signal
YouTube changed in a way this code hasn't seen. "No results" stays at INFO; it isn't a fault.

**Requirements**: `yt-dlp` + `PyNaCl` + `davey` (pip) and `ffmpeg` + libopus (system;
`brew install ffmpeg` covers both on macOS). All are checked in `configure()` at startup *and*
per command, so a missing piece degrades to a clear chat message. libopus isn't always on the
path discord.py autodetects, so `_ensure_opus()` tries the usual homebrew/linux locations
(override with `voice.opus_path`).

**DAVE / close code 4017**: Discord enforced end-to-end voice encryption (DAVE) on 2026-03-02.
discord.py implements it but leaves the `davey` package *optional*: with it absent,
`voice_state.max_dave_protocol_version` returns 0 and Discord kills the voice websocket with
close code 4017 - which surfaces only as `Failed to connect to voice... Retrying in 1.0s` in a
loop, with no clue about the cause. `davey` is therefore a hard dependency in pyproject, and
`_unavailable_reason()` checks for it so a missing install says so in chat instead of
retry-looping.

**Messages**: the chat lines ("Now playing…", "Queued…") use the same convention as tool
announcements - neutral defaults in `DEFAULT_MESSAGES`, Kronk-voiced overrides in the
`voice.messages:` config block, one variant picked at random.

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
[x] Self-recovery from hangs: in-process watchdog thread in main.py force-exits (`os._exit(1)`) if the event loop stops advancing (heartbeat stalls > `watchdog_timeout`, default 120s); an async task rewrites `bot.heartbeat` every 5s. The daemon runner (`run-bot.sh`, generated by setup-daemon-mac.sh) now restarts a *hung* bot (stale heartbeat), not just a dead one, on a fast (~15s) health tick, and wraps all git ops in timeouts + uses `git reset --hard origin/main` so a dirty tree / bad network can't wedge the guard or block a deploy. NOTE: the runner change only takes effect after re-running `./setup-daemon-mac.sh` on the host (run-bot.sh is generated, not pulled). The main.py watchdog deploys via the normal git auto-pull.
[x] Blocking web calls fixed: `ollama.web_search`/`web_fetch` are synchronous - they were called directly on the event loop and would freeze the whole bot on a cloud stall. Now offloaded via `asyncio.to_thread` + `asyncio.wait_for(WEB_TIMEOUT)`. They also short-circuit with a friendly message when `OLLAMA_API_KEY` is unset (tool can stay enabled in config). Root cause of the "Ollama error crashes the service" only bites once a key is added; the watchdog covers all other hang sources.
[x] Voice playback: `!play <song|url>` streams YouTube audio into the caller's voice channel (`voice.py`), with a per-guild queue, skip/stop/leave/pause, idle auto-disconnect and leave-when-alone. See the "Voice Playback" section above.
[x] Voice close code 4017 (DAVE): fixed by adding the `davey` dependency - see the DAVE note in the Voice Playback section. The host had ffmpeg/PyNaCl/yt-dlp fine (it cleared every `_unavailable_reason()` gate and reached the real voice handshake), it was purely the missing E2EE library.
[x] Silent playback failure (bot joined, announced, instantly skipped, no audio, no error): yt-dlp's default `ANDROID_VR` client hands out media URLs that ffmpeg gets a 403 on. Fixed with `voice.player_clients: ["android"]` + real failure detection in `_play()`. See the player-client note in the Voice Playback section.
[ ] Live-verify voice playback on the deployed bot: after the `davey` and player-client fixes, confirm audio actually reaches a voice channel. Unit + mock-integration tested (queue order, skip, stop, idle disconnect, all `!play` gating paths) and yt-dlp search/stream-URL resolution verified against real YouTube, but no audio has been pushed to a real voice channel yet. Needs the Connect/Speak permissions.
[ ] Standardise the command set: `voice.py` uses `!play`/`!skip`/… but `discord_logging.py` still uses `/setlogchannel` and `/clearlogchannel`, so the bot answers to two different prefixes (`!help` lists both, but it reads as an inconsistency). Settle on one (`!` avoids colliding with Discord's slash-command autocomplete popup) and move the log commands over. Each module still matches its own regex with `on_message` calling them in sequence - worth factoring into one shared dispatcher (name -> handler + help text) so adding a command is a single entry, the way `tools.py` does it. `COMMAND_HELP` is the first step of that: the help text already lives next to each module's regexes.
[ ] Song queue improvements: the basics are already in (`GuildPlayer.queue`, in-order playback, `!queue`, `!skip`, `max_queue` cap). Missing: playlist/album URLs (`noplaylist: True` currently takes only the first video of a link), shuffle and loop/repeat modes, removing or reordering entries (`!remove <n>`, `!move <from> <to>`), paging for long queues (`!queue` truncates at 10 with no way to see the rest), a "up next" hint when a track ends, and persisting the queue across restarts the way reminders are persisted.
[x] Expose music to the LLM: the `queue_song` tool lets the model queue tracks ("kronk, put on some jazz"). It routes through `voice.enqueue_request()`, the same path `!play` uses, so the model can't bypass the duration limit, queue cap or permission checks, and it can only ever join the *requester's* current voice channel - never one of its own choosing. Gated off entirely when voice playback isn't available.
[ ] DEPLOY HAZARD (src/ move): `run-bot.sh` is generated by setup-daemon-mac.sh and gitignored, so the host's copy still launches `uv run python main.py`. Deploying the src/ restructure without re-running `./setup-daemon-mac.sh` on the host leaves the runner unable to start the bot (and restart-looping). Re-run the setup script on the host as part of this deploy.
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

# Run (from the project root - the code is in src/)
uv run python src/main.py
```

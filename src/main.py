import os
import re
import sys
import signal
import asyncio
import random
import subprocess
import threading
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord
import ollama
import yaml

import claims
import expression
import memory
import tools
import voice
import discord_logging as dlog
from discord_logging import log
from tools import ToolContext


def strip_message_prefix(response: str) -> str:
    """Strip the input format prefix if the model mimics it in output.

    Matches patterns like: 'Name(123456)[12:34:56]: ' at the start of the response.
    Handles repeated prefixes by stripping until none remain.
    """
    #TODO: ideally we should get kronk to stop outputting these

    pattern = r'^[^(]+\(\d+\)\[\d{2}:\d{2}:\d{2}\]:\s*'
    while re.match(pattern, response):
        response = re.sub(pattern, '', response)
    return response


def strip_thinking(response: str) -> str:
    """Remove any <think>...</think> / <thinking>...</thinking> blocks a model might inline.

    With thinking enabled, Ollama returns the reasoning in a separate `message.thinking` field
    (which we never post), so content is normally clean. This is a safety net for models that
    inline the reasoning into content instead - we never want reasoning shown in chat.
    """
    if not response:
        return response
    cleaned = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", response,
                     flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


async def _safe_delete(msg) -> None:
    """Delete a message, ignoring any failure (e.g. already gone / no permission)."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass


# The bot's code lives in src/; the configs and all runtime state (memory db, reminders,
# heartbeat, logs) live in the project root next to it. Relative paths from config are resolved
# against that root rather than the current working directory, so the bot behaves identically
# whether it's launched from the repo root, from src/, or by the daemon - and, crucially, the
# heartbeat file always lands where run-bot.sh looks for it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def project_path(path) -> Path:
    """Resolve a configured path: absolute paths as given, relative ones from PROJECT_ROOT."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override values take precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(config_path: str = "config.yaml", default_path: str = "kronk_config.yaml") -> dict:
    """Load configuration with fallback to defaults.

    Loads kronk_config.yaml as the base, then deep-merges config.yaml on top.
    This allows config.yaml to only specify fields that differ from defaults.

    Both files live in the project root (next to src/), not in the working directory.
    """
    config_path = project_path(config_path)
    default_path = project_path(default_path)

    # Load default config (required)
    if not os.path.exists(default_path):
        print(f"Error: {default_path} not found.")
        exit(1)

    with open(default_path, "r") as f:
        config = yaml.safe_load(f)

    # Merge user config on top if it exists
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            user_config = yaml.safe_load(f) or {}
        config = deep_merge(config, user_config)
        print(f"Loaded config from {config_path} (with {default_path} defaults)")
    else:
        print(f"No {config_path} found, using defaults from {default_path}")

    # Template substitution for system prompt
    system_prompt = config.get("system_prompt", "")
    system_prompt = system_prompt.replace("{{github_url}}", config.get("github_url", ""))
    config["system_prompt"] = system_prompt

    return config


CONFIG = load_config()

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("KRONK_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")

# Load from config
MODEL = CONFIG.get("model", "qwen3.5:35b-a3b")
SYSTEM_PROMPT = CONFIG.get("system_prompt", "You are a helpful chatbot.")

# Max rounds of tool calls before forcing a final answer (prevents infinite loops)
MAX_TOOL_ROUNDS = CONFIG.get("max_tool_rounds", 5)
# Catch replies that claim an action ("Reminder set!") when no tool actually ran, and give the
# model one round to fix it. See claims.py; measured by evals/run_evals.py.
CLAIM_CHECK = CONFIG.get("claim_check", True)
# When the model writes a tool call out as text instead of calling it - set_nickname("Bucket")
# runs successfully! - perform the call for real rather than only correcting the claim. The
# intent and arguments are unambiguous, so this turns a failure into a success.
EXECUTE_TYPED_CALLS = CONFIG.get("execute_typed_calls", True)
MAX_TYPED_ROUNDS = 2  # bound: a model that keeps typing calls must not loop forever
# Whether the model does its (slow) internal "thinking" pass. Off by default: much faster,
# and stops reasoning traces from leaking into replies on reasoning models like qwen3.5.
MODEL_THINK = CONFIG.get("use_thinking", False)
# Placeholder shown while the model is thinking (only when MODEL_THINK); deleted once ready.
THINKING_MESSAGE = CONFIG.get("thinking_message", "🤔 Kronking…") #TODO(config): put in config
# Hard ceiling (seconds) on any single Ollama request. Prevents the bot from hanging silently
# forever if Ollama stalls (e.g. the model got evicted while the host slept). On timeout the
# request is cancelled and the user gets the timeout message instead of dead silence.
REQUEST_TIMEOUT = CONFIG.get("request_timeout", 180)
# Context window. Ollama's default is 4096 tokens, and the system prompt (~1800) plus the tool
# schemas (~1400 for 18 tools) already fill 78% of that before any conversation. Everything
# else - history, memory context, the reasoning pass, the answer itself - has to fit in what's
# left, and when it doesn't Ollama truncates from the FRONT: the system prompt goes first,
# taking the "you MUST call the tool, never act it out in prose" rules with it. The model then
# performs the tool instead of calling it, or emits reasoning with no room left for a reply.
NUM_CTX = CONFIG.get("num_ctx", 16384)
# --- Self-recovery watchdog ---------------------------------------------------------------
# A background OS thread checks that the asyncio event loop is still advancing. An async task
# bumps a heartbeat every few seconds; if it stops advancing for WATCHDOG_TIMEOUT seconds the
# loop is wedged (e.g. a blocking call froze it, or the gateway deadlocked), so the watchdog
# force-exits the process. The daemon (run-bot.sh / launchd KeepAlive) then starts a fresh one.
# Because it's a plain thread, it keeps running even when the event loop is fully blocked.
# The heartbeat is ALSO written to a file so the external daemon has an independent liveness
# signal (covers the rare case where even the watchdog thread is starved).
WATCHDOG_TIMEOUT = CONFIG.get("watchdog_timeout", 120)  # secs of stall before self-exit
WATCHDOG_INTERVAL = CONFIG.get("watchdog_interval", 15)  # how often the thread checks
HEARTBEAT_FILE = project_path(CONFIG.get("heartbeat_file", "./bot.heartbeat"))
_heartbeat_ts = time.time()  # last time the event loop proved it was alive
# Records the git commit the bot last started on, so on the next start we can tell whether the
# code actually changed (a deploy) versus a plain crash-recovery restart - and announce it.
VERSION_FILE = project_path(CONFIG.get("version_file", "./.bot_version"))
MESSAGE_HISTORY_LIMIT = CONFIG.get("message_history", {}).get("limit", 10)
MESSAGE_MAX_AGE_MINUTES = CONFIG.get("message_history", {}).get("max_age_minutes", 0)
USER_SUMMARY_CHANCE = CONFIG.get("memory", {}).get("user_summary_update_chance", 0.2)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Track recently processed messages to prevent double-replies
_processed_messages: set[int] = set()
_processed_messages_max = 50

# IDs of the bot's own tool-announcement messages ("Looking up ..."), so we can exclude them
# from fetched history - otherwise the model reads them back as its own prior turns and keeps
# "continuing" a half-started answer (a source of hallucination).
_announcement_message_ids: set[int] = set()
_announcement_ids_max = 200

# Per-channel locks so concurrent messages in the same channel are handled one at a time
# (prevents overlapping/duplicate replies and cross-contaminated context).
_channel_locks: dict[int, asyncio.Lock] = {}

# Guard so persisted reminders are only rescheduled once (on_ready can fire on reconnects)
_reminders_rescheduled = False


def _note_announcement_id(msg_id: int) -> None:
    """Remember an announcement message id so it's excluded from future history fetches."""
    _announcement_message_ids.add(msg_id)
    if len(_announcement_message_ids) > _announcement_ids_max:
        for old in sorted(_announcement_message_ids)[:_announcement_ids_max // 2]:
            _announcement_message_ids.discard(old)


async def _heartbeat_loop() -> None:
    """Bump the in-memory heartbeat (and touch the heartbeat file) while the loop is healthy.

    Runs as an asyncio task, so if the event loop is blocked this stops advancing - which is
    exactly the signal the watchdog thread and the external daemon watch for.
    """
    global _heartbeat_ts
    while True:
        _heartbeat_ts = time.time()
        try:
            HEARTBEAT_FILE.write_text(str(_heartbeat_ts))
        except Exception:
            pass  # a failed heartbeat write must never take the bot down
        await asyncio.sleep(5)


def _watchdog_thread() -> None:
    """Force-exit the process if the event loop stops advancing (self-recovery).

    Plain OS thread so it runs even when the loop is wedged. os._exit skips cleanup on purpose:
    the loop is presumed dead, so we bail hard and let the daemon restart a clean process.
    """
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        stalled = time.time() - _heartbeat_ts
        if stalled > WATCHDOG_TIMEOUT:
            print(
                f"[WATCHDOG] event loop unresponsive for {stalled:.0f}s "
                f"(> {WATCHDOG_TIMEOUT}s); force-exiting for a clean restart",
                file=sys.stderr, flush=True,
            )
            os._exit(1)


def _current_commit() -> str | None:
    """Return the short git SHA the bot is running from, or None if unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=PROJECT_ROOT,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _check_version_change() -> tuple[str | None, str | None]:
    """Return (current_sha, previous_sha) and persist the current one for the next start.

    previous_sha is what the bot ran on last time it started; if it differs from current, the
    code was updated between runs (a deploy). Reconnects re-read the now-persisted value, so
    the 'updated' announcement fires only once per actual change.
    """
    current = _current_commit()
    previous = None
    try:
        if VERSION_FILE.exists():
            previous = VERSION_FILE.read_text().strip() or None
    except Exception:
        previous = None
    if current:
        try:
            VERSION_FILE.write_text(current)
        except Exception:
            pass
    return current, previous


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    """Return the (shared) lock for a channel, creating it on first use."""
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock

# Post a short "looking this up…" line to the channel before running a lookup tool.
ANNOUNCE_TOOLS = CONFIG.get("announce_tools", True)

# Whether YouTube->voice playback (!play) is usable: set at startup by voice.configure(),
# which checks the config toggle plus yt-dlp / PyNaCl / ffmpeg being present.
VOICE_READY = False

do_memory = CONFIG.get("memory", {}).get("do_memory", False)
do_user_memory = CONFIG.get("memory", {}).get("user_memory", False)
do_conversation_memory = CONFIG.get("memory", {}).get("conversation_memory", False)
max_stored_conversations = CONFIG.get("memory", {}).get("max_stored_conversations", 500)


def process_message(msg: discord.Message, content_prefix: str="") -> dict[str, str]:
    """Converts a discord message into the format provided to the model."""
    return {
        "role": "assistant" if msg.author == msg.channel.guild.me else "user",
        "content": f"{msg.author.display_name}({msg.author.id})[{msg.created_at.strftime('%H:%M:%S')}]:{content_prefix} {msg.content}",
    }


async def fetch_channel_history(channel: discord.TextChannel, limit: int = MESSAGE_HISTORY_LIMIT) -> list[dict]:
    """Fetch the last N messages from the channel and convert to LLM message format."""
    # Calculate age cutoff if configured
    cutoff_time = None
    if MESSAGE_MAX_AGE_MINUTES > 0:
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=MESSAGE_MAX_AGE_MINUTES)

    messages = []
    async for msg in channel.history(limit=limit):
        if msg.id in _announcement_message_ids:
            continue  # Skip our own "Looking up ..." status messages (not real turns)
        if msg.author.bot and msg.author != channel.guild.me:
            continue  # Skip other bots, but include our own messages
        if not msg.content or not msg.content.strip():
            continue  # Skip empty messages (images, embeds, etc.)
        if cutoff_time and msg.created_at < cutoff_time:
            continue  # Skip messages older than max age
        messages.append(process_message(msg))
    messages.reverse()
    return messages


async def _model_chat(client: ollama.AsyncClient, **kwargs):
    """Call ollama chat with the configured `think` setting, bounded by REQUEST_TIMEOUT.

    Falls back to a plain call if the client/model doesn't accept `think`. Wrapping in
    asyncio.wait_for guarantees a hung Ollama call is cancelled instead of blocking forever;
    the resulting TimeoutError is handled upstream (user sees the timeout message).
    """
    # num_ctx must be set explicitly: Ollama defaults to 4096, which this prompt overflows.
    options = {"num_ctx": NUM_CTX, **kwargs.pop("options", {})}
    think = kwargs.pop("think", MODEL_THINK)  # callers can force the reasoning pass off

    async def _call():
        try:
            return await client.chat(think=think, options=options, **kwargs)
        except (TypeError, ollama.ResponseError) as e:
            print(f"[WARN] 'think' param not accepted ({e}); retrying without it")
            return await client.chat(options=options, **kwargs)

    return await asyncio.wait_for(_call(), timeout=REQUEST_TIMEOUT)


async def query_ollama(messages: list[dict], memory_context: str = None,
                       ctx: ToolContext = None) -> str:
    """Query the tool-calling model, running an agentic tool loop until a final answer.

    The single model natively decides when to call tools. On each round we execute any
    requested tools (via the tools registry), feed their results back as 'tool' messages,
    and let the model continue - so it can chain calls (e.g. search -> fetch -> answer) and
    see results before responding. Bounded by MAX_TOOL_ROUNDS to prevent runaway loops.

    `ctx` carries the Discord message/client so tools can act on the server; when None
    (e.g. offline tests) only context-free tools work.
    """
    ollama_client = ollama.AsyncClient(timeout=REQUEST_TIMEOUT)

    # Build system prompt with optional memory context
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n[Memory Context]\n{memory_context}\n"

    conversation = [{"role": "system", "content": system_content}] + list(messages)
    tool_schemas = tools.get_schemas()

    executed_tools: list[str] = []  # what actually ran, for the claim check below
    corrected = False               # the claim check gets exactly one shot per turn
    typed_rounds = 0                # times we rescued a call the model typed instead of made
    # Results of every distinct call made THIS TURN, keyed by (tool, args). Deduplication has
    # to span rounds, not just sit inside one: when a tool returns something unhelpful ("web
    # access isn't available") the model's instinct is to call it again, identically, until
    # MAX_TOOL_ROUNDS runs out - which produced an empty reply and the 🥒 failsafe in the evals.
    prior_results: dict[tuple[str, str], str] = {}

    for round_num in range(MAX_TOOL_ROUNDS):
        start_time = time.time()
        response = await _model_chat(
            ollama_client,
            model=MODEL,
            messages=conversation,
            tools=tool_schemas,
        )
        elapsed = time.time() - start_time
        print(f"[TIMING] Model ({MODEL}) round {round_num + 1}: {elapsed:.2f}s")

        tool_calls = response.message.tool_calls
        if not tool_calls:
            content = response.message.content or ""

            # The model wrote a call out as prose instead of making it. Both the tool and its
            # arguments are unambiguous there, so run it for real and feed the results back:
            # the loop then finishes the reply normally, the action has actually happened, and
            # the user never sees the raw call. Strictly better than a correction round, which
            # only asks the model to try again. No announcement is posted on this path - the
            # model has already written its own line about doing it.
            if EXECUTE_TYPED_CALLS and typed_rounds < MAX_TYPED_ROUNDS:
                typed = [
                    call for call in claims.extract_typed_calls(content)
                    if tools.is_enabled(call.name)
                    and (call.name, str(call.args)) not in prior_results
                ]
                if typed:
                    typed_rounds += 1
                    conversation.append(response.message)
                    for call in typed:
                        log.warning(
                            f"Model typed {call.matched!r} instead of calling it - "
                            f"executing {call.name}({call.args}) for real"
                        )
                        try:
                            result = await tools.execute(call.name, call.args,
                                                         ctx or ToolContext())
                            executed_tools.append(call.name)
                            if ctx is not None:
                                ctx.executed_tools.append(call.name)
                        except Exception as e:
                            log.warning(f"Rescued call {call.name} failed: {e}")
                            result = f"Tool error: {e}"
                        prior_results[(call.name, str(call.args))] = result
                        conversation.append({
                            "role": "tool",
                            "tool_name": call.name,
                            "content": (f"{result}\n(You wrote this call out as text rather "
                                        f"than calling it. It has now been run for you - "
                                        f"answer normally, and never write a call out again.)"),
                        })
                    continue

            # The model has decided it's finished. Before that answer goes to the user, check
            # it doesn't claim an action no tool performed ("Done!", "Reminder set!"). Prompt
            # instructions alone don't stop this - it's a sampling failure, not a
            # comprehension one - so it's caught here and sent back once for a fix.
            if CLAIM_CHECK:
                unbacked = claims.verify(content, executed_tools)
                if unbacked and not corrected:
                    claim = unbacked[0]
                    log.warning(
                        f"False action claim ({claim.kind}): said {claim.matched!r} but ran "
                        f"{executed_tools or 'no tools'} - forcing a correction round"
                    )
                    corrected = True
                    conversation.append(response.message)
                    conversation.append({"role": "system",
                                         "content": claims.correction_prompt(claim)})
                    continue
                if unbacked:
                    # Second offence: don't loop again (that's a latency hole), but this is
                    # the signal that the model/prompt needs work - surface it in the logs.
                    log.error(
                        f"Model repeated a false action claim after correction "
                        f"({unbacked[0].kind}): {unbacked[0].matched!r}"
                    )
            return content

        # Record the assistant's tool-call turn, then execute each requested tool.
        conversation.append(response.message)
        print(f"[DEBUG] Tool calls: {[t.function.name for t in tool_calls]}")

        for tool_call in tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments
            call_key = (name, str(args))

            # Repeat of a call already made this turn: hand back the SAME result with a note
            # rather than re-running it. Answering with a tool message (instead of silently
            # skipping) matters - a tool_call left without a matching result confuses the model
            # into trying again, which is the loop this is here to break.
            if call_key in prior_results:
                log.warning(f"Model repeated an identical {name} call; returning cached result")
                conversation.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": (f"You already called {name} with exactly these arguments this "
                                f"turn. The result was:\n{prior_results[call_key]}\n"
                                f"Do not call it again - answer the user with what you have, "
                                f"or tell them you could not find out."),
                })
                continue

            # Announce lookups so users know we're drawing from a real source.
            if ANNOUNCE_TOOLS and ctx is not None and ctx.message is not None:
                note = tools.announce(name, args, ctx)
                if note:
                    try:
                        sent = await ctx.message.channel.send(note)
                        _note_announcement_id(sent.id)  # keep it out of future history
                    except Exception as e:
                        log.warning(f"Could not send tool announcement: {e}")

            try:
                result = await tools.execute(name, args, ctx or ToolContext())
                executed_tools.append(name)
                if ctx is not None:
                    ctx.executed_tools.append(name)
                print(f"[DEBUG] Tool {name} returned {len(result)} chars")
            except Exception as e:
                log.warning(f"Tool {name} failed: {e}")
                result = f"Tool error: {e}"

            prior_results[call_key] = result
            conversation.append({
                "role": "tool",
                "tool_name": name,
                "content": result,
            })

    # Exhausted tool rounds - force a final answer with tools disabled. `think=False` even when
    # thinking is on globally: this call only has to summarise what the tools already returned,
    # and a reasoning pass here produced a reply that was ENTIRELY reasoning, leaving empty
    # content once it was stripped - the user then got the "not sure what to say" failsafe after
    # five successful tool calls.
    log.warning(f"Hit MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}); forcing final answer")
    response = await _model_chat(ollama_client, model=MODEL, messages=conversation, think=False)
    content = response.message.content or ""
    if not strip_thinking(content).strip():
        log.error("Forced final answer was empty; asking once more for a plain summary")
        conversation.append({
            "role": "system",
            "content": ("Answer the user now, in plain text, using the tool results above. "
                        "Do not call any more tools. If the tools did not give you what you "
                        "needed, just say so in your own words."),
        })
        response = await _model_chat(ollama_client, model=MODEL, messages=conversation,
                                     think=False)
        content = response.message.content or ""
    return content


@client.event
async def on_ready():
    global SYSTEM_PROMPT
    # Replace Discord-specific placeholders now that we have bot info
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{discord_display_name}}", client.user.display_name)
    # Let the claim detector recognise the bot talking about ITSELF in the third person
    # ("@Kronk's new name is ..."), which otherwise reads as a claim about somebody else.
    claims.configure_self_names(client.user.display_name, client.user.name)
    # So a call typed as prose - set_nickname("Bucket") runs successfully! - is recognised as
    # the fabrication it is. Full registry, not just the enabled tools.
    # The mapping (not just names) so a typed call's positional arguments can be resolved.
    claims.configure_tool_names(tools.arg_names_map())
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{discord_user_id}}", str(client.user.id))
    default_timezone = CONFIG.get('default_timezone', 'Europe/London')
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{time_zone}}", default_timezone)
    datetime_now = datetime.now(ZoneInfo(default_timezone)).strftime('%a %d %b %Y, %I:%M%p')
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{date_time}}", datetime_now)
    print(f"Logged in as {client.user}")

    # Set bot status based on enabled features
    status_config = CONFIG.get("status", {})
    status_parts = []
    if do_memory:
        status_parts.append(status_config.get("memory_enabled", "Memory on"))
    if tools.is_enabled("web_search"):
        status_parts.append(status_config.get("websearch_enabled", "Web search on"))
    if VOICE_READY:
        status_parts.append(status_config.get("voice_enabled", "DJ Kronk"))

    if status_parts:
        status_text = " | ".join(status_parts)
        await client.change_presence(activity=discord.Game(name=status_text))
        print(f"Status set: {status_text}")

    # Restore reminders that were pending before the last shutdown (once, not per reconnect).
    global _reminders_rescheduled
    if not _reminders_rescheduled:
        _reminders_rescheduled = True
        restored = await tools.reschedule_reminders(client)
        if restored:
            print(f"Restored {restored} pending reminder(s)")

        # Start the Discord log-channel flusher (once).
        client.loop.create_task(dlog.run_flusher(client))

        # Start the heartbeat task the watchdog/daemon watch for liveness (once).
        client.loop.create_task(_heartbeat_loop())

    # Announce we're up (only reaches channels where a log channel has been set). If the code
    # changed since the last start, say "updated" instead of the plain "online" line.
    base = " | ".join(status_parts) or "ready"
    current_sha, previous_sha = _check_version_change()
    if current_sha and previous_sha is None:
        # First run since version tracking was added - mark it as the baseline deployment.
        announce = f"🎉 Kronk deployed with self-recovery! Baseline `{current_sha}` — {base}"
    elif current_sha and previous_sha and current_sha != previous_sha:
        announce = f"🔄 Kronk updated! Now on `{current_sha}` (was `{previous_sha}`) — {base}"
    else:
        suffix = f" `{current_sha}`" if current_sha else ""
        announce = f"✅ Kronk online as {client.user} — {base}{suffix}"
    await dlog.notify(client, announce)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState,
                                after: discord.VoiceState):
    """Let the voice player leave when its channel empties out (or when we get kicked)."""
    try:
        await voice.handle_voice_state_update(client, member, before, after)
    except Exception as e:
        log.warning(f"Voice state update handling failed: {e}")


@client.event
async def on_error(event_method: str, *args, **kwargs):
    """Global handler for uncaught exceptions in event handlers - log with full traceback."""
    log.exception(f"Unhandled error in {event_method}")


def format_queued_songs(ctx: ToolContext) -> str:
    """Render the trailing "now playing" line(s) for anything the queue_song tool queued.

    A bare URL (not a markdown masked link) is deliberate: Discord only renders `[text](url)`
    inside embeds, so in a normal message a plain URL is what actually gives you a clickable
    link and a video preview card.
    """
    if not ctx or not ctx.queued_songs:
        return ""
    return "\n".join(f"\U0001F3B6 Now playing: {title} {url}" for title, url in ctx.queued_songs)


async def _formulate_and_reply(message: discord.Message, ref_msg):
    """Reply to `message`, guaranteeing that any tool-queued track gets announced.

    A track queued by the queue_song tool is announced ONLY in the model's reply - voice.py's
    player loop deliberately stays silent for it so the link isn't posted twice. That means if
    the reply never happens (Ollama timeout, API error, empty response, an unexpected crash),
    the music would start with nothing said at all. This wrapper closes that hole.
    """
    ctx = ToolContext(message=message, client=client, model=MODEL)
    try:
        await _generate_reply(message, ref_msg, ctx)
    finally:
        if ctx.queued_songs and not ctx.music_announced:
            log.warning("Reply never went out, but music was queued - announcing it directly")
            try:
                await message.reply(format_queued_songs(ctx))
            except Exception as e:
                log.error(f"Could not announce the queued track: {e}")


async def _generate_reply(message: discord.Message, ref_msg, ctx: ToolContext):
    """Fetch context, query the model (with tools), and send Kronk's reply.

    Runs under a per-channel lock (see on_message) so concurrent messages in the same channel
    are handled one at a time - the next message only starts once this reply is on screen, so
    it sees a clean, complete transcript instead of a half-finished one.
    """
    overall_start = time.time()
    async with message.channel.typing():
        # Fetch last messages from this channel
        messages = await fetch_channel_history(message.channel, limit=MESSAGE_HISTORY_LIMIT)

        # If replying to a message not in recent history, add it as context
        if ref_msg is not None:
            # Insert before the last message so model responds to the user
            messages.insert(-1, process_message(message, content_prefix="[Referenced message]"))

        # Build memory context for this user/channel (must happen before query)
        memory_context = None
        if do_memory:
            # Runs a (blocking) embedding call, so offload to a thread - otherwise a slow/hung
            # Ollama embed would freeze the whole event loop (gateway heartbeat included).
            try:
                memory_context = await asyncio.to_thread(
                    memory.build_memory_context,
                    user_id=str(message.author.id),
                    current_message=message.content,
                    channel_id=str(message.channel.id),
                    do_user_memory=do_user_memory,
                    do_conversation_memory=do_conversation_memory,
                )
            except Exception as mem_err:
                log.warning(f"Failed to build memory context: {mem_err}")

        # Show a "Kronking..." placeholder while the (slower) thinking pass runs, so a long
        # delay isn't dead air. Removed once the real reply is ready.
        thinking_msg = None
        if MODEL_THINK and THINKING_MESSAGE:
            try:
                thinking_msg = await message.channel.send(THINKING_MESSAGE)
            except Exception:
                thinking_msg = None

        # Query the LLM (ctx lets tools act on the server: react, set status, etc.)
        try:
            response = await query_ollama(messages, memory_context=memory_context, ctx=ctx)
        except TimeoutError:
            await _safe_delete(thinking_msg)
            log.warning(f"Ollama request timed out (>{REQUEST_TIMEOUT}s) for message {message.id}")
            await message.reply("The request timed out. Please try again. 🥒")
            return
        except ollama.ResponseError as e:
            await _safe_delete(thinking_msg)
            log.error(f"Ollama error: {e}")
            await message.reply(f"Error communicating with Ollama: {e} 🥒")
            return
        except Exception as e:
            await _safe_delete(thinking_msg)
            log.exception(f"Unexpected error while replying: {e}")
            await message.reply(f"<Weird Error>  🥒")
            return

    # Real reply is ready - clear the "Kronking..." placeholder.
    await _safe_delete(thinking_msg)

    # Failsafe for empty response
    if not response:
        log.warning(f"Model generated empty response - user message: {message.content!r}")
        await message.reply("I'm not sure what to say to that. 🥒")
        return

    # Strip any leaked reasoning, then the input-format prefix if the model mimics it
    response = strip_thinking(response)
    response = strip_message_prefix(response)

    # If a tool queued music this turn, append the track link ourselves. Done in code, not by
    # asking the model to include it: "always" has to mean always, and a model told to end with
    # an exact line will drop it, paraphrase it, or hallucinate a URL often enough to matter.
    music_line = format_queued_songs(ctx)

    # Send the reply first
    if "<ignore>" not in response:
        body = f"{response.rstrip()}\n\n{music_line}" if music_line else response
        if len(body) <= 2000:
            await message.reply(body)
        else:
            chunks = [body[i : i + 2000] for i in range(0, len(body), 2000)]
            for chunk in chunks:
                await message.reply(chunk)
        ctx.music_announced = bool(music_line)
    elif music_line:
        # The model chose to stay quiet, but it DID put music on - that still needs saying.
        await message.reply(music_line)
        ctx.music_announced = True

    overall_elapsed = time.time() - overall_start
    print(f"[TIMING] Overall response: {overall_elapsed:.2f}s")

    # Spontaneous expression: react, remember, change status/nickname/About Me on his own
    # initiative. Detached deliberately - the reply is already on screen, so this costs the
    # user no latency, and a reaction landing a moment later is how a person does it anyway.
    # It gets the tools that already ran so it never repeats what the reply just did.
    asyncio.create_task(expression.express(
        message,
        user_text=message.content,
        reply_text=response,
        ctx=ctx,
        already_done=set(ctx.executed_tools),
    ))

    # Process memory after responding (non-blocking for user experience)
    if do_memory:
        # Store this conversation for future recall. Offloaded to a thread (it runs a blocking
        # embedding call) so it can't freeze the event loop.
        if do_conversation_memory:
            try:
                await asyncio.to_thread(
                    memory.store_conversation,
                    channel_id=str(message.channel.id),
                    messages=messages,
                    max_conversations=max_stored_conversations,
                )
            except Exception as mem_err:
                log.warning(f"Failed to store conversation: {mem_err}")

        # Occasionally update user summary (bounded so a stalled Ollama can't hang here either)
        # TODO: we could use some basic language analysis to determine if the user said anything important
        if do_user_memory and random.random() < USER_SUMMARY_CHANCE:
            try:
                await asyncio.wait_for(
                    memory.generate_user_summary(
                        user_id=str(message.author.id),
                        user_name=message.author.display_name,
                        recent_messages=messages,
                        model=MODEL,
                    ),
                    timeout=REQUEST_TIMEOUT,
                )
            except Exception as sum_err:
                log.warning(f"Failed to generate user summary: {sum_err}")


# === !help ===================================================================================
# Each feature module owns its own COMMAND_HELP list (next to the regex that implements those
# commands, so they can't drift); this just renders them. Adding a command means adding one
# entry to its module's list, not touching this function.
# Anchored at the end (like the /setlogchannel commands) so "!help me pick a song" reaches the
# LLM as a normal question instead of being swallowed and answered with a command list.
_HELP_RE = re.compile(r"^!help\s*$", re.IGNORECASE)


def _format_help_section(title: str, entries: list[tuple[str, str]], note: str = "") -> list[str]:
    lines = [f"**{title}**" + (f" — {note}" if note else "")]
    lines += [f"`{usage}` — {description}" for usage, description in entries]
    return lines + [""]


async def handle_help_command(message: discord.Message) -> bool:
    """Answer !help with the full command list. Returns True if it consumed the message.

    Voice commands are listed even when playback is unavailable, with the reason attached -
    saying nothing at all would just look like the feature doesn't exist.
    """
    if not _HELP_RE.match((message.content or "").strip()):
        return False

    lines = [f"**Here's what I can do** 🥒", ""]
    lines += _format_help_section("Music", voice.COMMAND_HELP,
                                  note="" if VOICE_READY else f"unavailable: {voice.unavailable_reason()}")
    lines += _format_help_section("Server admin", dlog.COMMAND_HELP,
                                  note="needs the Manage Server permission")
    lines += _format_help_section("Chat", [
        (f"@{client.user.display_name} <message>", "Talk to me - I reply to mentions"),
        ("(reply to me)", "Reply to any of my messages to keep the conversation going"),
        ("!help", "Show this list"),
    ])
    await message.reply("\n".join(lines).strip()[:2000])
    return True


@client.event
async def on_message(message: discord.Message):
    global _processed_messages

    if message.is_system():
        return  # System message
    if message.author == client.user:
        return  # Message from this bot

    # Deduplicate: skip if we've already processed this message
    if message.id in _processed_messages:
        print(f"[WARN] Duplicate message event detected (id={message.id}), skipping")
        return
    _processed_messages.add(message.id)
    # Keep set bounded
    if len(_processed_messages) > _processed_messages_max:
        # Remove oldest entries (set is unordered, but message IDs are snowflakes so roughly ordered)
        oldest = sorted(_processed_messages)[:_processed_messages_max // 2]
        _processed_messages -= set(oldest)

    # Handle our own text commands (!help, /setlogchannel, !play) before any chat/LLM handling.
    if await handle_help_command(message):
        return
    if await dlog.handle_command(message):
        return
    if await voice.handle_command(message):
        return

    # fetch the referenced message if it exists:
    ref_msg = None
    if message.reference and message.reference.message_id:
        ref_msg = await message.channel.fetch_message(message.reference.message_id)

    # Only respond if mentioned or is responding to its message
    is_mentioned = str(client.user.id) in message.content
    is_reply = ref_msg is not None and ref_msg.author == client.user
    if not is_mentioned and not is_reply:
        return

    # Serialize handling per channel so overlapping messages (common when several people
    # talk to Kronk at once) don't produce duplicate replies or cross-contaminated context.
    async with _get_channel_lock(message.channel.id):
        await _formulate_and_reply(message, ref_msg)


def kill_other_instances() -> None:
    """Newest start wins: terminate any OTHER bot process running from this venv before we
    connect to Discord.

    This keeps the daemon's 'restart on sleep' reliable - a fresh instance always takes over,
    and a stale/frozen one can never block it. It also prevents duplicate replies. Critically,
    we NEVER exit this process here: unlike the old passive flock guard (which made a fresh
    instance exit when a frozen process still held the lock - suppressing the bot entirely),
    this only ever kills OTHERS, so the current process always proceeds. Worst case (scan
    fails) we just carry on and might briefly duplicate.

    Matches other processes whose command line contains this bot's project directory AND
    main.py (the venv python is invoked as `<project_root>/.venv/bin/python3 src/main.py`),
    which is exactly the sibling bot processes - not the `uv run` wrapper or bots in other
    directories. Anchoring on the project root rather than this file's directory matters:
    the interpreter path and the script path are separated in the command line, so `src` never
    appears immediately before `main.py`.
    """
    my_pid = os.getpid()
    pattern = f"{PROJECT_ROOT}.*main.py"
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        print(f"[WARN] could not scan for other instances (continuing anyway): {e}")
        return

    others = [int(p) for p in out.stdout.split() if p.strip().isdigit() and int(p) != my_pid]
    for pid in others:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Terminating older bot instance (pid {pid})")
        except (ProcessLookupError, PermissionError) as e:
            print(f"[WARN] Could not kill other process: {e}")
    if others:
        time.sleep(2)
        for pid in others:  # force any that ignored SIGTERM (e.g. a frozen process)
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError) as e:
                print(f"[WARN] Could not kill other process: {e}")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN (or KRONK_TOKEN) environment variable is not set")

    # Set up logging: prints to terminal AND (once a channel is set) forwards WARNING+ to Discord.
    dlog.init(str(project_path(CONFIG.get("log_channels_file", "./log_channels.json"))))

    # Take over from any older/stale instance (never blocks us from starting).
    kill_other_instances()

    # Start the self-recovery watchdog: if the event loop ever wedges, force-exit so the
    # daemon restarts a fresh process. Daemon thread so it never blocks interpreter shutdown.
    _heartbeat_ts = time.time()
    threading.Thread(target=_watchdog_thread, name="watchdog", daemon=True).start()

    #======
    # Init
    #======
    if do_memory:
        memory.init_memory(str(project_path(CONFIG.get("memory_dir", "./bot_memory"))))

    # Voice playback (!play). Reports why it's off (missing ffmpeg/yt-dlp/PyNaCl/davey) rather
    # than failing at command time; the commands themselves re-check and explain in chat.
    # MUST run before tools.configure(): the queue_song tool is gated on VOICE_READY.
    _voice_cfg = dict(CONFIG.get("voice", {}))
    if _voice_cfg.get("cookies_file"):
        _voice_cfg["cookies_file"] = str(project_path(_voice_cfg["cookies_file"]))
    VOICE_READY, voice_status = voice.configure(_voice_cfg)
    print(voice_status)

    # Configure the tool registry based on config + available capabilities.
    enabled_tools = tools.configure(
        CONFIG.get("tools", {}),
        has_api_key=bool(OLLAMA_API_KEY),
        memory_available=do_memory,
        voice_available=VOICE_READY,
    )
    # Load any persona-specific announcement templates (fall back to built-in defaults).
    tools.configure_announcements(CONFIG.get("tool_announcements"))

    # Spontaneous expression runs AFTER a reply is sent, so it must be configured after the
    # tool registry: every action it can take is gated on that tool being enabled.
    expressive = expression.configure(CONFIG.get("expression"), MODEL)
    print(f"Spontaneous expression: {', '.join(expressive) if expressive else 'off'}")

    # Point the persistent reminder store at its file (reminders are rescheduled in on_ready).
    tools.init_reminders(str(project_path(CONFIG.get("reminders_file", "./bot_reminders.json"))))
    if enabled_tools:
        print(f"Tools enabled ({len(enabled_tools)}): {', '.join(enabled_tools)}")
    else:
        print("No tools enabled.")
    _tools_cfg = CONFIG.get("tools", {})
    if (_tools_cfg.get("web_search") or _tools_cfg.get("web_fetch")) and not OLLAMA_API_KEY:
        print("Note: web tools requested but disabled - OLLAMA_API_KEY not set")

    # log_handler=None: we manage logging ourselves (dlog.init), so discord.py shouldn't add
    # its own duplicate terminal handler. Its logs still reach our root StreamHandler.
    client.run(DISCORD_TOKEN, log_handler=None)

"""
Tool registry for the Discord bot's native tool-calling model.

Each tool is a `Tool` (schema + async handler + config gating). `configure()` reads the
`tools:` section of config to decide which tools are offered; `get_schemas()` returns the
enabled tool schemas to pass to the model, and `execute()` dispatches a tool call.

Handlers receive `(args: dict, ctx: ToolContext)` and return a short string result that is
fed back to the model as a `role: "tool"` message. Adding a tool = write a handler and add
one `Tool(...)` entry to `_REGISTRY`.
"""

import ast
import asyncio
import json
import logging
import operator
import os
import random
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
import ollama

import memory
import voice

log = logging.getLogger("kronk")


@dataclass
class ToolContext:
    """Runtime context handed to tool handlers."""
    message: Optional[discord.Message] = None
    client: Optional[discord.Client] = None
    model: Optional[str] = None
    # Songs queued by tools during this turn, as (title, url). main.py reads this AFTER the
    # model has written its reply and appends a link line, so the URL is guaranteed to be there
    # rather than depending on the model remembering to include it.
    queued_songs: list[tuple[str, str]] = field(default_factory=list)
    # Set once the reply carrying the track line has actually been sent, so the caller knows
    # whether it still needs to announce the music itself.
    music_announced: bool = False
    # Tools that actually ran this turn. Used by the claim check, and by the expression pass so
    # it never offers an action the model already took while replying.
    executed_tools: list[str] = field(default_factory=list)


@dataclass
class Tool:
    name: str
    schema: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    default_enabled: bool = True
    needs_api_key: bool = False   # web tools: require OLLAMA_API_KEY
    needs_memory: bool = False    # memory tools: require do_memory
    needs_discord: bool = False   # need a live message/client context
    needs_voice: bool = False     # voice tools: require working audio playback


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    """Helper to build an ollama/OpenAI-style function tool schema."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


# ======================================================================================
# Web tools (require OLLAMA_API_KEY; the search/fetch runs on Ollama's cloud)
# ======================================================================================

# Hard ceiling (seconds) on any single blocking Ollama-cloud web call. These use the SYNC
# ollama client, so they MUST be offloaded to a thread and bounded - otherwise a stalled cloud
# request blocks the whole asyncio event loop (Discord heartbeat included) and the bot wedges.
WEB_TIMEOUT = 60


def _no_web_key() -> Optional[str]:
    """Return a friendly message if the Ollama cloud key is missing (web tools can't run).

    The tools are normally gated off entirely without a key, but this is a belt-and-braces
    guard so a mis-config (tool enabled, key absent) degrades to a clear message instead of a
    raw error or a stall. Safe to leave the tool 'on' in config for when a key is added later.
    """
    if not os.environ.get("OLLAMA_API_KEY"):
        return "Web access isn't available right now (no Ollama API key configured)."
    return None


async def _web_search(args: dict, ctx: ToolContext) -> str:
    if (msg := _no_web_key()):
        return msg
    query = args["query"]
    print(f"[TOOL] web_search: {query}")
    # ollama.web_search is synchronous + blocking: run it off the event loop, with a timeout.
    result = await asyncio.wait_for(
        asyncio.to_thread(ollama.web_search, query), timeout=WEB_TIMEOUT
    )
    return f"Web search results for '{query}':\n{result}"


async def _web_fetch(args: dict, ctx: ToolContext) -> str:
    if (msg := _no_web_key()):
        return msg
    url = args["url"]
    if not url.startswith(("http://", "https://")):
        print(f"[TOOL] web_fetch: invalid URL '{url}' (skipped)")
        return f"Invalid URL: {url} (must start with http:// or https://)"
    print(f"[TOOL] web_fetch: {url}")
    # ollama.web_fetch is synchronous + blocking: run it off the event loop, with a timeout.
    result = await asyncio.wait_for(
        asyncio.to_thread(ollama.web_fetch, url), timeout=WEB_TIMEOUT
    )
    return f"Contents of {url}:\n{result}"


# ======================================================================================
# Fun / social tools
# ======================================================================================

async def _roll_dice(args: dict, ctx: ToolContext) -> str:
    notation = str(args.get("notation", "1d20")).lower().strip()
    # Accept "NdS" or a bare number (treated as 1dN).
    if "d" in notation:
        count_str, _, sides_str = notation.partition("d")
        count = int(count_str) if count_str else 1
        sides = int(sides_str)
    else:
        count, sides = 1, int(notation)
    if not (1 <= count <= 100) or not (2 <= sides <= 1000):
        return "Invalid dice: use 1-100 dice with 2-1000 sides (e.g. '2d6')."
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)
    if count == 1:
        return f"Rolled {notation}: {total}"
    return f"Rolled {notation}: {rolls} (total {total})"


async def _flip_coin(args: dict, ctx: ToolContext) -> str:
    return f"Coin flip: {random.choice(['Heads', 'Tails'])}"


async def _random_choice(args: dict, ctx: ToolContext) -> str:
    options = args.get("options", [])
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    if not options:
        return "No options provided to choose from."
    return f"Random pick: {random.choice(options)}"


# --- Reminders (persisted to disk so they survive restarts) ---
# Each reminder is a record: {id, channel_id, user_id, text, fire_at (unix seconds)}.
# On creation it's appended to the JSON store and an asyncio task is scheduled; when it fires
# (or the send fails) it removes itself from the store. On startup, reschedule_reminders()
# reloads pending ones - anything already overdue fires immediately.
_reminder_tasks: set[asyncio.Task] = set()
_REMINDERS_FILE: Optional[Path] = None
_reminders_lock = asyncio.Lock()
MAX_REMINDER_MINUTES = 60 * 24 * 7  # 7 days


def init_reminders(path: str) -> None:
    """Point the reminder store at a JSON file (called once at startup)."""
    global _REMINDERS_FILE
    _REMINDERS_FILE = Path(path)


def _load_reminders() -> list[dict]:
    if _REMINDERS_FILE and _REMINDERS_FILE.exists():
        try:
            return json.loads(_REMINDERS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not read reminders file: {e}")
    return []


def _save_reminders(items: list[dict]) -> None:
    if _REMINDERS_FILE:
        try:
            _REMINDERS_FILE.write_text(json.dumps(items, indent=2))
        except OSError as e:
            log.warning(f"Could not write reminders file: {e}")


async def _persist_add(record: dict) -> None:
    async with _reminders_lock:
        items = _load_reminders()
        items.append(record)
        _save_reminders(items)


async def _persist_remove(reminder_id: str) -> None:
    async with _reminders_lock:
        items = [r for r in _load_reminders() if r.get("id") != reminder_id]
        _save_reminders(items)


async def _run_reminder(client: discord.Client, record: dict) -> None:
    """Sleep until fire_at (or fire now if overdue), send the ping, then drop from the store."""
    delay = record["fire_at"] - time.time()
    if delay > 0:
        await asyncio.sleep(delay)
    try:
        channel = client.get_channel(int(record["channel_id"]))
        if channel is None:
            channel = await client.fetch_channel(int(record["channel_id"]))
        await channel.send(f"<@{record['user_id']}> ⏰ Reminder: {record['text']}")
    except Exception as e:
        log.warning(f"Reminder {record.get('id')} failed to fire: {e}")
    finally:
        await _persist_remove(record["id"])


def _schedule_reminder(client: discord.Client, record: dict) -> None:
    task = asyncio.create_task(_run_reminder(client, record))
    _reminder_tasks.add(task)
    task.add_done_callback(_reminder_tasks.discard)


async def reschedule_reminders(client: discord.Client) -> int:
    """Reload persisted reminders on startup and schedule them. Returns how many were restored."""
    items = _load_reminders()
    count = 0
    for record in items:
        if "fire_at" not in record or "channel_id" not in record:
            continue
        _schedule_reminder(client, record)
        count += 1
    return count


async def _set_reminder(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None or ctx.client is None:
        return "Reminders need an active channel context."
    try:
        minutes = float(args["minutes"])
    except (TypeError, ValueError):
        return "Provide the reminder time in minutes as a number."
    text = str(args.get("text", "reminder"))
    if not (0 < minutes <= MAX_REMINDER_MINUTES):
        return "Reminder time must be between 0 and 10080 minutes (7 days)."

    record = {
        "id": uuid.uuid4().hex,
        "channel_id": str(ctx.message.channel.id),
        "user_id": str(ctx.message.author.id),
        "text": text,
        "fire_at": time.time() + minutes * 60,
    }
    await _persist_add(record)
    _schedule_reminder(ctx.client, record)
    return f"Reminder set for {minutes:g} minute(s) from now: '{text}'"


async def _queue_song(args: dict, ctx: ToolContext) -> str:
    """Queue a song in the voice channel the requester is sitting in.

    Delegates to voice.enqueue_request, the same path `!play` uses, so the model can't bypass
    the duration limit, queue cap or permission checks. The user must already be in a voice
    channel - the bot never picks one on its own.
    """
    if ctx.message is None:
        return "Queueing music needs an active channel context."
    query = str(args.get("query", "")).strip()
    if not query:
        return "No song was given, so nothing was queued."
    print(f"[TOOL] queue_song: {query}")
    result = await voice.enqueue_request(ctx.message, query, source="tool")
    if not result.ok:
        return f"Could not queue it: {result.message}"
    if result.url:
        ctx.queued_songs.append((result.title, result.url))
    # The link is appended to the reply by main.py, so tell the model not to invent its own.
    return (f"{result.message} (the track link is appended to your reply automatically - "
            f"do not repeat the URL yourself)")


# ======================================================================================
# Knowledge / utility tools
# ======================================================================================

_SAFE_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node):
    """Recursively evaluate an arithmetic AST with only whitelisted operators."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numbers allowed")
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


async def _calculator(args: dict, ctx: ToolContext) -> str:
    expression = str(args["expression"])
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval(tree.body)
        return f"{expression} = {result}"
    except Exception:
        return f"Could not evaluate '{expression}' (only basic arithmetic is supported)."


async def _get_time(args: dict, ctx: ToolContext) -> str:
    tz_name = str(args.get("timezone", "UTC"))
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return f"Unknown timezone '{tz_name}'. Use an IANA name like 'Europe/London' or 'UTC'."
    now = datetime.now(tz)
    return f"Current time in {tz_name}: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"


def _wiki_lookup(query: str) -> str:
    """Blocking Wikipedia REST lookup (run via asyncio.to_thread). No API key needed."""
    # 1) Resolve the query to a page title via opensearch.
    search_url = (
        "https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&format=json&search="
        + urllib.parse.quote(query)
    )
    req = urllib.request.Request(search_url, headers={"User-Agent": "llm-discord-bot/0.2"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode())
    titles = data[1] if len(data) > 1 else []
    if not titles:
        return f"No Wikipedia article found for '{query}'."
    title = titles[0]
    # 2) Fetch the page summary.
    summary_url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title)
    req = urllib.request.Request(summary_url, headers={"User-Agent": "llm-discord-bot/0.2"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        summary = json.loads(resp.read().decode())
    extract = summary.get("extract", "").strip()
    if not extract:
        return f"Found '{title}' but it has no summary."
    return f"Wikipedia — {title}:\n{extract}"


async def _wikipedia(args: dict, ctx: ToolContext) -> str:
    query = str(args["query"])
    print(f"[TOOL] wikipedia: {query}")
    try:
        return await asyncio.to_thread(_wiki_lookup, query)
    except Exception as e:
        return f"Wikipedia lookup failed: {e}"


# ======================================================================================
# Embodiment / presence tools (Discord-native; need a live message/client)
# ======================================================================================

async def _add_reaction(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None:
        return "No message to react to."
    emoji = str(args["emoji"])
    try:
        await ctx.message.add_reaction(emoji)
        return f"Reacted with {emoji}."
    except Exception as e:
        return f"Could not react with '{emoji}': {e}"


# The verb Discord renders in front of a status. "custom" renders the text alone, with no
# prefix at all, which is the only way to say something that isn't shaped like an activity.
_ACTIVITY_TYPES = {
    "playing": (discord.ActivityType.playing, "Playing"),
    "watching": (discord.ActivityType.watching, "Watching"),
    "listening": (discord.ActivityType.listening, "Listening to"),
    "competing": (discord.ActivityType.competing, "Competing in"),
    "custom": (discord.ActivityType.custom, ""),
}


async def _set_status(args: dict, ctx: ToolContext) -> str:
    if ctx.client is None:
        return "No client available to set status."
    text = str(args["text"])[:128]
    kind = str(args.get("activity", "playing")).strip().lower()
    if kind not in _ACTIVITY_TYPES:
        kind = "playing"  # an unknown verb is not worth failing the whole action over
    activity_type, verb = _ACTIVITY_TYPES[kind]
    try:
        if kind == "custom":
            activity = discord.CustomActivity(name=text)
        else:
            activity = discord.Activity(type=activity_type, name=text)
        await ctx.client.change_presence(activity=activity)
        # verb is empty for "custom" (the text shows with no prefix), so join rather than
        # interpolate - otherwise the result carries a double space.
        return "Status set to: " + " ".join(part for part in (verb, text) if part)
    except Exception as e:
        return f"Could not set status: {e}"


async def _set_nickname(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None or ctx.message.guild is None:
        return "Can only change nickname inside a server."
    nickname = str(args["nickname"])[:32]
    try:
        await ctx.message.guild.me.edit(nick=nickname)
        return f"Nickname changed to: {nickname}"
    except discord.Forbidden:
        return "Missing permission to change my nickname."
    except Exception as e:
        return f"Could not change nickname: {e}"


async def _set_about(args: dict, ctx: ToolContext) -> str:
    """Rewrite the bot's own "About Me" blurb on its Discord profile.

    Unlike nickname (per-server) and status (transient), this is global and sticks until
    changed again, so it's the most permanent thing the bot can say about itself.
    PATCH /applications/@me is allowed for a bot editing its OWN application.
    """
    if ctx.client is None:
        return "No client available to set my About Me."
    text = str(args["text"])[:400]
    try:
        app = ctx.client.application
        if app is None:
            app = await ctx.client.application_info()
        await app.edit(description=text)
        return f"About Me set to: {text}"
    except discord.Forbidden:
        return "Not allowed to edit my own About Me."
    except Exception as e:
        return f"Could not set About Me: {e}"


async def _get_user_info(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None or ctx.message.guild is None:
        return "User info is only available inside a server."
    try:
        user_id = int(args["user_id"])
    except (ValueError, KeyError):
        return "Provide a numeric user_id (the number in the message prefix)."
    member = ctx.message.guild.get_member(user_id)
    if member is None:
        return f"No member with id {user_id} found in this server."
    roles = [r.name for r in member.roles if r.name != "@everyone"]
    joined = member.joined_at.strftime("%Y-%m-%d") if member.joined_at else "unknown"
    created = member.created_at.strftime("%Y-%m-%d")
    return (
        f"{member.display_name} (id {user_id}): "
        f"joined server {joined}, account created {created}, "
        f"roles: {', '.join(roles) if roles else 'none'}, status: {member.status}"
    )


async def _create_poll(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None:
        return "Polls need an active channel context."
    question = str(args["question"])
    options = args.get("options", [])
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",") if o.strip()]
    if not (2 <= len(options) <= 10):
        return "A poll needs between 2 and 10 options."
    try:
        poll = discord.Poll(question=question, duration=timedelta(hours=24))
        for opt in options:
            poll.add_answer(text=str(opt)[:55])
        await ctx.message.channel.send(poll=poll)
        return f"Poll created: {question}"
    except AttributeError:
        return "This discord.py version doesn't support native polls (needs 2.4+)."
    except Exception as e:
        return f"Could not create poll: {e}"


async def _start_thread(args: dict, ctx: ToolContext) -> str:
    if ctx.message is None:
        return "Threads need an active message context."
    name = str(args["name"])[:100]
    try:
        await ctx.message.create_thread(name=name)
        return f"Thread created: {name}"
    except Exception as e:
        return f"Could not create thread: {e}"


# ======================================================================================
# Memory tools (require do_memory)
# ======================================================================================

async def _remember_fact(args: dict, ctx: ToolContext) -> str:
    user_id = str(args["user_id"])
    fact = str(args["fact"]).strip()
    if not fact:
        return "No fact provided to remember."
    existing = memory.get_user_summary(user_id)
    updated = f"{existing}\n- {fact}" if existing else f"- {fact}"
    memory.update_user_summary(user_id, updated)
    return f"Noted about user {user_id}: {fact}"


async def _recall(args: dict, ctx: ToolContext) -> str:
    query = str(args["query"])
    channel_id = str(ctx.message.channel.id) if ctx.message else None
    try:
        results = await asyncio.to_thread(
            memory.recall_relevant_conversations, query, 3, channel_id
        )
    except Exception as e:
        return f"Recall failed: {e}"
    if not results:
        return f"No relevant past conversations found for '{query}'."
    return "Recalled past conversations:\n" + "\n---\n".join(results)


# ======================================================================================
# Registry
# ======================================================================================

_REGISTRY: list[Tool] = [
    # --- web ---
    Tool("web_search", _fn(
        "web_search",
        "Search the web. Call this for any factual question that isn't plain common knowledge "
        "- current events, niche or technical topics, anything the user asks you to check or "
        "verify. Do NOT call it for opinions, preferences, banter or questions about yourself.",
        {"query": {"type": "string", "description": "The search query"}}, ["query"],
    ), _web_search, needs_api_key=True),
    Tool("web_fetch", _fn(
        "web_fetch",
        "Fetch the contents of a web page. Only use when given a real HTTP/HTTPS URL. Never "
        "invent URLs; do not use for Discord IDs or numbers.",
        {"url": {"type": "string", "description": "A full HTTP/HTTPS URL."}}, ["url"],
    ), _web_fetch, needs_api_key=True),

    # --- fun / social ---
    Tool("roll_dice", _fn(
        "roll_dice", "Roll dice using NdS notation (e.g. '2d6', '1d20').",
        {"notation": {"type": "string", "description": "Dice notation like '1d20' or '3d6'."}}, [],
    ), _roll_dice),
    Tool("flip_coin", _fn(
        "flip_coin", "Flip a coin (Heads or Tails).", {}, [],
    ), _flip_coin),
    Tool("random_choice", _fn(
        "random_choice", "Pick one option at random from a list.",
        {"options": {"type": "array", "items": {"type": "string"},
                     "description": "Options to choose from."}}, ["options"],
    ), _random_choice),
    Tool("set_reminder", _fn(
        "set_reminder",
        "Ping the user about something after a delay. Call this whenever someone asks to be "
        "reminded, nudged, poked or woken at a later time. Saying you will remind them does "
        "nothing - only this tool schedules it.",
        {"minutes": {"type": "number", "description": "Minutes from now to send the reminder."},
         "text": {"type": "string", "description": "What to remind them about."}},
        ["minutes", "text"],
    ), _set_reminder, needs_discord=True),
    Tool("queue_song", _fn(
        "queue_song",
        "Play or queue a song in the voice channel the user is currently sitting in. Use this "
        "whenever someone asks you to play, put on, or queue music. The user must already be in "
        "a voice channel. Accepts a song name, an artist, or a YouTube URL.",
        {"query": {"type": "string",
                   "description": "Song/artist to search for, or a YouTube URL."}}, ["query"],
    ), _queue_song, needs_discord=True, needs_voice=True),

    # --- knowledge / utility ---
    Tool("wikipedia", _fn(
        "wikipedia",
        "Look up a topic on Wikipedia. Prefer this over web_search for encyclopedic subjects - "
        "people, places, history, science, films - where a summary answers the question.",
        {"query": {"type": "string", "description": "The topic or article title."}}, ["query"],
    ), _wikipedia),
    Tool("calculator", _fn(
        "calculator", "Evaluate a basic arithmetic expression (+ - * / % ** and parentheses).",
        {"expression": {"type": "string", "description": "e.g. '(3 + 4) * 2'"}}, ["expression"],
    ), _calculator),
    Tool("get_time", _fn(
        "get_time", "Get the current time in a given IANA timezone.",
        {"timezone": {"type": "string", "description": "IANA tz like 'Europe/London' or 'UTC'."}},
        ["timezone"],
    ), _get_time),

    # --- embodiment / presence ---
    Tool("add_reaction", _fn(
        "add_reaction",
        # Reactions are the one action the model can convincingly fake: typing 🔥 in a reply
        # LOOKS like reacting, so it does that instead of calling this (production returned
        # "🔥 (done!)" with no call, and this is the weakest action tool by a wide margin in
        # the evals). Nobody can type a poll or a rename, which is why those tools need no
        # such warning and this one does.
        "Add an emoji reaction to the user's message. Call this whenever you are asked to "
        "react, and also unprompted when a message genuinely deserves one - something "
        "impressive, funny, sad, or that you strongly agree with - as body language alongside "
        "your reply. Use it for messages that earn it, not every message. Typing an emoji in "
        "your reply is NOT a reaction and does nothing - only this tool adds one.",
        {"emoji": {"type": "string", "description": "A single emoji, e.g. 😂 or 👍"}}, ["emoji"],
    ), _add_reaction, needs_discord=True),
    Tool("set_status", _fn(
        "set_status",
        "Change the status under your name. Pick the activity type that fits what you're "
        "saying - watching/listening/competing read far better than playing for most things, "
        "and 'custom' shows your text with no verb in front at all. Call this whenever someone "
        "asks you to set or change your status. Describing it in your reply does nothing.",
        {"text": {"type": "string",
                  "description": "The status text, without the verb (e.g. 'the kitchen')."},
         "activity": {"type": "string",
                      "enum": ["playing", "watching", "listening", "competing", "custom"],
                      "description": "How it reads: Playing X / Watching X / Listening to X / "
                                     "Competing in X / or custom for the bare text."}},
        ["text"],
    ), _set_status, needs_discord=True),
    Tool("set_nickname", _fn(
        "set_nickname",
        "Change your own nickname in this server. Call this whenever someone asks you to "
        "change, switch or update your name. Writing the new name in your reply does nothing.",
        {"nickname": {"type": "string", "description": "The new nickname."}}, ["nickname"],
    ), _set_nickname, needs_discord=True),
    Tool("set_about", _fn(
        "set_about",
        "Rewrite your own 'About Me' blurb on your Discord profile. Unlike your status this "
        "is permanent until you change it again, so keep it something you'd stand behind. "
        "Call this when asked to change your bio/about, or when yours has gone stale.",
        {"text": {"type": "string", "description": "The new About Me text (max 400 chars)."}},
        ["text"],
    ), _set_about, needs_discord=True),
    Tool("get_user_info", _fn(
        "get_user_info", "Look up a server member's join date, account age and roles by user_id.",
        {"user_id": {"type": "string", "description": "The numeric Discord user id."}}, ["user_id"],
    ), _get_user_info, needs_discord=True),
    Tool("create_poll", _fn(
        "create_poll",
        "Post a real Discord poll people can vote in. Call this whenever someone asks for a "
        "poll or a vote. Listing the options in your reply is not a poll.",
        {"question": {"type": "string", "description": "The poll question."},
         "options": {"type": "array", "items": {"type": "string"},
                     "description": "2-10 answer options."}}, ["question", "options"],
    ), _create_poll, needs_discord=True),
    Tool("start_thread", _fn(
        "start_thread",
        "Start a real thread off the user's message. Call this whenever someone asks for a "
        "thread or to move a tangent out of the channel. Suggesting one is not starting one.",
        {"name": {"type": "string", "description": "The thread name."}}, ["name"],
    ), _start_thread, needs_discord=True),

    # --- memory ---
    Tool("remember_fact", _fn(
        "remember_fact",
        "Store a fact about a user so you still know it in future conversations. Call this "
        "whenever someone tells you to remember something. Saying you will remember does "
        "nothing - only this tool saves it.",
        {"user_id": {"type": "string", "description": "The numeric Discord user id."},
         "fact": {"type": "string", "description": "The fact to remember."}}, ["user_id", "fact"],
    ), _remember_fact, needs_memory=True),
    Tool("recall", _fn(
        "recall", "Search your memory of past conversations for something relevant.",
        {"query": {"type": "string", "description": "What to recall."}}, ["query"],
    ), _recall, needs_memory=True),
]

_BY_NAME = {t.name: t for t in _REGISTRY}
_enabled: dict[str, Tool] = {}


# "I'm looking this up" announcements, posted to the channel before a lookup tool runs, so
# users know the reply draws from a real/deterministic source. Only information-retrieval
# tools announce (narrating an action like a reaction would be silly). Each tool has a LIST of
# variants; one is chosen at random for expressive variety. Templates use str.format
# placeholders drawn from the tool's arguments (plus {name} for get_user_info, resolved from
# the server without pinging anyone). Missing placeholders render as empty via _SafeDict.
# These are overridable per-tool from config (`tool_announcements:`) via configure_announcements().
DEFAULT_ANNOUNCEMENTS: dict[str, list[str]] = {
    "web_search": [
        "🔎 Ooh, let me look that up — searching the web for “{query}”…",
        "🔎 One sec, scouring the web for “{query}”…",
        "🔎 Good question! Off to hunt down “{query}”…",
    ],
    "web_fetch": [
        "🌐 Pulling up {url} for a read…",
        "🌐 Hang on, let me go open {url}…",
        "🌐 Fetching that page — {url}…",
    ],
    "wikipedia": [
        "📖 Let me flip through Wikipedia for “{query}”…",
        "📖 Ooh, cracking open the encyclopedia on “{query}”…",
        "📖 Off to Wikipedia — looking up “{query}”…",
    ],
    "get_user_info": [
        "🪪 Let me pull up {name}’s profile…",
        "🪪 Peeking at {name}’s file real quick…",
        "🪪 Checking my notes on {name}…",
    ],
    "recall": [
        "🧠 Hang on, racking my brain for “{query}”…",
        "🧠 Let me dig through my memory for “{query}”…",
        "🧠 I feel like I remember something about “{query}”…",
    ],
    "calculator": [
        "🧮 Crunching the numbers on {expression}…",
        "🧮 Let me do the math — {expression}…",
        "🧮 One sec, working out {expression}…",
    ],
    "get_time": [
        "🕐 Let me check the clock in {timezone}…",
        "🕐 Peeking at what time it is in {timezone}…",
    ],
    # Action tools - announce what I'm about to do so it's visible.
    "set_nickname": [
        "🏷️ Changing my nickname to “{nickname}”…",
        "🏷️ Renaming myself to “{nickname}”…",
    ],
    "set_status": [
        "🎭 Updating my status to “{text}”…",
    ],
    "set_reminder": [
        "⏰ Setting a reminder for {minutes} min…",
        "⏰ On it — timer for {minutes} min…",
    ],
    "create_poll": [
        "📊 Putting up a poll: “{question}”…",
    ],
    "start_thread": [
        "🧵 Starting a thread: “{name}”…",
    ],
    "queue_song": [
        "🎵 Queueing up “{query}”…",
        "🎵 Righto, finding “{query}” for the voice channel…",
    ],
    "remember_fact": [
        "📝 Noting that down…",
    ],
}

# Active templates (defaults, overridden by configure_announcements()).
_announcements: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_ANNOUNCEMENTS.items()}


class _SafeDict(dict):
    """dict that renders missing str.format keys as empty, so a template can't KeyError."""
    def __missing__(self, key):
        return ""


def configure_announcements(overrides: Optional[dict]) -> None:
    """Merge per-tool announcement templates from config over the built-in defaults.

    A tool's config value may be a single string or a list of variants; either replaces that
    tool's default list. An empty list / None silences that tool. Tools not mentioned keep
    their defaults.
    """
    global _announcements
    merged = {k: list(v) for k, v in DEFAULT_ANNOUNCEMENTS.items()}
    for name, templates in (overrides or {}).items():
        if templates is None:
            merged[name] = []
        elif isinstance(templates, str):
            merged[name] = [templates]
        else:
            merged[name] = list(templates)
    _announcements = merged


def _announcement_fields(name: str, args: dict, ctx: ToolContext) -> dict:
    """Build the placeholder values available to an announcement template."""
    fields = dict(args)
    if name == "get_user_info":
        who = None
        if ctx and ctx.message and ctx.message.guild:
            try:
                member = ctx.message.guild.get_member(int(args.get("user_id", 0) or 0))
                who = member.display_name if member else None
            except (ValueError, TypeError):
                who = None
        fields["name"] = who or "that user"
    return fields


def announce(name: str, args: dict, ctx: ToolContext = None) -> Optional[str]:
    """Return a randomly-chosen announcement line for a tool call, or None to stay silent."""
    templates = _announcements.get(name)
    if not templates:
        return None
    try:
        fields = _announcement_fields(name, dict(args), ctx)
        return random.choice(templates).format_map(_SafeDict(fields))
    except Exception:
        return None


def configure(tools_config: dict, has_api_key: bool, memory_available: bool,
              voice_available: bool = False) -> list[str]:
    """Decide which tools are active based on config + capabilities.

    A tool is enabled if config['tools'][name] is truthy (or its default when unset) AND its
    capability requirements are met (API key for web tools, memory for memory tools).
    Returns the list of enabled tool names.
    """
    global _enabled
    tools_config = tools_config or {}
    _enabled = {}
    for tool in _REGISTRY:
        wanted = tools_config.get(tool.name, tool.default_enabled)
        if not wanted:
            continue
        if tool.needs_api_key and not has_api_key:
            continue
        if tool.needs_memory and not memory_available:
            continue
        if tool.needs_voice and not voice_available:
            continue
        _enabled[tool.name] = tool
    return list(_enabled)


def get_schemas() -> Optional[list[dict]]:
    """Return schemas for the enabled tools, or None if there are none."""
    schemas = [t.schema for t in _enabled.values()]
    return schemas or None


def is_enabled(name: str) -> bool:
    """Whether a given tool is currently active."""
    return name in _enabled


async def execute(name: str, args: dict, ctx: ToolContext) -> str:
    """Dispatch a tool call to its handler. Guards against disabled/unknown tools."""
    tool = _enabled.get(name)
    if tool is None:
        return f"Tool '{name}' is not available."
    return await tool.handler(dict(args), ctx)

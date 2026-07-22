import os
import re
import asyncio
import random
import time
from datetime import datetime, timezone, timedelta
import discord
import ollama
import yaml

import memory
import tools
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
    """
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
# Whether the model does its (slow) internal "thinking" pass. Off by default: much faster,
# and stops reasoning traces from leaking into replies on reasoning models like qwen3.5.
MODEL_THINK = CONFIG.get("use_thinking", False)
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


def _get_channel_lock(channel_id: int) -> asyncio.Lock:
    """Return the (shared) lock for a channel, creating it on first use."""
    lock = _channel_locks.get(channel_id)
    if lock is None:
        lock = asyncio.Lock()
        _channel_locks[channel_id] = lock
    return lock

# Post a short "looking this up…" line to the channel before running a lookup tool.
ANNOUNCE_TOOLS = CONFIG.get("announce_tools", True)

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
    """Call ollama chat, passing the configured `think` setting (off by default for speed).

    Falls back to a plain call if the installed client/model doesn't accept `think`, so a
    non-reasoning model still works.
    """
    try:
        return await client.chat(think=MODEL_THINK, **kwargs)
    except (TypeError, ollama.ResponseError) as e:
        print(f"[WARN] 'think' param not accepted ({e}); retrying without it")
        return await client.chat(**kwargs)


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
    ollama_client = ollama.AsyncClient()

    # Build system prompt with optional memory context
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n[Memory Context]\n{memory_context}\n"

    conversation = [{"role": "system", "content": system_content}] + list(messages)
    tool_schemas = tools.get_schemas()

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
            return response.message.content

        # Record the assistant's tool-call turn, then execute each requested tool.
        conversation.append(response.message)
        print(f"[DEBUG] Tool calls: {[t.function.name for t in tool_calls]}")

        seen_calls = set()  # Deduplicate identical calls within a round
        for tool_call in tool_calls:
            name = tool_call.function.name
            args = tool_call.function.arguments
            call_key = (name, str(args))
            if call_key in seen_calls:
                print(f"[DEBUG] Skipping duplicate: {name}")
                continue
            seen_calls.add(call_key)

            # Announce lookups so users know we're drawing from a real source.
            if ANNOUNCE_TOOLS and ctx is not None and ctx.message is not None:
                note = tools.announce(name, args, ctx)
                if note:
                    try:
                        sent = await ctx.message.channel.send(note)
                        _note_announcement_id(sent.id)  # keep it out of future history
                    except Exception as e:
                        print(f"[WARN] Could not send tool announcement: {e}")

            try:
                result = await tools.execute(name, args, ctx or ToolContext())
                print(f"[DEBUG] Tool {name} returned {len(result)} chars")
            except Exception as e:
                print(f"[WARN] Tool {name} failed: {e}")
                result = f"Tool error: {e}"

            conversation.append({
                "role": "tool",
                "tool_name": name,
                "content": result,
            })

    # Exhausted tool rounds - force a final answer with tools disabled.
    print(f"[WARN] Hit MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}); forcing final answer")
    response = await _model_chat(ollama_client, model=MODEL, messages=conversation)
    return response.message.content


@client.event
async def on_ready():
    global SYSTEM_PROMPT
    # Replace Discord-specific placeholders now that we have bot info
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{discord_display_name}}", client.user.display_name)
    SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{{discord_user_id}}", str(client.user.id))
    print(f"Logged in as {client.user}")

    # Set bot status based on enabled features
    status_config = CONFIG.get("status", {})
    status_parts = []
    if do_memory:
        status_parts.append(status_config.get("memory_enabled", "Memory on"))
    if tools.is_enabled("web_search"):
        status_parts.append(status_config.get("websearch_enabled", "Web search on"))

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


async def _formulate_and_reply(message: discord.Message, ref_msg):
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
            memory_context = memory.build_memory_context(
                user_id=str(message.author.id),
                current_message=message.content,
                channel_id=str(message.channel.id),
                do_user_memory=do_user_memory,
                do_conversation_memory=do_conversation_memory,
            )

        # Query the LLM (ctx lets tools act on the server: react, set status, etc.)
        ctx = ToolContext(message=message, client=client, model=MODEL)
        try:
            response = await query_ollama(messages, memory_context=memory_context, ctx=ctx)
        except TimeoutError:
            await message.reply("The request timed out. Please try again. 🥒")
            return
        except ollama.ResponseError as e:
            await message.reply(f"Error communicating with Ollama: {e} 🥒")
            return
        except Exception as e:
            await message.reply(f"<Weird Error>  🥒")
            print(f"<Weird Error> {e} 🥒")
            return

    # Failsafe for empty response
    if not response:
        print(f"[WARN] model generated empty response - user message: {message.content}")
        await message.reply("I'm not sure what to say to that. 🥒")
        return

    # Strip input format prefix if model mimics it
    response = strip_message_prefix(response)

    # Send the reply first
    if "<ignore>" not in response:
        if len(response) <= 2000:
            await message.reply(response)
        else:
            chunks = [response[i : i + 2000] for i in range(0, len(response), 2000)]
            for chunk in chunks:
                await message.reply(chunk)

    overall_elapsed = time.time() - overall_start
    print(f"[TIMING] Overall response: {overall_elapsed:.2f}s")

    # Process memory after responding (non-blocking for user experience)
    if do_memory:
        # Store this conversation for future recall
        if do_conversation_memory:
            try:
                memory.store_conversation(
                    channel_id=str(message.channel.id),
                    messages=messages,
                    max_conversations=max_stored_conversations
                )
            except Exception as mem_err:
                print(f"[WARN] Failed to store conversation: {mem_err}")

        # Occasionally update user summary
        # TODO: we could use some basic language analysis to determine if the user said anything important
        if do_user_memory and random.random() < USER_SUMMARY_CHANCE:
            try:
                await memory.generate_user_summary(
                    user_id=str(message.author.id),
                    user_name=message.author.display_name,
                    recent_messages=messages,
                    model=MODEL
                )
            except Exception as sum_err:
                print(f"[WARN] Failed to generate user summary: {sum_err}")


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


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN (or KRONK_TOKEN) environment variable is not set")

    #======
    # Init
    #======
    if do_memory:
        memory.init_memory(CONFIG.get("memory_dir", "./bot_memory"))

    # Configure the tool registry based on config + available capabilities.
    enabled_tools = tools.configure(
        CONFIG.get("tools", {}),
        has_api_key=bool(OLLAMA_API_KEY),
        memory_available=do_memory,
    )
    # Load any persona-specific announcement templates (fall back to built-in defaults).
    tools.configure_announcements(CONFIG.get("tool_announcements"))

    # Point the persistent reminder store at its file (reminders are rescheduled in on_ready).
    tools.init_reminders(CONFIG.get("reminders_file", "./bot_reminders.json"))
    if enabled_tools:
        print(f"Tools enabled ({len(enabled_tools)}): {', '.join(enabled_tools)}")
    else:
        print("No tools enabled.")
    _tools_cfg = CONFIG.get("tools", {})
    if (_tools_cfg.get("web_search") or _tools_cfg.get("web_fetch")) and not OLLAMA_API_KEY:
        print("Note: web tools requested but disabled - OLLAMA_API_KEY not set")

    client.run(DISCORD_TOKEN)

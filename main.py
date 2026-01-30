import os
import re
import random
import time
from collections.abc import Sequence
from datetime import datetime, timezone, timedelta
import discord
import ollama
import yaml

import memory


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

# Web search requires config enabled and API key (ollama.web_search uses cloud API)
do_websearch = CONFIG.get("web_search", False) and bool(OLLAMA_API_KEY)

# Load from config
MODEL = CONFIG.get("model", "gemma3:27b")
FUNCTION_MODEL = CONFIG.get("function_model", "functionary")
SYSTEM_PROMPT = CONFIG.get("system_prompt", "You are a helpful chatbot.")
MESSAGE_HISTORY_LIMIT = CONFIG.get("message_history", {}).get("limit", 10)
MESSAGE_MAX_AGE_MINUTES = CONFIG.get("message_history", {}).get("max_age_minutes", 0)
USER_SUMMARY_CHANCE = CONFIG.get("memory", {}).get("user_summary_update_chance", 0.2)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Tool definitions for the function-calling model
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Use this when the user asks about recent events, "
                           "needs up-to-date information, or asks you to look something up online."
                           "Only use this when necessary, as this operation is very intensive",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the contents of a web page. Only use when the user explicitly provides an HTTP/HTTPS "
                           "URL (like https://example.com). Do NOT use for Discord IDs, numbers, or non-URL strings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "A full HTTP/HTTPS URL."
                    }
                },
                "required": ["url"]
            }
        }
    }
]

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
        if msg.author.bot and msg.author != channel.guild.me:
            continue  # Skip other bots, but include our own messages
        if not msg.content or not msg.content.strip():
            continue  # Skip empty messages (images, embeds, etc.)
        if cutoff_time and msg.created_at < cutoff_time:
            continue  # Skip messages older than max age
        messages.append(process_message(msg))
    messages.reverse()
    return messages


async def execute_tool(tool_name: str, arguments: dict) -> str:
    """Execute a tool and return its result as a string."""
    if tool_name == "web_search":
        print(f"[TOOL] web_search: {arguments['query']}")
        result = ollama.web_search(arguments["query"])
        return f"Web search results for '{arguments['query']}':\n{result}"
    elif tool_name == "web_fetch":
        url = arguments["url"]
        # Validate URL before fetching
        if not url.startswith(("http://", "https://")):
            print(f"[TOOL] web_fetch: invalid URL '{url}' (skipped)")
            return f"Invalid URL: {url} (must start with http:// or https://)"
        print(f"[TOOL] web_fetch: {url}")
        result = ollama.web_fetch(url)
        return f"Contents of {url}:\n{result}"
    else:
        return f"Unknown tool: {tool_name}"


async def query_function_model(messages: list[dict]) -> Sequence[dict] | None:
    """Query the function-calling model to determine if tools should be used.

    Returns a list of tool calls if the model decides to use tools, None otherwise.
    """
    if not do_websearch:
        return None

    ollama_client = ollama.AsyncClient()

    # Build a simplified prompt for tool decision
    function_system = """You decide whether to use tools based on the user's MOST RECENT message.

RULES:
- web_search: ONLY use if the user explicitly asks to "search", "look up", or "find" something online.
- web_fetch: ONLY use if the user's message contains an actual URL (starting with http:// or https://). NEVER invent URLs.
- If unsure, do NOT use any tools.
- Most messages need NO tools - only use them when clearly requested."""

    function_messages = [{"role": "system", "content": function_system}] + messages

    start_time = time.time()
    response = await ollama_client.chat(
        model=FUNCTION_MODEL,
        messages=function_messages,
        tools=TOOL_DEFINITIONS,
    )
    elapsed = time.time() - start_time
    print(f"[TIMING] Function model ({FUNCTION_MODEL}): {elapsed:.2f}s")

    if response.message.tool_calls:
        return response.message.tool_calls
    return None


async def query_ollama(messages: list[dict], memory_context: str = None) -> str:
    """Query the main conversation model, optionally using tools via the function model."""
    ollama_client = ollama.AsyncClient()

    # Build system prompt with optional memory context
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n[Memory Context]\n{memory_context}\n"

    # First, check if we need to use any tools (via the function-calling model)
    tool_results = []
    tool_calls = await query_function_model(messages)

    if tool_calls:
        print(f"[DEBUG] Function model requested tools: {[t.function.name for t in tool_calls]}")
        seen_calls = set()  # Deduplicate tool calls
        for tool_call in tool_calls:
            # Create a key for deduplication
            call_key = (tool_call.function.name, str(tool_call.function.arguments))
            if call_key in seen_calls:
                print(f"[DEBUG] Skipping duplicate: {tool_call.function.name}")
                continue
            seen_calls.add(call_key)

            try:
                result = await execute_tool(
                    tool_call.function.name,
                    tool_call.function.arguments
                )
                tool_results.append(result)
                print(f"[DEBUG] Tool {tool_call.function.name} returned {len(result)} chars")
            except Exception as e:
                print(f"[WARN] Tool {tool_call.function.name} failed: {e}")
                tool_results.append(f"Tool error: {e}")

    # Add tool results to system prompt if any
    if tool_results:
        system_content += "\n\n[Tool Results]\n" + "\n\n".join(tool_results) + "\n"

    full_messages = [{"role": "system", "content": system_content}] + messages

    # Query the main conversation model
    start_time = time.time()
    response = await ollama_client.chat(
        model=MODEL,
        messages=full_messages,
    )
    elapsed = time.time() - start_time
    print(f"[TIMING] Main model ({MODEL}): {elapsed:.2f}s")

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
    if do_websearch:
        status_parts.append(status_config.get("websearch_enabled", "Web search on"))

    if status_parts:
        status_text = " | ".join(status_parts)
        await client.change_presence(activity=discord.Game(name=status_text))
        print(f"Status set: {status_text}")


@client.event
async def on_message(message: discord.Message):
    # print(f"Message received: {message.content}")

    if message.is_system():
        return  # System message
    if message.author == client.user:
        return  # Message from this bot

    # fetch the referenced message if it exists:
    ref_msg = None
    if message.reference and message.reference.message_id:
        ref_msg = await message.channel.fetch_message(message.reference.message_id)

    # Only respond if mentioned or is responding to its message
    is_mentioned = str(client.user.id) in message.content
    is_reply = ref_msg is not None and ref_msg.author == client.user
    if not is_mentioned and not is_reply:
        return

    # Formulating a response:
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

        # Query the LLM
        try:
            response = await query_ollama(messages, memory_context=memory_context)
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


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN (or KRONK_TOKEN) environment variable is not set")

    #======
    # Init
    #======
    if do_memory:
        memory.init_memory(CONFIG.get("memory_dir", "./bot_memory"))

    if do_websearch:
        print(f"Web search enabled (function model: {FUNCTION_MODEL})")
    else:
        if CONFIG.get("web_search", False) and not OLLAMA_API_KEY:
            print("Web search disabled: OLLAMA_API_KEY not set")
        else:
            print("Web search disabled (enable with web_search: true in config + OLLAMA_API_KEY)")

    client.run(DISCORD_TOKEN)

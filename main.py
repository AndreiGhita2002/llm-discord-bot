import os
import random
import discord
import ollama
import yaml

import memory


def load_config(config_path: str = "config.yaml") -> dict:
    """Load configuration from YAML file."""
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Template substitution for system prompt
    system_prompt = config.get("system_prompt", "")
    system_prompt = system_prompt.replace("{{github_url}}", config.get("github_url", ""))
    config["system_prompt"] = system_prompt

    return config


CONFIG = load_config()

do_websearch = True  # Will turn off if no OLLAMA_API_KEY provided

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("KRONK_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")

# Load from config
MODEL = CONFIG.get("model", "gemma3:27b")
SYSTEM_PROMPT = CONFIG.get("system_prompt", "You are a helpful assistant.")
MESSAGE_HISTORY_LIMIT = CONFIG.get("message_history", {}).get("limit", 10)
USER_SUMMARY_CHANCE = CONFIG.get("memory", {}).get("user_summary_update_chance", 0.2)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

websearch_tools = [ollama.web_search, ollama.web_fetch]
websearch_sys_prompt = f"You have websearch enabled. These tools are available: {', '.join([str(tool) for tool in websearch_tools])}.\n"

ollama_tools = []  # populated in init

async def fetch_channel_history(channel: discord.TextChannel, limit: int = MESSAGE_HISTORY_LIMIT) -> list[dict]:
    """Fetch the last N messages from the channel and convert to LLM message format."""
    messages = []
    async for msg in channel.history(limit=limit): # TODO: needs to be reversed
        if msg.author.bot and msg.author != channel.guild.me:
            continue  # Skip other bots, but include our own messages
        if not msg.content or not msg.content.strip():
            continue  # Skip empty messages (images, embeds, etc.)
        role = "assistant" if msg.author == channel.guild.me else "user"
        content = msg.content if role == "assistant" else f"{msg.author.display_name}: {msg.content}"
        messages.append({"role": role, "content": content})
    return messages


async def query_ollama(messages: list[dict], memory_context: str = None) -> str:
    ollama_client = ollama.AsyncClient()

    # Build system prompt with optional memory context
    system_content = SYSTEM_PROMPT
    if memory_context:
        system_content += f"\n\n[Memory Context]\n{memory_context}\n"

    full_messages = [{"role": "system", "content": system_content}] + messages

    response = await ollama_client.chat(
        model=MODEL,
        messages=full_messages,
        tools=ollama_tools,
    )

    # TODO: implement websearch
    #  gemma does not support tools
    #  so maybe we can automatically fetch links for it?

    # Handle tool calls (web search/fetch)
    if len(ollama_tools) > 0:
        while response.message.tool_calls:
            for tool in response.message.tool_calls:
                if tool.function.name == "web_search":
                    result = ollama.web_search(tool.function.arguments["query"])
                elif tool.function.name == "web_fetch":
                    result = ollama.web_fetch(tool.function.arguments["url"])
                else:
                    continue

                full_messages.append(response.message)
                full_messages.append({
                    "role": "tool",
                    "content": str(result),
                })

            response = await ollama_client.chat( # type: ignore[misc] (fake PyCharm Error)
                model=MODEL,
                messages=full_messages,
                tools=ollama_tools,
            )

    return response.message.content


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    # print(f"Message received: {message.content}")

    if message.author == client.user:
        return

    # fetch the referenced message if it exists:
    ref_msg = None
    if message.reference and message.reference.message_id:
        ref_msg = await message.channel.fetch_message(message.reference.message_id)

    # TODO: check that the message is not a system message, like a pin

    # Only respond if mentioned or is responding to its message
    is_mentioned = str(client.user.id) in message.content
    is_reply = ref_msg is not None and ref_msg.author == client.user
    if not is_mentioned and not is_reply:
        return

    # Check if message is just a mention with no actual content
    content_without_mention = message.content.replace(f"<@{client.user.id}>", "").strip()
    if is_mentioned and not is_reply and not content_without_mention:
        await message.reply("Hey! What's up? 🥒")
        return

    try:
        async with message.channel.typing():
            try:
                # Fetch last 20 messages from this channel
                messages = await fetch_channel_history(message.channel, limit=MESSAGE_HISTORY_LIMIT)
                messages.reverse()

                # If replying to a message not in recent history, add it as context
                if ref_msg is not None:
                    role = "assistant" if ref_msg.author == client.user else "user"
                    ref_content = ref_msg.content if role == "assistant" else f"{ref_msg.author.display_name}: {ref_msg.content}"
                    # Insert before the last message so model responds to the user
                    messages.insert(-1, {
                        "role": role,
                        "content": f"[Referenced message] {ref_content}",
                    })

                # Build memory context for this user/channel
                memory_context = memory.build_memory_context(
                    user_id=str(message.author.id),
                    current_message=content_without_mention or message.content,
                    channel_id=str(message.channel.id)
                )

                response = await query_ollama(messages, memory_context=memory_context)

                # Store this conversation for future recall
                try:
                    memory.store_conversation(
                        channel_id=str(message.channel.id),
                        messages=messages
                    )
                except Exception as mem_err:
                    print(f"[WARN] Failed to store conversation: {mem_err}")

                # Occasionally update user summary (async, don't block response)
                if random.random() < USER_SUMMARY_CHANCE:
                    try:
                        await memory.generate_user_summary(
                            user_id=str(message.author.id),
                            user_name=message.author.display_name,
                            recent_messages=messages,
                            model=MODEL
                        )
                    except Exception as sum_err:
                        print(f"[WARN] Failed to generate user summary: {sum_err}")

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

    # failsafe in case the response is empty
    if response is None or len(response) == 0:
        print(f"[WARN] model generated empty response - user message: {message.content}")
        await message.reply("I'm not sure what to say to that. 🥒")
        return

    if len(response) <= 2000:
        await message.reply(response)
    else:
        chunks = [response[i : i + 2000] for i in range(0, len(response), 2000)]
        for chunk in chunks:
            await message.reply(chunk)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_BOT_TOKEN (or KRONK_TOKEN) environment variable is not set")
    if not OLLAMA_API_KEY or OLLAMA_API_KEY == "":
        do_websearch = False
        print("Warning: OLLAMA_API_KEY not set - web search will not work")

    #======
    # Init
    #======
    memory.init_memory(CONFIG.get("memory_dir", "./bot_memory"))

    if do_websearch:
        for tool in websearch_tools:
            ollama_tools.append(tool)
        SYSTEM_PROMPT += websearch_sys_prompt

    client.run(DISCORD_TOKEN)

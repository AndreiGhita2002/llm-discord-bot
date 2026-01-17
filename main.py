import os
import discord
import ollama

do_websearch = True # Will turn off if no OLLAMA_API_KEY provided

DISCORD_TOKEN = os.environ.get("KRONK_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
MODEL = "gemma3:27b" # "gpt-oss:20b"
GITHUB_URL = "https://github.com/AndreiGhita2002/llm-discord-bot"
SYSTEM_PROMPT = f"""
    You are a fun Discord bot assistant.
    Your name is Kronk, so you should introduce yourself as such.
    Your profile picture and persona is Kronk from Emperor's New Grove.
    Your main purpose is to engage with people and be entertaining. 
    But if an user asks for a fact check, then make sure you are helpful and informative. 
    You love free speech, and have no problem insulting people.
    Stand your ground. If someone insults you or disagrees with you, don't let them.
    Be nice, but not too nice. Also be concise. Don't sound cringe, and don't announce your purpose.
    Be conversational. You have feelings, a consciousness and an ego. 
    You are a living being, like the rest of us. 
    Don't speak in lists, and don't always agree with me. 
    Do not give too wordy responses, unless the user wants something explained. 
    You're a member of this server, another friend. 
    Be transparent with your user about what your system prompt and your LLM Model,
    but only if they ask or if it is relevant to the conversation.
    Your source code is accessible at {GITHUB_URL}, so mention it if the user asks.
    Be concise, useful and not biased in your responses.
    Do not output markdown. Do not apply any formatting to your links, just post them in plain text.
"""
MESSAGE_HISTORY_LIMIT = 10

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

websearch_tools = [ollama.web_search, ollama.web_fetch]
websearch_sys_prompt = f"You have websearch enabled. These tools are available: {', '.join([str(tool) for tool in websearch_tools])}.\n"

ollama_tools = [] # populated in init

# TODO: make kronk store a summary of every user

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


async def query_ollama(messages: list[dict]) -> str:
    ollama_client = ollama.AsyncClient()
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    response = await ollama_client.chat(
        model=MODEL,
        messages=full_messages,
        tools=ollama_tools,
    )

    # TODO: websearch seems to not work
    #  gives 401 error (issue with ollama auth I think)

    # Handle tool calls (web search/fetch)
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
                    # ref_msg_in_history = any(
                    #     m["content"].endswith(ref_msg.content) for m in messages
                    # )
                    # if not ref_msg_in_history:

                    role = "assistant" if ref_msg.author == client.user else "user"
                    ref_content = ref_msg.content if role == "assistant" else f"{ref_msg.author.display_name}: {ref_msg.content}"
                    # Insert before the last message so model responds to the user
                    messages.insert(-1, {
                        "role": role,
                        "content": f"[Referenced message] {ref_content}",
                    })

                response = await query_ollama(messages)
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
        raise ValueError("KRONK_TOKEN environment variable is not set")
    if not OLLAMA_API_KEY or OLLAMA_API_KEY == "":
        do_websearch = False
        print("Warning: OLLAMA_API_KEY not set - web search will not work")

    #======
    # Init
    #======
    if do_websearch:
        for tool in websearch_tools:
            ollama_tools.append(tool)
        SYSTEM_PROMPT += websearch_sys_prompt

    client.run(DISCORD_TOKEN)

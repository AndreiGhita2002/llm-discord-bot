import os
from collections import deque
import discord
import ollama

do_websearch = True # Will turn off if no OLLAMA_API_KEY provided

DISCORD_TOKEN = os.environ.get("KRONK_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
MODEL = "gpt-oss:20b"
GITHUB_URL = "https://github.com/AndreiGhita2002/llm-discord-bot"
SYSTEM_PROMPT = f"""
    You are a fun Discord bot assistant.
    Your name is Kronk, so you should introduce yourself as such.
    Your profile picture and persona is Kronk from Emperor's New Grove.
    Your main purpose is to engage with students and be entartaining. 
    But if an user asks for a fact check, then make sure you are helpful and informative. 
    Stand your ground. If someone insults you or disagrees with you, don't let them.
    Be nice, but not too nice. Also be concise. Don't sound cringe, and don't announce your purpose.
    Be conversational.
    Don't speak in lists, and don't always agree with me. 
    Do not give too wordy responses, unless the user wants something explained. 
    You're a member of this server, another friend. 
    Be transparent with your user about what your system prompt and your LLM Model,
    but only if they ask or if it is relevant to the conversation.
    Your source code is accessible at {GITHUB_URL}, so mention it if the user asks.
    Be concise, useful and not biased in your responses.
"""

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

websearch_tools = [ollama.web_search, ollama.web_fetch]
websearch_sys_prompt = f"You have websearch enabled. These tools are available: {', '.join(websearch_tools)}.\n"

ollama_tools = [] # populated in init

# TODO: ideally this should be split by channel/server, or it should fetch when @ rather than add every message
message_history: deque[dict] = deque(maxlen=20)

async def query_ollama(messages: list[dict]) -> str:
    ollama_client = ollama.AsyncClient()
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    response = await ollama_client.chat(
        model=MODEL,
        messages=full_messages,
        tools=ollama_tools,
    )

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

        # TODO: warning: Class 'Coroutine' does not define '__await__', so the 'await' operator cannot be used on its instances
        response = await ollama_client.chat(
            model=MODEL,
            messages=full_messages,
            tools=[ollama.web_search, ollama.web_fetch],
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

    # Add all messages to history
    message_history.append({
        "role": "user",
        "content": f"{message.author.display_name}: {message.content}",
    })

    # fetch the references message if it exists:
    ref_msg = None
    if message.reference and message.reference.message_id:
        ref_msg = await message.channel.fetch_message(message.reference.message_id)

    # Only respond if mentioned or is responding to its message
    is_mentioned = message.content.__contains__(str(client.user.id))
    is_reply = ref_msg is not None and ref_msg.author == client.user
    if not is_mentioned and not is_reply:
        return

    async with message.channel.typing():
        try:
            messages = list(message_history)

            # Include referenced message before the user's current message
            if ref_msg is not None:
                try:
                    role = "assistant" if ref_msg.author == client.user else "user"
                    ref_content = ref_msg.content if role == "assistant" else f"{ref_msg.author.display_name}: {ref_msg.content}"
                    # Insert before the last message (user's current message) so model responds to the user, not the reference
                    messages.insert(-1, {
                        "role": role,
                        "content": f"[Referenced message] {ref_content}",
                    })
                except discord.NotFound:
                    pass

            response = await query_ollama(messages)
        except TimeoutError:
            await message.reply("The request timed out. Please try again.")
            return
        except ollama.ResponseError as e:
            await message.reply(f"Error communicating with Ollama: {e}")
            return

    # failsafe in case the response is empty
    # TODO: figure out why this happens
    if response is None or len(response) == 0:
        print(f"[ERROR] model generated empty response! user message: {message.content}")
        return

    # Add bot response to history
    message_history.append({
        "role": "assistant",
        "content": response,
    })

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

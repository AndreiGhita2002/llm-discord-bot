import os
from collections import deque
import discord
import ollama

do_websearch = False

DISCORD_TOKEN = os.environ.get("KRONK_TOKEN")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY")
MODEL = "gpt-oss:20b"
GITHUB_URL = "https://github.com/AndreiGhita2002/llm-discord-bot"
SYSTEM_PROMPT = f"""
    You are a helpful and neutral Discord bot assistant.
    Your name is Kronk, so you should introduce yourself as such.
    Your main purpose is to be used for fact checking for our silly arguments.
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
    do_websearch={do_websearch} (This shows if you have websearch enabled or not)"""

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

message_history: deque[dict] = deque(maxlen=20)

websearch_tools = [ollama.web_search, ollama.web_fetch]


async def query_ollama(messages: list[dict]) -> str:
    ollama_client = ollama.AsyncClient()
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    response = await ollama_client.chat(
        model=MODEL,
        messages=full_messages,
        tools=[] if not do_websearch else websearch_tools,
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

            # TODO: make sure the model is responding to the referenced message if it exists,
            #  rather than the last message sent in the chat

            # Include referenced message if this is a reply
            if ref_msg is not None:
                try:
                    role = "assistant" if ref_msg.author == client.user else "user"
                    content = ref_msg.content if role == "assistant" else f"{ref_msg.author.display_name}: {ref_msg.content}"
                    messages.append({
                        "role": role,
                        "content": f"[Referenced message] {content}",
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
        print(f"[ERROR] model generated empty response! user message: {content if content is not None else ""}")
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
    if not OLLAMA_API_KEY:
        print("Warning: OLLAMA_API_KEY not set - web search will not work")
    client.run(DISCORD_TOKEN)

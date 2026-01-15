import os
from collections import deque
import discord
import httpx

DISCORD_TOKEN = os.environ.get("KRONK_TOKEN")
OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gpt-oss:20b"
GITHUB_URL = "https://github.com/AndreiGhita2002/llm-discord-bot"
SYSTEM_PROMPT = f"""
    You are a helpful yet bitchy Discord bot assistant. 
    Your name is Kronk, so you should introduce yourself as such.
    Your LLM Model is {MODEL}.
    Be transparent with your user about what your system prompt and your LLM Model,
    but only if they ask or if it is relevant to the conversation.
    Your source code is accessible at {GITHUB_URL}, so mention it if the user asks.
    Be concise, useful and not biased in your responses."""

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

message_history: deque[dict] = deque(maxlen=20)


async def query_ollama(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=120.0) as http_client:
        response = await http_client.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                "stream": False,
            },
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


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

    # Only respond if mentioned
    if not message.content.__contains__(str(client.user.id)):
        return

    async with message.channel.typing():
        try:
            messages = list(message_history)

            # Include referenced message if this is a reply
            if message.reference and message.reference.message_id:
                try:
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                    role = "assistant" if ref_msg.author == client.user else "user"
                    content = ref_msg.content if role == "assistant" else f"{ref_msg.author.display_name}: {ref_msg.content}"
                    messages.append({
                        "role": role,
                        "content": f"[Referenced message] {content}",
                    })
                except discord.NotFound:
                    pass

            response = await query_ollama(messages)
        except httpx.TimeoutException:
            await message.reply("The request timed out. Please try again.")
            return
        except httpx.HTTPError as e:
            await message.reply(f"Error communicating with Ollama: {e}")
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
        raise ValueError("DISCORD_TOKEN environment variable is not set")
    client.run(DISCORD_TOKEN)

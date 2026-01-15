import os
import discord
import httpx

DISCORD_TOKEN = os.environ.get("KRONK_TOKEN")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL = "gpt-oss:20b"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def query_ollama(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=120.0) as http_client:
        response = await http_client.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False},
        )
        response.raise_for_status()
        return response.json()["response"]


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    # print(f"Message received: {message.content}")

    if message.author == client.user:
        return

    # I do not know why this doesn't work:
    # if client.user not in message.mentions
    # Instead I check for the user id in the message:
    if not message.content.__contains__(str(client.user.id)):
        # print(f"Was not @ in this message {client.user} {message.mentions}")
        return

    prompt = message.content.replace(f"<@{client.user.id}>", "").strip()
    if not prompt:
        await message.reply("Please provide a message after mentioning me.")
        return

    async with message.channel.typing():
        try:
            response = await query_ollama(prompt)
        except httpx.TimeoutException:
            await message.reply("The request timed out. Please try again.")
            return
        except httpx.HTTPError as e:
            await message.reply(f"Error communicating with Ollama: {e}")
            return

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

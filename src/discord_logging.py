"""
Optional Discord log channel: forward warnings/errors (and startup/shutdown notices) to a
channel per server, so you can see what Kronk is doing without SSHing into the host.

- Per-guild channel mapping, persisted to JSON. Defaults to NONE (nothing is sent until an
  admin runs /setlogchannel).
- A logging.Handler captures WARNING+ records from the whole app (including discord.py's own
  gateway warnings) and a background task flushes them to the configured channel(s).
- Logging (sync, possibly from worker threads) is decoupled from sending (async) via a queue.
"""

import asyncio
import json
import logging
import queue
import re
from pathlib import Path

import discord

log = logging.getLogger("kronk")  # app logger; use log.warning(...) / log.exception(...)

_LOG_FILE: Path = None
_log_channels: dict[int, int] = {}                 # guild_id -> channel_id (empty = disabled)
_log_queue: "queue.Queue[tuple[int, str]]" = queue.Queue(maxsize=1000)
_MAX_LEN = 1900                                     # keep under Discord's 2000-char limit


# === Persistence ===

def _load() -> None:
    global _log_channels
    if _LOG_FILE and _LOG_FILE.exists():
        try:
            raw = json.loads(_LOG_FILE.read_text())
            _log_channels = {int(k): int(v) for k, v in raw.items()}
        except Exception as e:
            print(f"[WARN] could not read log channels file: {e}")
            _log_channels = {}


def _save() -> None:
    if _LOG_FILE:
        try:
            _LOG_FILE.write_text(json.dumps({str(k): v for k, v in _log_channels.items()}, indent=2))
        except Exception as e:
            print(f"[WARN] could not write log channels file: {e}")


def set_channel(guild_id: int, channel_id: int) -> None:
    _log_channels[int(guild_id)] = int(channel_id)
    _save()


def clear_channel(guild_id: int) -> None:
    _log_channels.pop(int(guild_id), None)
    _save()


def get_channel(guild_id: int):
    return _log_channels.get(int(guild_id))


# === Logging bridge ===

class _DiscordLogHandler(logging.Handler):
    """Queues formatted WARNING+ records; the async flusher sends them to Discord."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_queue.put_nowait((record.levelno, self.format(record)))
        except Exception:
            pass  # never let logging break the app


def init(path: str, level: int = logging.WARNING) -> None:
    """Load the saved channel map and attach logging handlers to the root logger.

    Two handlers: a StreamHandler so everything still prints to the terminal/bot.log, and the
    Discord handler that forwards WARNING+ to the configured channel(s).
    """
    global _LOG_FILE
    _LOG_FILE = Path(path)
    _load()
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Terminal / bot.log - keep printing errors to stdout as before.
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("[%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(stream)

    # Discord log channel - WARNING and above.
    discord_handler = _DiscordLogHandler()
    discord_handler.setLevel(level)
    discord_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.addHandler(discord_handler)


async def run_flusher(client: discord.Client) -> None:
    """Background task: drain queued log records and post them to configured channel(s)."""
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(2)
        batch = []
        while len(batch) < 10:
            try:
                batch.append(_log_queue.get_nowait())
            except queue.Empty:
                break
        if not batch or not _log_channels:
            continue  # drained (and discarded if no channels) so the queue can't grow unbounded
        for levelno, msg in batch:
            emoji = "❌" if levelno >= logging.ERROR else "⚠️"
            await _broadcast(client, f"{emoji} {msg}")


async def _broadcast(client: discord.Client, text: str) -> None:
    """Send text to every configured log channel (best-effort)."""
    text = text[:_MAX_LEN]
    for channel_id in list(_log_channels.values()):
        channel = client.get_channel(channel_id)
        if channel is None:
            continue
        try:
            await channel.send(text)
        except Exception:
            pass


async def notify(client: discord.Client, text: str) -> None:
    """Send an informational line directly to the log channel(s), bypassing the level filter.

    Use for startup/shutdown notices ('Kronk online', etc.).
    """
    if _log_channels:
        await _broadcast(client, text)


# === Text commands (matched by regex, NOT Discord's slash-command API) ===
# Users type these as normal messages beginning with '/'; we parse them ourselves.
#   /setlogchannel            -> log to the current channel
#   /setlogchannel #channel   -> log to the given channel
#   /clearlogchannel          -> stop logging to a channel
_SET_RE = re.compile(r"^/setlogchannel(?:\s+<#(\d+)>)?\s*$", re.IGNORECASE)
_CLEAR_RE = re.compile(r"^/clearlogchannel\s*$", re.IGNORECASE)

# Help text for !help, kept next to the regexes above so the two can't drift apart.
# NOTE the '/' prefix: these predate the voice commands' '!' prefix - unifying them is a TODO.
COMMAND_HELP: list[tuple[str, str]] = [
    ("/setlogchannel [#channel]", "Send my warnings and errors to a channel"),
    ("/clearlogchannel", "Stop posting logs to a channel"),
]


async def handle_command(message: discord.Message) -> bool:
    """If the message is a log-channel command, handle it and return True; otherwise False.

    Requires the Manage Server permission. Meant to be called early in on_message so command
    messages don't get passed to the LLM.
    """
    content = (message.content or "").strip()
    set_match = _SET_RE.match(content)
    clear_match = _CLEAR_RE.match(content)
    if not set_match and not clear_match:
        return False

    if message.guild is None:
        await message.reply("That command only works in a server.")
        return True

    perms = getattr(message.author, "guild_permissions", None)
    if perms is None or not perms.manage_guild:
        await message.reply("You need the Manage Server permission to do that.")
        return True

    if set_match:
        channel_id = int(set_match.group(1)) if set_match.group(1) else message.channel.id
        channel = message.guild.get_channel(channel_id)
        if channel is None:
            await message.reply("I can't find that channel.")
            return True
        set_channel(message.guild.id, channel_id)
        await message.reply(
            f"✅ Log channel set to {channel.mention}. Warnings and errors will post here."
        )
    else:  # clear_match
        clear_channel(message.guild.id)
        await message.reply("✅ Logging to a channel is now disabled for this server.")
    return True

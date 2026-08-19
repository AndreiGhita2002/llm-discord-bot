"""
YouTube audio playback in Discord voice channels.

Commands are plain text messages matched by regex, NOT Discord's slash-command API (the bot
runs on a bare `discord.Client`). They're prefixed with `!` rather than `/` so typing one
doesn't trigger Discord's native slash-command autocomplete popup:

    !play <search terms | url>   join the caller's voice channel and play (or queue) it
    !skip                        skip the current track
    !stop                        clear the queue and stop playing (stay connected)
    !leave                       stop and disconnect
    !queue                       show what's lined up
    !np                          show the current track
    !pause  !resume

Audio is streamed, never downloaded: yt-dlp resolves a direct media URL and ffmpeg pipes it
into Discord. yt-dlp does blocking network I/O, so every resolve runs through
`asyncio.to_thread` with a timeout - a stalled YouTube request must never freeze the event
loop (the bot has been bitten by exactly that before, see the web tools in tools.py).

State is per-guild (`GuildPlayer`): a queue plus one background task that plays tracks in
order and disconnects after `idle_timeout` seconds with nothing to play.

Requirements: `yt-dlp` + `PyNaCl` + `davey` (pip, see pyproject) and `ffmpeg` + `libopus` (system).
Each is checked at startup and again per command, so a missing piece degrades to a clear
message in chat instead of a traceback.
"""

import asyncio
import logging
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import discord

log = logging.getLogger("kronk")

try:  # optional dependency: without it the feature reports itself unavailable
    import yt_dlp
except ImportError:  # pragma: no cover - depends on the install
    yt_dlp = None

try:  # discord.py needs PyNaCl to encrypt the voice stream
    import nacl  # noqa: F401
    _HAS_NACL = True
except ImportError:  # pragma: no cover
    _HAS_NACL = False

try:  # DAVE = Discord's end-to-end voice encryption, mandatory since 2026-03-02
    import davey  # noqa: F401
    _HAS_DAVEY = True
except ImportError:  # pragma: no cover
    _HAS_DAVEY = False


# === Config (set by configure()) ===

ENABLED = False
FFMPEG_PATH = "ffmpeg"
OPUS_PATH: Optional[str] = None      # explicit libopus path; only needed if autodetect fails
COOKIES_FILE: Optional[str] = None   # yt-dlp cookies file (YouTube sometimes demands one)
VOLUME = 1.0                         # 0.0-2.0, applied via PCMVolumeTransformer
MAX_DURATION_MINUTES = 180           # refuse anything longer (0 = no limit)
MAX_QUEUE = 20                       # per-guild queue cap (excluding the current track)
IDLE_TIMEOUT = 300                   # secs with an empty queue before leaving the channel
RESOLVE_TIMEOUT = 60                 # hard ceiling on a single yt-dlp resolve
STREAM_URL_TTL = 1800                # re-resolve a queued track's media URL after this long
LEAVE_WHEN_ALONE = True              # leave once the last human leaves the voice channel
# Which YouTube client yt-dlp impersonates when resolving a media URL. This matters a LOT:
# YouTube ties the URL it hands out to the requesting client, and ffmpeg (which fetches it
# separately, with its own headers) gets a 403 Forbidden for most of them. yt-dlp's own default
# currently resolves via ANDROID_VR, whose URLs ffmpeg CANNOT fetch - the symptom is a track
# that joins, announces, then vanishes from the queue instantly with no audio and no error.
# Expect to revisit this list whenever YouTube changes things; it is config so it can be fixed
# without a code change.
PLAYER_CLIENTS: list[str] = ["android"]
# A track that "ends" faster than this never really played - see _play().
MIN_PLAYBACK_SECONDS = 1.5
# Ceiling on the voice handshake. Matters most on the LLM tool path: a hanging connect there
# stalls the whole agentic loop and burns the model's request timeout instead of failing fast.
CONNECT_TIMEOUT = 30.0

# ffmpeg flags: -vn drops video; the reconnect flags let it recover from a dropped HTTP
# stream mid-song instead of ending the track early.
FFMPEG_BEFORE_OPTIONS = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn -loglevel warning"

_YDL_BASE_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,       # a link with &list=... plays the one video, not the playlist
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": False,
    "skip_download": True,
    "extract_flat": False,
    "source_address": "0.0.0.0",  # force IPv4; IPv6 egress is often blocked by YouTube
}


# === Chat messages (overridable per persona from config: voice.messages) ===
# Same convention as tools.py's announcements: each key holds a LIST of variants and one is
# picked at random. Placeholders are filled with str.format; missing ones render empty.

DEFAULT_MESSAGES: dict[str, list[str]] = {
    "searching": [
        "🔎 Looking up “{query}” on YouTube…",
        "🔎 One sec, digging up “{query}”…",
    ],
    # The bare {url} on its own line is deliberate: Discord auto-embeds a YouTube link into a
    # player card, which a markdown masked link ([text](url)) would NOT do in a normal message.
    "now_playing": [
        "🎶 Now playing: **{title}** [{duration}] — for {user}\n{url}",
        "🎶 Here we go — **{title}** [{duration}], requested by {user}\n{url}",
    ],
    "queued": [
        "➕ Queued **{title}** [{duration}] — #{position} in line.",
    ],
    "skipped": [
        "⏭️ Skipped **{title}**.",
    ],
    "stopped": [
        "⏹️ Stopped, and the queue is clear.",
    ],
    "left": [
        "👋 Hopped out of the voice channel.",
    ],
    "idle_left": [
        "👋 Nothing left to play — leaving the voice channel.",
    ],
    "paused": ["⏸️ Paused."],
    "resumed": ["▶️ Resumed."],
    "not_in_voice": [
        "You need to be in a voice channel first.",
    ],
    "nothing_playing": [
        "I'm not playing anything right now.",
    ],
    "no_results": [
        "I couldn't find anything on YouTube for “{query}”.",
    ],
    "too_long": [
        "That one's {duration} long — my limit is {limit}.",
    ],
    "queue_full": [
        "The queue is full ({limit} tracks). Let some play out first.",
    ],
    "unavailable": [
        "I can't play audio right now: {reason}",
    ],
    "error": [
        "Something went wrong with that one: {error}",
    ],
    "playback_failed": [
        "⚠️ I couldn't actually play **{title}** — {error}",
    ],
    "youtube_error": [
        "⚠️ YouTube wouldn't give me “{query}” — {error}",
    ],
}

_messages: dict[str, list[str]] = {k: list(v) for k, v in DEFAULT_MESSAGES.items()}


class _SafeDict(dict):
    """dict that renders missing str.format keys as empty, so a template can't KeyError."""
    def __missing__(self, key):
        return ""


def _say(key: str, **fields) -> str:
    """Render one of the (randomly chosen) templates for `key`."""
    templates = _messages.get(key) or DEFAULT_MESSAGES.get(key) or [""]
    try:
        return random.choice(templates).format_map(_SafeDict(fields))
    except Exception:
        return random.choice(DEFAULT_MESSAGES.get(key, [""]))


def configure(cfg: Optional[dict]) -> tuple[bool, str]:
    """Apply the `voice:` config section. Returns (ready, human-readable status).

    `ready` is False when the feature is off or a dependency is missing; the status string is
    printed at startup so the operator knows exactly what's absent.
    """
    global ENABLED, FFMPEG_PATH, OPUS_PATH, COOKIES_FILE, VOLUME, MAX_DURATION_MINUTES
    global MAX_QUEUE, IDLE_TIMEOUT, RESOLVE_TIMEOUT, LEAVE_WHEN_ALONE, _messages
    global PLAYER_CLIENTS

    cfg = cfg or {}
    ENABLED = bool(cfg.get("enabled", True))
    FFMPEG_PATH = cfg.get("ffmpeg_path", "ffmpeg")
    OPUS_PATH = cfg.get("opus_path") or None
    COOKIES_FILE = cfg.get("cookies_file") or None
    VOLUME = float(cfg.get("volume", 1.0))
    MAX_DURATION_MINUTES = int(cfg.get("max_duration_minutes", 180))
    MAX_QUEUE = int(cfg.get("max_queue", 20))
    IDLE_TIMEOUT = int(cfg.get("idle_timeout_seconds", 300))
    RESOLVE_TIMEOUT = int(cfg.get("resolve_timeout", 60))
    PLAYER_CLIENTS = list(cfg.get("player_clients") or ["android"])
    LEAVE_WHEN_ALONE = bool(cfg.get("leave_when_alone", True))

    merged = {k: list(v) for k, v in DEFAULT_MESSAGES.items()}
    for key, templates in (cfg.get("messages") or {}).items():
        if templates is None:
            merged[key] = []
        elif isinstance(templates, str):
            merged[key] = [templates]
        else:
            merged[key] = list(templates)
    _messages = merged

    if not ENABLED:
        return False, "Voice playback disabled in config"
    reason = _unavailable_reason()
    if reason:
        return False, f"Voice playback unavailable: {reason}"
    return True, "Voice playback ready (!play)"


def _unavailable_reason() -> Optional[str]:
    """Why voice playback can't run right now, or None if everything's in place."""
    if not ENABLED:
        return "the voice feature is switched off in my config"
    if yt_dlp is None:
        return "the yt-dlp package isn't installed (`uv sync`)"
    if not _HAS_NACL:
        return "PyNaCl isn't installed, so I can't send voice audio (`uv sync`)"
    if not _HAS_DAVEY:
        # Without davey, discord.py advertises max_dave_protocol_version 0 and Discord closes
        # the voice websocket with 4017 - which otherwise shows up only as a silent retry loop.
        return "the davey package isn't installed, so Discord refuses my voice connection (`uv sync`)"
    if shutil.which(FFMPEG_PATH) is None:
        return f"ffmpeg isn't installed (looked for '{FFMPEG_PATH}')"
    return None


def unavailable_reason() -> Optional[str]:
    """Public wrapper for the capability check, so !help can explain why voice is off."""
    return _unavailable_reason()


# Help text for !help, kept right here next to the regex that implements these commands so the
# two can't drift apart. main.py renders it; see the "standardise the command set" TODO for the
# eventual shared dispatcher.
COMMAND_HELP: list[tuple[str, str]] = [
    ("!play <song or url>", "Join your voice channel and play the top YouTube result"),
    ("!skip", "Skip the current track"),
    ("!stop", "Stop playing and clear the queue"),
    ("!leave", "Stop and leave the voice channel"),
    ("!queue", "Show what's lined up"),
    ("!np", "Show what's playing right now"),
    ("!pause / !resume", "Pause or resume playback"),
]


# libopus ships with ffmpeg's brew formula but isn't always on the loader path that
# discord.py's autodetect searches, so try the usual homebrew/linux locations before giving up.
_OPUS_CANDIDATES = (
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
    "libopus.so.0",
    "opus",
)


def _ensure_opus() -> None:
    """Load libopus if discord.py's autodetect didn't find it. Best effort."""
    if discord.opus.is_loaded():
        return
    for candidate in ([OPUS_PATH] if OPUS_PATH else []) + list(_OPUS_CANDIDATES):
        try:
            discord.opus.load_opus(candidate)
            if discord.opus.is_loaded():
                log.info(f"Loaded libopus from {candidate}")
                return
        except Exception:
            continue
    log.warning("Could not load libopus; voice playback may fail")


# === Track resolution (yt-dlp) ===

@dataclass
class Track:
    title: str
    webpage_url: str
    duration: Optional[int]      # seconds; None for livestreams
    stream_url: str
    uploader: str = ""
    requested_by: str = ""
    resolved_at: float = field(default_factory=time.time)


def _format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "live"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _blocking_resolve(target: str) -> dict:
    """Run yt-dlp for a URL or a search phrase and return the chosen entry's info dict.

    BLOCKING - always call via asyncio.to_thread. A bare phrase is turned into a `ytsearch1:`
    query so the top YouTube hit is used; anything starting with http(s) is passed straight
    through (so direct links, and any other site yt-dlp supports, work too).
    """
    opts = dict(_YDL_BASE_OPTS)
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if PLAYER_CLIENTS:
        opts["extractor_args"] = {"youtube": {"player_client": list(PLAYER_CLIENTS)}}

    query = target if _URL_RE.match(target) else f"ytsearch1:{target}"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)

    if not info:
        raise LookupError("no results")
    if "entries" in info:  # search results / playlist -> take the first usable entry
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise LookupError("no results")
        info = entries[0]
    return info


def _stream_url_from(info: dict) -> str:
    """Pull a direct media URL out of a yt-dlp info dict."""
    if info.get("url"):
        return info["url"]
    # Fallback for formats that were split into video+audio.
    for fmt in info.get("requested_formats") or []:
        if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
            return fmt["url"]
    for fmt in reversed(info.get("formats") or []):
        if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
            return fmt["url"]
    raise LookupError("no playable audio stream")


# Known YouTube failure signatures. Each maps a substring of yt-dlp's error text to a short
# kind (for the log line) and a human explanation (for chat). These are worth distinguishing
# because they need DIFFERENT fixes: a cookies file, waiting out a rate limit, a yt-dlp upgrade,
# or a different `player_clients` setting.
_YT_ERROR_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("sign in to confirm", "bot-check",
     "YouTube wants me to prove I'm not a bot (set `voice.cookies_file` to fix)"),
    ("confirm your age", "age-gated", "that video is age-restricted"),
    ("429", "rate-limited", "YouTube is rate-limiting me - try again in a bit"),
    ("too many requests", "rate-limited", "YouTube is rate-limiting me - try again in a bit"),
    ("http error 403", "forbidden", "YouTube refused the request (403)"),
    ("private video", "unavailable", "that video is private"),
    ("video unavailable", "unavailable", "that video isn't available"),
    ("removed by the uploader", "unavailable", "that video was taken down"),
    ("requested format is not available", "no-format",
     "no playable audio came back (the `voice.player_clients` setting may need changing)"),
    ("unable to extract", "extractor-broken",
     "YouTube changed something yt-dlp can't read yet (yt-dlp probably needs updating)"),
    ("page needs to be reloaded", "extractor-broken",
     "YouTube changed something yt-dlp can't read yet (yt-dlp probably needs updating)"),
)


def classify_youtube_error(exc: Exception) -> tuple[str, str]:
    """Map a yt-dlp exception to (kind, human explanation).

    Anything unrecognised comes back as "unexpected", which is the signal that YouTube changed
    in a way this code hasn't seen before - worth an ERROR in the log channel rather than a
    shrug, because it usually means playback is broken for everyone until someone looks.
    """
    text = str(exc).lower()
    for needle, kind, hint in _YT_ERROR_SIGNATURES:
        if needle in text:
            return kind, hint
    return "unexpected", "YouTube gave me an answer I didn't understand"


async def resolve_track(target: str, requested_by: str = "") -> Track:
    """Resolve a search phrase or URL into a playable Track (off the event loop, bounded)."""
    info = await asyncio.wait_for(
        asyncio.to_thread(_blocking_resolve, target), timeout=RESOLVE_TIMEOUT
    )
    return Track(
        title=info.get("title") or "Unknown title",
        webpage_url=info.get("webpage_url") or info.get("original_url") or target,
        duration=info.get("duration"),
        stream_url=_stream_url_from(info),
        uploader=info.get("uploader") or "",
        requested_by=requested_by,
    )


async def _refresh_stream_url(track: Track) -> None:
    """Re-resolve a track's media URL if it's gone stale sitting in the queue.

    YouTube's direct URLs are time-limited, so a track queued behind a long set could have an
    expired link by the time it plays.
    """
    if time.time() - track.resolved_at < STREAM_URL_TTL:
        return
    info = await asyncio.wait_for(
        asyncio.to_thread(_blocking_resolve, track.webpage_url), timeout=RESOLVE_TIMEOUT
    )
    track.stream_url = _stream_url_from(info)
    track.resolved_at = time.time()


# === Per-guild player ===

class GuildPlayer:
    """Queue + playback loop for one guild.

    One background task (`_run`) owns playback: it pops tracks, starts ffmpeg, and waits for
    the track to end. Commands only ever mutate the queue or call into the voice client, so
    there's a single place where "what plays next" is decided.
    """

    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.queue: list[Track] = []
        self.current: Optional[Track] = None
        self.text_channel: Optional[discord.abc.Messageable] = None
        self._task: Optional[asyncio.Task] = None
        self._wakeup = asyncio.Event()    # set when a track is added
        self._finished = asyncio.Event()  # set (from the ffmpeg thread) when a track ends
        self._skip_requested = False      # so a manual skip isn't misread as a failure
        self._play_error: Optional[Exception] = None

    # --- helpers ---

    @property
    def voice(self) -> Optional[discord.VoiceClient]:
        return self.guild.voice_client

    def is_active(self) -> bool:
        vc = self.voice
        return bool(vc and vc.is_connected())

    async def _announce(self, text: str) -> None:
        if not text or self.text_channel is None:
            return
        try:
            await self.text_channel.send(text)
        except Exception as e:
            log.warning(f"Could not post voice status: {e}")

    # --- connection ---

    async def connect(self, channel: discord.VoiceChannel) -> None:
        """Join (or move to) a voice channel. Raises on permission/connection failure."""
        _ensure_opus()
        perms = channel.permissions_for(channel.guild.me)
        if not perms.connect:
            raise PermissionError(f"I'm not allowed to join {channel.name}")
        if not perms.speak:
            raise PermissionError(f"I'm not allowed to speak in {channel.name}")

        vc = self.voice
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return
        await channel.connect(timeout=30.0, reconnect=True)

    async def disconnect(self) -> None:
        """Stop playback, drop the queue and leave the channel."""
        self.queue.clear()
        self._skip_requested = True   # a deliberate teardown, not a failed track
        task = self._task
        self._task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        vc = self.voice
        if vc:
            try:
                if vc.is_playing() or vc.is_paused():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as e:
                log.warning(f"Voice disconnect failed: {e}")
        self.current = None

    # --- queueing ---

    def enqueue(self, track: Track) -> int:
        """Add a track and return its 1-based position in line (1 = plays next)."""
        self.queue.append(track)
        self._wakeup.set()
        return len(self.queue)

    def start(self) -> None:
        """Make sure the playback loop is running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"voice-{self.guild.id}")

    # --- playback loop ---

    async def _run(self) -> None:
        idle_exit = False
        try:
            while True:
                if not self.is_active():
                    break  # kicked/disconnected externally
                if not self.queue:
                    self._wakeup.clear()
                    try:
                        await asyncio.wait_for(self._wakeup.wait(), timeout=IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        idle_exit = True
                        break
                    continue

                track = self.queue.pop(0)
                self.current = track
                try:
                    await self._play(track)
                except Exception as e:
                    log.warning(f"Playback failed for {track.title!r}: {e}")
                    await self._announce(_say("error", error=str(e)[:200]))
                finally:
                    self.current = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Voice player loop crashed")
        finally:
            await self._wind_down(idle_exit)

    async def _wind_down(self, idle_exit: bool) -> None:
        """Leave the channel - unless a !play landed while we were shutting down.

        The handle is cleared first so disconnect() doesn't cancel the task we're running in.
        The queue re-check closes the narrow race where someone queues a track in the same
        instant the idle timer fires: rather than joining and immediately leaving, we just
        hand the loop over to a fresh task.
        """
        self._task = None
        if idle_exit and not self.queue:
            await self._announce(_say("idle_left"))
        if self.queue and self.is_active():
            self.start()
            return
        await self.disconnect()

    async def _play(self, track: Track) -> None:
        """Stream one track to completion (or until skipped)."""
        try:
            await _refresh_stream_url(track)
        except Exception as e:
            kind, hint = classify_youtube_error(e)
            log.error(f"Could not refresh the stream URL for {track.title!r} [{kind}]: "
                      f"{type(e).__name__}: {str(e)[:300]}")
            await self._announce(_say("playback_failed", title=track.title, error=hint))
            return
        vc = self.voice
        if vc is None or not vc.is_connected():
            log.warning(f"Voice client gone before {track.title!r} could start; dropping it")
            return

        # ffmpeg's stderr goes to a temp FILE, not a PIPE: nothing drains a pipe while the track
        # plays, so a chatty stream could fill the 64KB buffer and wedge ffmpeg mid-song.
        errfile = tempfile.TemporaryFile()
        raw = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG_PATH,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
            stderr=errfile,
        )
        source = discord.PCMVolumeTransformer(raw, volume=VOLUME)
        # Keep our OWN reference to the ffmpeg process. discord.py's AudioPlayer.run() calls the
        # `after` callback and THEN source.cleanup(), which resets `_process` to its MISSING
        # sentinel - and our callback only *schedules* the event set, so by the time this
        # coroutine resumes, reading raw._process would hand back the sentinel (which is not
        # None, so a `is not None` guard doesn't save you: it has no .poll()).
        ffmpeg_proc = getattr(raw, "_process", None)
        if not hasattr(ffmpeg_proc, "poll"):
            ffmpeg_proc = None

        self._finished.clear()
        self._play_error = None
        self._skip_requested = False
        loop = asyncio.get_running_loop()

        def _after(error: Optional[Exception]) -> None:
            # Runs on ffmpeg's thread - hop back to the loop before touching any state.
            self._play_error = error
            loop.call_soon_threadsafe(self._finished.set)

        log.info(f"Playing {track.title!r} ({_format_duration(track.duration)}) in "
                 f"guild {self.guild.id}, requested by {track.requested_by}")
        started = time.monotonic()
        vc.play(source, after=_after)
        await self._announce(_say(
            "now_playing",
            title=track.title,
            duration=_format_duration(track.duration),
            user=track.requested_by,
            url=track.webpage_url,
            uploader=track.uploader,
        ))
        await self._finished.wait()
        elapsed = time.monotonic() - started

        # Diagnostics must never be able to break playback - that is exactly how the
        # '_MissingSentinel has no attribute poll' bug took down every track it reported on.
        try:
            await self._report_playback(track, elapsed, errfile, ffmpeg_proc)
        except Exception:
            log.exception(f"Playback diagnostics failed for {track.title!r} (playback itself "
                          f"was unaffected)")

    async def _report_playback(self, track: Track, elapsed: float, errfile,
                               ffmpeg_proc) -> None:
        """Work out whether the track really played, and say so loudly if it didn't."""
        stderr_text = _read_stderr(errfile)
        returncode = None
        if ffmpeg_proc is not None:
            try:
                returncode = ffmpeg_proc.poll()   # poll() so the process is actually reaped
            except Exception:
                returncode = None

        if self._play_error:
            log.error(f"Audio error on {track.title!r}: {self._play_error}")

        # THE IMPORTANT CHECK. When ffmpeg cannot open the stream (an expired or client-mismatched
        # YouTube URL 403s), it exits immediately and discord.py reports that through `after` as
        # an ordinary end-of-track. Without this, a whole queue silently drains in seconds with no
        # audio, no error, and nothing in the logs - which is exactly how this bug hid.
        # `duration is None` means a livestream, which should never end this fast either.
        expected_to_run = track.duration is None or track.duration > MIN_PLAYBACK_SECONDS
        if not self._skip_requested and expected_to_run and elapsed < MIN_PLAYBACK_SECONDS:
            detail = _first_error_line(stderr_text) or f"ffmpeg exited with code {returncode}"
            log.error(
                f"NO AUDIO for {track.title!r}: ffmpeg ended after {elapsed:.2f}s "
                f"(rc={returncode}). {detail} | url={track.webpage_url}"
            )
            await self._announce(_say("playback_failed", title=track.title, error=detail))
        elif stderr_text:
            log.warning(f"ffmpeg warnings on {track.title!r}: {_first_error_line(stderr_text)}")
        else:
            log.info(f"Finished {track.title!r} after {elapsed:.0f}s")


def _read_stderr(errfile) -> str:
    """Read back whatever ffmpeg wrote to its stderr file, then close it. Never raises."""
    try:
        errfile.seek(0)
        return errfile.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""
    finally:
        try:
            errfile.close()
        except Exception:
            pass


def _first_error_line(stderr_text: str) -> str:
    """Pick the most useful line out of an ffmpeg stderr dump for a chat/log message."""
    if not stderr_text:
        return ""
    # Strip ffmpeg's "[https @ 0x7f...]" component/address prefix - pure noise in a chat message.
    lines = [re.sub(r"^\[[^\]]*@\s*0x[0-9a-f]+\]\s*", "", ln.strip())
             for ln in stderr_text.splitlines() if ln.strip()]
    # Prefer a line that names the actual failure over ffmpeg's banner noise.
    for line in lines:
        if any(marker in line for marker in ("403", "404", "Error opening", "Forbidden",
                                             "Invalid data", "Server returned", "No such")):
            return line[:250]
    return lines[0][:250] if lines else ""


_players: dict[int, GuildPlayer] = {}


def _player_for(guild: discord.Guild) -> GuildPlayer:
    player = _players.get(guild.id)
    if player is None or player.guild.id != guild.id:
        player = GuildPlayer(guild)
        _players[guild.id] = player
    return player


# === Commands ===

_CMD_RE = re.compile(
    r"^!(play|skip|stop|leave|disconnect|queue|np|nowplaying|pause|resume)\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


async def handle_command(message: discord.Message) -> bool:
    """If the message is a voice command, handle it and return True; otherwise False.

    Called early in on_message (like discord_logging.handle_command) so command messages
    never reach the LLM.
    """
    match = _CMD_RE.match((message.content or "").strip())
    if not match:
        return False

    command = match.group(1).lower()
    argument = match.group(2).strip()

    if message.guild is None:
        await message.reply("That command only works in a server.")
        return True

    reason = _unavailable_reason()
    if reason:
        await message.reply(_say("unavailable", reason=reason))
        return True

    player = _player_for(message.guild)
    player.text_channel = message.channel

    if command == "play":
        await _cmd_play(message, argument)
    elif command == "skip":
        await _cmd_skip(message, player)
    elif command == "stop":
        await _cmd_stop(message, player)
    elif command in ("leave", "disconnect"):
        await _cmd_leave(message, player)
    elif command == "queue":
        await _cmd_queue(message, player)
    elif command in ("np", "nowplaying"):
        await _cmd_nowplaying(message, player)
    elif command == "pause":
        await _cmd_pause(message, player)
    elif command == "resume":
        await _cmd_resume(message, player)
    return True


def _author_voice_channel(message: discord.Message) -> Optional[discord.VoiceChannel]:
    state = getattr(message.author, "voice", None)
    return state.channel if state else None


@dataclass
class PlayResult:
    """Outcome of a play/queue request."""
    ok: bool
    message: str           # human-readable line: safe to post in chat OR hand back to the model
    started: bool = False  # went straight to playing (the player loop announces that itself)
    title: str = ""
    url: str = ""          # the YouTube page URL, so callers can link the track
    position: int = 0


def play_precheck(message: discord.Message) -> Optional[str]:
    """Cheap checks to run before spending a yt-dlp lookup. Returns an error line, or None."""
    reason = _unavailable_reason()
    if reason:
        return _say("unavailable", reason=reason)
    if message.guild is None:
        return "That only works in a server."
    if _author_voice_channel(message) is None:
        return _say("not_in_voice")
    if len(_player_for(message.guild).queue) >= MAX_QUEUE:
        return _say("queue_full", limit=MAX_QUEUE)
    return None


async def enqueue_request(message: discord.Message, query: str,
                          source: str = "command") -> PlayResult:
    """Resolve `query`, join the requester's voice channel and queue it.

    This is the ONE path used by both `!play` and the LLM's queue_song tool, so the two can't
    drift apart on the things that matter: duration limits, the queue cap, channel permissions
    and stale-stream-URL handling. Callers differ only in how they report the result.

    `source` ("command" or "tool") only tags the log lines - when one route works and the other
    doesn't, the logs need to say which one was running.
    """
    author = getattr(message.author, "display_name", "?")
    guild_id = getattr(message.guild, "id", None)
    log.info(f"[{source}] play request {query!r} from {author} in guild {guild_id}")

    error = play_precheck(message)
    if error:
        # WARNING (not debug) on purpose: this reaches the Discord log channel, and "the bot
        # just didn't join" is otherwise invisible - the model paraphrases the refusal away.
        state = getattr(message.author, "voice", None)
        log.warning(
            f"[{source}] request rejected before lookup: {error!r} "
            f"(author={author}, voice_state={'present' if state else 'None'}, "
            f"channel={getattr(getattr(state, 'channel', None), 'id', None)}, "
            f"guild={guild_id})"
        )
        return PlayResult(False, error)

    channel = _author_voice_channel(message)
    player = _player_for(message.guild)
    player.text_channel = message.channel

    try:
        track = await resolve_track(query, requested_by=message.author.display_name)
        log.info(f"Resolved {query!r} -> {track.title!r} ({_format_duration(track.duration)})")
    except (asyncio.TimeoutError, TimeoutError):
        log.error(f"YouTube did not answer within {RESOLVE_TIMEOUT}s for {query!r}")
        return PlayResult(False, f"YouTube took too long to answer (>{RESOLVE_TIMEOUT}s).")
    except LookupError:
        log.info(f"No YouTube results for {query!r}")   # a normal outcome, not a fault
        return PlayResult(False, _say("no_results", query=query))
    except Exception as e:
        # Anything else means YouTube/yt-dlp behaved unexpectedly. ERROR so it reaches the
        # Discord log channel: these break playback for everyone until someone intervenes.
        kind, hint = classify_youtube_error(e)
        log.error(f"Unexpected YouTube response resolving {query!r} [{kind}]: "
                  f"{type(e).__name__}: {str(e)[:400]}")
        return PlayResult(False, _say("youtube_error", query=query, error=hint))

    limit_seconds = MAX_DURATION_MINUTES * 60
    if limit_seconds and track.duration and track.duration > limit_seconds:
        return PlayResult(False, _say(
            "too_long",
            duration=_format_duration(track.duration),
            limit=_format_duration(limit_seconds),
            title=track.title,
        ))

    already_in = player.is_active()
    log.info(f"[{source}] {'already connected' if already_in else 'joining'} voice channel "
             f"#{channel.name} ({channel.id})")
    try:
        await asyncio.wait_for(player.connect(channel), timeout=CONNECT_TIMEOUT)
    except PermissionError as e:
        log.warning(f"[{source}] missing voice permissions for #{channel.name}: {e}")
        return PlayResult(False, str(e))
    except (asyncio.TimeoutError, TimeoutError):
        log.error(f"[{source}] voice handshake to #{channel.name} ({channel.id}) timed out "
                  f"after {CONNECT_TIMEOUT:.0f}s - not joining")
        return PlayResult(False, f"I couldn't get into {channel.name} - the voice connection "
                                 f"timed out.")
    except Exception as e:
        log.exception(f"[{source}] failed to join voice channel #{channel.name} "
                      f"({channel.id}): {type(e).__name__}: {e}")
        return PlayResult(False, f"I couldn't join {channel.name}: {e}")
    if not already_in:
        log.info(f"[{source}] connected to #{channel.name}; voice_client="
                 f"{'up' if player.is_active() else 'DOWN (unexpected)'}")

    position = player.enqueue(track)
    player.start()
    log.info(f"[{source}] queued {track.title!r} at position {position} "
             f"(currently playing: {player.current.title if player.current else 'nothing'})")

    # Position 1 with nothing playing means the player loop is about to post its own
    # "now playing" line, so the caller must not repeat it.
    if position == 1 and player.current is None:
        return PlayResult(True, f"Now playing: {track.title}", started=True,
                          title=track.title, url=track.webpage_url, position=position)
    return PlayResult(True, _say(
        "queued",
        title=track.title,
        duration=_format_duration(track.duration),
        position=position,
        url=track.webpage_url,
        uploader=track.uploader,
    ), title=track.title, url=track.webpage_url, position=position)


async def _delete_quietly(msg) -> None:
    """Delete a message, ignoring any failure (already gone / no permission)."""
    if msg is None:
        return
    try:
        await msg.delete()
    except Exception:
        pass


async def _cmd_play(message: discord.Message, query: str) -> None:
    if not query:
        await message.reply("Give me something to play: `!play <song name or youtube link>`")
        return

    # Check the cheap things before posting "searching…", so an error doesn't flash a
    # placeholder first.
    error = play_precheck(message)
    if error:
        await message.reply(error)
        return

    status = None
    try:
        status = await message.channel.send(_say("searching", query=query))
    except Exception:
        pass

    result = await enqueue_request(message, query, source="command")
    if result.started:
        await _delete_quietly(status)   # the player loop posts "now playing" instead
    else:
        await _edit_or_send(status, message, result.message)


async def _edit_or_send(status, message: discord.Message, text: str) -> None:
    """Update the '🔎 searching…' placeholder if we still have it, else just reply."""
    if status is not None:
        try:
            await status.edit(content=text)
            return
        except Exception:
            pass
    await message.reply(text)


async def _cmd_skip(message: discord.Message, player: GuildPlayer) -> None:
    vc = player.voice
    if not player.is_active() or player.current is None or vc is None:
        await message.reply(_say("nothing_playing"))
        return
    title = player.current.title
    player._skip_requested = True
    vc.stop()  # fires the `after` callback -> the loop moves to the next track
    await message.reply(_say("skipped", title=title))


async def _cmd_stop(message: discord.Message, player: GuildPlayer) -> None:
    vc = player.voice
    if not player.is_active():
        await message.reply(_say("nothing_playing"))
        return
    player.queue.clear()
    player._skip_requested = True
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
    await message.reply(_say("stopped"))


async def _cmd_leave(message: discord.Message, player: GuildPlayer) -> None:
    if not player.is_active():
        await message.reply("I'm not in a voice channel.")
        return
    await player.disconnect()
    await message.reply(_say("left"))


async def _cmd_queue(message: discord.Message, player: GuildPlayer) -> None:
    lines = []
    if player.current:
        lines.append(f"▶️ **{player.current.title}** [{_format_duration(player.current.duration)}]")
    for i, track in enumerate(player.queue[:10], start=1):
        lines.append(f"{i}. {track.title} [{_format_duration(track.duration)}] — {track.requested_by}")
    if len(player.queue) > 10:
        lines.append(f"…and {len(player.queue) - 10} more.")
    if not lines:
        await message.reply(_say("nothing_playing"))
        return
    await message.reply("\n".join(lines)[:1900])


async def _cmd_nowplaying(message: discord.Message, player: GuildPlayer) -> None:
    track = player.current
    if track is None:
        await message.reply(_say("nothing_playing"))
        return
    await message.reply(
        f"🎶 **{track.title}** [{_format_duration(track.duration)}] "
        f"— requested by {track.requested_by}\n{track.webpage_url}"
    )


async def _cmd_pause(message: discord.Message, player: GuildPlayer) -> None:
    vc = player.voice
    if vc is None or not vc.is_playing():
        await message.reply(_say("nothing_playing"))
        return
    vc.pause()
    await message.reply(_say("paused"))


async def _cmd_resume(message: discord.Message, player: GuildPlayer) -> None:
    vc = player.voice
    if vc is None or not vc.is_paused():
        await message.reply("Nothing's paused.")
        return
    vc.resume()
    await message.reply(_say("resumed"))


# === Events ===

async def handle_voice_state_update(client: discord.Client, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
    """Leave when the last human leaves our channel, and clean up if we get disconnected."""
    guild = member.guild
    player = _players.get(guild.id)
    if player is None:
        return

    # We were moved out / kicked from voice: tear the player down.
    if member.id == client.user.id and after.channel is None:
        await player.disconnect()
        return

    if not LEAVE_WHEN_ALONE or not player.is_active():
        return
    channel = player.voice.channel
    if before.channel != channel or after.channel == channel:
        return  # not someone leaving our channel
    if not any(not m.bot for m in channel.members):
        await player.disconnect()


async def shutdown() -> None:
    """Disconnect every guild player (used when the bot is closing)."""
    for player in list(_players.values()):
        await player.disconnect()
    _players.clear()

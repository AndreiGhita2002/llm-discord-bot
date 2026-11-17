"""
Spotify links -> YouTube tracks.

Spotify audio can't be streamed: it is DRM-protected, yt-dlp doesn't support it, and the Web
API hands out metadata only. So a Spotify link is read for WHAT to play (title, artists,
duration) and the audio comes from a matching YouTube upload - the same approach every
Discord music bot uses.

**Where the metadata comes from**: Spotify's public embed page
(`open.spotify.com/embed/{track|album|playlist}/<id>`), which carries the entity as JSON in
its `__NEXT_DATA__` script tag. No key, no account. The official Web API is deliberately NOT
used: since the February 2026 Development Mode changes it needs a Premium account, and it
omits the track list of any playlist you don't own - which is every playlist someone pastes.
The embed is undocumented, so `fetch()` raises `SpotifyError` with a readable reason if its
shape ever changes, rather than queueing garbage. Track lists are capped at 100 by the embed.

**Matching**: `pick_match()` scores YouTube search results against the Spotify track. The
duration is the strongest signal - an upload within a couple of seconds of Spotify's length
is almost always the studio recording, while official music videos run long with intros.
Results whose titles add words like "live", "remix" or "slowed" that the Spotify title
doesn't have are pushed down. Everything here is pure (no network) except `fetch()`, so the
matcher is unit-tested in evals/test_spotify.py.
"""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

FETCH_TIMEOUT = 15   # seconds per HTTP request
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# open.spotify.com/track/<id>, with optional /intl-xx/ locale and /embed/ prefixes and any
# query string (?si=...); or a spotify:track:<id> URI.
_LINK_RE = re.compile(
    r"^(?:https?://(?:open|play)\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?"
    r"(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]{22})"
    r"|spotify:(?P<ukind>track|album|playlist):(?P<uid>[A-Za-z0-9]{22}))",
    re.IGNORECASE,
)
# Share-sheet short links (spotify.link/<code>, and the older spoti.fi) redirect to the above.
_SHORT_RE = re.compile(r"^https?://(?:spotify\.link|spoti\.fi)/\w+", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


class SpotifyError(Exception):
    """A Spotify link that couldn't be read. str() is safe to show in chat."""


@dataclass
class SpotifyTrack:
    title: str
    artists: list[str]
    duration: Optional[int]   # seconds
    uri: str = ""

    @property
    def artist_line(self) -> str:
        return ", ".join(self.artists)

    @property
    def display(self) -> str:
        """How the track is named in the queue and in chat."""
        return f"{self.artist_line} - {self.title}" if self.artists else self.title

    @property
    def query(self) -> str:
        """The YouTube search for this track. Only the lead artist: a featured-artist list
        mostly adds noise to the search."""
        return f"{self.artists[0]} - {self.title}" if self.artists else self.title


@dataclass
class SpotifyItem:
    kind: str                 # "track" | "album" | "playlist"
    name: str
    tracks: list[SpotifyTrack] = field(default_factory=list)
    unplayable: int = 0       # entries Spotify itself marks unplayable, or with no title


def is_spotify_link(target: str) -> bool:
    target = target.strip()
    return bool(_LINK_RE.match(target) or _SHORT_RE.match(target))


def parse_link(target: str) -> Optional[tuple[str, str]]:
    """(kind, id) for a full Spotify link or URI, else None. Short links need fetch()."""
    m = _LINK_RE.match(target.strip())
    if not m:
        return None
    kind = m.group("kind") or m.group("ukind")
    return kind.lower(), m.group("id") or m.group("uid")


def _http_get(url: str) -> tuple[str, str]:
    """GET a URL following redirects. Returns (final url, body). BLOCKING."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT,
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.geturl(), resp.read().decode("utf-8", "replace")


def _expand_short_link(url: str) -> tuple[str, str]:
    """Follow a spotify.link short link to the (kind, id) it points at."""
    final_url, body = _http_get(url)
    found = parse_link(final_url)
    if found:
        return found
    # Some short links land on an interstitial page instead of redirecting outright; the
    # real link is in its markup.
    m = re.search(r"https://open\.spotify\.com/(?:intl-[\w-]+/)?(?:track|album|playlist)/"
                  r"[A-Za-z0-9]{22}", body)
    if m:
        return parse_link(m.group(0))
    raise SpotifyError("that Spotify short link didn't lead to a track, album or playlist")


def _ms_to_s(value) -> Optional[int]:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    return round(ms / 1000) if ms > 0 else None


def _split_artists(subtitle: str) -> list[str]:
    """Track-list entries carry their artists as one string: "KAROL G,\\xa0Judeline"."""
    return [a.strip() for a in (subtitle or "").replace("\xa0", " ").split(",") if a.strip()]


def parse_embed(html: str, kind: str) -> SpotifyItem:
    """Read the entity out of an embed page. Pure - the unit tests feed it synthetic pages."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise SpotifyError("Spotify's page didn't have the data I read tracks from "
                           "(its format may have changed)")
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
    except (ValueError, KeyError, TypeError):
        raise SpotifyError("Spotify's page data was unreadable (its format may have changed)")

    if props.get("status") == 404:
        raise SpotifyError(f"that Spotify {kind} doesn't exist (or isn't public)")
    entity = ((props.get("state") or {}).get("data") or {}).get("entity")
    if not isinstance(entity, dict):
        raise SpotifyError("Spotify's page data was missing the track info "
                           "(its format may have changed)")

    name = entity.get("name") or entity.get("title") or f"that Spotify {kind}"
    if kind == "track":
        artists = [a.get("name", "") for a in entity.get("artists") or [] if a.get("name")]
        return SpotifyItem("track", name, [SpotifyTrack(
            title=name, artists=artists, duration=_ms_to_s(entity.get("duration")),
            uri=entity.get("uri") or "",
        )])

    item = SpotifyItem(kind, name)
    for entry in entity.get("trackList") or []:
        title = (entry.get("title") or "").strip()
        if not title or entry.get("isPlayable") is False:
            item.unplayable += 1
            continue
        item.tracks.append(SpotifyTrack(
            title=title, artists=_split_artists(entry.get("subtitle", "")),
            duration=_ms_to_s(entry.get("duration")), uri=entry.get("uri") or "",
        ))
    return item


def fetch(target: str) -> SpotifyItem:
    """Read a Spotify track/album/playlist link. BLOCKING - call via asyncio.to_thread.

    Raises SpotifyError (chat-safe message) for anything that isn't a readable public item,
    network failures included - so they're never reported as a YouTube problem.
    """
    target = target.strip()
    try:
        parsed = parse_link(target)
        if parsed is None and _SHORT_RE.match(target):
            parsed = _expand_short_link(target)
        if parsed is None:
            raise SpotifyError("that isn't a Spotify track, album or playlist link")
        kind, item_id = parsed
        _, html = _http_get(f"https://open.spotify.com/embed/{kind}/{item_id}")
    except urllib.error.HTTPError as e:
        raise SpotifyError(f"Spotify answered with an error (HTTP {e.code})") from e
    except (urllib.error.URLError, OSError) as e:   # OSError covers socket timeouts
        raise SpotifyError("I couldn't reach Spotify just now") from e
    return parse_embed(html, kind)


# === Matching a Spotify track to a YouTube search result ===

# Words that mark a DIFFERENT recording or an edit of it. Penalised only when the YouTube title
# has one and the Spotify title doesn't - so a Spotify "Live at Wembley" track still matches
# a live upload.
_VARIANT_RE = re.compile(
    r"\b(live|remix|cover|karaoke|instrumental|slowed|reverb|sped up|speed up|nightcore|8d|"
    r"acoustic|demo|extended|mashup|reaction|tutorial|lesson|loop|1 hour|10 hours)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Lowercase alphanumerics only, for loose containment checks."""
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def score_candidate(track: SpotifyTrack, entry: dict, rank: int) -> float:
    """Higher is a better match. `entry` is a flat yt-dlp search result."""
    title = entry.get("title") or ""
    channel = entry.get("channel") or entry.get("uploader") or ""
    score = -0.5 * rank   # YouTube's own relevance ordering breaks ties

    duration = entry.get("duration")
    if track.duration and duration:
        delta = abs(duration - track.duration)
        if delta <= 2:
            score += 20
        elif delta <= 5:
            score += 12
        elif delta <= 15:
            score += 4
        else:
            score -= min(delta, 120) / 10
    elif track.duration and not duration:
        score -= 10   # a livestream (or unknown length) is never the studio track

    expected = {w.lower() for w in _VARIANT_RE.findall(track.title)}
    for word in {w.lower() for w in _VARIANT_RE.findall(title)}:
        if word not in expected:
            score -= 25

    if _norm(track.title) and _norm(track.title) not in _norm(title):
        score -= 10
    lead = _norm(track.artists[0]) if track.artists else ""
    if channel.endswith(" - Topic") or (lead and lead in _norm(channel)):
        score += 4
    return score


def pick_match(track: SpotifyTrack, entries: list[dict]) -> Optional[dict]:
    """The best YouTube search result for `track`, or None if there were no results."""
    entries = [e for e in entries if e]
    if not entries:
        return None
    return max(enumerate(entries), key=lambda ie: score_candidate(track, ie[1], ie[0]))[1]

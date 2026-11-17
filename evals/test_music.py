#!/usr/bin/env python3
"""
Tests for Spotify links, the !search picker and the over-length fallback. No network, no
Discord, no yt-dlp calls:

    uv run python evals/test_music.py

Spotify's embed page and YouTube's search results are replaced with canned data shaped like
the real responses (captured 2026-11-17). The matcher cases use the actual top-5 results
YouTube returned for those tracks on that date, so they check the scoring against what the
bot really sees, not an idealised list.
"""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import spotify  # noqa: E402
import voice  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def embed_page(page_props: dict) -> str:
    data = {"props": {"pageProps": page_props}}
    return (f'<html><head></head><body><script id="__NEXT_DATA__" type="application/json">'
            f'{json.dumps(data)}</script></body></html>')


def entity_page(entity: dict) -> str:
    return embed_page({"state": {"data": {"entity": entity}}})


def yt(title, duration, channel, vid="x"):
    return {"id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "ie_key": "Youtube",
            "title": title, "duration": duration, "channel": channel}


# === Link parsing ===

@check
async def spotify_links_are_recognised_in_every_shape():
    tid = "003vvx7Niy0yvhvHt4a68B"
    for link, kind in [
        (f"https://open.spotify.com/track/{tid}", "track"),
        (f"https://open.spotify.com/track/{tid}?si=abc123", "track"),
        (f"https://open.spotify.com/intl-de/track/{tid}", "track"),
        (f"https://open.spotify.com/embed/playlist/{tid}", "playlist"),
        (f"https://open.spotify.com/album/{tid}", "album"),
        (f"spotify:track:{tid}", "track"),
    ]:
        assert spotify.is_spotify_link(link), link
        assert spotify.parse_link(link) == (kind, tid), (link, spotify.parse_link(link))
    assert spotify.is_spotify_link("https://spotify.link/AbCdEf123")
    assert spotify.parse_link("https://spotify.link/AbCdEf123") is None   # needs a fetch


@check
async def other_links_and_phrases_are_not_spotify():
    for text in ["https://www.youtube.com/watch?v=abc", "mr brightside",
                 "https://open.spotify.com/artist/003vvx7Niy0yvhvHt4a68B",   # not playable
                 "https://open.spotify.com/track/short"]:
        assert not spotify.is_spotify_link(text), text


# === Embed parsing ===

@check
async def a_track_embed_gives_title_artists_and_duration():
    item = spotify.parse_embed(entity_page({
        "name": "Mr. Brightside", "title": "Mr. Brightside", "duration": 222973,
        "artists": [{"name": "The Killers"}], "uri": "spotify:track:003vvx7Niy0yvhvHt4a68B",
    }), "track")
    assert item.kind == "track" and len(item.tracks) == 1
    t = item.tracks[0]
    assert (t.title, t.artists, t.duration) == ("Mr. Brightside", ["The Killers"], 223)
    assert t.query == "The Killers - Mr. Brightside"
    assert t.display == "The Killers - Mr. Brightside"


@check
async def a_playlist_embed_splits_artists_and_drops_unplayable_entries():
    item = spotify.parse_embed(entity_page({
        "name": "Today's Top Hits", "trackList": [
            {"title": "BbY WOW", "subtitle": "KAROL G,\xa0Judeline,\xa0rusowsky",
             "duration": 225834, "isPlayable": True},
            {"title": "Gone", "subtitle": "Nobody", "duration": 1000, "isPlayable": False},
            {"title": "", "subtitle": "Nobody", "duration": 1000},
            {"title": "Housewife", "subtitle": "Tate McRae", "duration": 208810},
        ],
    }), "playlist")
    assert item.name == "Today's Top Hits"
    assert [t.title for t in item.tracks] == ["BbY WOW", "Housewife"]
    assert item.tracks[0].artists == ["KAROL G", "Judeline", "rusowsky"]
    assert item.tracks[0].query == "KAROL G - BbY WOW"   # lead artist only
    assert item.unplayable == 2


@check
async def a_missing_or_changed_page_is_a_readable_error_not_a_crash():
    cases = [
        (embed_page({"status": 404, "title": "Page not found"}), "doesn't exist"),
        ("<html>no data here</html>", "format may have changed"),
        (embed_page({"state": {}}), "format may have changed"),
    ]
    for html, expected in cases:
        try:
            spotify.parse_embed(html, "track")
        except spotify.SpotifyError as e:
            assert expected in str(e), str(e)
        else:
            raise AssertionError(f"no SpotifyError for {html[:60]!r}")


# === Matching (real YouTube results, 2026-11-17) ===

def sp(title, artist, seconds):
    return spotify.SpotifyTrack(title=title, artists=[artist], duration=seconds)


@check
async def matcher_prefers_the_studio_length_over_the_music_video():
    pick = spotify.pick_match(sp("Mr. Brightside", "The Killers", 223), [
        yt("Mr. Brightside", 223, "TheKillersMusic", "a"),
        yt("Mr. Brightside HQ (The Killers)", 224, "Dank Music Channel", "b"),
        yt("The Killers - Mr. Brightside (Official Music Video)", 228, "TheKillersMusic", "c"),
        yt("The Killers - Mr Brightside (Lyrics)", 224, "Lost Panda", "d"),
        yt("Mr. Brightside (Original Demo)", 263, "TheKillersMusic", "e"),
    ])
    assert pick["id"] == "a", pick


@check
async def matcher_skips_a_long_official_video_for_an_exact_length_upload():
    pick = spotify.pick_match(sp("Stop The Wedding!", "Ashe", 199), [
        yt("Ashe - Stop The Wedding! (Official Video)", 209, "Ashe", "video"),
        yt("Ashe - Stop The Wedding!", 200, "LatinHype", "audio"),
        yt("Ashe - Stop The Wedding! (Lyrics)", 199, "MAYELEVRSE", "lyrics"),
        yt("Ashe - Stop The Wedding! (Live In-Studio)", 202, "Ashe", "live"),
    ])
    assert pick["id"] in ("audio", "lyrics"), pick


@check
async def matcher_avoids_variants_the_spotify_title_does_not_have():
    pick = spotify.pick_match(sp("Housewife", "Tate McRae", 209), [
        yt("Housewife (Slowed + Reverb)", 209, "SlowedVibes", "slowed"),
        yt("Tate McRae - Housewife (Live)", 209, "Tate McRae", "live"),
        yt("Tate McRae - Housewife (Lyrics)", 209, "7clouds", "real"),
    ])
    assert pick["id"] == "real", pick
    # ...but a Spotify track that IS live matches a live upload.
    pick = spotify.pick_match(sp("Housewife - Live", "Tate McRae", 209), [
        yt("Tate McRae - Housewife (Lyrics)", 190, "7clouds", "studio"),
        yt("Tate McRae - Housewife (Live)", 209, "Tate McRae", "live"),
    ])
    assert pick["id"] == "live", pick


@check
async def matcher_returns_none_only_without_results():
    assert spotify.pick_match(sp("x", "y", 100), []) is None
    assert spotify.pick_match(sp("x", "y", 100), [yt("totally different", 999, "z")]) is not None


# === Over-length fallback for !play ===

class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records the full resolve, serves canned search hits."""
    search_hits: list = []
    resolved: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, target, download=False):
        if target.startswith("ytsearch"):
            return {"entries": list(FakeYDL.search_hits)}
        FakeYDL.resolved.append(target)
        return {"title": target, "url": "media://" + target, "duration": 200,
                "webpage_url": target}


def with_fake_ytdlp(hits):
    FakeYDL.search_hits, FakeYDL.resolved = hits, []
    voice.yt_dlp = SimpleNamespace(YoutubeDL=FakeYDL)


@check
async def play_skips_a_top_hit_over_the_length_limit():
    voice.MAX_DURATION_MINUTES = 60
    with_fake_ytdlp([yt("Song 10 hour loop", 36000, "Loops", "loop"),
                     yt("Song", 200, "Artist", "song")])
    voice._blocking_resolve("song")
    assert FakeYDL.resolved == ["https://www.youtube.com/watch?v=song"], FakeYDL.resolved


@check
async def play_takes_the_top_hit_when_it_fits():
    voice.MAX_DURATION_MINUTES = 60
    with_fake_ytdlp([yt("Song", 200, "Artist", "first"), yt("Song again", 210, "B", "second")])
    voice._blocking_resolve("song")
    assert FakeYDL.resolved == ["https://www.youtube.com/watch?v=first"]


@check
async def play_falls_back_to_the_top_hit_when_nothing_fits_so_it_is_refused_normally():
    voice.MAX_DURATION_MINUTES = 60
    with_fake_ytdlp([yt("Loop A", 36000, "L", "a"), yt("Loop B", 40000, "L", "b")])
    voice._blocking_resolve("song")
    assert FakeYDL.resolved == ["https://www.youtube.com/watch?v=a"]


@check
async def links_are_resolved_directly_without_a_search():
    with_fake_ytdlp([])
    voice._blocking_resolve("https://www.youtube.com/watch?v=direct")
    assert FakeYDL.resolved == ["https://www.youtube.com/watch?v=direct"]


# === Spotify -> queued tracks ===

def fake_spotify(item):
    spotify_fetch = spotify.fetch
    spotify.fetch = lambda target: item
    return spotify_fetch


@check
async def a_spotify_track_is_matched_and_resolved_immediately():
    voice.MAX_DURATION_MINUTES = 60
    with_fake_ytdlp([yt("The Killers - Mr. Brightside (Official Music Video)", 228, "K", "mv"),
                     yt("Mr. Brightside", 223, "TheKillersMusic", "audio")])
    real = fake_spotify(spotify.SpotifyItem("track", "Mr. Brightside",
                                            [sp("Mr. Brightside", "The Killers", 223)]))
    try:
        resolved = await voice.resolve_request("https://open.spotify.com/track/"
                                               "003vvx7Niy0yvhvHt4a68B", "andrei")
    finally:
        spotify.fetch = real
    assert resolved.playlist is None
    track = resolved.tracks[0]
    assert track.webpage_url == "https://www.youtube.com/watch?v=audio", track.webpage_url
    assert track.stream_url and track.title == "The Killers - Mr. Brightside"


@check
async def a_spotify_playlist_queues_lazily_and_matches_on_play():
    with_fake_ytdlp([yt("Housewife", 209, "Tate McRae - Topic", "hw")])
    real = fake_spotify(spotify.SpotifyItem(
        "playlist", "Hits", [sp("Housewife", "Tate McRae", 209), sp("Other", "Someone", 180)],
        unplayable=1))
    try:
        resolved = await voice.resolve_request("https://open.spotify.com/playlist/"
                                               "37i9dQZF1DXcBWIGoYBM5M", "andrei")
    finally:
        spotify.fetch = real
    assert resolved.playlist == "Hits" and resolved.dead == 1
    assert [t.webpage_url for t in resolved.tracks] == ["", ""]   # no searches up front
    assert FakeYDL.resolved == []
    await voice._refresh_stream_url(resolved.tracks[0])   # what the player does at its turn
    assert resolved.tracks[0].webpage_url == "https://www.youtube.com/watch?v=hw"
    assert resolved.tracks[0].stream_url


# === !search picker ===

class FakeResponse:
    def __init__(self):
        self.edits, self.deferred, self.sent = [], 0, []

    async def edit_message(self, **kw):
        self.edits.append(kw)

    async def defer(self):
        self.deferred += 1

    async def send_message(self, text, **kw):
        self.sent.append((text, kw))


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, text, **kw):
        self.sent.append(text)


def interaction(user_id=1):
    return SimpleNamespace(user=SimpleNamespace(id=user_id, display_name="andrei"),
                           guild=SimpleNamespace(id=9), channel=SimpleNamespace(id=5),
                           response=FakeResponse(), followup=FakeFollowup())


def picker(entries):
    return voice.SearchPicker("song", entries, requester_id=1, source="search")


async def with_fake_enqueue(fn):
    calls = []

    async def fake_enqueue(message, query, source="command"):
        calls.append((message, query, source))
        return voice.PlayResult(True, "➕ Queued it", position=2)

    real, voice.enqueue_request = voice.enqueue_request, fake_enqueue
    try:
        await fn()
    finally:
        voice.enqueue_request = real
    return calls


@check
async def picking_a_result_queues_it_through_enqueue_request_once():
    voice.MAX_DURATION_MINUTES = 60
    view = picker([yt("A", 100, "c", "a"), yt("B", 100, "c", "b")])
    first, second = interaction(), interaction()

    async def clicks():
        await view._pick(first, 1)
        await view._pick(second, 0)   # a double click must not queue a second track

    calls = await with_fake_enqueue(clicks)
    assert len(calls) == 1, calls
    requester, query, source = calls[0]
    assert query == "https://www.youtube.com/watch?v=b" and source == "search"
    assert requester.author is first.user and requester.guild is first.guild
    assert first.response.edits and first.response.edits[0]["view"] is None
    assert first.followup.sent == ["➕ Queued it"]
    assert second.response.deferred == 1


@check
async def only_the_searcher_can_pick():
    view = picker([yt("A", 100, "c", "a")])
    stranger = interaction(user_id=2)
    assert await view.interaction_check(stranger) is False
    assert stranger.response.sent and stranger.response.sent[0][1].get("ephemeral")
    assert await view.interaction_check(interaction(user_id=1)) is True


@check
async def over_long_results_get_a_disabled_button():
    voice.MAX_DURATION_MINUTES = 60
    view = picker([yt("Short", 100, "c", "a"), yt("Loop", 36000, "c", "b")])
    buttons = [c for c in view.children if c.label in ("1", "2")]
    assert [b.disabled for b in buttons] == [False, True]
    assert "too long" in view.render().splitlines()[2]


@check
async def search_rejects_links():
    voice.ENABLED = True
    real_reason, real_target = voice._unavailable_reason, voice._target_channel
    voice._unavailable_reason = lambda: None
    voice._target_channel = lambda m: (SimpleNamespace(name="vc"), None)
    try:
        msg = SimpleNamespace(guild=SimpleNamespace(id=777), author=SimpleNamespace(id=1))
        offer = await voice.offer_search(msg, "https://youtu.be/abc")
    finally:
        voice._unavailable_reason, voice._target_channel = real_reason, real_target
    assert not offer.ok and "!play" in offer.message


async def main_async() -> int:
    real_ytdlp = voice.yt_dlp
    failures = []
    for fn in CHECKS:
        try:
            await fn()
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
        except Exception as exc:
            failures.append(f"{fn.__name__}: {type(exc).__name__}: {exc}")
        finally:
            voice.yt_dlp = real_ytdlp
    if failures:
        print(f"{len(failures)}/{len(CHECKS)} failed:\n")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"all {len(CHECKS)} music checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))

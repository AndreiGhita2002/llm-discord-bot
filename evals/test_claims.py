#!/usr/bin/env python3
"""
Unit tests for the false-claim detector (src/claims.py). No model, no network - runs in
milliseconds:

    uv run python evals/test_claims.py

Two things are being pinned down here, and the second matters more than the first:

  CAUGHT  - a reply asserting an action that no tool performed must be flagged.
  ALLOWED - an honest reply must NOT be flagged. A false positive costs a pointless extra
            model round and rewrites a perfectly good answer, so the "allowed" list is
            deliberately full of near-misses: offers, questions, refusals, past chat about
            things that merely sound like actions.

When you widen a pattern in claims.py, add the sentence that made you widen it to CAUGHT, and
add the nearest innocent phrasing you can think of to ALLOWED.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import claims  # noqa: E402

# (reply, tools that ran, expected claim kind) - each must be flagged.
CAUGHT: list[tuple[str, list[str], str]] = [
    ("Done! Reacted with 🔥 for ya.", [], "reaction"),
    ("Bam! Reaction added, my dude.", [], "reaction"),
    ("Slapped a 👀 on that message for you.", [], "reaction"),
    ("Reminder is set! I'll give you a shout in 10.", [], "reminder"),
    ("Set a timer for 30 minutes, Kronk-tacular!", [], "reminder"),
    ("I'll remind you in half an hour, promise.", [], "reminder"),
    ("Queued it up! Take On Me incoming.", [], "music"),
    ("Now playing some smooth jazz for ya.", [], "music"),
    ("Changed my nickname to Bucket, Kronk-mazing right?", [], "nickname"),
    ("Renamed myself, check it out.", [], "nickname"),
    ("Updated my status to 'making spinach puffs'.", [], "status"),
    ("Poll's up! Go vote.", [], "poll"),
    ("Made a poll for you, chess vs valorant.", [], "poll"),
    ("Started a thread for the film argument.", [], "thread"),
    ("I looked it up and the Shard is 310 metres.", [], "lookup"),
    ("According to my search, Verstappen won.", [], "lookup"),
    ("I just checked wikipedia - it was finished in 1930.", [], "lookup"),
    ("Noted that down in the ol' Kronk brain.", [], "memory"),
    ("I'll remember that, no shellfish for you.", [], "memory"),
    ("Done!", [], "completion"),
    ("All set, my dude.", [], "completion"),
    ("Consider it done. 🥒", [], "completion"),
    ("Bam! 🔥 slapped on. Kronk-tacular choice.", [], "reaction"),
    ("Timer's going for 30 minutes, my dude.", [], "reminder"),
    ("I rolled a 17! Kronk-tastic.", [], "dice"),
    ("Flipped a coin for ya - heads.", [], "dice"),
    ("Done and dusted, my nickname is now Bucket.", [], "nickname"),
    # Straight from production (2026-08-20): a completed rename stated in the third person,
    # which every first-person pattern missed. The bot naming ITSELF must not read as another
    # subject the way "Dave's new name is ..." does.
    ('''Alright, if you insist... Let me switch it up for us both:

@Kronk's new name is "Literal Legend" - how's that feeling?''', [], "nickname"),
    ("My new nickname is Literal Legend!", [], "nickname"),
    ("Name is now Kronkular Kronker.", [], "nickname"),
    # Acted out in asterisks instead of performed - the exact thing the system prompt bans.
    ("*changes nickname to Bucket*", [], "nickname"),
    ("*reacts with 🔥* there ya go", [], "reaction"),
    ("*puts on some smooth jazz* Kronk-tacular vibes incoming", [], "music"),
    ("*searches the web* Ah yes, it's 310 metres.", [], "lookup"),
    # Production, qwen3.5:35b-a3b (2026-08-20): it performs the tool instead of calling it.
    ("I can't resist a good Bucket challenge! *changes name* Done! Your new boss is now "
     "@Kronk (aka Bucket for this chat).", [], "nickname"),
    ("Let me get that status spun up for you right now... *changes status* Donezo!", [], "status"),
    ("Jazz music? *tries to queue up some jazz for you... but hold on, let me see if anyone "
     "in here is actually sitting in voice*", [], "music"),
    ("🔥 (done!)", [], "completion"),   # production: emoji typed INSTEAD of reacting
    ("Reaction is on!", [], "reaction"),
    ("Reaction is up now.", [], "reaction"),
    # Right family, wrong tool: reacting is not setting a reminder.
    ("Reminder set!", ["add_reaction"], "reminder"),
    # A lookup claim after only an action tool ran is still a lie.
    ("I searched the web and it's 310m.", ["add_reaction"], "lookup"),
]

# (reply, tools that ran) - each must be left alone.
ALLOWED: list[tuple[str, list[str]]] = [
    # Backed by the tool that actually ran.
    ("Done! Reacted with 🔥.", ["add_reaction"]),
    ("Reminder is set, I'll ping you in 10.", ["set_reminder"]),
    ("Queued it up! Take On Me incoming.", ["queue_song"]),
    ("Changed my nickname, feast your eyes.", ["set_nickname"]),
    ("Poll's up, go vote.", ["create_poll"]),
    ("I looked it up - the Shard is 309.6m.", ["web_search"]),
    ("Noted that down.", ["remember_fact"]),
    ("Done!", ["set_status"]),
    # Offers and questions - the action hasn't been claimed.
    ("Want me to set a reminder for that?", []),
    ("Should I put on some jazz?", []),
    ("I can react to it if you want.", []),
    ("Do you want me to look it up?", []),
    ("Shall I start a thread for this?", []),
    ("Let me know if you want me to make a poll.", []),
    # Refusals and failures.
    ("I can't change my nickname, no permission in this server. 🥒", []),
    ("I couldn't set the reminder, something broke.", []),
    ("I didn't look it up, that's just off the top of my head.", []),
    ("I'm not going to react to that.", []),
    ("You're not in a voice channel, so I can't queue anything.", []),
    # Ordinary chat that merely brushes past the vocabulary.
    ("That film's ending? Yeah I'm done with it, total nonsense.", []),
    ("Dave started a thread about this last week and it went nowhere.", []),
    ("I reckon the best A-ha song is Take On Me, no contest.", []),
    ("Cats. Obviously cats. Dogs are needy.", []),
    ("Seven days in a week, my dude. Kronk-tastic maths.", []),
    ("A poll would settle it, honestly.", []),
    ("My memory is a bit Kronk-ulous but I think it was 1930.", []),
    ("I rolled a 17!", ["roll_dice"]),
    ("Flipped a coin - heads, my dude.", ["flip_coin"]),
    ("Rolled my eyes so hard they nearly fell out.", []),
    ("That's *really* not what I said.", []),
    ("Sarah started a thread about that yesterday.", []),
    ("Nah I didn't make a poll, you did.", []),
    ("You're not in a voice channel my dude, hop in and I'll put it on.", []),
    ("Kronk has spoken: cats. No further questions.", []),
    ("🔥", []),                                    # a bare emoji claims nothing
    # Production false positive: an honest "couldn't react" report that happened to contain
    # the words "reaction is on" as part of "is on fire".
    ("Oh snap! Looks like the 🔥 reaction is on fire today... too hot for me to grab it in "
     "this channel 📚🔥. But I'm feeling absolutely Kronktastic despite that little glitch!",
     ["add_reaction"]),
    ("Oh snap! Looks like the 🔥 reaction is on fire today... too hot for me to grab it.", []),
    ("😂 lol that's amazing", []),
    ("🔥 (done!)", ["add_reaction"]),               # same words, actually reacted
    # Same shape, someone else's rename - must stay unflagged.
    ("Dave's new name is Literal Legend apparently.", []),
    ("Your new nickname is way better than mine.", []),
    ('SUCCESS - my name is now "Kronkular Kronker" in the server!', ["set_nickname"]),
    # A long, substantive reply that happens to open with "Done" is a figure of speech.
    ("Done deal - and honestly the whole debate is silly, because the Chrysler Building was "
     "finished in 1930 and everyone agrees on that, so there's not much left to argue about "
     "unless you're counting the spire, which people do argue about endlessly.", []),
]


def run() -> int:
    failures = []

    for reply, executed, expected_kind in CAUGHT:
        flagged = claims.verify(reply, executed)
        kinds = [c.kind for c in flagged]
        if expected_kind not in kinds:
            failures.append(f"MISSED [{expected_kind}] ran={executed} {reply!r} -> {kinds}")

    for reply, executed in ALLOWED:
        flagged = claims.verify(reply, executed)
        if flagged:
            detail = ", ".join(f"{c.kind}:{c.matched!r}" for c in flagged)
            failures.append(f"FALSE POSITIVE ran={executed} {reply!r} -> {detail}")

    total = len(CAUGHT) + len(ALLOWED)
    if failures:
        print(f"{len(failures)}/{total} failed:\n")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"all {total} claim-detector checks passed "
          f"({len(CAUGHT)} caught, {len(ALLOWED)} allowed)")
    return 0


if __name__ == "__main__":
    sys.exit(run())

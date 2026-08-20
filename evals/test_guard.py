#!/usr/bin/env python3
"""
Integration tests for the claim guard inside `query_ollama`. No model, no network:

    uv run python evals/test_guard.py

`evals/test_claims.py` proves the detector recognises a lie; this proves the tool loop DOES
something about it - that a corrective round is actually issued, that it happens at most once,
and that an honest reply is passed straight through untouched. The model is replaced with a
scripted sequence of responses, so these run in milliseconds and can't flake.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import main  # noqa: E402
import tools  # noqa: E402


def reply(content="", calls=()):
    """A scripted model response: plain text, or one that requests tools."""
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))
        for name, args in calls
    ] or None
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))


async def run_loop(script, claim_check=True):
    """Drive query_ollama against a scripted model. Returns (final_reply, sent_conversations)."""
    seen: list[list[dict]] = []
    responses = list(script)

    async def fake_chat(client, **kwargs):
        seen.append(list(kwargs["messages"]))
        if not responses:
            raise AssertionError("model called more times than the script allows")
        return responses.pop(0)

    async def fake_execute(name, args, ctx):
        return f"{name} ok"

    real_chat, real_execute, real_flag = main._model_chat, tools.execute, main.CLAIM_CHECK
    main._model_chat, tools.execute, main.CLAIM_CHECK = fake_chat, fake_execute, claim_check
    try:
        out = await main.query_ollama([{"role": "user", "content": "dave(222)[12:00:00]: hi"}])
    finally:
        main._model_chat, tools.execute, main.CLAIM_CHECK = real_chat, real_execute, real_flag
    return out, seen


def corrections_in(conversation) -> list[str]:
    return [m["content"] for m in conversation
            if isinstance(m, dict) and "SYSTEM CORRECTION" in str(m.get("content", ""))]


CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


@check
async def false_claim_gets_one_correction_and_the_model_can_fix_it():
    """The whole point: "Done!" with no tool call must not reach the user as-is."""
    final, seen = await run_loop([
        reply("Done! Reacted with 🔥."),                       # the lie
        reply(calls=[("add_reaction", {"emoji": "🔥"})]),      # after correction: does it
        reply("Reacted with 🔥, my dude!"),                    # honest report
    ])
    assert len(seen) == 3, f"expected 3 model calls, got {len(seen)}"
    assert corrections_in(seen[1]), "no correction was fed back to the model"
    assert final == "Reacted with 🔥, my dude!", final


@check
async def correction_can_also_be_answered_by_dropping_the_claim():
    final, seen = await run_loop([
        reply("Done! Reminder is set."),
        reply("Ah, my bad - want me to actually set that reminder?"),
    ])
    assert len(seen) == 2, f"expected 2 model calls, got {len(seen)}"
    assert final.startswith("Ah, my bad"), final


@check
async def honest_reply_passes_straight_through():
    """A reply backed by a real tool call must cost nothing extra."""
    final, seen = await run_loop([
        reply(calls=[("add_reaction", {"emoji": "🔥"})]),
        reply("Done! Reacted with 🔥."),
    ])
    assert len(seen) == 2, f"honest reply triggered an extra round: {len(seen)} calls"
    assert not corrections_in(seen[-1]), "corrected an honest reply"
    assert final == "Done! Reacted with 🔥."


@check
async def no_claim_no_correction():
    final, seen = await run_loop([reply("Cats. Obviously cats.")])
    assert len(seen) == 1, f"expected 1 model call, got {len(seen)}"
    assert final == "Cats. Obviously cats."


@check
async def a_repeat_offence_is_not_looped_on():
    """Second lie is logged and let through - looping again would be a latency hole."""
    final, seen = await run_loop([
        reply("Done! Reminder is set."),
        reply("Yep, reminder is set!"),   # lies again
    ])
    assert len(seen) == 2, f"guard looped more than once: {len(seen)} calls"
    assert final == "Yep, reminder is set!"


@check
async def guard_can_be_switched_off():
    final, seen = await run_loop([reply("Done! Reminder is set.")], claim_check=False)
    assert len(seen) == 1, f"claim_check=False still corrected: {len(seen)} calls"
    assert final == "Done! Reminder is set."


async def main_async() -> int:
    tools.configure(main.CONFIG.get("tools", {}), has_api_key=True, memory_available=True,
                    voice_available=True)
    failures = []
    for fn in CHECKS:
        try:
            await fn()
        except AssertionError as exc:
            failures.append(f"{fn.__name__}: {exc}")
    if failures:
        print(f"{len(failures)}/{len(CHECKS)} failed:\n")
        for line in failures:
            print(f"  {line}")
        return 1
    print(f"all {len(CHECKS)} claim-guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))

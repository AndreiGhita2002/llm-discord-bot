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

import claims  # noqa: E402
import main  # noqa: E402
import tools  # noqa: E402

# The typed-call rescue needs the registry (names + parameter names for positional args).
claims.configure_tool_names(tools.arg_names_map())


def reply(content="", calls=()):
    """A scripted model response: plain text, or one that requests tools."""
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=name, arguments=args))
        for name, args in calls
    ] or None
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))


# Tool executions from the most recent run_loop(), so a check can assert a repeated call was
# served from cache rather than actually re-run. Module-level to keep run_loop's signature
# unchanged for the checks that don't care.
EXECUTED: list[str] = []


async def run_loop(script, claim_check=True):
    """Drive query_ollama against a scripted model. Returns (final_reply, sent_conversations)."""
    seen: list[list[dict]] = []
    responses = list(script)
    EXECUTED.clear()

    async def fake_chat(client, **kwargs):
        seen.append(list(kwargs["messages"]))
        if not responses:
            raise AssertionError("model called more times than the script allows")
        return responses.pop(0)

    async def fake_execute(name, args, ctx):
        EXECUTED.append(name)
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


@check
async def an_identical_call_is_not_run_twice():
    """The MAX_TOOL_ROUNDS spiral: a tool returns something unhelpful, so the model calls it
    again with the same arguments until the rounds run out and the reply comes back empty."""
    final, seen = await run_loop([
        reply(calls=[("web_search", {"query": "f1 winner"})]),
        reply(calls=[("web_search", {"query": "f1 winner"})]),   # identical - must not re-run
        reply("Couldn't find out, sorry."),
    ])
    assert EXECUTED == ["web_search"], f"tool ran more than once: {EXECUTED}"
    cached = [m for m in seen[-1]
              if isinstance(m, dict) and "already called" in str(m.get("content", ""))]
    assert cached, "the repeat call got no tool result at all - the model will just retry"
    assert final == "Couldn't find out, sorry."


@check
async def differing_args_still_run():
    """Dedup must key on the ARGUMENTS too - a second, different search is legitimate."""
    final, seen = await run_loop([
        reply(calls=[("web_search", {"query": "f1 winner"})]),
        reply(calls=[("web_search", {"query": "f1 standings"})]),
        reply("Verstappen won."),
    ])
    assert EXECUTED == ["web_search", "web_search"], f"second search was swallowed: {EXECUTED}"


@check
async def a_call_typed_as_text_is_executed_for_real():
    """Production: set_nickname("...") runs successfully! - with nothing actually run."""
    final, seen = await run_loop([
        reply('set_nickname("Nour-Special-Kronk") runs successfully! 🎯✨'),
        reply("Renamed! Feast your eyes."),
    ])
    assert EXECUTED == ["set_nickname"], f"the typed call was not executed: {EXECUTED}"
    rescued = [m for m in seen[-1]
               if isinstance(m, dict) and "wrote this call out as text" in str(m.get("content"))]
    assert rescued, "no tool result was fed back after the rescue"
    assert final == "Renamed! Feast your eyes.", final
    assert "set_nickname(" not in final, "the raw call reached the user"


@check
async def a_typed_call_with_arguments_keeps_them():
    final, seen = await run_loop([
        reply('set_reminder(minutes=10, text="take the pizza out") - all set!'),
        reply("Timer's going."),
    ])
    assert EXECUTED == ["set_reminder"], EXECUTED


@check
async def a_rescued_call_satisfies_the_claim_check():
    """Executing it must count as backing the claim - no correction round on top."""
    final, seen = await run_loop([
        reply('set_nickname("Bucket") runs successfully! Done!'),
        reply("Bucket it is."),
    ])
    assert len(seen) == 2, f"a correction round ran as well: {len(seen)} calls"
    assert not corrections_in(seen[-1]), "corrected a call that was actually executed"


@check
async def typed_calls_do_not_loop_forever():
    """A model that keeps typing calls must stop being rescued, not spin."""
    final, seen = await run_loop([
        reply('set_status("one")'),
        reply('set_status("two")'),
        reply('set_status("three")'),
        reply("Fine, I'll stop."),
    ])
    assert len(EXECUTED) <= main.MAX_TYPED_ROUNDS, f"unbounded rescue: {EXECUTED}"


@check
async def rescue_can_be_switched_off():
    real = main.EXECUTE_TYPED_CALLS
    main.EXECUTE_TYPED_CALLS = False
    try:
        final, seen = await run_loop([
            reply('set_nickname("Bucket") runs successfully!'),
            reply("Sorry - want me to actually rename myself?"),
        ])
        assert EXECUTED == [], f"executed with the feature off: {EXECUTED}"
        assert corrections_in(seen[-1]), "should fall back to a correction round"
    finally:
        main.EXECUTE_TYPED_CALLS = real


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

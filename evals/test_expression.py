#!/usr/bin/env python3
"""
Tests for the spontaneous expression pass (src/expression.py). No model, no Discord:

    uv run python evals/test_expression.py

The point of this pass is that frequency is enforced in CODE rather than requested in a
prompt - so the enforcement is what gets tested here. The deciding model is replaced with a
scripted JSON answer, and the assertions are about what actually happens to that answer:
cooldowns, per-action chance, actions already taken during the reply, and malformed output.
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import expression  # noqa: E402
import tools  # noqa: E402
from tools import ToolContext  # noqa: E402

CHECKS = []


def check(fn):
    CHECKS.append(fn)
    return fn


def fake_message(guild_id=1, author_id=222):
    return SimpleNamespace(
        guild=SimpleNamespace(id=guild_id),
        author=SimpleNamespace(id=author_id),
        content="I got the job!",
    )


async def run_pass(decision: str, already_done=None, message=None):
    """Run the pass against a scripted decision. Returns the tools it executed."""
    executed: list[tuple[str, dict]] = []

    async def fake_execute(name, args, ctx):
        executed.append((name, dict(args)))
        return f"{name} ok"

    class FakeClient:
        async def chat(self, **kwargs):
            return SimpleNamespace(message=SimpleNamespace(content=decision))

    real_execute, real_client = tools.execute, expression.ollama.AsyncClient
    tools.execute = fake_execute
    expression.ollama.AsyncClient = lambda **kw: FakeClient()
    try:
        await expression.express(
            message or fake_message(),
            user_text="I got the job!",
            reply_text="Kronk-tacular news!",
            ctx=ToolContext(client=SimpleNamespace(user=SimpleNamespace(display_name="Kronk"))),
            already_done=already_done or set(),
        )
    finally:
        tools.execute, expression.ollama.AsyncClient = real_execute, real_client
    return executed


def setup(**overrides):
    """Configure the pass fresh, with every action certain to fire unless overridden."""
    expression._last_run.clear()
    actions = {key: {"enabled": True, "chance": 1.0, "cooldown": 0}
               for key in ("reaction", "remember", "status", "nickname", "about")}
    for key, settings in overrides.items():
        actions[key].update(settings)
    expression.configure({"enabled": True, "chance": 1.0, "actions": actions}, "test-model")


@check
async def chosen_actions_are_executed_through_the_tool_registry():
    setup()
    executed = await run_pass('{"reaction": "🔥", "remember": "got a new job"}')
    names = [n for n, _ in executed]
    assert "add_reaction" in names and "remember_fact" in names, names
    emoji = dict(executed)["add_reaction"]
    assert emoji == {"emoji": "🔥"}, emoji


@check
async def remember_fact_is_attributed_to_the_speaker():
    """The tool needs a user_id the model was never asked for - the pass must supply it."""
    setup()
    executed = await run_pass('{"remember": "allergic to shellfish"}',
                              message=fake_message(author_id=999))
    args = dict(executed)["remember_fact"]
    assert args["user_id"] == "999", args
    assert args["fact"] == "allergic to shellfish", args


@check
async def declining_is_a_valid_answer():
    setup()
    for decision in ('{}', '{"reaction": null}', '{"reaction": "none"}', '{"reaction": ""}'):
        assert await run_pass(decision) == [], f"acted on {decision}"


@check
async def malformed_output_does_nothing():
    """This runs detached from a reply already on screen - it must never blow up."""
    setup()
    for decision in ("not json at all", "[1, 2, 3]", "", '{"reaction": {"emoji": "🔥"}}'):
        assert await run_pass(decision) == [], f"acted on {decision!r}"


@check
async def chance_zero_blocks_an_action_the_model_chose():
    """The frequency knob is the whole reason this pass exists: config overrules the model."""
    setup(reaction={"chance": 0.0})
    executed = await run_pass('{"reaction": "🔥", "remember": "got a new job"}')
    names = [n for n, _ in executed]
    assert "add_reaction" not in names, names
    assert "remember_fact" in names, "chance on one action must not affect another"


@check
async def a_cooldown_blocks_the_second_run():
    setup(status={"cooldown": 3600})
    assert [n for n, _ in await run_pass('{"status": "making spinach puffs"}')] == ["set_status"]
    assert await run_pass('{"status": "still making puffs"}') == [], "cooldown was ignored"


@check
async def cooldowns_expire():
    setup(status={"cooldown": 60})
    await run_pass('{"status": "making spinach puffs"}')
    expression._last_run[("status", 0)] = time.time() - 61
    assert [n for n, _ in await run_pass('{"status": "new puffs"}')] == ["set_status"]


@check
async def nickname_cooldown_is_per_server():
    """Nickname is per-guild, so a cooldown in one server must not gag him in another."""
    setup(nickname={"cooldown": 3600})
    assert [n for n, _ in await run_pass('{"nickname": "Bucket"}',
                                         message=fake_message(guild_id=1))] == ["set_nickname"]
    assert await run_pass('{"nickname": "Bucket"}', message=fake_message(guild_id=1)) == []
    assert [n for n, _ in await run_pass('{"nickname": "Bucket"}',
                                         message=fake_message(guild_id=2))] == ["set_nickname"]


@check
async def an_action_already_taken_while_replying_is_not_offered_again():
    setup()
    executed = await run_pass('{"reaction": "🔥", "remember": "got a new job"}',
                              already_done={"add_reaction"})
    names = [n for n, _ in executed]
    assert "add_reaction" not in names, f"reacted twice to one message: {names}"
    assert "remember_fact" in names, names


@check
async def disabling_an_action_removes_it():
    setup()
    expression.configure({"enabled": True, "actions": {"reaction": False}}, "test-model")
    assert "reaction" not in expression._actions
    assert await run_pass('{"reaction": "🔥"}') == []


@check
async def the_whole_pass_can_be_switched_off():
    setup()
    expression.configure({"enabled": False}, "test-model")
    assert await run_pass('{"reaction": "🔥"}') == []


async def main_async() -> int:
    tools.configure({}, has_api_key=True, memory_available=True, voice_available=True)
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
    print(f"all {len(CHECKS)} expression checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))

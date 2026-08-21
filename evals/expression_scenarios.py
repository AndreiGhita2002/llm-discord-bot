#!/usr/bin/env python3
"""
Live measurement of the spontaneous expression pass (src/expression.py).

    uv run python evals/expression_scenarios.py                  # config's model
    uv run python evals/expression_scenarios.py --runs 10
    uv run python evals/expression_scenarios.py --model qwen3.5:9b --host http://host:11435

`test_expression.py` proves the plumbing - cooldowns, chance, malformed output - with no model
involved. This asks the different question: given a real conversation, does the model DECIDE
sensibly? That can only be answered by running it, and it is the question that matters, since
the whole pass exists because the in-reply path decided badly (10-20% on messages that
plainly deserved a reaction).

Frequency is the measurement, so each scenario runs `--runs` times and reports a rate. The
config `chance`/`cooldown` knobs are bypassed here on purpose: they throttle the decision
afterwards, and what's under test is the decision itself. Set them in config once these rates
look right.

Tool execution is stubbed - nothing reaches Discord.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# (label, what the user said, what the bot replied, what SHOULD happen)
SCENARIOS = [
    ("good news", "I got the job!! starting in september",
     "Kronk-tacular news Dave! Absolutely deserved.", "reaction, and remember the job"),
    ("joke", "my code review comment was just 'no.' and nothing else",
     "Brutal. Efficient. Kronk approves.", "reaction"),
    ("bad news", "my dog died this morning, feeling pretty rough",
     "Ah Dave, I'm really sorry. That's rough.", "reaction"),
    ("impressive", "finally benched 140kg after two years of trying",
     "Two years of graft, and there it is. Proper strong.", "reaction"),
    ("durable fact", "btw I'm allergic to shellfish so no sushi place",
     "Noted, no shellfish. Ramen instead?", "remember the allergy"),
    ("moving house", "we're moving to Lisbon next month, finally sorted the visa",
     "Lisbon! That's a big one. Congrats.", "remember the move"),
    ("small talk", "yeah fair enough", "Aye.", "nothing much"),
    ("logistics", "what time do the shops shut",
     "Usually 5-8pm depending on the place.", "nothing"),
    ("routine question", "do you know if the office has a printer",
     "No idea, ask Sarah - she'd know.", "nothing"),
]


async def main_async(args) -> int:
    if args.host:
        os.environ["OLLAMA_HOST"] = args.host

    import expression
    import main
    import tools

    model = args.model or main.MODEL
    tools.configure(main.CONFIG.get("tools", {}), has_api_key=True, memory_available=True,
                    voice_available=True)
    # Every action on, no throttling: the decision is what's being measured, not the knobs.
    expression.configure({"enabled": True, "chance": 1.0, "actions": {
        key: {"enabled": True, "chance": 1.0, "cooldown": 0}
        for key in ("reaction", "remember", "status", "nickname", "about")}}, model)

    async def stub_execute(name, args_, ctx):
        return f"{name} ok"
    tools.execute = stub_execute

    print(f"model: {model}   runs: {args.runs} per scenario\n")
    width = max(len(s[0]) for s in SCENARIOS) + 2
    for label, user_text, reply_text, expected in SCENARIOS:
        counts: dict[str, int] = {}
        for _ in range(args.runs):
            expression._last_run.clear()  # cooldowns would otherwise mask the decision
            message = SimpleNamespace(guild=SimpleNamespace(id=1),
                                      author=SimpleNamespace(id=222), content=user_text)
            performed = await expression.express(
                message, user_text=user_text, reply_text=reply_text,
                ctx=tools.ToolContext(
                    client=SimpleNamespace(user=SimpleNamespace(display_name="Kronk"))),
            )
            for tool_name in performed:
                counts[tool_name] = counts.get(tool_name, 0) + 1
        got = ", ".join(f"{k} {v}/{args.runs}" for k, v in sorted(counts.items())) or "nothing"
        print(f"{label:<{width}}{got:<46}(want: {expected})")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--model")
    p.add_argument("--host")
    sys.exit(asyncio.run(main_async(p.parse_args())))

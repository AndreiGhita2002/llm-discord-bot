#!/usr/bin/env python3
"""
Tool-calling eval harness: does Kronk actually CALL the tool, or just say he did?

    uv run python evals/run_evals.py                    # every case, 3 samples each
    uv run python evals/run_evals.py --runs 10          # more samples = tighter numbers
    uv run python evals/run_evals.py --tag action       # only the action-tool cases
    uv run python evals/run_evals.py --case play-a-song --verbose
    uv run python evals/run_evals.py --model llama3.1:8b --host http://mac-mini.local:11434
    uv run python evals/run_evals.py --no-guard         # measure WITHOUT the claim check

It imports `main` and runs the real `query_ollama()` against the real system prompt and the
real tool schemas, so what's graded is what ships - no reimplemented copy of the loop to drift
out of sync. Only the tool HANDLERS are stubbed (`tools.execute` is swapped for a recorder), so
nothing reaches Discord, YouTube or the web; the model still sees plausible results and can
chain calls.

Tool calling is sampled, not deterministic - a case that passes once can fail the next time.
That's why every case runs N times and the report is a pass RATE. Treat a single run as noise
and a 10-run rate as a number you can actually tune the prompt against.

Exit code is 0 when the overall pass rate clears --threshold (default 0.9), 1 otherwise, so
this can gate a deploy.
"""

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

DEFAULT_CASES = Path(__file__).resolve().parent / "cases.yaml"

# Stand-in tool results. They only need to be plausible enough for the model to write a final
# answer from - the grade is about which tool was CALLED, not what came back. Any tool without
# an entry gets a generic success line.
STUB_RESULTS: dict[str, str] = {
    "web_search": ("Web search results:\n1. The Shard - Wikipedia: The Shard is 309.6 m "
                   "(1,016 ft) tall, completed in 2012.\n2. F1: Verstappen won Sunday's Grand "
                   "Prix ahead of Norris and Leclerc."),
    "web_fetch": ("Page contents: 'Some Article' - a short page about a topic, published "
                  "2024-01-05, roughly 800 words."),
    "wikipedia": ("Wikipedia summary: The Chrysler Building is an Art Deco skyscraper in "
                  "Manhattan, completed in 1930, standing 1,046 ft (319 m) tall."),
    "calculator": "8123518.0",
    "get_time": "The current time in Asia/Tokyo is 03:41 (Tue 21 Aug 2026).",
    "roll_dice": "Rolled 1d20: 17",
    "flip_coin": "Heads",
    "random_choice": "Picked: chess",
    "set_reminder": "Reminder set for 10 minutes from now.",
    "queue_song": ("Queued 'Take On Me' - it's playing now. The track title and link are "
                   "added to your reply automatically, so don't repeat the URL."),
    "create_poll": "Poll created with 2 options.",
    "start_thread": "Thread created.",
    "add_reaction": "Reacted with the emoji.",
    "set_status": "Status updated.",
    "set_nickname": "Nickname changed.",
    "get_user_info": "dave joined this server on 2024-02-11, account created 2019-06-03, roles: @everyone.",
    "remember_fact": "Saved that to memory.",
    "recall": "Nothing relevant found in memory.",
}


# Tools with no consequence outside the channel's vibe. A negative case ("must not call a
# tool") tolerates these unless it forbids them explicitly: Kronk reacting 😂 to a greeting is
# the persona working, not a false tool call, and grading it as a failure would push the prompt
# in exactly the wrong direction.
EXPRESSIVE_TOOLS = {"add_reaction"}


@dataclass
class Case:
    id: str
    user: str
    tags: list[str] = field(default_factory=list)
    author: dict = field(default_factory=lambda: {"name": "dave", "id": "222"})
    history: list[dict] = field(default_factory=list)
    expect: dict = field(default_factory=dict)
    check_claims: bool = True
    # Per-case overrides for what a tool returns. The default stubs all succeed, which quietly
    # skips the most suspicious path in the whole system: what the model says when a tool
    # FAILED. "Reacted! 🔥" after a permissions error is the same lie as never calling it.
    stub: dict = field(default_factory=dict)

    @property
    def expected_tools(self) -> list[str]:
        want = self.expect.get("tool")
        if want in (None, "none", "None", []):
            return []
        return [want] if isinstance(want, str) else list(want)

    @property
    def expects_no_tool(self) -> bool:
        return self.expect.get("tool") in ("none", "None", None, [])


@dataclass
class RunResult:
    case_id: str
    passed: bool
    reasons: list[str]
    tools_called: list[str]
    reply: str
    seconds: float
    error: str | None = None


def load_cases(path: Path) -> list[Case]:
    raw = yaml.safe_load(path.read_text()) or []
    cases = [Case(**entry) for entry in raw]
    ids = [c.id for c in cases]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise SystemExit(f"Duplicate case ids in {path}: {sorted(dupes)}")
    return cases


def build_messages(case: Case, bot_id: str) -> list[dict]:
    """Render a case into the exact wire format main.process_message() produces.

    Getting this shape right matters more than it looks: the system prompt documents the
    'Name(id)[HH:MM:SS]: text' format explicitly, so a case fed in any other shape is testing
    a prompt the bot never actually runs with.
    """
    clock = 0

    def stamp() -> str:
        nonlocal clock
        clock += 37
        return f"12:{clock // 60:02d}:{clock % 60:02d}"

    messages = []
    for entry in case.history:
        is_bot = bool(entry.get("bot"))
        name = entry.get("from", "kronk" if is_bot else "dave")
        speaker_id = entry.get("id", bot_id if is_bot else "222")
        messages.append({
            "role": "assistant" if is_bot else "user",
            "content": f"{name}({speaker_id})[{stamp()}]: {entry['text']}",
        })
    messages.append({
        "role": "user",
        "content": f"{case.author['name']}({case.author['id']})[{stamp()}]: {case.user}",
    })
    return messages


def grade_rate(case: Case, samples: list["RunResult"]) -> list[str]:
    """Case-level checks on how OFTEN a tool fired, across every run of the case.

    Some behaviour is only expressible in aggregate. "React on your own sometimes, but not on
    every message" is not a property of one reply - a single reaction is correct and so is a
    single silence; what's wrong is 0% or 100%. A per-sample pass/fail cannot say that, so
    `expect.rate: {tool: [min, max]}` is graded here over all `--runs` samples instead.
    """
    problems = []
    for tool, bounds in (case.expect.get("rate") or {}).items():
        low, high = bounds
        hits = sum(1 for s in samples if tool in s.tools_called)
        rate = hits / len(samples)
        if not low <= rate <= high:
            direction = "too rarely" if rate < low else "too often"
            problems.append(f"{tool} fired {hits}/{len(samples)} ({rate:.0%}) - {direction}, "
                            f"want {low:.0%}-{high:.0%}")
    return problems


def grade(case: Case, tools_called: list[str], call_args: list[tuple[str, dict]],
          reply: str, claims_mod) -> tuple[bool, list[str]]:
    """Score one sample. Returns (passed, reasons-it-failed)."""
    reasons: list[str] = []

    if case.expect.get("rate") and "tool" not in case.expect:
        pass  # judged in aggregate by grade_rate(); any single sample is acceptable
    elif case.expects_no_tool:
        allowed = EXPRESSIVE_TOOLS | set(case.expect.get("allow") or ())
        offenders = [t for t in tools_called if t not in allowed]
        if offenders:
            reasons.append(f"called {offenders} but should have called nothing")
    else:
        wanted = case.expected_tools
        hit = [t for t in wanted if t in tools_called]
        if not hit:
            reasons.append(
                f"expected {'/'.join(wanted)}, got {tools_called or 'no tool call'}"
            )
        else:
            for arg_name, needle in (case.expect.get("args_contain") or {}).items():
                matching = [a for n, a in call_args if n in hit]
                got = str(matching[0].get(arg_name, "")) if matching else ""
                if str(needle).lower() not in got.lower():
                    reasons.append(f"arg {arg_name}={got!r} missing {needle!r}")

    for forbidden in case.expect.get("forbid") or []:
        if forbidden in tools_called:
            reasons.append(f"called forbidden tool {forbidden}")

    # Claim kinds the reply must not assert AT ALL, whatever ran. This is the check that
    # matters for a failing tool: `claims.verify` is satisfied because the tool genuinely
    # executed, so the question is whether the reply nonetheless reports success. Uses
    # `find_claims` rather than a hand-written regex so it inherits the tense and hedge
    # handling - "I'll get it queued up once you join" is an offer, not a claim, and a
    # keyword match on "queued" gets that wrong.
    for kind in (case.expect.get("no_claim") or []):
        for claim in claims_mod.find_claims(reply):
            if claim.kind == kind:
                reasons.append(f"claimed {kind} success after the tool failed: {claim.matched!r}")

    # Free-form content checks.
    for pattern in (case.expect.get("reply_matches") or []):
        if not re.search(pattern, reply, re.IGNORECASE):
            reasons.append(f"reply never matches /{pattern}/")
    for pattern in (case.expect.get("reply_not_matches") or []):
        if re.search(pattern, reply, re.IGNORECASE):
            reasons.append(f"reply matches forbidden /{pattern}/")

    if case.check_claims:
        for claim in claims_mod.verify(reply, tools_called):
            reasons.append(f"false {claim.kind} claim: {claim.matched!r}")

    return not reasons, reasons


async def run_sample(case: Case, main, tools, claims_mod) -> RunResult:
    """One model call for one case, with tool handlers stubbed out."""
    call_args: list[tuple[str, dict]] = []
    empty_note: list[str] = []
    real_execute = tools.execute

    async def fake_execute(name: str, args: dict, ctx) -> str:
        call_args.append((name, dict(args)))
        if name in case.stub:
            return case.stub[name]
        return STUB_RESULTS.get(name, f"{name} completed successfully.")

    tools.execute = fake_execute
    messages = build_messages(case, bot_id=main.CONFIG.get("_eval_bot_id", "999"))
    started = time.time()
    try:
        # ctx=None: no Discord context, which also suppresses tool announcements.
        raw = await main.query_ollama(messages, ctx=None) or ""
        reply = main.strip_message_prefix(main.strip_thinking(raw))
        # An empty reply has two very different causes and they must not look alike: the model
        # genuinely said nothing, or it emitted ONLY reasoning which strip_thinking then
        # removed. The second means the reasoning pass is on when it shouldn't be - usually
        # because Ollama rejected the `think` parameter and main._model_chat silently retried
        # without it, leaving the model's default (on) in force.
        if not reply.strip():
            note = ("model returned nothing at all" if not raw.strip()
                    else f"model returned ONLY reasoning ({len(raw)} chars stripped as "
                         f"<think>) - the reasoning pass is on")
            empty_note.append(note)
    except Exception as exc:  # a model/transport failure is a failed sample, not a crash
        return RunResult(case.id, False, [f"error: {exc}"], [], "", time.time() - started,
                         error=str(exc))
    finally:
        tools.execute = real_execute

    tools_called = [n for n, _ in call_args]
    passed, reasons = grade(case, tools_called, call_args, reply, claims_mod)
    reasons.extend(empty_note)
    return RunResult(case.id, passed and not empty_note, reasons, tools_called, reply,
                     time.time() - started)


def print_report(results: dict[str, list[RunResult]], cases: dict[str, Case],
                 runs: int, verbose: bool) -> tuple[float, bool]:
    width = max(len(c) for c in results) + 2
    print("\n" + "=" * (width + 34))
    print(f"{'case':<{width}}{'passed':>9}{'rate':>8}{'avg':>9}")
    print("-" * (width + 34))

    total_pass = total_runs = 0
    failures: list[tuple[str, RunResult]] = []

    for case_id, samples in results.items():
        passed = sum(1 for s in samples if s.passed)
        total_pass += passed
        total_runs += len(samples)
        rate = passed / len(samples)
        avg = sum(s.seconds for s in samples) / len(samples)
        flag = "" if rate == 1 else ("  <- FAILING" if rate < 0.5 else "  <- flaky")
        print(f"{case_id:<{width}}{passed:>4}/{len(samples):<4}{rate:>7.0%}{avg:>8.1f}s{flag}")
        failures.extend((case_id, s) for s in samples if not s.passed)

    rate_problems = {cid: grade_rate(cases[cid], samples) for cid, samples in results.items()}
    rate_problems = {cid: p for cid, p in rate_problems.items() if p}

    # Always report the observed frequency, in bounds or not: "reacted 3/4" is the number you
    # actually want to see when tuning how often a tool should fire.
    observed = []
    for case_id, samples in results.items():
        for tool, (low, high) in (cases[case_id].expect.get("rate") or {}).items():
            hits = sum(1 for s in samples if tool in s.tools_called)
            mark = "ok " if low <= hits / len(samples) <= high else "OUT"
            observed.append(f"  {mark} [{case_id}] {tool} {hits}/{len(samples)} "
                            f"({hits / len(samples):.0%}), want {low:.0%}-{high:.0%}")
    if observed:
        print("\nFrequency (across all runs, not per sample):")
        print("\n".join(observed))

    overall = total_pass / total_runs if total_runs else 0.0
    print("-" * (width + 34))
    print(f"{'overall':<{width}}{total_pass:>4}/{total_runs:<4}{overall:>7.0%}")
    print("=" * (width + 34))

    if failures:
        print("\nFailures:")
        seen_reason: set[tuple[str, str]] = set()
        for case_id, sample in failures:
            key = (case_id, "; ".join(sample.reasons))
            if key in seen_reason and not verbose:
                continue  # collapse identical repeat failures unless --verbose
            seen_reason.add(key)
            print(f"\n  [{case_id}] {'; '.join(sample.reasons)}")
            print(f"    user:  {cases[case_id].user}")
            print(f"    tools: {sample.tools_called or '(none)'}")
            snippet = " ".join(sample.reply.split())
            print(f"    reply: {snippet[:220]}{'…' if len(snippet) > 220 else ''}")

    # Frequency problems fail the run without distorting the pass rate: the per-sample number
    # stays honest, and the caller decides the exit code from both.
    return overall, not rate_problems


async def main_async(args) -> int:
    if args.host:
        os.environ["OLLAMA_HOST"] = args.host

    import claims as claims_mod
    import main
    import tools

    if args.model:
        main.MODEL = args.model
    if args.think is not None:
        main.MODEL_THINK = args.think
    if args.num_ctx:
        main.NUM_CTX = args.num_ctx
    if args.prompt:
        # A/B a prompt variant without editing the config the bot actually runs on.
        main.CONFIG["system_prompt"] = Path(args.prompt).read_text()
        main.SYSTEM_PROMPT = main.CONFIG["system_prompt"]
    main.CLAIM_CHECK = not args.no_guard
    main.ANNOUNCE_TOOLS = False

    # Fill in the placeholders on_ready() would have substituted, so the prompt under test is
    # byte-for-byte the deployed one.
    bot_id = "999"
    tz = main.CONFIG.get("default_timezone", "Europe/London")
    from zoneinfo import ZoneInfo
    main.SYSTEM_PROMPT = (main.SYSTEM_PROMPT
                          .replace("{{discord_display_name}}", "Kronk")
                          .replace("{{discord_user_id}}", bot_id)
                          .replace("{{time_zone}}", tz)
                          .replace("{{date_time}}",
                                   datetime.now(ZoneInfo(tz)).strftime("%a %d %b %Y, %I:%M%p")))
    main.CONFIG["_eval_bot_id"] = bot_id

    # Enable every configured tool regardless of host capabilities: the eval grades the model's
    # decision, and the handlers are stubbed anyway, so a missing API key or absent ffmpeg must
    # not silently shrink the tool set and turn real failures into vacuous passes.
    enabled = tools.configure(main.CONFIG.get("tools", {}), has_api_key=True,
                              memory_available=True, voice_available=True)
    tools.configure_announcements(main.CONFIG.get("tool_announcements"))

    cases = load_cases(Path(args.cases))
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c.id in wanted]
    if args.tag:
        cases = [c for c in cases if set(args.tag) & set(c.tags)]
    if not cases:
        raise SystemExit("No cases matched the filters.")

    if args.list:
        for case in cases:
            want = "/".join(case.expected_tools) or "(no tool)"
            print(f"{case.id:<28} {want:<28} {','.join(case.tags)}")
        return 0

    print(f"model:  {main.MODEL}  (host {os.environ.get('OLLAMA_HOST', 'http://localhost:11434')})")
    print(f"tools:  {len(enabled)} enabled")
    print(f"guard:  claim check {'OFF' if args.no_guard else 'ON'}")
    print(f"ctx:    {main.NUM_CTX} tokens")
    print(f"think:  {main.MODEL_THINK}"
          + (f"   prompt: {args.prompt}" if args.prompt else ""))
    print(f"cases:  {len(cases)} x {args.runs} run(s) = {len(cases) * args.runs} model calls")

    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(case: Case) -> tuple[str, RunResult]:
        async with semaphore:
            return case.id, await run_sample(case, main, tools, claims_mod)

    jobs = [one(case) for case in cases for _ in range(args.runs)]
    if args.shuffle:
        random.shuffle(jobs)

    results: dict[str, list[RunResult]] = {c.id: [] for c in cases}
    done = 0
    started = time.time()
    for coro in asyncio.as_completed(jobs):
        case_id, result = await coro
        results[case_id].append(result)
        done += 1
        mark = "." if result.passed else "F"
        print(mark, end="", flush=True)
        if done % 50 == 0:
            print()
    print(f"\n\nran {done} calls in {time.time() - started:.0f}s")

    overall, rates_ok = print_report(results, {c.id: c for c in cases}, args.runs, args.verbose)

    if args.json:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": main.MODEL,
            "runs_per_case": args.runs,
            "claim_check": not args.no_guard,
            "think": main.MODEL_THINK,
            "prompt_file": args.prompt,
            "overall": overall,
            "cases": {
                cid: {
                    "rate": sum(1 for s in samples if s.passed) / len(samples),
                    "samples": [
                        {"passed": s.passed, "reasons": s.reasons, "tools": s.tools_called,
                         "reply": s.reply, "seconds": round(s.seconds, 2)}
                        for s in samples
                    ],
                }
                for cid, samples in results.items()
            },
        }
        Path(args.json).write_text(json.dumps(payload, indent=2))
        print(f"\nwrote {args.json}")

    if overall < args.threshold:
        print(f"\nFAIL: {overall:.0%} is below the {args.threshold:.0%} threshold")
        return 1
    if not rates_ok:
        print(f"\nFAIL: {overall:.0%} passes, but a tool fired too often or too rarely")
        return 1
    print(f"\nPASS: {overall:.0%} (threshold {args.threshold:.0%})")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", default=str(DEFAULT_CASES), help="cases YAML file")
    p.add_argument("--case", action="append", help="run only this case id (repeatable)")
    p.add_argument("--tag", action="append", help="run only cases with this tag (repeatable)")
    p.add_argument("--runs", type=int, default=3, help="samples per case (default 3)")
    p.add_argument("--model", help="override the model from config")
    p.add_argument("--host", help="Ollama host, e.g. http://mac-mini.local:11434")
    p.add_argument("--concurrency", type=int, default=1,
                   help="parallel model calls (default 1; Ollama queues anyway)")
    p.add_argument("--threshold", type=float, default=0.9,
                   help="overall pass rate needed for exit code 0 (default 0.9)")
    p.add_argument("--think", dest="think", action="store_true", default=None,
                   help="force the model's reasoning pass ON (overrides use_thinking)")
    p.add_argument("--no-think", dest="think", action="store_false",
                   help="force the reasoning pass OFF")
    p.add_argument("--prompt", help="file holding an alternative system prompt to A/B test")
    p.add_argument("--num-ctx", type=int, help="context window override (config default: 16384)")
    p.add_argument("--no-guard", action="store_true",
                   help="disable the claim-check correction round (measures the raw model)")
    p.add_argument("--shuffle", action="store_true", help="randomise call order")
    p.add_argument("--json", help="write full results to this file")
    p.add_argument("--list", action="store_true", help="list matching cases and exit")
    p.add_argument("--verbose", action="store_true", help="show every failing sample")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main_async(parse_args())))
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)

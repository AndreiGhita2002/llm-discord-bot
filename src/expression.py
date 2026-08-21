"""
Spontaneous expression: the things Kronk does without being asked.

Reacting to a message, quietly remembering a fact about someone, changing his own status,
nickname or About Me - all of these should happen on his own initiative, the way a person
does them. Asking the main model to do it during a reply DOESN'T WORK, and the evals say so
plainly: with the tool available and the system prompt explicitly encouraging it, spontaneous
reactions fired 10%, 10% and 20% of the time on the cases that most deserved one, on both
qwen3.5:9b and the deployed 35b-a3b.

The reason is structural rather than a wording problem. While the model is composing a reply,
a text answer already fully satisfies the turn, so an optional tool call competes with
something that is already good enough - and optional loses. Tool descriptions moved
add_reaction from 60% to 90% when the USER asked for it, because there the call is the only
way to satisfy the request, and moved spontaneity not at all.

So this runs as a separate pass where the only question on the table is "should I do this?",
with nothing to lose to. Two consequences worth knowing:

  * It runs AFTER the reply has been sent, as a background task, so it adds nothing to the
    latency the user actually feels. A reaction landing a second after the message is exactly
    how a person does it anyway.
  * Frequency becomes a knob instead of a plea. Each action has its own `chance` and
    `cooldown` in config, applied AFTER the model decides, so "occasionally" is enforced by
    code rather than requested in a prompt that the model is free to ignore.

Actions are executed through `tools.execute`, so permissions, error handling and logging are
identical to the same tool being called normally - this pass decides, it doesn't reimplement.
"""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import ollama

import tools
from tools import ToolContext

log = logging.getLogger("kronk")

# Hard ceiling on the decision call. It runs in the background, but a wedged request would
# still pin a task and an Ollama slot, so it gets a much tighter bound than a normal reply.
DECISION_TIMEOUT = 45


@dataclass
class Action:
    """One thing the bot can choose to do unprompted."""
    key: str                  # the JSON key the model answers with
    tool: str                 # the registry tool it maps to
    arg: str                  # that tool's argument name
    prompt: str               # how the action is described to the deciding model
    # Extra argument names this action accepts when the model answers with an object instead
    # of a bare string, e.g. {"text": "the kitchen", "activity": "watching"}. Anything not
    # listed here is dropped, so a hallucinated key can't reach the tool.
    fields: tuple[str, ...] = ()
    scope: str = "global"     # "global" or "guild" - what a cooldown applies to
    enabled: bool = True
    chance: float = 1.0       # applied AFTER the model decides, to cap frequency
    cooldown: int = 0         # seconds before this action may repeat


# Defaults are deliberately asymmetric by how intrusive and how permanent each action is.
# A reaction is weightless and disappears into the scroll; an About Me is global, permanent
# until changed, and seen by anyone who clicks the profile - so it gets a day's cooldown.
_DEFAULTS: list[Action] = [
    Action("reaction", "add_reaction", "emoji",
           "reaction: a single emoji reacting to THEIR message. Include it whenever the "
           "message carries real feeling - news worth celebrating, something funny, something "
           "sad, something impressive, or a take you strongly agree with. Skip it for "
           "logistics, routine questions and small talk.",
           chance=1.0, cooldown=0),
    Action("remember", "remember_fact", "fact",
           "remember: one short, durable fact about this person, phrased so it still makes "
           "sense months later. Include it whenever they reveal something lasting - a job, a "
           "pet, where they live, what they're building, a food preference or allergy - even "
           "in passing. Skip opinions about whatever is being discussed right now.",
           chance=1.0, cooldown=0),
    Action("status", "set_status", "text",
           # The example here MUST stay schematic. A concrete one ("the kitchen") was copied
           # verbatim into unrelated conversations - the model lifted the sample text instead
           # of writing its own, so music talk and cooking both produced "the kitchen".
           "status: the line under your name, if the conversation has put you in a mood or "
           "given you a daft idea worth wearing. Answer with an object of the form "
           '{\"text\": \"<what you are up to, no verb>\", \"activity\": \"<type>\"}, '
           "where type is one of playing, watching, listening, competing, or custom for bare "
           "text with no verb in front. Pick the type that actually reads well, and write text "
           "drawn from THIS conversation. Max 60 characters.",
           fields=("activity",), chance=0.5, cooldown=1800),
    Action("nickname", "set_nickname", "nickname",
           "nickname: a new nickname for yourself in this server, if something in the "
           "conversation is funnier than your current one. Keep your identity recognisable. "
           "Max 32 characters.",
           scope="guild", chance=0.3, cooldown=7200),
    Action("about", "set_about", "text",
           "about: a rewrite of your profile's About Me blurb, if the current one has gone "
           "stale or the conversation suggested something much better. This is permanent "
           "until changed again, so only for something you'd stand behind. Max 200 characters.",
           chance=0.2, cooldown=86400),
]

_actions: dict[str, Action] = {}
_enabled = False
_chance = 1.0
_model: Optional[str] = None
# (action key, scope id) -> unix time of the last run, for cooldowns.
_last_run: dict[tuple[str, int], float] = {}


def configure(cfg: Optional[dict], model: str) -> list[str]:
    """Read the `expression:` config block. Returns the active action keys."""
    global _actions, _enabled, _chance, _model
    cfg = cfg or {}
    _enabled = bool(cfg.get("enabled", True))
    _chance = float(cfg.get("chance", 1.0))
    _model = cfg.get("model") or model

    overrides = cfg.get("actions") or {}
    _actions = {}
    for action in _DEFAULTS:
        override = overrides.get(action.key)
        if override is False:
            continue
        settings = override if isinstance(override, dict) else {}
        if not settings.get("enabled", action.enabled):
            continue
        _actions[action.key] = Action(
            key=action.key, tool=action.tool, arg=action.arg, prompt=action.prompt,
            fields=action.fields, scope=action.scope,
            chance=float(settings.get("chance", action.chance)),
            cooldown=int(settings.get("cooldown", action.cooldown)),
        )
    return list(_actions) if _enabled else []


def _scope_id(action: Action, message) -> int:
    if action.scope == "guild" and getattr(message, "guild", None) is not None:
        return message.guild.id
    return 0


def _eligible(message, already_done: set[str]) -> list[Action]:
    """Actions worth offering the model right now.

    Filters out anything disabled, still cooling down, unavailable as a tool (config or
    missing capability), or already performed during the reply itself - offering an action the
    model just took would invite an immediate duplicate.
    """
    now = time.time()
    out = []
    for action in _actions.values():
        if action.tool in already_done or not tools.is_enabled(action.tool):
            continue
        if action.cooldown:
            last = _last_run.get((action.key, _scope_id(action, message)), 0.0)
            if now - last < action.cooldown:
                continue
        if action.scope == "guild" and getattr(message, "guild", None) is None:
            continue
        out.append(action)
    return out


def _build_prompt(actions: list[Action], bot_name: str) -> str:
    lines = "\n".join(f"  {a.prompt}" for a in actions)
    # Note what this prompt does NOT do: plead for restraint. An earlier version opened with
    # "most of the time the answer is NONE of them", which suppressed the very behaviour the
    # pass exists to produce - reactions fired 2/5 on a message that plainly deserved one.
    # Rarity is enforced afterwards by `chance` and `cooldown`, in code, so the model's job
    # here is an honest judgement of THIS moment, not rationing.
    return (
        f"You are {bot_name}, a member of a Discord server. You have just replied to a "
        f"message. Separately from the reply, judge whether this moment calls for any of the "
        f"actions below.\n\n"
        f"Judge each one independently and include every action that fits - they are not "
        f"alternatives, and a message can easily deserve two. Skip an action when it plainly "
        f"doesn't fit; routine chatter usually deserves none.\n\n"
        # A standing rule, not per-action wording: measurement showed the model filing grief
        # away as a durable fact AND - worse - rewriting its own status and About Me off the
        # back of someone's breakup. Every action needs suppressing on these messages, not
        # just the memory one.
        f"HARD RULE: if the message touches something private or painful - a death, illness, "
        f"mental health, a breakup, money or family trouble - the ONLY action you may take is "
        f"a quiet, respectful reaction. Never store it as a fact about them, and never turn it "
        f"into a status, nickname or About Me. It is theirs, not material.\n\n"
        f"Available actions:\n{lines}\n\n"
        f"Answer with ONLY a JSON object, using one key per action you are taking and omitting "
        f"the rest. {{}} is fine when nothing fits."
    )


def _parse(content: str, actions: list[Action]) -> dict[str, dict]:
    """Pull the chosen actions out of the model's JSON, ignoring anything malformed."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        log.warning(f"Expression pass returned unparseable JSON: {content[:120]!r}")
        return {}
    if not isinstance(data, dict):
        return {}

    chosen = {}
    for action in actions:
        value = data.get(action.key)
        # An action with extra fields may come back as an object; take the main argument from
        # it and keep only the extras it declared. Models also mix the two forms freely, so a
        # bare string stays valid for those actions too.
        extras = {}
        if isinstance(value, dict) and action.fields:
            extras = {k: str(v) for k, v in value.items()
                      if k in action.fields and isinstance(v, (str, int, float))}
            value = value.get(action.arg) or value.get("text") or value.get("value")
        # Models answer "none"/"null"/"" for "no thanks" as often as they omit the key.
        if not isinstance(value, str):
            continue
        value = value.strip()
        if not value or value.lower() in {"none", "null", "no", "n/a", "-"}:
            continue
        chosen[action.key] = {action.arg: value, **extras}
    return chosen


async def express(message, user_text: str, reply_text: str, ctx: ToolContext,
                  already_done: set[str] | None = None) -> list[str]:
    """Decide on and perform spontaneous actions. Returns the tools actually run.

    Never raises: this runs detached from the reply that already went out, so a failure here
    must not surface to the user or take down the task that spawned it.
    """
    if not _enabled or not _actions:
        return []
    try:
        if random.random() > _chance:
            return []
        actions = _eligible(message, already_done or set())
        if not actions:
            return []

        bot_name = "the bot"
        if ctx.client is not None and ctx.client.user is not None:
            bot_name = ctx.client.user.display_name

        client = ollama.AsyncClient(timeout=DECISION_TIMEOUT)
        response = await asyncio.wait_for(
            client.chat(
                model=_model,
                messages=[
                    {"role": "system", "content": _build_prompt(actions, bot_name)},
                    {"role": "user",
                     "content": f"They said:\n{user_text}\n\nYou replied:\n{reply_text}"},
                ],
                # A short, single-purpose prompt: no reasoning pass, and JSON mode so the
                # answer is parseable rather than a sentence about wanting to react.
                think=False,
                format="json",
                options={"num_ctx": 4096},
            ),
            timeout=DECISION_TIMEOUT,
        )
        chosen = _parse(response.message.content or "", actions)
        if not chosen:
            return []

        performed = []
        by_key = {a.key: a for a in actions}
        for key, args in chosen.items():
            action = by_key[key]
            value = args[action.arg]
            # The frequency knob sits HERE, after the decision: the model judges whether the
            # moment deserves it, config decides how often that judgement is acted on.
            if action.chance < 1.0 and random.random() > action.chance:
                log.info(f"Expression: skipping {action.tool} (chance {action.chance})")
                continue
            if action.tool == "remember_fact":
                args["user_id"] = str(message.author.id)
            result = await tools.execute(action.tool, args, ctx)
            _last_run[(action.key, _scope_id(action, message))] = time.time()
            performed.append(action.tool)
            log.info(f"Expression: {action.tool}({value!r}) -> {result}")
        return performed
    except asyncio.TimeoutError:
        log.warning(f"Expression pass timed out after {DECISION_TIMEOUT}s")
    except Exception as e:
        log.warning(f"Expression pass failed: {e}")
    return []

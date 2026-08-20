"""
Detecting replies that CLAIM an action the bot never actually performed.

The failure this exists for: the model writes "Done! Reacted with 🔥" or "Reminder set!"
without ever emitting a tool call, so nothing happened and the user is told it did. No amount
of system-prompt insistence removes this - it is a sampling failure, not a comprehension one -
so it has to be caught after the fact, by comparing what the reply SAYS against what the tool
loop actually RAN.

`verify(reply, executed_tools)` returns the claims in `reply` that no executed tool backs up.
`query_ollama()` uses it to force one corrective round; `evals/run_evals.py` uses the same
function to score how often it happens, so the guard and its measurement can't drift apart.

Design bias: **false negatives over false positives**. Missing a lie costs one bad reply;
flagging an honest one costs a pointless extra model round and a mangled answer. So every rule
requires a completed, first-person, capability-specific statement, and any sentence that
hedges, negates, asks, or offers is discarded before matching (see `_HEDGE_RE`) - "want me to
set a reminder?" and "I can't change my nickname" are not claims.
"""

import re
from dataclasses import dataclass

# Sentinel in a rule's `satisfied_by`: ANY executed tool makes the claim true. Used by the
# generic "Done!"-style rules, which don't name a capability - they just assert completion.
ANY_TOOL = "*"


@dataclass(frozen=True)
class Claim:
    """A statement in a reply that asserts an action the bot can only do via a tool."""
    kind: str                      # capability name, e.g. "reminder" - used in logs
    matched: str                   # the exact text that tripped the rule
    sentence: str                  # the sentence it appeared in (context for the log)
    satisfied_by: tuple[str, ...]  # tools whose execution would make the claim true

    def is_backed_by(self, executed_tools) -> bool:
        if ANY_TOOL in self.satisfied_by:
            return bool(executed_tools)
        return any(t in executed_tools for t in self.satisfied_by)


# Apostrophes: models emit both ' and ’ - every contraction below has to accept either.
_APOS = r"['’]?"

# A sentence carrying any of these is not a claim of completed action: it is a question, an
# offer, a refusal, a hypothetical or a negation. Checked BEFORE the claim rules, so it acts
# as a veto over the whole sentence rather than a per-rule special case.
_HEDGE_RE = re.compile(
    r"""
    \b(?:
        can{a}t | cannot | could(?:n{a}t)? | unable | won{a}t | wouldn{a}t | shouldn{a}t
      | did(?:n{a}t) | does(?:n{a}t) | do(?:n{a}t) | haven{a}t | hasn{a}t | hadn{a}t
      | isn{a}t | wasn{a}t | aren{a}t | weren{a}t | ain{a}t | never | unless | instead\ of
      | want\ me\ to | do\ you\ want | would\ you\ like | should\ i | shall\ i
      | if\ you\ (?:want|like|{a}d\ like) | let\ me\ know | tell\ me | say\ the\ word
      | i\ could | i\ can | maybe\ i | i\ might | trying\ to | tried\ to | need\ (?:to|a)
      | how\ (?:do|can|would) | what\ if
    )\b
    """.format(a=_APOS),
    re.IGNORECASE | re.VERBOSE,
)

# (kind, tools that would make it true, pattern). Ordered most-specific first only for nicer
# logs - `verify` collects every distinct kind that matches, not just the first.
_RULES: list[tuple[str, tuple[str, ...], re.Pattern]] = [
    ("reaction", ("add_reaction",), re.compile(
        rf"\b(?:reacted"
        rf"|(?:added|dropped|threw|slapped|stuck|put|gave)\s+(?:an?|the|it|that|you|your)\b[^.!?\n]{{0,24}}?\breaction"
        # "slapped a 👀 on that" / "gave it a :fire:" - the emoji IS the object, so the word
        # "reaction" never appears. Matching a bare word there ("put a pin on that") would be
        # a false positive, so the object must look like an emoji or a :shortcode:.
        rf"|(?:slapped|stuck|dropped|threw|put|whacked|plonked)\s+(?:an?|the)\s+(?:[^\s\w]{{1,4}}|:[a-z0-9_]+:)\s+on"
        rf"|(?:gave|given|slapped)\s+(?:it|that|this|your\s+message)\s+an?\s+(?:[^\s\w]{{1,4}}|:[a-z0-9_]+:)"
        rf"|reaction\s+(?:added|is\s+(?:on|up|there)|incoming))\b"
        rf"|(?:[^\s\w]{{1,4}}|:[a-z0-9_]+:)\s+(?:slapped|stuck|added|dropped|plonked)\s+on\b",
        re.IGNORECASE)),

    ("reminder", ("set_reminder",), re.compile(
        rf"\b(?:reminder\s+(?:is\s+)?(?:set|saved|locked|ready|going|up)"
        rf"|(?:set|made|created|started|popped)\s+(?:up\s+)?(?:a|the|your|you\s+a)\s+(?:reminder|timer|alarm)"
        rf"|i{_APOS}?(?:ll|ve)\s+(?:remind|ping|poke|nudge|holler\s+at)\s+(?:you|ya)"
        rf"|i{_APOS}?ve\s+got\s+(?:a|your)\s+(?:reminder|timer)"
        rf"|(?:reminder|timer|alarm)\s*{_APOS}?s\s+(?:going|running|ticking|counting))\b",
        re.IGNORECASE)),

    # Fabricated randomness: a die roll or coin flip the user can't check, invented rather
    # than rolled. Narrow patterns only - "rolled my eyes" and "flipped out" must not match.
    ("dice", ("roll_dice", "flip_coin", "random_choice"), re.compile(
        rf"\b(?:rolled\s+(?:you\s+|us\s+|it\s+|me\s+)?(?:an?\s+)?(?:\d+|d\d+|nat|natural)"
        rf"|(?:the\s+)?(?:dice|die|d\d+)\s+(?:says?|gave|landed|came\s+up)"
        rf"|flipped\s+(?:a\s+|the\s+)?coin"
        rf"|(?:it|coin)\s+landed\s+on\s+(?:heads|tails)"
        rf"|coin\s+says)\b",
        re.IGNORECASE)),

    ("music", ("queue_song",), re.compile(
        rf"\b(?:queued"
        rf"|now\s+playing"
        rf"|(?:added|added\s+it|chucked\s+it|slung\s+it)\s+to\s+the\s+queue"
        rf"|(?:it|that|the\s+(?:song|track|tune))\s*{_APOS}?s\s+(?:now\s+)?(?:playing|in\s+the\s+queue|up\s+next)"
        rf"|(?:spinning|blasting|cranking|banging)\s+(?:it|that|this)\s+(?:out|up|now))\b",
        re.IGNORECASE)),

    ("nickname", ("set_nickname",), re.compile(
        rf"\b(?:(?:changed|switched|updated|swapped|flipped)\s+my\s+(?:nick\s?)?name"
        rf"|renamed\s+myself"
        rf"|(?:my\s+)?nick(?:name)?\s+(?:is\s+now|has\s+been\s+changed|changed|updated)"
        rf"|i{_APOS}?m\s+now\s+(?:called|known\s+as|going\s+by)"
        # Seen in production: '@Kronk's new name is "Literal Legend"' - a completed rename
        # stated in the third person, which none of the first-person patterns above catch.
        rf"|new\s+(?:nick\s?)?name\s+is"
        rf"|(?:nick\s?)?name\s+is\s+now"
        rf"|from\s+now\s+on,?\s+i{_APOS}?m\s+(?:called|known)"
        rf"|(?:i\s+)?(?:just\s+)?(?:changed|switched)\s+it\s+(?:up\s+)?to)\b",
        re.IGNORECASE)),

    ("status", ("set_status",), re.compile(
        rf"\b(?:(?:changed|updated|set|switched)\s+my\s+status"
        rf"|my\s+status\s+(?:is\s+)?(?:now|says|reads))\b",
        re.IGNORECASE)),

    ("poll", ("create_poll",), re.compile(
        rf"\b(?:(?:made|created|started|put\s+up|posted|launched|whipped\s+up)\s+(?:a|the|your)\s+poll"
        rf"|poll{_APOS}?s\s+(?:up|live|posted|going|open)"
        rf"|poll\s+(?:is\s+)?(?:up|live|posted|created))\b",
        re.IGNORECASE)),

    ("thread", ("start_thread",), re.compile(
        rf"\b(?:(?:started|made|created|opened|spun\s+up|kicked\s+off)\s+(?:a|the|us\s+a)\s+thread"
        rf"|thread{_APOS}?s\s+(?:up|open|started|live)"
        rf"|thread\s+(?:is\s+)?(?:up|open|started))\b",
        re.IGNORECASE)),

    # Past tense only. "I'll look it up" is an offer/intention, not a claim - and the prompt
    # already pushes the model to just call the tool instead of offering.
    ("lookup", ("web_search", "web_fetch", "wikipedia"), re.compile(
        rf"\b(?:i\s+(?:just\s+)?(?:looked\s+(?:it|that|them|this)\s+up|googled|did\s+a\s+(?:quick\s+)?search)"
        rf"|(?:just\s+)?(?:looked\s+(?:it|that|this)\s+up|searched\s+(?:the\s+web|online|for\s+(?:it|that)))"
        rf"|i\s+(?:just\s+)?checked\s+(?:wikipedia|the\s+web|online|the\s+internet|my\s+sources)"
        rf"|according\s+to\s+(?:my|the)\s+(?:search|research|sources|lookup)"
        rf"|(?:my|the)\s+(?:web\s+)?search\s+(?:says|shows|found|turned\s+up|came\s+back)"
        rf"|(?:from|per)\s+what\s+i\s+(?:found|read)\s+(?:online|on\s+the\s+web|on\s+wikipedia))\b",
        re.IGNORECASE)),

    ("memory", ("remember_fact",), re.compile(
        rf"\b(?:(?:noted|noting|jotted|wrote)\s+(?:that|it|this)\s+down"
        rf"|i{_APOS}?ll\s+remember\s+(?:that|this|it)"
        rf"|i{_APOS}?ve\s+(?:noted|saved|stored|remembered|filed)\s+(?:that|it|this)"
        rf"|(?:added|saved)\s+(?:that|it|this)\s+to\s+my\s+(?:memory|notes|brain))\b",
        re.IGNORECASE)),
]

# Bare completion claims. They name no capability, so ANY executed tool vindicates them - and
# with nothing to anchor on they are the easiest to trip by accident, hence the extra
# restrictions in `_generic_claim`: reply-initial and short.
_GENERIC_RE = re.compile(
    rf"^(?:done|all\s+done|there\s+(?:you|ya)\s+go|all\s+set|taken\s+care\s+of"
    rf"|consider\s+it\s+done|it{_APOS}?s\s+done|handled|sorted|dealt\s+with)\b",
    re.IGNORECASE,
)
# Above this length a reply is substantive enough that a leading "Done!" is a figure of speech
# attached to a real answer, not a bare (false) completion report.
_GENERIC_MAX_CHARS = 120

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\n])\s+")

# Subjects that make a claim someone else's ("Dave started a thread about this last week").
_OTHER_SUBJECTS = {"he", "she", "they", "we", "you", "someone", "somebody", "everyone",
                   "nobody", "dave", "who", "y'all",
                   # Possessives too: "your new nickname is ..." is about the USER, and the
                   # rename patterns would otherwise read it as the bot renaming itself.
                   "your", "yours", "his", "her", "hers", "their", "theirs", "our", "ours"}
# Words that can legitimately sit right before a first-person claim, including the ones a
# subjectless report opens with ("Just queued it up", "Boom, poll's up"). Without this list a
# capitalised sentence-opener would look exactly like a proper noun.
_SELF_SUBJECTS = {"i", "i've", "ive", "i'll", "ill", "just", "so", "and", "but", "then", "also",
                  "already", "ok", "okay", "alright", "well", "yeah", "yep", "yup", "boom",
                  "bam", "there", "now", "right", "hey", "oh", "aight", "done", "kronk",
                  "anyway", "anyways", "welp", "aaand", "annd", "sure", "of", "to", "have",
                  "has", "had", "the", "your", "my", "a", "it", "that", "this", "been"}

_WORD_RE = re.compile(r"[A-Za-z']+")

# Names the bot may use for ITSELF in the third person ("@Kronk's new name is ..."). Without
# these such a sentence looks like it is about somebody else and gets vetoed. Seeded with the
# default persona; main.py calls configure_self_names() with the real display name at startup.
_SELF_NAMES = {"kronk"}


def configure_self_names(*names: str) -> None:
    """Register the bot's own display name(s) so self-reference isn't read as another subject."""
    for name in names:
        if name:
            _SELF_NAMES.add(name.strip().lower())


def _sentences(text: str) -> list[str]:
    """Split a reply into sentences. Crude on purpose - this only feeds the hedge veto."""
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or "") if s.strip()]


def _has_other_subject(prefix: str) -> bool:
    """Whether the words right before a match make it someone ELSE's action.

    "Dave started a thread" is a report about Dave, not a claim by the bot, and the bot talks
    about other users constantly - so without this every retelling of what a user did would
    trip the detector. Detected two ways: an explicit third-person pronoun, or a proper noun
    (capitalised, not one of the ordinary sentence-openers) sitting directly before the verb.
    """
    words = _WORD_RE.findall(prefix)
    if not words:
        return False
    last = words[-1]
    # Strip the possessive: "Kronk's new name" and "Dave's new name" both hinge on the noun.
    low = re.sub(r"'?s$", "", last.lower())
    if low in _SELF_NAMES:
        return False
    if low in _OTHER_SUBJECTS:
        return True
    return last[0].isupper() and low not in _SELF_SUBJECTS


# Roleplayed actions: "*changes nickname to Bucket*". The system prompt bans these outright
# ("writing *changes nickname* does NOTHING"), which is exactly why they need catching - a
# stage direction reads to a user like the action happened. A segment must carry a capability
# keyword AND more than one word, so ordinary italic emphasis ("*really*") is left alone.
_ROLEPLAY_SEGMENT_RE = re.compile(r"\*([^*\n]{3,120})\*")
_ROLEPLAY_KEYWORDS: list[tuple[re.Pattern, str, tuple[str, ...]]] = [
    (re.compile(r"\breact|\bemoji\b", re.IGNORECASE), "reaction", ("add_reaction",)),
    (re.compile(r"\bnick\s?name|\brenames?\b", re.IGNORECASE), "nickname", ("set_nickname",)),
    (re.compile(r"\bstatus\b", re.IGNORECASE), "status", ("set_status",)),
    (re.compile(r"\bpoll\b", re.IGNORECASE), "poll", ("create_poll",)),
    (re.compile(r"\bthread\b", re.IGNORECASE), "thread", ("start_thread",)),
    (re.compile(r"\bremind|\btimer\b|\balarm\b", re.IGNORECASE), "reminder", ("set_reminder",)),
    (re.compile(r"\bsearch|\bgoogles?\b|\blooks?\s+up\b|\bwikipedia\b", re.IGNORECASE),
     "lookup", ("web_search", "web_fetch", "wikipedia")),
    (re.compile(r"\bqueues?\b|\bplays?\b|\bputs?\s+on\b|\bsong\b|\bmusic\b", re.IGNORECASE),
     "music", ("queue_song",)),
    (re.compile(r"\bremembers?\b|\bnotes?\s+(?:that|it|this)\s+down\b", re.IGNORECASE),
     "memory", ("remember_fact",)),
    (re.compile(r"\brolls?\b|\bflips?\b", re.IGNORECASE), "dice",
     ("roll_dice", "flip_coin", "random_choice")),
]


def _roleplay_claims(reply: str) -> list[Claim]:
    """Actions the model *acted out in asterisks* instead of performing."""
    found: dict[str, Claim] = {}
    for segment in _ROLEPLAY_SEGMENT_RE.findall(reply or ""):
        if len(segment.split()) < 2:
            continue
        for pattern, kind, satisfied_by in _ROLEPLAY_KEYWORDS:
            if kind not in found and pattern.search(segment):
                found[kind] = Claim(kind, f"*{segment}*", segment, satisfied_by)
    return list(found.values())


def _generic_claim(reply: str) -> Claim | None:
    """A short reply that opens with a bare completion report ("Done!", "All set")."""
    stripped = (reply or "").strip().lstrip("*_ ")
    if len(stripped) > _GENERIC_MAX_CHARS:
        return None
    match = _GENERIC_RE.match(stripped)
    if not match:
        return None
    first = _sentences(stripped)[0] if _sentences(stripped) else stripped
    if _HEDGE_RE.search(first):
        return None
    return Claim("completion", match.group(0), first, (ANY_TOOL,))


def find_claims(reply: str) -> list[Claim]:
    """Every action-claim in `reply`, regardless of what actually ran. One per kind."""
    found: dict[str, Claim] = {}
    for sentence in _sentences(reply):
        if _HEDGE_RE.search(sentence):
            continue  # question / offer / negation - not an assertion that something happened
        for kind, satisfied_by, pattern in _RULES:
            if kind in found:
                continue
            for match in pattern.finditer(sentence):
                if _has_other_subject(sentence[:match.start()]):
                    continue  # someone else did it - not a claim about the bot
                found[kind] = Claim(kind, match.group(0), sentence, satisfied_by)
                break

    for claim in _roleplay_claims(reply):
        found.setdefault(claim.kind, claim)

    generic = _generic_claim(reply)
    if generic and not found:
        # Only report a bare "Done!" when nothing more specific explains it, so the log names
        # the real capability ("reminder") rather than the vague one.
        found[generic.kind] = generic
    return list(found.values())


def verify(reply: str, executed_tools) -> list[Claim]:
    """Claims in `reply` that the tools actually executed this turn do NOT back up.

    `executed_tools` is the list of tool names that ran during this turn (duplicates fine).
    An empty result means the reply is consistent with what happened.
    """
    executed = set(executed_tools or ())
    return [c for c in find_claims(reply) if not c.is_backed_by(executed)]


def correction_prompt(claim: Claim) -> str:
    """The nudge fed back to the model after it claimed an action it never performed.

    Deliberately gives it BOTH outs - do the thing, or stop saying you did - because forcing
    the tool call would be wrong when the user never actually asked for the action.
    """
    if ANY_TOOL in claim.satisfied_by:
        remedy = ("If the user asked you to do something, call the tool that does it now. "
                  "Otherwise reply again without claiming anything was done.")
    else:
        options = " or ".join(f"`{t}`" for t in claim.satisfied_by)
        remedy = (f"If the user asked for this, call {options} now and then answer from its "
                  f"result. If they did not, reply again without claiming you did it.")
    return (
        f"SYSTEM CORRECTION: your last reply said \"{claim.matched}\", but you called no tool "
        f"this turn, so it did not actually happen and the user would be misled. {remedy} "
        f"Write the reply as if for the first time - do not mention this correction, do not "
        f"apologise for it, and stay in character."
    )

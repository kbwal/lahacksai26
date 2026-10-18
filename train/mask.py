"""Replace hard facts in the user's own messages with typed placeholders.

Training only puts loss on the user's ("me") messages, so this is what teaches
the model to write "[DATE] works!" instead of inventing "Tuesday works!", and
what keeps the user's own numbers and addresses out of drafts to other people.
Other people's messages are never masked: the model needs them as context, and
sees them unmasked at inference.

Dates and times are always masked, even when someone else mentioned them,
because picking one is a decision the user makes (with the calendar fill).
Links, phones, addresses and amounts that someone else already mentioned are
kept, so the model learns to echo facts it was given and to use placeholders
only for the rest. Any fact the user's own request names (train/augment.py) is
kept whatever its type: if they asked for "dinner on tuesday", the message
should say Tuesday.

Regex only, so it is deterministic and dependency-free. Running it on already
masked text is a no-op.
"""

import re

PLACEHOLDERS = ("DATE", "TIME", "AMOUNT", "PHONE", "ADDRESS", "LINK")
ECHO_DEFAULT = frozenset({"LINK", "PHONE", "ADDRESS", "AMOUNT"})

_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\b\.?)"
)
_ORD = r"\d{1,2}(?:st|nd|rd|th)?"
_HOUR = r"(?:1[0-2]|0?[1-9])(?::[0-5]\d)?"
_AMPM = r"(?:[ap]\.m\.|[ap]m\b)"
_NUM = r"\d(?:[\d,]*\d)?(?:\.\d+)?"
_STREET = (
    r"(?i:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Ct|Court"
    r"|Pl|Place|Pkwy|Parkway|Hwy|Highway|Ter|Terrace|Cir|Circle|Sq|Square)"
)

# Order matters: links go first because URLs contain digits that would otherwise
# look like phones or dates, and each match is replaced before the next rule runs.
_RULES: list[tuple[str, re.Pattern]] = [
    ("LINK", re.compile(r"\b(?:https?://|www\.)[^\s<>()\"']+[^\s<>()\"'.,;:!?]")),
    ("LINK", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    (
        "LINK",
        re.compile(
            r"(?<![@\w.])[\w-]+(?:\.[\w-]+)*\.(?:com|org|net|edu|gov|io|us|co|ai|dev|app|me|ly|gg)"
            r"\b(?:/[^\s<>()\"']*[^\s<>()\"'.,;:!?])?"
        ),
    ),
    ("PHONE", re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}\b")),
    ("PHONE", re.compile(r"(?<![\w+])(?:1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")),
    (
        "ADDRESS",
        re.compile(
            rf"\b\d{{1,5}}\s+(?:[NSEW]\.?\s+)?(?:(?:[A-Z][\w'-]*|\d+(?:st|nd|rd|th))\s+){{1,3}}{_STREET}\b\.?"
            r"(?:,?\s*(?i:apt|apartment|suite|ste|unit|fl|floor|\#)\.?\s*[\w-]+)?"
            r"(?:,\s*[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)*,?\s*[A-Z]{2}(?:\s+\d{5}(?:-\d{4})?)?)?"
        ),
    ),
    (
        "AMOUNT",
        re.compile(
            rf"[$€£]\s?{_NUM}(?:\s?(?:k|K|M|million|billion|thousand)\b)?"
            rf"(?:\s?[-–]\s?[$€£]?{_NUM}(?:\s?(?:k|K|M)\b)?)?"
        ),
    ),
    ("AMOUNT", re.compile(rf"\b{_NUM}\s?(?:k\s?)?(?i:dollars?|bucks|usd|eur|gbp|euros?)\b")),
    ("DATE", re.compile(r"\b\d{4}-[01]\d-[0-3]\d\b")),
    ("DATE", re.compile(r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])(?:/(?:\d{4}|\d{2}))?\b")),
    ("DATE", re.compile(rf"\b{_MONTH}\s+{_ORD}\b(?:,?\s+\d{{4}}\b)?")),
    ("DATE", re.compile(rf"\b{_ORD}\s+(?:of\s+)?{_MONTH}(?:,?\s+\d{{4}}\b)?")),
    ("DATE", re.compile(r"\b(?i:mon|tues|wednes|thurs|fri|satur|sun)(?i:day)s?\b")),
    (
        "DATE",
        re.compile(r"\b(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)\b\.?(?=\s*(?:[\d,/]|\[DATE\])|\s+(?:at|morning|afternoon|evening|night)\b)"),
    ),
    ("DATE", re.compile(r"\b(?i:today|tonight|tomorrow|tmrw|tmr|yesterday)\b")),
    ("TIME", re.compile(rf"\b(?:{_HOUR}\s?(?:{_AMPM}\s?)?(?:-|–|to)\s?)?{_HOUR}\s?{_AMPM}", re.IGNORECASE)),
    ("TIME", re.compile(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")),
    ("TIME", re.compile(rf"\b{_HOUR}\s?o'clock\b|\b(?i:noon|midnight)\b")),
    # "at 3" is a time only when what follows reads like the end of a time
    # ("at 3.", "at 3 works", "at 3 then"), not a count ("at 3 tables", "at 3.5").
    (
        "TIME",
        re.compile(
            rf"(?<=\b[Aa]t\s){_HOUR}\b(?![.,]\d)"
            r"(?=\s*(?:$|[^\w\s%$]|(?:is|works?|then|ok|okay|or|and|if|sounds?|should|would|could"
            r"|sharp|on|instead|still|too|please|for|today|tonight|tomorrow)\b))"
        ),
    ),
]

# "Monday, Oct 21" or "Tue 10/21" is one date, and "3pm EST" one time.
_MERGE = [
    (re.compile(r"\[DATE\](?:,?\s+(?:the\s+)?|\s*)\[DATE\]"), "[DATE]"),
    (re.compile(r"\[TIME\]\s*(?:[ECMP][SD]?T|UTC|GMT)\b"), "[TIME]"),
]

# Private-use characters stand in for kept spans while later rules run, so a kept
# link's digits can't be re-matched as a phone. They contain no digits or letters.
_KEEP_BASE = 0xE000
_KEEP = re.compile("[-]")


def _normalize(kind: str, value: str) -> str:
    v = value.lower()
    if kind == "PHONE":
        return re.sub(r"\D", "", v)[-10:]
    if kind == "LINK":
        v = re.sub(r"^(?:https?://)?(?:www\.)?", "", v)
        return v.rstrip("/")
    if kind == "AMOUNT":
        v = re.sub(r"[\s,]", "", v)
        return re.sub(r"\.00\b", "", v)
    return re.sub(r"[^a-z0-9]", "", v)


def _scan(text: str, on_match) -> str:
    """Run every rule in order. `on_match(kind, value)` returns the replacement,
    or None to keep the original span untouched by later rules."""
    kept: list[str] = []

    def sub(kind):
        def repl(m):
            out = on_match(kind, m.group())
            if out is not None:
                return out
            kept.append(m.group())
            return chr(_KEEP_BASE + len(kept) - 1)

        return repl

    text = _KEEP.sub("", text)
    for kind, pattern in _RULES:
        text = pattern.sub(sub(kind), text)
    return _KEEP.sub(lambda m: kept[ord(m.group()) - _KEEP_BASE], text)


def facts(text: str) -> dict[str, set[str]]:
    """Normalized facts found in `text`, by placeholder type."""
    found: dict[str, set[str]] = {k: set() for k in PLACEHOLDERS}

    def record(kind, value):
        found[kind].add(_normalize(kind, value))
        return None

    _scan(text, record)
    return found


def mask_reply(reply: str, thread: str = "", echo: frozenset[str] = ECHO_DEFAULT, request: str = "") -> str:
    """`reply` with facts replaced by [TYPE] placeholders, except `echo` types found in
    `thread` and facts of any type found in `request`."""
    given = facts(thread) if echo and thread else {}
    asked = facts(request) if request else {}

    def repl(kind, value):
        v = _normalize(kind, value)
        if (kind in echo and v in given.get(kind, ())) or v in asked.get(kind, ()):
            return None
        return f"[{kind}]"

    out = _scan(reply, repl)
    for pattern, merged in _MERGE:
        while True:
            new = pattern.sub(merged, out)
            if new == out:
                break
            out = new
    return out

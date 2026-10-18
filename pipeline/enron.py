"""Turn one person's Enron sent mail (rows of the Kaggle emails.csv) into conversation JSONL.

Each sent email becomes one conversation. The history quoted or forwarded below
the user's text (Outlook "-----Original Message-----" blocks, Lotus Notes
header blocks, "Forwarded by" lines, "> " quotes) is split into messages,
each marked "me" or "them" by its sender, and ordered oldest first. The user's
own text comes last. See train/format.py for the record format.

Lotus Notes hard-wraps lines and leaves a space before each break, so those
breaks are joined back up. Otherwise the model would learn to break lines mid-sentence.

The persona is a recipient rule: "social" if nobody at the user's company is on
the email and at least one recipient is a personal address, otherwise
"professional". The newest --test emails are marked type "test".

    uv run python -m pipeline.enron --csv train/data/enron/dasovich-j.csv \\
        --me "dasovich|jdasovic" --test 250 --out train/data/enron/dasovich-j.jsonl
"""

import argparse
import csv
import json
import quopri
import re
import sys
from collections import Counter
from email import message_from_string
from email.utils import parsedate_to_datetime

# Personal ISPs, plus the domains of Jeff Dasovich's friends and family, found by
# reading his most common external recipients.
SOCIAL_DOMAINS = {
    "hotmail.com", "yahoo.com", "aol.com", "earthlink.net", "pacbell.net", "msn.com", "juno.com",
    "mindspring.com", "home.com", "worldnet.att.net", "sonic.net", "mcn.org", "direcpc.com",
    "interx.net", "robertmondavi.com", "perfect.com",
}  # fmt: skip

_DATE = r"\d{1,2}/\d{1,2}/\d{2,4}[ \t]+\d{1,2}:\d{2}(?::\d{2})?[ \t]*[AP]M"

# Lines that only separate messages: the header (if any) follows as its own block.
_SEPARATOR = re.compile(r"^[ \t>]*-{3,}[ \t]*(?:Original Message[ \t]*-{3,}|Forwarded by[^-]*-{5,})[ \t]*$", re.M | re.I)
# Outlook-style header: "From: X" with Sent/Date/To soon after.
_FROM_HEADER = re.compile(
    r"^[ \t>]*(?:From:|-{3,}[ \t]*Message from)[ \t]*(?P<who>[^\n]*)\n(?=(?:[^\n]*\n){0,3}?[ \t>]*(?:Sent|Date|To)[ \t]*:)",
    re.M | re.I,
)
# Lotus Notes header: "Name <addr> [on] 08/29/2000 08:11 PM", or the name on its own line
# above the date (blank lines allowed), with a To:/Subject: line soon after.
_NOTES_HEADER = re.compile(
    rf"^(?:[ \t]*(?P<above>\S[^\n]*)\n(?:[ \t]*\n)*)?[ \t]*(?P<who>[^\n]*?)[ \t]*{_DATE}"
    r"(?:[ \t]*$(?=(?:\n[^\n]*){0,10}?\n[ \t]*(?:To|Subject)[ \t]*:)|(?=[ \t]+To[ \t]*:))",
    re.M,
)
_WROTE = re.compile(r"^(?:[^\n]*?On [^\n]*?,[ \t]*)?(?P<who>[^\n]*?)[ \t]*wrote:[ \t]*$", re.M)
_SUBJECT = re.compile(r"Subject:[^\n]*(?:\n|$)", re.I)
# Header text still left in a message, i.e. a quoted block we couldn't split (mostly Notes'
# multi-column header tables).
_RESIDUE = re.compile(rf"^[ \t]*(?:To|Subject|Sent|From)[ \t]*:|Original Message|Forwarded by|{_DATE}", re.M | re.I)
_HEADER_LINE = re.compile(r"(?:[ \t>]*\n|[ \t>]*(?:To|Cc|Bcc|Sent|Date|Importance)[ \t]*:[^\n]*\n)*", re.I)


def _blocks(body: str) -> list[tuple[int, int, str, str]]:
    """(start, header end, sender, kind) of each quoted or forwarded message, in order."""
    found = [(m.start(), m.end(), "", "separator") for m in _SEPARATOR.finditer(body)]
    found += [(m.start(), m.end(), m["who"], "header") for m in _FROM_HEADER.finditer(body)]
    for m in _NOTES_HEADER.finditer(body):
        who = m["who"].strip()
        if who:  # sender on the date line; the line above belongs to the previous message
            start = body.rfind("\n", 0, m.start("who")) + 1
        else:
            start, who = m.start(), m["above"] or ""
        found.append((start, m.end(), who, "header"))
    found += [(m.start(), m.end(), m["who"], "quote") for m in _WROTE.finditer(body)]

    blocks = []
    for b in sorted(found):
        if not blocks or b[0] >= blocks[-1][1]:
            blocks.append(b)
    return blocks


def _sender(who: str) -> str:
    who = re.sub(r"^\s*From:\s*", "", who, flags=re.I)
    return re.sub(rf"\s+on\s+.*$|\s+on\s*$|-+\s*$", "", who).strip()


def _clean(text: str) -> str:
    text = re.sub(r"^[ \t]*(?:> ?)+", "", text, flags=re.M)  # "> " quoting, possibly nested
    text = re.sub(r"(?<=\S) \n(?=[^\s>])", " ", text)  # Notes soft wraps
    text = "\n".join(l.rstrip() for l in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode(body: str) -> str:
    """Undo quoted-printable encoding that some bodies still carry (=20, =09, "=" line breaks)."""
    if len(re.findall(r"=(?:\n|20|09|3D)", body)) < 2:
        return body
    return quopri.decodestring(body.encode("latin-1", "replace")).decode("utf-8", "replace")


def split_thread(body: str, me: re.Pattern) -> tuple[list[dict], int]:
    """The messages in an email body, oldest first, and how many quoted messages were
    dropped because they still contain header text. The last message is the sender's
    own text, unless it is empty or has header text too (then the caller skips the email)."""
    body = _decode(body)
    blocks = _blocks(body)
    messages = [{"from": "me", "text": _clean(body[: blocks[0][0]] if blocks else body)}]
    for (start, end, who, kind), nxt in zip(blocks, [*blocks[1:], None]):
        limit = nxt[0] if nxt else len(body)
        if kind == "header":
            # The header runs to its Subject: line (To/cc lists wrap freely).
            if (subject := _SUBJECT.search(body, end, limit)) is not None:
                end = subject.end()
            end += _HEADER_LINE.match(body, end, limit).end() - end
        text = _clean(body[end:limit])
        messages.append({"from": "me" if me.search(_sender(who)) else "them", "text": text})
    own, quoted = messages[0], [m for m in reversed(messages[1:]) if m["text"]]
    kept = [m for m in quoted if not _RESIDUE.search(m["text"])]
    dropped = len(quoted) - len(kept)
    if own["text"] and not _RESIDUE.search(own["text"]):
        kept.append(own)
    return kept, dropped


def persona(msg, social_domains: set[str], company: str = "enron.com") -> str:
    domains = [d.lower() for d in re.findall(r"@([\w.-]+)", " ".join(filter(None, [msg["To"], msg["Cc"]])))]
    if domains and not any(d.endswith(company) for d in domains) and any(d in social_domains for d in domains):
        return "social"
    return "professional"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Kaggle emails.csv rows (file, message) of one person's sent mail")
    ap.add_argument("--me", required=True, help="regex matching the user's name/address in quoted headers")
    ap.add_argument("--out", required=True)
    ap.add_argument("--test", type=int, default=0, help="mark the newest N emails as type 'test'")
    args = ap.parse_args()

    csv.field_size_limit(sys.maxsize)
    me = re.compile(args.me, re.I)
    with open(args.csv, newline="") as f:
        rows = [(r["file"], message_from_string(r["message"])) for r in csv.DictReader(f)]
    rows.sort(key=lambda r: parsedate_to_datetime(r[1]["Date"]))

    records, skipped, dropped = [], 0, 0
    for i, (path, msg) in enumerate(rows):
        conversation, n = split_thread(msg.get_payload(), me)
        dropped += n
        if not conversation or conversation[-1]["from"] != "me":
            skipped += 1
            continue
        records.append(
            {
                "adapter_name": persona(msg, SOCIAL_DOMAINS),
                "type": "test" if i >= len(rows) - args.test else "train",
                "conversation": conversation,
                "source": path,
                "date": parsedate_to_datetime(msg["Date"]).isoformat(),
            }
        )

    with open(args.out, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)
    counts = Counter((r["type"], r["adapter_name"]) for r in records)
    print(f"{len(records)} conversations -> {args.out} ({skipped} skipped: the user's own text was empty or unparseable)")
    print(f"  {sum(len(r['conversation']) for r in records)} messages kept, {dropped} quoted messages dropped (unparsed header text)")
    for (t, name), n in sorted(counts.items()):
        print(f"  {t:5} {name:12} {n}")
    lengths = Counter(len(r["conversation"]) for r in records)
    print("  messages per conversation:", ", ".join(f"{k}: {v}" for k, v in sorted(lengths.items())))


if __name__ == "__main__":
    main()

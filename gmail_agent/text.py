"""Comparison and cleaning rules for model text and MIME-decoded mail bodies."""

import re


def comparable_body(text):
    # SMTP changes line endings, not the author's internal spaces or blank lines.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


_ATTRIBUTION = re.compile(
    r"(wrote|a écrit|schrieb|escribió|ha escrito|scrisse|escreveu|schreef|написал)\s*:\s*$",
    re.IGNORECASE,
)
_SEPARATOR = re.compile(
    r"^\s*(-{2,}\s*(Original Message|Forwarded message)\s*-*|_{8,})\s*$", re.IGNORECASE
)
_MOBILE = re.compile(
    r"^\s*(Sent from my \w+|Sent from (Mail|Outlook|Yahoo Mail)|Get Outlook for \w+)",
    re.IGNORECASE,
)
_OUTLOOK_FIELD = re.compile(r"^\s*\*?(Sent|Date|To|Subject|Cc)\s*:\*?\s", re.IGNORECASE)


def _quote_starts(lines, index):
    line = lines[index]
    if _SEPARATOR.match(line) or _MOBILE.match(line) or line in {"--", "-- "}:
        return True
    # "On <date>, <name> wrote:", possibly wrapped onto the next line.
    if re.match(r"^\s*(On|Le|Am|El|Il|Em|Op)\s", line):
        joined = " ".join(lines[index : index + 3])
        if _ATTRIBUTION.search(line) or _ATTRIBUTION.search(" ".join(lines[index : index + 2])):
            return True
        if re.search(r"\bwrote:", joined, re.IGNORECASE) and len(joined) < 400:
            return True
    # Outlook header block: "From: …" followed by Sent/Date/To/Subject fields.
    if re.match(r"^\s*\*?From\s*:", line, re.IGNORECASE):
        return any(_OUTLOOK_FIELD.match(next_line) for next_line in lines[index + 1 : index + 5])
    return False


def clean_text(text):
    """Conservatively remove quoted tails, mobile footers and signature delimiters.

    Training targets and drafting context both go through this, so the model sees
    the same kind of text in both."""
    lines = comparable_body(text).splitlines()
    kept = []
    for index, line in enumerate(lines):
        if _quote_starts(lines, index):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def unwrap(text, width=78):
    """Rejoin plain text that a mail client hard-wrapped at about `width` characters.

    Only applies when the text looks wrapped: no line is longer than the width and
    some lines are close to it."""
    lines = text.split("\n")
    lengths = [len(line.rstrip()) for line in lines]
    if not lengths or max(lengths) > width or not any(n >= width - 18 for n in lengths):
        return text
    out = []
    for line in lines:
        previous = out[-1] if out else ""
        if (
            out
            and line.strip()
            and previous.strip()
            and width - 18 <= len(previous.rstrip()) <= width
            and not previous.lstrip().startswith(">")
            and not re.match(r"^\s*([-*•]|\d+[.)])\s", line)
        ):
            out[-1] = previous.rstrip() + " " + line.lstrip()
        else:
            out.append(line)
    return "\n".join(out)

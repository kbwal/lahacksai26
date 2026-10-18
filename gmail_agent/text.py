"""Comparison and cleaning rules for model text and MIME-decoded mail bodies."""

import re


def comparable_body(text):
    # SMTP changes line endings, not the author's internal spaces or blank lines.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_text(text):
    """Conservatively remove standard quoted tails and signature delimiters.

    Training targets and drafting context both go through this, so the model sees
    the same kind of text in both."""
    lines = comparable_body(text).splitlines()
    kept = []
    for line in lines:
        if (
            re.match(r"^\s*On .+wrote:\s*$", line, re.IGNORECASE)
            or re.match(
                r"^\s*-{2,}\s*(Original Message|Forwarded message)\s*-*\s*$", line, re.IGNORECASE
            )
            or line in {"--", "-- "}
        ):
            break
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()

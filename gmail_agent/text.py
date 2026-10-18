"""Comparison rules for model text and MIME-decoded mail bodies."""


def comparable_body(text):
    # SMTP changes line endings, not the author's internal spaces or blank lines.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()

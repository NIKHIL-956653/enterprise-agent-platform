"""
PII redaction for audit payloads.

Applied inside record() - never at call sites, because a call site that must remember to
redact is a call site that eventually forgets. Shape-based on purpose: anything that LOOKS
like an email, a card number or a phone number is scrubbed, because a false positive costs
one number in a trail and a false negative costs a card number in permanent storage.

The policy, stated once: emails, card-shaped digit runs (13-19 digits), phone-shaped runs.
Names and addresses are NOT caught - shape alone cannot find them. That gap is real and
belongs to a future NER pass, not to a pretence that regexes solve it.
"""

import re
from typing import Any

# Order matters: card before phone, so a 16-digit run is labelled card, not phone.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("phone", re.compile(r"\+?\d[\d\s().-]{8,14}\d")),
)


def redact_text(value: str) -> tuple[str, int]:
    """One string in, scrubbed string and the number of redactions out."""
    count = 0
    for kind, pattern in _PATTERNS:
        value, n = pattern.subn(f"[REDACTED:{kind}]", value)
        count += n
    return value, count


def redact(value: Any) -> tuple[Any, int]:
    """Walk dicts, lists and strings; leave everything else untouched."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        total, out = 0, {}
        for k, v in value.items():
            out[k], n = redact(v)
            total += n
        return out, total
    if isinstance(value, list):
        total, items = 0, []
        for v in value:
            rv, n = redact(v)
            items.append(rv)
            total += n
        return items, total
    return value, 0

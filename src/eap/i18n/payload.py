"""
Which language a run was asked in (EAP-L4).

Every agent takes a different input - `summarize` a text, `answer` a question, `docwriter` a
ticket - so this looks at none of them by name. It gathers every string anywhere in the
payload, however nested, and runs detect() over all of them joined. A new agent gets a
language on its runs the day it is registered, with no per-agent code to remember.

Keys do not vote: "question", "message" and "text" are English whatever the lead typed.
Nor do numbers, booleans or nulls. When the strings carry no confident signal - an empty
payload, a bare number, "ok" - the tenant's default language stands in, so the column is
always a real language and the ?language= filter never has a bucket of "unknown".
"""

from collections.abc import Iterator
from typing import Any

from eap.i18n.detect import detect
from eap.i18n.language import Language


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def language_of(payload: Any, *, default: Language) -> Language:
    """The language of a run's input, or `default` when the input does not say."""
    result = detect("\n".join(_strings(payload)))
    return result.language if result.confident else default

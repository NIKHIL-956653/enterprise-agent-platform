"""
Internationalisation primitives (M6).

Callers import from `eap.i18n`, never from `eap.i18n.detect`, `.language` or `.format`, so
how the package is split into modules - and anything EAP-L6 adds - stays an internal detail.

`__all__` is the contract. A name not in it is not for outside use, and ruff enforces that
the re-exports here are intentional rather than accidental leakage.
"""

from eap.i18n.detect import detect
from eap.i18n.format import (
    DUBAI_TZ,
    Digits,
    format_aed,
    format_date,
    format_datetime,
    format_number,
    isolate,
)
from eap.i18n.language import DEFAULT_LANGUAGE, Detection, Language
from eap.i18n.payload import language_of

__all__ = [
    "DEFAULT_LANGUAGE",
    "DUBAI_TZ",
    "Detection",
    "Digits",
    "Language",
    "detect",
    "format_aed",
    "format_date",
    "format_datetime",
    "format_number",
    "isolate",
    "language_of",
]

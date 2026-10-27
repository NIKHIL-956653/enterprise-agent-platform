"""
Internationalisation primitives (M6).

The public surface of this package is deliberately four names. Callers import from
`eap.i18n`, never from `eap.i18n.detect` or `eap.i18n.language`, so the split between types
and algorithm - and anything added in EAP-L3 and EAP-L6 - stays an internal detail.

`__all__` is the contract. A name not in it is not for outside use, and ruff enforces that
the re-exports here are intentional rather than accidental leakage.
"""

from eap.i18n.detect import detect
from eap.i18n.language import DEFAULT_LANGUAGE, Detection, Language

__all__ = ["DEFAULT_LANGUAGE", "Detection", "Language", "detect"]

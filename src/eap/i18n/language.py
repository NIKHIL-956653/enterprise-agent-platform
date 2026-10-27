"""
Languages the platform speaks, and what a detection result looks like (EAP-L1).

Two types, no behaviour - the algorithm lives in detect.py. Everything downstream is keyed
by this enum's *value* ('en', 'ar'), never by a boolean like is_arabic. A third language is
then a row in tenants.supported_languages plus one member here, not a new branch in every
call site. That is the whole i18n bet (ADR, EAP-L6).
"""

from dataclasses import dataclass
from enum import StrEnum


class Language(StrEnum):
    """A language the platform can detect, store and generate in.

    The value is the BCP-47 primary subtag, and it is the value - not the member name - that
    goes in the database, the JSON and the catalogue filename. So a table dump stays
    readable and a migration constraint can name it literally:
    CHECK (language IN ('en', 'ar')).
    """

    EN = "en"
    AR = "ar"


DEFAULT_LANGUAGE = Language.EN
"""Used when detection has nothing to go on: empty text, digits only, emoji only.

A real language rather than None, because every caller would otherwise have to invent the
same fallback. A tenant's default_language (EAP-L2) overrides it the moment one exists.
"""


@dataclass(frozen=True, slots=True)
class Detection:
    """What detect() concluded, and how much evidence it had.

    `confident` is the field M7 actually depends on. Sticky language with hysteresis
    (EAP-L14) has to tell "this message is Arabic" apart from "this message is one word and
    could be anything" - a lead who answers "ok" in an Arabic thread has not switched to
    English. Keeping that judgement here means callers never re-count letters themselves.

    The raw counts stay on the result because tests assert on them and because a future
    third script would otherwise need this dataclass reshaped rather than extended.
    """

    language: Language
    confident: bool
    arabic_letters: int
    latin_letters: int

    @property
    def letters(self) -> int:
        """How many characters voted. Digits, punctuation, emoji and spaces do not vote."""
        return self.arabic_letters + self.latin_letters

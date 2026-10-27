"""
Tests for language detection (EAP-L1).

Organised around the three rules in detect.py rather than around the functions, because what
can break here is a threshold, not an algorithm. Every number in detect.py has a test that
fails if it moves: ARABIC_FLOOR has the boundary pair, MIN_ARABIC_LETTERS has the stray-char
case, MIN_CONFIDENT_LETTERS has the one-word cases.

Arabic strings are written as literals where they are readable and as \\u escapes where the
point of the test is the specific Unicode block - a presentation form pasted from a PDF looks
identical to a normal letter in an editor, which is exactly why it caused the bug.
"""

import dataclasses

import pytest

from eap.i18n import DEFAULT_LANGUAGE, Language, detect

# Not re-exported from eap.i18n on purpose - it is a helper, not public surface - so the test
# reaches into the module the same way a future detector inside this package would.
from eap.i18n.detect import (
    ARABIC_FLOOR,
    MIN_ARABIC_LETTERS,
    MIN_CONFIDENT_LETTERS,
    is_arabic_char,
)

# --------------------------------------------------------------------------------------
# Nothing to go on: the default, never an exception, never None
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t  ", id="whitespace"),
        pytest.param("12345", id="western-digits"),
        pytest.param("\u0660\u0661\u0662\u0663\u0664", id="arabic-indic-digits"),
        pytest.param("2,500,000", id="formatted-number"),
        pytest.param("!!! ... ???", id="punctuation"),
        pytest.param("\u061f", id="arabic-question-mark-alone"),
        pytest.param("\u064b\u064e\u0651", id="diacritics-alone"),
        pytest.param("\U0001f3e0\U0001f511", id="emoji"),
    ],
)
def test_no_letters_returns_default_unconfident(text: str) -> None:
    """Script-neutral input gets the default language and confident=False.

    confident=False is the load-bearing half. It is what tells EAP-L14 to leave a
    conversation's language alone, so a lead who sends "👍" or a bare price is not treated as
    having switched languages.
    """
    result = detect(text)
    assert result.language is DEFAULT_LANGUAGE
    assert result.confident is False
    assert result.letters == 0
    assert result.arabic_letters == 0
    assert result.latin_letters == 0


def test_arabic_indic_digits_do_not_vote_for_arabic() -> None:
    """٠١٢٣ sits inside the 0600-06FF block but is not a letter, so it carries no signal."""
    assert detect("\u0660\u0661\u0662\u0663").language is Language.EN


# --------------------------------------------------------------------------------------
# Single-script text
# --------------------------------------------------------------------------------------


def test_plain_english() -> None:
    result = detect("hello there")
    assert result.language is Language.EN
    assert result.confident is True
    assert result.arabic_letters == 0
    assert result.latin_letters == 10


def test_plain_arabic() -> None:
    result = detect("مرحبا")
    assert result.language is Language.AR
    assert result.confident is True
    assert result.arabic_letters == 5
    assert result.latin_letters == 0


def test_arabic_sentence_ignores_arabic_punctuation() -> None:
    """The ؟ is punctuation, not a letter - 10 letters vote, not 11."""
    result = detect("ما سعر الشقة؟")
    assert result.language is Language.AR
    assert result.arabic_letters == 10


def test_digits_inside_arabic_do_not_dilute_it() -> None:
    """A price in the middle of an Arabic sentence must not drag the result toward English."""
    result = detect("سعر الشقة 2500000 درهم")
    assert result.language is Language.AR
    assert result.arabic_letters == 12
    assert result.latin_letters == 0


# --------------------------------------------------------------------------------------
# Rule 2: Arabic needs only a small share. The case this whole module exists for.
# --------------------------------------------------------------------------------------


def test_latin_building_name_inside_arabic_question_stays_arabic() -> None:
    """13 Latin letters vs 10 Arabic, and the answer is still Arabic.

    If this ever returns EN we answer an Arabic-speaking lead in English, which is the single
    most visible failure the bilingual build can have.
    """
    result = detect("ما سعر شقة في Marina Heights؟")
    assert result.language is Language.AR
    assert result.arabic_letters == 10
    assert result.latin_letters == 13


def test_email_address_inside_arabic_stays_arabic() -> None:
    """Latin outnumbers Arabic 15 to 9 and Arabic still wins, by design."""
    assert detect("راسلني على ahmed@example.com").language is Language.AR


def test_one_arabic_word_quoted_in_english_stays_english() -> None:
    """The reverse case. 3 Arabic letters in 63 is under the floor, so English holds."""
    result = detect("The developer calls this building Burj, or برج in Arabic marketing material.")
    assert result.language is Language.EN
    assert result.arabic_letters == 3
    assert result.latin_letters == 60


def test_floor_boundary_exactly_at_the_threshold_is_arabic() -> None:
    """2 Arabic of 10 letters is exactly ARABIC_FLOOR, and >= means Arabic wins."""
    result = detect("في Downtown")
    assert result.arabic_letters == 2
    assert result.latin_letters == 8
    assert result.arabic_letters / result.letters == pytest.approx(ARABIC_FLOOR)
    assert result.language is Language.AR


def test_floor_boundary_just_below_the_threshold_is_english() -> None:
    """One more Latin letter - 2 of 11 - drops under the floor and flips the answer."""
    result = detect("في Downtowns")
    assert result.arabic_letters == 2
    assert result.latin_letters == 9
    assert result.arabic_letters / result.letters < ARABIC_FLOOR
    assert result.language is Language.EN


# --------------------------------------------------------------------------------------
# Rule 3: two Arabic letters minimum, whatever the share
# --------------------------------------------------------------------------------------


def test_single_stray_arabic_letter_does_not_flip_short_english() -> None:
    """1 of 3 letters is a 33% share - over the floor - but one character is not evidence.

    Without MIN_ARABIC_LETTERS a fat-fingered keystroke would switch a conversation's
    language, and the lead would get an Arabic reply to "ok".
    """
    result = detect("ok ب")
    assert result.arabic_letters == 1
    assert result.arabic_letters < MIN_ARABIC_LETTERS
    assert result.language is Language.EN


def test_two_arabic_letters_are_enough() -> None:
    """The other side of the same threshold: 2 letters do count."""
    result = detect("hi في")
    assert result.arabic_letters == MIN_ARABIC_LETTERS
    assert result.language is Language.AR


# --------------------------------------------------------------------------------------
# Confidence: the field EAP-L14 consumes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "language"),
    [
        pytest.param("ok", Language.EN, id="english-ok"),
        pytest.param("yes", Language.EN, id="english-yes"),
        pytest.param("نعم", Language.AR, id="arabic-naam"),
        pytest.param("لا", Language.AR, id="arabic-la"),
    ],
)
def test_short_replies_detect_a_language_but_are_not_confident(text: str, language: Language) -> None:
    """A one-word reply still gets a language - it just does not get to change anything.

    This is the "ok" case from the scope decisions: an Arabic lead typing one English word has
    not switched to English, and hysteresis needs to be told that.
    """
    result = detect(text)
    assert result.language is language
    assert result.letters < MIN_CONFIDENT_LETTERS
    assert result.confident is False


def test_four_letters_is_the_confidence_bar() -> None:
    """Exactly MIN_CONFIDENT_LETTERS letters is confident; three is not."""
    assert detect("okay").confident is True
    assert detect("oka").confident is False


# --------------------------------------------------------------------------------------
# Unicode coverage: the blocks that fail silently when missing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("\u0750\u0751\u0752\u0753", id="arabic-supplement"),
        pytest.param("\u08a0\u08a1\u08a2\u08a3", id="arabic-extended-a"),
        pytest.param("\ufefb\ufef2\ufedf\ufee4", id="presentation-forms-b"),
    ],
)
def test_every_arabic_block_counts_as_arabic(text: str) -> None:
    """Arabic pasted out of a PDF or an old Windows app arrives as presentation forms.

    Miss these blocks and the text scores zero Arabic letters and detects as English, with no
    error anywhere - the failure the module docstring calls out as the worst kind.
    """
    result = detect(text)
    assert result.language is Language.AR
    assert result.arabic_letters == 4


def test_presentation_forms_a_counts_as_arabic() -> None:
    result = detect("\ufb50\ufdf2")
    assert result.language is Language.AR
    assert result.arabic_letters == 2


def test_diacritics_do_not_inflate_the_letter_count() -> None:
    """Fully vowelled مَرْحَبًا is 5 letters plus 4 combining marks, and only the 5 vote."""
    result = detect("مَرْحَبًا")
    assert result.language is Language.AR
    assert result.arabic_letters == 5


def test_tatweel_counts_as_an_arabic_letter() -> None:
    """The stretching character ـ is a modifier letter, so it votes Arabic like any other."""
    result = detect("مـــرحبا")
    assert result.language is Language.AR
    assert result.arabic_letters == 8


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("привет мир", id="cyrillic"),
        pytest.param("नमस्ते दुनिया", id="devanagari"),
        pytest.param("你好世界", id="han"),
    ],
)
def test_other_scripts_fall_back_to_the_default(text: str) -> None:
    """A documented limitation, not an oversight.

    While Arabic is the only non-default script, "not Arabic" is the only question worth
    asking, and the platform answers in its default language. The day a third script is
    supported, this test changes with the detector.
    """
    assert detect(text).language is DEFAULT_LANGUAGE


# --------------------------------------------------------------------------------------
# Types and invariants
# --------------------------------------------------------------------------------------


def test_letters_property_sums_both_scripts() -> None:
    result = detect("hello مرحبا")
    assert result.letters == 10
    assert result.letters == result.arabic_letters + result.latin_letters


def test_detection_is_immutable() -> None:
    """Frozen so a detection cannot be edited downstream into saying something else."""
    result = detect("مرحبا")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.language = Language.EN  # type: ignore[misc]


def test_detect_is_deterministic() -> None:
    """Same input, same output, every time - what makes the M8 cassettes replayable."""
    text = "ما سعر شقة في Marina Heights؟"
    assert detect(text) == detect(text)


def test_language_values_are_the_bcp47_subtags() -> None:
    """The *value* is what reaches the database and the CHECK constraint in EAP-L2."""
    assert Language.EN == "en"
    assert Language.AR == "ar"
    assert f"{Language.AR}" == "ar"
    assert sorted(lang.value for lang in Language) == ["ar", "en"]


def test_default_language_is_english() -> None:
    assert DEFAULT_LANGUAGE is Language.EN


@pytest.mark.parametrize(
    ("ch", "expected"),
    [
        pytest.param("م", True, id="arabic-letter"),
        pytest.param("\u0660", True, id="arabic-indic-digit-is-in-the-block"),
        pytest.param("\ufef2", True, id="presentation-form"),
        pytest.param("m", False, id="latin-letter"),
        pytest.param("5", False, id="western-digit"),
        pytest.param(" ", False, id="space"),
    ],
)
def test_is_arabic_char_reports_block_membership_not_letterhood(ch: str, expected: bool) -> None:
    """It answers "is this in an Arabic block", nothing more.

    The Arabic-Indic digit returning True is the point: callers gate on str.isalpha() first,
    which is why detect() does and this helper does not.
    """
    assert is_arabic_char(ch) is expected

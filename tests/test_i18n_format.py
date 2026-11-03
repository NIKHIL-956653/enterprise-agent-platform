"""
Tests for locale formatting (EAP-L3).

Snapshot tests: every expected string is written out by hand, invisible characters included,
so a change to any rule - a separator, a word, a space - fails loudly instead of shipping. The
escapes are spelled out where they matter: \\u00a0 is the no-break space inside a value,
\\u2068 / \\u2069 are the isolate marks, \\u060c is the Arabic comma.

Instants in these tests are built in UTC and rendered in Dubai on purpose. The interesting
bugs live in that conversion - the date that changes at 20:00 UTC, the 12 that is midnight.
"""

import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from eap.i18n import (
    DUBAI_TZ,
    Digits,
    Language,
    format_aed,
    format_date,
    format_datetime,
    format_number,
    isolate,
)

# Reaching into the module for the tables, as the detection tests do for their helper: they
# are data the package keeps to itself, but completeness is a property worth pinning.
from eap.i18n.format import LOCALES, NBSP, NUMBERING

EN, AR = Language.EN, Language.AR

# Friday 30 October 2026, 12:30 UTC = 16:30 in Dubai.
FRIDAY_AFTERNOON = datetime(2026, 10, 30, 12, 30, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# The tables: a language or digit system without a complete row cannot render anything
# --------------------------------------------------------------------------------------


def test_every_language_has_a_complete_locale_row() -> None:
    """Adding a Language member without its row fails here, not mid-conversation."""
    assert set(LOCALES) == set(Language)
    for rules in LOCALES.values():
        assert len(rules.months) == 12
        assert len(rules.weekdays) == 7
        assert len(rules.periods) == 2


def test_every_digit_system_has_ten_real_digits() -> None:
    """Each system's zero is followed by the other nine, and Unicode agrees on their values."""
    assert set(NUMBERING) == set(Digits)
    for numbering in NUMBERING.values():
        for value in range(10):
            assert unicodedata.digit(chr(ord(numbering.zero) + value)) == value


# --------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        pytest.param(0, 0, "0", id="zero"),
        pytest.param(7, 0, "7", id="one-digit"),
        pytest.param(999, 0, "999", id="no-group-yet"),
        pytest.param(1000, 0, "1,000", id="first-group"),
        pytest.param(1234567, 0, "1,234,567", id="millions"),
        pytest.param(-1234, 0, "-1,234", id="negative"),
        pytest.param(Decimal("1234.5"), 2, "1,234.50", id="pads-to-decimals"),
        pytest.param(Decimal("2.665"), 2, "2.67", id="half-up-not-bankers"),
        pytest.param(Decimal("2.675"), 2, "2.68", id="decimal-not-float"),
        pytest.param(Decimal("-0.4"), 0, "0", id="no-negative-zero"),
        pytest.param(Decimal("2.5E+6"), 0, "2,500,000", id="exponent-notation-in"),
    ],
)
def test_format_number_western_digits(value: int | Decimal, decimals: int, expected: str) -> None:
    assert format_number(value, language=EN, decimals=decimals) == expected


def test_arabic_text_uses_western_digits_and_separators_by_default() -> None:
    """CLDR's ar-AE default: Western digits, and with them the Western "," and "."."""
    assert format_number(1234567, language=AR) == "1,234,567"
    assert format_number(Decimal("1234.5"), language=AR, decimals=2) == "1,234.50"


def test_arabic_indic_digits_bring_their_own_separators() -> None:
    """٬ (U+066C) groups and ٫ (U+066B) marks the decimal - neither is a comma."""
    assert format_number(1234567, language=AR, digits=Digits.ARAB) == "١٬٢٣٤٬٥٦٧"
    assert format_number(Decimal("1234.5"), language=AR, digits=Digits.ARAB, decimals=2) == "١٬٢٣٤٫٥٠"
    assert format_number(-1234, language=AR, digits=Digits.ARAB) == "-١٬٢٣٤"


@pytest.mark.parametrize(
    ("value", "error"),
    [
        pytest.param(2.5, TypeError, id="float"),
        pytest.param(True, TypeError, id="bool"),
        pytest.param("123", TypeError, id="string"),
        pytest.param(Decimal("NaN"), ValueError, id="nan"),
        pytest.param(Decimal("Infinity"), ValueError, id="infinity"),
    ],
)
def test_format_number_refuses_values_it_cannot_render_honestly(value: object, error: type) -> None:
    with pytest.raises(error):
        format_number(value, language=EN)  # type: ignore[arg-type]


def test_negative_decimals_are_refused() -> None:
    with pytest.raises(ValueError, match="decimals"):
        format_number(1, language=EN, decimals=-1)


# --------------------------------------------------------------------------------------
# AED
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("language", "digits", "expected"),
    [
        pytest.param(EN, None, "AED 2,500,000", id="en"),
        pytest.param(AR, None, "2,500,000 درهم", id="ar"),
        pytest.param(AR, Digits.ARAB, "٢٬٥٠٠٬٠٠٠ درهم", id="ar-arabic-indic"),
    ],
)
def test_format_aed(language: Language, digits: Digits | None, expected: str) -> None:
    """The currency goes before the amount in English and after it in Arabic."""
    assert format_aed(2_500_000, language=language, digits=digits) == expected


def test_fils_need_decimals_and_are_never_rounded_away() -> None:
    """AED 1,999.50 shown as "AED 2,000" would be a price that is not in the database."""
    with pytest.raises(ValueError, match="decimal places"):
        format_aed(Decimal("1999.50"), language=EN)
    assert format_aed(Decimal("1999.50"), language=EN, decimals=2) == "AED 1,999.50"


def test_trailing_zero_fils_are_not_fils() -> None:
    """2500000.00 out of a NUMERIC(12,2) column is a whole amount and renders as one."""
    assert format_aed(Decimal("2500000.00"), language=EN) == "AED 2,500,000"


# --------------------------------------------------------------------------------------
# Dates and times
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "language", "digits", "expected"),
    [
        pytest.param(date(2026, 10, 30), EN, None, "Fri 30 Oct 2026", id="en"),
        pytest.param(date(2026, 10, 30), AR, None, "الجمعة 30 أكتوبر 2026", id="ar"),
        pytest.param(date(2026, 10, 30), AR, Digits.ARAB, "الجمعة ٣٠ أكتوبر ٢٠٢٦", id="ar-arabic-indic"),
        pytest.param(date(2026, 11, 1), EN, None, "Sun 1 Nov 2026", id="day-not-zero-padded"),
    ],
)
def test_format_date(value: date, language: Language, digits: Digits | None, expected: str) -> None:
    assert format_date(value, language=language, digits=digits) == expected


@pytest.mark.parametrize("language", [EN, AR])
def test_every_month_and_weekday_renders(language: Language) -> None:
    rules = LOCALES[language]
    for month in range(1, 13):
        assert rules.months[month - 1] in format_date(date(2026, month, 1), language=language)
    for offset in range(7):  # 2 Nov 2026 is a Monday, so offset 0..6 walks Mon..Sun
        assert format_date(date(2026, 11, 2 + offset), language=language).startswith(rules.weekdays[offset])


def test_format_date_refuses_a_datetime() -> None:
    """datetime is a date subclass; letting one through would format the UTC calendar day."""
    with pytest.raises(TypeError, match="format_datetime"):
        format_date(FRIDAY_AFTERNOON, language=EN)


@pytest.mark.parametrize(
    ("language", "digits", "expected"),
    [
        pytest.param(EN, None, "Fri 30 Oct 2026, 4:30 PM", id="en"),
        pytest.param(AR, None, "الجمعة 30 أكتوبر 2026، 4:30 م", id="ar"),
        pytest.param(AR, Digits.ARAB, "الجمعة ٣٠ أكتوبر ٢٠٢٦، ٤:٣٠ م", id="ar-arabic-indic"),
    ],
)
def test_format_datetime_in_dubai(language: Language, digits: Digits | None, expected: str) -> None:
    """12:30 UTC is 16:30 in Dubai, on a 12-hour clock in both languages."""
    assert format_datetime(FRIDAY_AFTERNOON, language=language, tz=DUBAI_TZ, digits=digits) == expected


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        pytest.param(
            datetime(2026, 10, 30, 21, 0, tzinfo=UTC),
            "Sat 31 Oct 2026, 1:00 AM",
            id="date-rolls-over-before-utc-does",
        ),
        pytest.param(
            datetime(2026, 10, 30, 20, 5, tzinfo=UTC),
            "Sat 31 Oct 2026, 12:05 AM",
            id="just-after-midnight-is-12-am",
        ),
        pytest.param(
            datetime(2026, 10, 30, 8, 0, tzinfo=UTC),
            "Fri 30 Oct 2026, 12:00 PM",
            id="noon-is-12-pm",
        ),
        pytest.param(
            datetime(2026, 6, 15, 8, 0, tzinfo=UTC),
            "Mon 15 Jun 2026, 12:00 PM",
            id="no-daylight-saving-in-summer",
        ),
    ],
)
def test_format_datetime_edges(utc: datetime, expected: str) -> None:
    assert format_datetime(utc, language=EN, tz=DUBAI_TZ) == expected


def test_any_aware_input_zone_lands_on_the_same_dubai_time() -> None:
    """18:00 in India is 12:30 UTC is 16:30 in Dubai - the input's own zone does not leak through."""
    india = datetime(2026, 10, 30, 18, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert format_datetime(india, language=EN, tz=DUBAI_TZ) == "Fri 30 Oct 2026, 4:30 PM"


def test_format_datetime_refuses_a_naive_datetime() -> None:
    """With no zone there is no instant, so "which day is it in Dubai" has no answer."""
    with pytest.raises(ValueError, match="naive"):
        format_datetime(datetime(2026, 10, 30, 12, 30), language=EN, tz=DUBAI_TZ)


# --------------------------------------------------------------------------------------
# Bidi: isolate() is the only place direction marks come from
# --------------------------------------------------------------------------------------


def test_isolate_wraps_in_first_strong_isolate_and_pop() -> None:
    assert isolate("Marina Heights") == "⁨Marina Heights⁩"


def test_isolate_of_nothing_is_nothing() -> None:
    assert isolate("") == ""


def test_arabic_price_inside_an_english_sentence() -> None:
    sentence = f"Asking price: {isolate(format_aed(2_500_000, language=AR))}."
    assert sentence == "Asking price: ⁨2,500,000 درهم⁩."


def test_english_name_and_arabic_values_inside_an_arabic_sentence() -> None:
    price = isolate(format_aed(2_500_000, language=AR, digits=Digits.ARAB))
    viewing = isolate(format_datetime(FRIDAY_AFTERNOON, language=AR, tz=DUBAI_TZ, digits=Digits.ARAB))
    sentence = f"شقة في {isolate('Marina Heights')} بسعر {price}، والمعاينة {viewing}"
    assert sentence == ("شقة في ⁨Marina Heights⁩ بسعر ⁨٢٬٥٠٠٬٠٠٠ درهم⁩، والمعاينة ⁨الجمعة ٣٠ أكتوبر ٢٠٢٦، ٤:٣٠ م⁩")


BIDI_CONTROLS = set("‎‏؜‪‫‬‭‮⁦⁧⁨⁩")


@pytest.mark.parametrize("language", [EN, AR])
@pytest.mark.parametrize("digits", [Digits.LATN, Digits.ARAB])
def test_formatters_emit_no_direction_marks(language: Language, digits: Digits) -> None:
    """The reason this module exists instead of Babel: no hidden LRM/RLM/ALM, no embeddings.

    The only invisible character a formatter may emit is the no-break space inside a value.
    """
    outputs = [
        format_number(Decimal("-1234.5"), language=language, digits=digits, decimals=2),
        format_aed(2_500_000, language=language, digits=digits),
        format_date(date(2026, 10, 30), language=language, digits=digits),
        format_datetime(FRIDAY_AFTERNOON, language=language, tz=DUBAI_TZ, digits=digits),
    ]
    for text in outputs:
        assert not BIDI_CONTROLS & set(text), repr(text)
        assert all(ch.isprintable() or ch == NBSP for ch in text), repr(text)

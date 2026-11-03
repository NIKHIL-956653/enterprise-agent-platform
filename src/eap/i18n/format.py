"""
Locale formatting for numbers, AED amounts, dates and times (EAP-L3).

Everything a reply shows that a model must never type - a price, an area, a date - is rendered
here, deterministically, from data. M7's renderer (EAP-L12) fills each {{listing:<id>:<field>}}
slot with one of these functions and wraps the result in isolate().

Three rules shape the module.

1. i18n is data, not branches. Each language is one row in LOCALES and each digit system one row
   in NUMBERING; the functions read rows and never ask which language they are in. A third
   language is a new row (plus the CHECK migration and catalogue entries), not a new `if`.

2. Our own table, not Babel. The rows hold CLDR's data - every month, weekday, AM/PM marker and
   separator below was checked against CLDR through Babel 2.18 - but CLDR's patterns do not
   match how UAE listings read, and using them would mean overriding most of the output. Babel
   renders 2,500,000 AED in Arabic with two hidden U+200F right-to-left marks, a ".00" no listing
   shows and the formal symbol "د.إ.", and in English as "AED2,500,000.00" with no space. Owning
   the table also means a dependency upgrade can never quietly change a price string.

3. Formatters emit no invisible direction marks. Bidi control belongs to the caller that knows
   where the text is going: isolate() wraps one value for embedding in a sentence, while a CSV
   export or an API field takes the plain value. The one invisible character a formatter does
   emit is U+00A0 NO-BREAK SPACE inside a value, so "AED 2,500,000" never splits across two
   lines of a chat bubble.

Money and time are strict on purpose. Amounts are int or Decimal - a float turns 2.675 into
2.67 - and format_aed refuses to round fils away silently. Datetimes must carry a zone, because
the date of an instant depends on where you stand: 21:00 UTC on the 30th is the 31st in Dubai.
"""

from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from eap.i18n.language import Language

NBSP = " "  # NO-BREAK SPACE: the space inside a value, so the value never wraps
FSI = "⁨"  # FIRST STRONG ISOLATE
PDI = "⁩"  # POP DIRECTIONAL ISOLATE

# UTC+4 all year: the UAE has no daylight saving time. Built at import so a missing time zone
# database fails the app at startup, not at the first viewing confirmation. On Windows that
# database is the `tzdata` package, which is why pyproject declares it.
DUBAI_TZ = ZoneInfo("Asia/Dubai")


class Digits(StrEnum):
    """A digit system, named by its CLDR numbering-system id."""

    LATN = "latn"  # 0123456789 - CLDR's default for Arabic in the UAE, as well as for English
    ARAB = "arab"  # Arabic-Indic, U+0660-U+0669 - a tenant opt-in


@dataclass(frozen=True, slots=True)
class Numbering:
    """How one digit system writes a number.

    `zero` is enough to find all ten digits, because every Unicode digit system is encoded as
    ten consecutive code points. Separators belong to the digit system, not the language:
    Arabic written with Western digits uses "," and ".", exactly as CLDR's ar-AE latn does.
    """

    zero: str
    group: str
    decimal: str


NUMBERING: dict[Digits, Numbering] = {
    Digits.LATN: Numbering(zero="0", group=",", decimal="."),
    Digits.ARAB: Numbering(
        zero="٠",  # ARABIC-INDIC DIGIT ZERO
        group="٬",  # ARABIC THOUSANDS SEPARATOR - not a comma, though it looks like one
        decimal="٫",  # ARABIC DECIMAL SEPARATOR
    ),
}


@dataclass(frozen=True, slots=True)
class LocaleRules:
    """Everything language-specific about rendering a value. Patterns are str.format strings."""

    digits: Digits  # the digit system used when the caller does not choose one
    money_pattern: str  # {amount}: where the currency word goes, and on which side
    date_pattern: str  # {weekday} {day} {month} {year}
    time_pattern: str  # {hour} {minute} {period}
    datetime_pattern: str  # {date} {time}
    months: tuple[str, ...]  # January first
    weekdays: tuple[str, ...]  # Monday first - the order date.weekday() counts in
    periods: tuple[str, str]  # (AM, PM)


LOCALES: dict[Language, LocaleRules] = {
    Language.EN: LocaleRules(
        digits=Digits.LATN,
        money_pattern="AED {amount}",
        date_pattern="{weekday} {day} {month} {year}",
        time_pattern="{hour}:{minute} {period}",
        datetime_pattern="{date}, {time}",
        months=("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        weekdays=("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        periods=("AM", "PM"),
    ),
    Language.AR: LocaleRules(
        digits=Digits.LATN,
        # The plain word for dirham, after the number - how Arabic listings print a price.
        money_pattern="{amount} درهم",
        date_pattern="{weekday} {day} {month} {year}",
        time_pattern="{hour}:{minute} {period}",
        datetime_pattern="{date}، {time}",  # U+060C ARABIC COMMA, not ","
        # Gulf month names, as CLDR has them for ar-AE (not the Levantine كانون الثاني etc.).
        months=(
            "يناير",
            "فبراير",
            "مارس",
            "أبريل",
            "مايو",
            "يونيو",
            "يوليو",
            "أغسطس",
            "سبتمبر",
            "أكتوبر",
            "نوفمبر",
            "ديسمبر",
        ),
        weekdays=("الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"),
        periods=("ص", "م"),  # صباحاً / مساءً
    ),
}


def _to_decimal(value: int | Decimal) -> Decimal:
    # bool is a subclass of int, and True would otherwise render as "1".
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise TypeError(
            f"expected int or Decimal, got {type(value).__name__}. Floats are refused because "
            "binary rounding turns 2.675 into 2.67 - convert with Decimal(str(x)) at the edge."
        )
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError(f"cannot format {value}")
    return number


def _quantum(decimals: int) -> Decimal:
    if decimals < 0:
        raise ValueError(f"decimals must be 0 or more, got {decimals}")
    return Decimal(1).scaleb(-decimals)  # 0 -> 1, 2 -> 0.01


def _to_digits(text: str, numbering: Numbering) -> str:
    """Rewrite ASCII digits into the target system; leaves every other character alone."""
    offset = ord(numbering.zero) - ord("0")
    return "".join(chr(ord(ch) + offset) if "0" <= ch <= "9" else ch for ch in text)


def format_number(
    value: int | Decimal,
    *,
    language: Language,
    digits: Digits | None = None,
    decimals: int = 0,
) -> str:
    """Group thousands and round half-up to `decimals` places.

    1234567 -> "1,234,567" (or "١٬٢٣٤٬٥٦٧" with Digits.ARAB). Half-up, not banker's rounding:
    2.665 shows as 2.67, which is what a person checking the sum by hand expects.
    """
    numbering = NUMBERING[digits or LOCALES[language].digits]
    number = _to_decimal(value).quantize(_quantum(decimals), rounding=ROUND_HALF_UP)
    # A value that rounds to zero must not print as "-0"; Decimal("-0") < 0 is False.
    sign = "-" if number < 0 else ""
    whole, _, fraction = f"{abs(number):f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", numbering.group)
    body = f"{grouped}{numbering.decimal}{fraction}" if fraction else grouped
    return sign + _to_digits(body, numbering)


def format_aed(
    amount: int | Decimal,
    *,
    language: Language,
    digits: Digits | None = None,
    decimals: int = 0,
) -> str:
    """An AED amount: "AED 2,500,000" / "2,500,000 درهم".

    Whole dirhams by default, because that is how listings price. An amount with fils needs
    decimals=2 and is refused otherwise - a renderer that showed AED 1,999.50 as "AED 2,000"
    would put a number in front of a lead that is not in the database.
    """
    exact = _to_decimal(amount)
    if exact != exact.quantize(_quantum(decimals)):
        raise ValueError(f"{exact} AED has more than {decimals} decimal places; pass decimals=")
    number = format_number(exact, language=language, digits=digits, decimals=decimals)
    return LOCALES[language].money_pattern.format(amount=number)


def _date_text(value: date, rules: LocaleRules) -> str:
    return rules.date_pattern.format(
        weekday=rules.weekdays[value.weekday()],
        day=value.day,
        month=rules.months[value.month - 1],
        year=value.year,
    )


def format_date(value: date, *, language: Language, digits: Digits | None = None) -> str:
    """A calendar date: "Fri 30 Oct 2026" / "الجمعة 30 أكتوبر 2026".

    Takes a date and refuses a datetime (which is a date subclass, so it would otherwise slip
    through). Turning an instant into a date needs a zone - use format_datetime, or call
    .astimezone(tz).date() yourself so the choice of zone is visible.
    """
    if isinstance(value, datetime):
        raise TypeError("format_date takes a date; for an instant use format_datetime")
    rules = LOCALES[language]
    return _to_digits(_date_text(value, rules), NUMBERING[digits or rules.digits])


def format_datetime(
    value: datetime,
    *,
    language: Language,
    tz: tzinfo,
    digits: Digits | None = None,
) -> str:
    """An instant as local time in `tz`: "Fri 30 Oct 2026, 4:30 PM" / "الجمعة 30 أكتوبر 2026، 4:30 م".

    12-hour clock in both languages, as UAE listings and viewing confirmations write it. The
    zone is a required argument rather than a default, so every caller states whose clock the
    lead is reading - DUBAI_TZ for the concierge.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime: attach a zone first - timestamptz columns load as aware")
    local = value.astimezone(tz)
    rules = LOCALES[language]
    time_text = rules.time_pattern.format(
        hour=local.hour % 12 or 12,
        minute=f"{local.minute:02d}",
        period=rules.periods[1 if local.hour >= 12 else 0],
    )
    text = rules.datetime_pattern.format(date=_date_text(local.date(), rules), time=time_text)
    return _to_digits(text, NUMBERING[digits or rules.digits])


def isolate(text: str) -> str:
    """Wrap one value so it keeps its own direction inside a sentence of the other script.

    Without isolation the Unicode bidi algorithm resolves a value's digits and punctuation
    against the surrounding sentence, and a number or a trailing full stop can land visually on
    the wrong side. FSI takes the direction from the value's first strong letter, so one call
    serves both languages: "AED" makes an English price left-to-right, "درهم" makes an Arabic
    one right-to-left.

    Empty text comes back empty - an isolate around nothing is two stray invisible characters.
    """
    return f"{FSI}{text}{PDI}" if text else ""

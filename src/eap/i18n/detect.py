"""
Which language a piece of text is written in (EAP-L1).

Deterministic script counting, no model call. Detection runs on every inbound message, so a
model call here would add latency and cost to the cheapest decision in the system - and
would be non-deterministic, which makes the M8 eval harness unable to replay a conversation
and get the same routing twice.

The method: count the letters, see which script they belong to. Three rules make it behave
on real messages rather than on clean test strings.

1. Only letters vote. Digits, punctuation, whitespace, emoji and combining marks are
   script-neutral in practice - "50000 AED" and "٥٠٠٠٠" tell you about formatting, not about
   the language someone is writing in. Note this puts Arabic-Indic digits on the neutral
   side: a message of nothing but ٠١٢٣ detects as the default, which is correct, because the
   *reply* language is what we are choosing and digits give no signal about it.

2. Arabic needs only a small share to win. A lead writing Arabic will type Latin inside it
   constantly - building names, developer names, "Downtown", "JVC", an email address. If
   detection needed a majority, "ما سعر شقة في Marina Heights؟" would come back English and
   we would answer an Arabic lead in English. A low floor fixes that, and the reverse case
   stays safe because one Arabic word quoted in a long English sentence lands well under it.

3. Two Arabic letters minimum, whatever the share. A single stray Arabic character - a
   mis-keyed character, a copy-paste artefact, a lone ؟ - should not flip a short English
   message, and in a 3-character message one letter is a 33% share.

What this module deliberately does NOT do is decide anything about a conversation. It reports
on one string. Sticky language and hysteresis (EAP-L14) are the conversation-level policy and
they consume `Detection.confident`; keeping the two apart means the thresholds here can be
re-tuned without touching that state machine.
"""

from eap.i18n.language import DEFAULT_LANGUAGE, Detection, Language

# Arabic script blocks, in order. Covers the Arabic alphabet and its diacritics (0600-06FF),
# letters for other languages written in Arabic script (Supplement, Extended-A), and the
# legacy presentation forms that Windows clipboards and older PDFs still emit. Without the
# presentation-form blocks, Arabic pasted out of some documents counts as zero Arabic letters
# and the message detects as English - a silent failure, which is the worst kind here.
ARABIC_RANGES: tuple[tuple[int, int], ...] = (
    (0x0600, 0x06FF),  # Arabic
    (0x0750, 0x077F),  # Arabic Supplement
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB50, 0xFDFF),  # Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
)

# Arabic's share of the voting letters at or above which the text is Arabic. Low on purpose -
# see rule 2 above.
ARABIC_FLOOR = 0.20

# Below this, a share is not evidence, it is an accident. See rule 3.
MIN_ARABIC_LETTERS = 2

# Fewer voting letters than this and the result is a guess, not a reading. EAP-L14 requires
# >= 4 letters of signal before it will consider flipping a conversation's language, so the
# same number is the confidence bar here and there is only one place to change it.
MIN_CONFIDENT_LETTERS = 4


def is_arabic_char(ch: str) -> bool:
    """True if this character sits in an Arabic script block.

    Says nothing about whether it is a letter - 0600-06FF holds Arabic-Indic digits and
    combining marks too. Callers that care gate on str.isalpha() first, as detect() does.
    """
    code = ord(ch)
    return any(start <= code <= end for start, end in ARABIC_RANGES)


def detect(text: str) -> Detection:
    """Read `text` and report which language it is written in.

    Never raises and never returns None: text with nothing to go on - empty, whitespace,
    digits, emoji - comes back as DEFAULT_LANGUAGE with confident=False, which is the signal
    callers use to leave an existing language alone.

    One pass over the string, no allocation per character, so this is cheap enough to run on
    every message and on every chunk of a long document (EAP-L5).
    """
    arabic = 0
    latin = 0

    for ch in text:
        if not ch.isalpha():
            # Digits, punctuation, whitespace, emoji, and Arabic diacritics (category Mn)
            # all land here. Diacritics riding on Arabic letters cost nothing - the base
            # letters they sit on are counted, so a fully vowelled word still votes Arabic.
            continue
        if is_arabic_char(ch):
            arabic += 1
        else:
            # Anything alphabetic that is not Arabic script. Latin is what we actually expect,
            # and the name says so, but a Cyrillic or Devanagari letter counting here is the
            # right behaviour while Arabic is the only non-default script we support: it is
            # "not Arabic", and the platform answers in its default language.
            latin += 1

    letters = arabic + latin
    if letters == 0:
        return Detection(
            language=DEFAULT_LANGUAGE,
            confident=False,
            arabic_letters=0,
            latin_letters=0,
        )

    is_arabic = arabic >= MIN_ARABIC_LETTERS and (arabic / letters) >= ARABIC_FLOOR

    return Detection(
        language=Language.AR if is_arabic else Language.EN,
        confident=letters >= MIN_CONFIDENT_LETTERS,
        arabic_letters=arabic,
        latin_letters=latin,
    )

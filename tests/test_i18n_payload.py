"""
Which language a run was asked in (EAP-L4), from its input payload alone. No database.
"""

import pytest

from eap.i18n import Language, language_of

EN, AR = Language.EN, Language.AR


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param({"message": "hello there"}, EN, id="echo-english"),
        pytest.param({"message": "مرحبا بكم"}, AR, id="echo-arabic"),
        pytest.param({"question": "ما سعر شقة في Marina Heights؟"}, AR, id="answer-arabic-with-latin-name"),
        pytest.param({"text": "The quarterly report is attached.", "max_words": 50}, EN, id="summarize"),
        pytest.param({"ticket": {"title": "عطل في الدفع", "body": "لا يعمل زر الدفع"}}, AR, id="nested-dict"),
        pytest.param({"items": ["مرحبا", "أهلا", "كيف الحال"]}, AR, id="list-of-strings"),
    ],
)
def test_detects_from_any_string_anywhere_in_the_payload(payload, expected) -> None:
    assert language_of(payload, default=EN) is expected


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty"),
        pytest.param({"n": 42, "flag": True, "none": None}, id="no-strings"),
        pytest.param({"message": "ok"}, id="too-short-to-be-confident"),
        pytest.param({"message": "12345 !!!"}, id="digits-only"),
    ],
)
def test_no_confident_signal_means_the_tenant_default(payload) -> None:
    """The default is whatever the tenant says, so an Arabic-first tenant's "ok" is Arabic."""
    assert language_of(payload, default=EN) is EN
    assert language_of(payload, default=AR) is AR


def test_keys_do_not_vote() -> None:
    """ "question", "message", "text" are English however the lead wrote the value."""
    assert language_of({"question": "كم", "message": "لا"}, default=EN) is AR


def test_a_confident_reading_beats_the_default() -> None:
    assert language_of({"message": "hello there"}, default=AR) is EN

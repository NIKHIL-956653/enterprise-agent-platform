"""
Chunking.

No database, no model, no fixtures — so there is no excuse for thin coverage here, and this is
the file that catches the bugs which would otherwise surface as "retrieval feels bad".
"""

import pytest

from eap.rag.chunking import chunk

PARAGRAPHS = (
    "Dubai Marina is a waterfront community on the western edge of the city. "
    "It is built around an artificial canal and is dense with high-rise towers.\n\n"
    "Units there are mostly apartments, from studios to four-bedroom penthouses. "
    "Service charges are higher than average because of the marina upkeep.\n\n"
    "The area is served by two metro stations and a tram loop. "
    "Parking is included with most units but visitor parking is limited."
)


def test_empty_text_yields_no_chunks() -> None:
    assert chunk("") == []


@pytest.mark.parametrize("text", ["   ", "\n\n", "\t\n  \n"])
def test_whitespace_only_yields_no_chunks(text: str) -> None:
    assert chunk(text) == []


def test_short_text_is_one_chunk_unchanged() -> None:
    assert chunk("A one bedroom in JBR.") == ["A one bedroom in JBR."]


def test_no_chunk_exceeds_the_size_budget() -> None:
    # The invariant that matters: an oversized chunk is rejected by the embedder or the vector
    # column, thousands of rows into an ingestion, with an opaque error.
    for c in chunk(PARAGRAPHS, size=120, overlap=20):
        assert len(c) <= 120


def test_chunks_are_split_on_paragraph_boundaries_when_they_fit() -> None:
    # size chosen so each paragraph fits alone but two do not.
    chunks = chunk(PARAGRAPHS, size=200, overlap=0)
    assert len(chunks) >= 3
    assert chunks[0].startswith("Dubai Marina")


def test_overlap_carries_the_previous_tail_forward() -> None:
    chunks = chunk(PARAGRAPHS, size=200, overlap=40)
    assert len(chunks) > 1
    # Something from the end of chunk 0 must reappear at the start of chunk 1, or a sentence
    # whose subject was named in the previous chunk retrieves without its referent.
    tail_words = chunks[0].split()[-3:]
    assert any(w in chunks[1] for w in tail_words)


def test_zero_overlap_carries_nothing() -> None:
    chunks = chunk(PARAGRAPHS, size=200, overlap=0)
    assert len(chunks) > 1
    assert not chunks[1].startswith(chunks[0].split()[-1])


def test_a_sentence_longer_than_a_chunk_is_hard_split() -> None:
    monster = "x" * 500
    chunks = chunk(monster, size=100, overlap=0)
    assert len(chunks) == 5
    assert all(len(c) == 100 for c in chunks)


def test_every_chunk_has_content() -> None:
    # Empty or whitespace chunks get embedded, cost money, and match nothing.
    assert all(c.strip() for c in chunk(PARAGRAPHS, size=90, overlap=15))


def test_no_text_is_lost() -> None:
    # Every word of the source must appear somewhere in the output. Silently dropping a
    # paragraph is the worst failure mode here: retrieval just never finds it and nothing errors.
    chunks = chunk(PARAGRAPHS, size=150, overlap=0)
    joined = " ".join(chunks)
    for word in PARAGRAPHS.split():
        assert word in joined


@pytest.mark.parametrize(
    ("size", "overlap"),
    [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 150)],
)
def test_invalid_parameters_are_rejected(size: int, overlap: int) -> None:
    # overlap >= size is the dangerous one: it is an infinite loop, not a wrong answer.
    with pytest.raises(ValueError):
        chunk("some text", size=size, overlap=overlap)

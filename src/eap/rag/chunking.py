"""
Splitting a document into retrievable chunks.

Pure: no database, no model, no clock. Every decision about retrieval quality that can be made
without a network call is made here, which is why this is the one part of the RAG path with
exhaustive tests.

Why not a fixed slice every `size` characters: it cuts mid-word and mid-sentence, so a chunk
opens with half a thought and closes with half another. The embedding of a fragment lands
somewhere between the two ideas it straddles and matches neither query well. So the text breaks
on the boundaries a writer already put there — paragraphs first, then sentences — and is only
cut mid-sentence when a single sentence is longer than a whole chunk.

Sizes are in CHARACTERS, not tokens. A real tokenizer is a dependency and a model-specific one
at that; for prose the ratio is stable enough that a character budget is a fine proxy, and
`token_count` on the row is computed at ingestion where the embedder is already in hand.
"""

import re

# A blank line is the strongest signal a writer gives about where one idea ends.
_PARAGRAPH = re.compile(r"\n\s*\n")

# Sentence end followed by whitespace. Deliberately naive: it over-splits on "e.g." and on
# abbreviations. That asymmetry is chosen — over-splitting costs a slightly short chunk, while
# under-splitting costs a chunk that blows the budget and gets hard-cut mid-word instead.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def chunk(text: str, *, size: int = 1200, overlap: int = 150) -> list[str]:
    """
    Split `text` into chunks of at most `size` characters, each carrying `overlap`
    characters of the previous chunk's tail.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")
    if overlap >= size:
        # Otherwise every chunk starts with at least as much carried text as it can hold, the
        # loop never advances, and you get an infinite loop that only appears on a long
        # document in production.
        raise ValueError("overlap must be smaller than size")

    units = _split_into_units(text, size)
    if not units:
        return []
    return _pack(units, size=size, overlap=overlap)


def _split_into_units(text: str, size: int) -> list[str]:
    """Break text into pieces that each fit inside one chunk, preferring human boundaries."""
    units: list[str] = []

    for paragraph in _PARAGRAPH.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            units.append(paragraph)
            continue

        for sentence in _SENTENCE.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= size:
                units.append(sentence)
            else:
                # Longer than an entire chunk: a URL, a table row, minified text. There is no
                # human boundary left to respect, so cut it hard rather than emit something
                # oversized that the vector column would reject downstream.
                units.extend(sentence[i : i + size] for i in range(0, len(sentence), size))

    return units


def _pack(units: list[str], *, size: int, overlap: int) -> list[str]:
    """Greedily fill chunks, carrying the tail of each one into the next."""
    chunks: list[str] = []
    current = ""

    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= size:
            current = candidate
            continue

        if current:
            chunks.append(current)

        # Carry context forward so a boundary does not orphan the sentence that explains the
        # next one — the pronoun problem: "It also includes parking" is useless without the
        # sentence naming what "it" is.
        carried = _tail(current, overlap) if overlap else ""
        current = f"{carried}\n\n{unit}".strip() if carried else unit

        # Carry plus one unit can exceed the budget when the unit is nearly a full chunk.
        # Losing the carry is strictly better than emitting an oversized chunk.
        if len(current) > size:
            current = unit

    if current:
        chunks.append(current)
    return chunks


def _tail(text: str, length: int) -> str:
    """The last `length` characters, snapped forward to a word boundary."""
    if len(text) <= length:
        return text
    tail = text[-length:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail

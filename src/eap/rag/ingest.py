"""
Document ingestion: text in, chunks and vectors out.

Ordering matters here. The text is chunked and embedded BEFORE anything is written, so a
failed or rate-limited embedding call leaves no half-ingested document behind — the caller's
transaction never opened a write it has to roll back mid-corpus.

Vectors are bound with an explicit CAST(... AS vector) rather than through the ORM column
type. pgvector's SQLAlchemy type binds a vector as a string, while its asyncpg codec expects
binary, so relying on either one means relying on a driver-level type registration that is
easy to lose and fails opaquely at the first INSERT. The cast makes the wire format
unambiguous and needs nothing registered.
"""

import re
import secrets
import unicodedata
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.llm.base import Embedder
from eap.llm.gemini import get_embedder
from eap.rag.chunking import chunk


class EmptyDocument(ValueError):
    """The text produced no chunks - nothing to embed and nothing worth storing."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: UUID
    external_id: str
    chunk_count: int


def _default_embedder() -> Embedder:
    """
    Indirection so tests can swap in a fake by monkeypatching this name.

    Same shape as runner._default_llm, and for the same reason: the embedder is fetched
    inside the service rather than injected by FastAPI, so dependency_overrides cannot see
    it. EAP-25 covers turning both into real dependencies.
    """
    return get_embedder()


def _to_pgvector(values: list[float]) -> str:
    """pgvector's text input format: a bracketed, comma-separated list."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


# A document posted without an external_id gets one derived from its title (D4), so a
# citation reads "marina-handbook-3f9a2c1d" rather than "upload:35a9fc7b-75cf-...". The
# random suffix is what keeps "omitted means a new document every time" true without
# looking anything up. A lookup here would run under RLS, which hides documents above the
# caller's clearance - so an id scheme that checked whether "exam-key" was taken would tell
# a student that a document with that title exists at a level they cannot read.
SLUG_MAX = 60
SUFFIX_HEX = 8


def slugify(title: str) -> str:
    """Case-folded; letters and digits in any script kept; everything else collapsed to '-'."""
    s = unicodedata.normalize("NFKC", title).casefold()
    s = re.sub(r"[^\w]+", "-", s)
    s = re.sub(r"[-_]+", "-", s).strip("-")
    return s[:SLUG_MAX].rstrip("-") or "document"


def _suffix() -> str:
    """Indirection so a test can force a collision."""
    return secrets.token_hex(SUFFIX_HEX // 2)


def auto_external_id(title: str) -> str:
    return f"{slugify(title)}-{_suffix()}"


# Uniqueness is (tenant_id, classification, external_id) since migration 0012 (EAP-61,
# ADR-006). A re-post therefore only ever conflicts with a row at the SAME classification -
# one the caller can see, because they are writing at that level. A copy of the id above
# their clearance is not in their way, so there is nothing for RLS to refuse and nothing for
# the response to reveal.
_UPSERT_BY_EXTERNAL_ID = text(
    "INSERT INTO documents (id, tenant_id, external_id, title, source, classification) "
    "VALUES (gen_random_uuid(), :tid, :xid, :title, :source, :cls) "
    "ON CONFLICT (tenant_id, classification, external_id) DO UPDATE "
    "SET title = EXCLUDED.title, source = EXCLUDED.source, updated_at = now() "
    "RETURNING id"
)

# Re-posting an id at a new classification is how a document gets reclassified, so the copy
# at the old level goes - but only if the caller can see it. RLS scopes this DELETE like every
# other statement: a copy above their clearance is untouched and unmentioned. It runs on every
# explicit-id ingest whether or not such a copy exists, so neither the response nor the work
# done depends on what the caller cannot see. Chunks follow by ON DELETE CASCADE.
_DROP_VISIBLE_COPIES_AT_OTHER_LEVELS = text(
    "DELETE FROM documents WHERE tenant_id = :tid AND external_id = :xid AND classification <> :cls"
)

# DO NOTHING, not DO UPDATE: an auto-generated id must never replace a document that happens
# to share its title and suffix - visible to this caller or not. No row back means the id
# was taken, and the caller draws another.
_INSERT_FRESH = text(
    "INSERT INTO documents (id, tenant_id, external_id, title, source, classification) "
    "VALUES (gen_random_uuid(), :tid, :xid, :title, :source, :cls) "
    "ON CONFLICT (tenant_id, classification, external_id) DO NOTHING "
    "RETURNING id"
)


async def ingest_document(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    title: str,
    body: str,
    external_id: str | None = None,
    source: str = "upload",
    classification: int = 0,
    embedder: Embedder | None = None,
) -> IngestResult:
    """
    Chunk, embed and store one document, replacing any previous version of the same source.

    `external_id` is the caller's own identifier - a Jira key, a Confluence page id, a
    filename. Re-posting the same one replaces its chunks instead of adding a second near
    duplicate that competes with the first for every query. Re-posting it at a different
    classification moves the document there, for every copy the caller can see (ADR-006).

    `classification` is the sensitivity level (0 public .. 3 restricted). The RLS policy's
    WITH CHECK rejects a value above the caller's own clearance, so this needs no guard
    here - attempting it fails at the database rather than in a rule someone can forget.
    """
    chunks = chunk(body)
    if not chunks:
        raise EmptyDocument("document produced no chunks")

    # Before any write. A model outage must not leave a document row with no chunks under it,
    # which would look like a successfully ingested but permanently unsearchable document.
    embedder = embedder or _default_embedder()
    vectors = await embedder.embed_documents(chunks)

    params = {"tid": str(tenant_id), "title": title, "source": source, "cls": int(classification)}
    if external_id:
        await session.execute(_DROP_VISIBLE_COPIES_AT_OTHER_LEVELS, {**params, "xid": external_id})
        document_id = (
            await session.execute(_UPSERT_BY_EXTERNAL_ID, {**params, "xid": external_id})
        ).scalar_one()
    else:
        for _attempt in range(5):
            external_id = auto_external_id(title)
            document_id = (
                await session.execute(_INSERT_FRESH, {**params, "xid": external_id})
            ).scalar_one_or_none()
            if document_id is not None:
                break
        else:  # five 32-bit collisions on one title: something other than chance is going on
            raise RuntimeError("could not allocate a unique external_id")

    # Replace rather than append. Re-chunking with different settings would otherwise leave
    # the old chunks alongside the new ones, both embedded, both matching, silently doubling
    # the corpus every time someone re-ingests.
    await session.execute(
        text("DELETE FROM document_chunks WHERE document_id = :did"), {"did": str(document_id)}
    )

    await session.execute(
        text(
            "INSERT INTO document_chunks "
            "(id, tenant_id, document_id, ordinal, content, embedding, token_count, classification) "
            "VALUES (gen_random_uuid(), :tid, :did, :ordinal, :content, "
            "CAST(:embedding AS vector), :tokens, :cls)"
        ),
        [
            {
                "tid": str(tenant_id),
                "did": str(document_id),
                "ordinal": i,
                "content": content,
                "embedding": _to_pgvector(vector),
                # Characters over four: a rough proxy, honest about being one. It exists for
                # cost reporting, not for billing, and a real tokenizer is a model-specific
                # dependency this layer does not need.
                "tokens": max(len(content) // 4, 1),
                "cls": int(classification),
            }
            for i, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )

    return IngestResult(document_id=document_id, external_id=external_id, chunk_count=len(chunks))

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

import uuid
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


async def ingest_document(
    *,
    session: AsyncSession,
    tenant_id: UUID,
    title: str,
    body: str,
    external_id: str | None = None,
    source: str = "upload",
    embedder: Embedder | None = None,
) -> IngestResult:
    """
    Chunk, embed and store one document, replacing any previous version of the same source.

    `external_id` is the caller's own identifier - a Jira key, a Confluence page id, a
    filename. Re-posting the same one replaces its chunks instead of adding a second near
    duplicate that competes with the first for every query.
    """
    chunks = chunk(body)
    if not chunks:
        raise EmptyDocument("document produced no chunks")

    # Before any write. A model outage must not leave a document row with no chunks under it,
    # which would look like a successfully ingested but permanently unsearchable document.
    embedder = embedder or _default_embedder()
    vectors = await embedder.embed_documents(chunks)

    external_id = external_id or f"upload:{uuid.uuid4()}"

    document_id = (
        await session.execute(
            text(
                "INSERT INTO documents (id, tenant_id, external_id, title, source) "
                "VALUES (gen_random_uuid(), :tid, :xid, :title, :source) "
                "ON CONFLICT (tenant_id, external_id) DO UPDATE "
                "SET title = EXCLUDED.title, source = EXCLUDED.source, updated_at = now() "
                "RETURNING id"
            ),
            {"tid": str(tenant_id), "xid": external_id, "title": title, "source": source},
        )
    ).scalar_one()

    # Replace rather than append. Re-chunking with different settings would otherwise leave
    # the old chunks alongside the new ones, both embedded, both matching, silently doubling
    # the corpus every time someone re-ingests.
    await session.execute(
        text("DELETE FROM document_chunks WHERE document_id = :did"), {"did": str(document_id)}
    )

    await session.execute(
        text(
            "INSERT INTO document_chunks "
            "(id, tenant_id, document_id, ordinal, content, embedding, token_count) "
            "VALUES (gen_random_uuid(), :tid, :did, :ordinal, :content, "
            "CAST(:embedding AS vector), :tokens)"
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
            }
            for i, (content, vector) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )

    return IngestResult(document_id=document_id, external_id=external_id, chunk_count=len(chunks))

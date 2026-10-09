"""
Retrieval: a query string in, this tenant's most similar chunks out.

No tenant filter in the SQL below. That is not an omission - the session carries
app.tenant_id and RLS scopes `document_chunks` before the similarity search sees a row.
Tenant B's vectors are not ranked low for tenant A's query; they do not exist for it.
EAP-35 proves that by asserting an empty result rather than a poor one.

Two numbers decide whether this is useful or dangerous.

top_k bounds how much context the answer agent gets. Too few and the answer is thin; too
many and the real passage is buried among near-misses that cost tokens and invite the model
to blend sources.

min_similarity is the one that matters. A vector search ALWAYS returns k rows - ask "what is
the capital of France" against an HR corpus and you get the five most France-ish HR chunks,
ranked confidently. Without a floor the answer agent would cite them. The floor is what
makes "I don't know" reachable.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.llm.base import Embedder

# The MODULE, not the names inside it. `from ingest import _default_embedder` would bind a
# copy at import time, and a test patching ingest._default_embedder would not affect this
# module - it would quietly call the real Gemini client instead of the fake.
from eap.rag import ingest

DEFAULT_TOP_K = 5
MAX_TOP_K = 20

# Tuned for L2-normalised 768-dim Gemini vectors, where unrelated text lands around 0.2-0.3
# and a genuine match sits comfortably above 0.5. Deliberately a parameter, because the right
# value is corpus-dependent and the only honest way to pick it is to measure.
DEFAULT_MIN_SIMILARITY = 0.35


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    document_title: str
    external_id: str
    ordinal: int
    content: str
    similarity: float


async def retrieve(
    *,
    session: AsyncSession,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    embedder: Embedder | None = None,
) -> list[RetrievedChunk]:
    """Embed the query and return this tenant's closest chunks, nearest first."""
    if not query.strip():
        # No embedding call, no database round trip. An empty query has no nearest
        # neighbour, and charging for one would be the wrong answer dressed as a result.
        return []

    top_k = max(1, min(MAX_TOP_K, top_k))

    embedder = embedder or ingest._default_embedder()
    # embed_query, not embed_documents: Gemini takes a task_type, and a query embedded as a
    # document lands somewhere slightly different from the same text embedded as a query.
    # Using the wrong one degrades recall silently - nothing errors, results just get worse.
    vector = ingest._to_pgvector(await embedder.embed_query(query))

    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.document_id, d.title, d.external_id, c.ordinal, c.content, "
                "       1 - (c.embedding <=> CAST(:q AS vector)) AS similarity "
                "FROM document_chunks c "
                "JOIN documents d ON d.id = c.document_id "
                "WHERE 1 - (c.embedding <=> CAST(:q AS vector)) >= :min_sim "
                # Order by the raw distance operator, not the computed similarity. Same
                # ranking, but this is the form an IVFFlat/HNSW index can serve later.
                "ORDER BY c.embedding <=> CAST(:q AS vector) "
                "LIMIT :k"
            ),
            {"q": vector, "min_sim": float(min_similarity), "k": top_k},
        )
    ).all()

    return [
        RetrievedChunk(
            chunk_id=r[0],
            document_id=r[1],
            document_title=r[2],
            external_id=r[3],
            ordinal=r[4],
            content=r[5],
            similarity=float(r[6]),
        )
        for r in rows
    ]

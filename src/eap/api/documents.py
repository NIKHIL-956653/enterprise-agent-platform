"""
Document endpoints.

POST /v1/documents   chunk, embed and store a document for this tenant
GET  /v1/documents   list this tenant's documents, newest first (EAP-52)

No query here names a tenant and none needs to: the session from get_tenant_session carries
app.tenant_id and RLS does the rest, exactly as in runs.py.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.deps import get_current_tenant_id, get_tenant_session
from eap.llm.base import LLMError
from eap.models.document import Document, DocumentChunk
from eap.rag.ingest import EmptyDocument, ingest_document

router = APIRouter(prefix="/documents", tags=["documents"])


class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=200_000)
    # The caller's own id for this source. Supplied means "this is the same document as last
    # time", and re-posting replaces its chunks. Omitted means a new document every time,
    # under an id derived from the title: "marina-handbook-3f9a2c1d" (D4).
    external_id: str | None = Field(default=None, max_length=200)
    source: str = Field(default="upload", max_length=50)
    # 0 public, 1 internal, 2 confidential, 3 restricted. Above your own clearance is
    # rejected by the database, not by a check here.
    classification: int = Field(default=0, ge=0, le=3)


class IngestResponse(BaseModel):
    document_id: UUID
    external_id: str
    chunk_count: int


@router.post("", response_model=IngestResponse, status_code=status.HTTP_201_CREATED)
async def create_document(
    payload: IngestRequest,
    tenant_id: Annotated[UUID, Depends(get_current_tenant_id)],
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
) -> IngestResponse:
    try:
        result = await ingest_document(
            session=session,
            tenant_id=tenant_id,
            title=payload.title,
            body=payload.body,
            external_id=payload.external_id,
            source=payload.source,
            classification=payload.classification,
        )
    except DBAPIError as e:
        # The RLS WITH CHECK refused the row. The only way to trip it is classifying a
        # document above your own clearance, so say that rather than leaking a driver error.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="classification exceeds your clearance",
        ) from e
    except EmptyDocument as e:
        # 422, not 500: the caller sent something, it just had no content in it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="document produced no chunks",
        ) from e
    except LLMError as e:
        # 503, not 500: the request was fine and retrying later is the right response.
        # The provider's own message never reaches the caller - see llm/base.py.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="embedding unavailable",
        ) from e

    return IngestResponse(
        document_id=result.document_id,
        external_id=result.external_id,
        chunk_count=result.chunk_count,
    )


class DocumentSummary(BaseModel):
    document_id: UUID
    title: str
    external_id: str
    source: str
    classification: int
    chunk_count: int
    created_at: datetime


class DocumentPage(BaseModel):
    items: list[DocumentSummary]
    # Send this back as ?before=... for the next page. None means there is no next page.
    next_before: datetime | None


@router.get("", response_model=DocumentPage)
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_tenant_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[datetime | None, Query()] = None,
) -> DocumentPage:
    """
    The tenant's documents, newest first - and never their bodies: a list that returns
    200KB per row is how a browse screen becomes a bulk-export endpoint. Keyset pagination,
    same contract as /v1/runs (and the same EAP-37 tie caveat). No tenant or clearance
    filter in the query: the session carries both, and RLS applies them - a document above
    the caller's clearance simply does not exist here.
    """
    if before is not None and before.tzinfo is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before must include a timezone")

    stmt = (
        select(Document, func.count(DocumentChunk.id))
        .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
        .group_by(Document.id)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(limit + 1)  # one extra row tells us whether a next page exists
    )
    if before is not None:
        stmt = stmt.where(Document.created_at < before)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    items = [
        DocumentSummary(
            document_id=doc.id,
            title=doc.title,
            external_id=doc.external_id,
            source=doc.source,
            classification=doc.classification,
            chunk_count=count,
            created_at=doc.created_at,
        )
        for doc, count in rows[:limit]
    ]
    return DocumentPage(items=items, next_before=items[-1].created_at if has_more else None)

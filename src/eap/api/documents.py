"""
Document ingestion endpoint.

POST /v1/documents   chunk, embed and store a document for this tenant

No query here names a tenant and none needs to: the session from get_tenant_session carries
app.tenant_id and RLS does the rest, exactly as in runs.py.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.deps import get_current_tenant_id, get_tenant_session
from eap.llm.base import LLMError
from eap.rag.ingest import EmptyDocument, ingest_document

router = APIRouter(prefix="/documents", tags=["documents"])


class IngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=200_000)
    # The caller's own id for this source. Omitted means "a new document every time"; supplied
    # means "this is the same document as last time", and re-posting replaces it.
    external_id: str | None = Field(default=None, max_length=200)
    source: str = Field(default="upload", max_length=50)


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
        )
    except EmptyDocument as e:
        # 422, not 500: the caller sent something, it just had no content in it.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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

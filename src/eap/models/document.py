"""
The per-tenant document corpus.

Two tables rather than one. A document is what someone uploaded and recognises by name; a
chunk is what retrieval actually searches. Keeping them apart means re-chunking with a new
strategy replaces the chunks and leaves the document — and its id, which other rows may
already reference — untouched.
"""

import enum
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap.i18n import Language
from eap.models.base import Base
from eap.models.meta import Timestamps, UUIDPk, language_type

# Frozen in the database by migration 0007. Repeated here rather than read from Settings
# on purpose: a model whose shape changes with an env var makes `alembic check` pass or
# fail depending on the machine it runs on. tests/test_document_models.py asserts this and
# settings.embedding_dimensions agree, so drift is caught in CI, not at the first INSERT.
EMBEDDING_DIMENSIONS = 768


class Classification(enum.IntEnum):
    """
    Sensitivity levels, ordered. A caller sees a row when their clearance is at least its
    classification, so the ORDER is the whole semantics.

    Stored as smallint rather than a Postgres enum on purpose: `<=` on a Postgres enum
    compares by declaration order, which silently becomes wrong the first time someone
    inserts a new level in the middle. An integer cannot drift like that.
    """

    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3


class Document(UUIDPk, Timestamps, Base):
    __tablename__ = "documents"

    __table_args__ = (
        # external_id is the caller's own identifier — a Jira key, a Confluence page id.
        # Unique per tenant so re-ingesting a source replaces it instead of piling up
        # near-duplicates that all match the same query and crowd out everything else.
        UniqueConstraint("tenant_id", "external_id", name="uq_documents_tenant_id_external_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    external_id: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(50), default="upload", server_default="upload")
    classification: Mapped[int] = mapped_column(
        SmallInteger, default=Classification.PUBLIC, server_default="0"
    )

    # Whatever the caller wants to filter or display later — author, space key, url. JSONB
    # for the same reason agent config is: a new source must not require a migration.
    # Named `meta`, not `metadata`: that attribute is reserved by Declarative.
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")

    def __repr__(self) -> str:
        return f"<Document {self.external_id} tenant={self.tenant_id}>"


class DocumentChunk(UUIDPk, Timestamps, Base):
    __tablename__ = "document_chunks"

    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_document_chunks_document_id_ordinal"),
        # tenant_id leads for the same reason it does on agent_runs: RLS filters on it
        # before anything else can help, so an index that starts elsewhere is dead weight.
        Index("ix_document_chunks_tenant_id_document_id", "tenant_id", "document_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )

    # Denormalised on purpose: tenant_id is already on documents, but the RLS policy runs
    # against THIS table and cannot follow a foreign key to find it.
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )

    # Position within the document. Restores reading order after a similarity search hands
    # back chunks scattered across the corpus.
    ordinal: Mapped[int] = mapped_column(Integer)

    content: Mapped[str] = mapped_column(Text)

    # No vector index yet — deliberate, see ADR-003. At this corpus size a sequential scan
    # is faster and exactly correct, and an IVFFlat index built on an empty table trains
    # its lists on nothing and returns poor recall forever afterwards.
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS))

    token_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    classification: Mapped[int] = mapped_column(
        SmallInteger, default=Classification.PUBLIC, server_default="0"
    )

    # Per chunk, not per document: one upload can hold an English section and an Arabic
    # one, and retrieval works on chunks. Ingest starts tagging it in EAP-L5; until then
    # every chunk is 'en', which is what v1 ingested.
    language: Mapped[Language] = mapped_column(
        language_type(), default=Language.EN, server_default=Language.EN.value
    )

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.document_id}#{self.ordinal}>"

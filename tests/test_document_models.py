"""
Schema guards for the document corpus.

No database here on purpose — these assert the shape the migration has to match, which is
the part that is expensive to discover late.
"""

from sqlalchemy import UniqueConstraint

from eap.core.config import get_settings
from eap.models import Document, DocumentChunk
from eap.models.document import EMBEDDING_DIMENSIONS


def test_vector_width_matches_settings() -> None:
    # The column width is frozen in migration 0007. If these two ever disagree, every
    # INSERT fails at the database with an opaque width error and nothing says why.
    assert EMBEDDING_DIMENSIONS == get_settings().embedding_dimensions


def test_the_embedding_column_declares_its_dimension() -> None:
    # vector with no dimension is legal in Postgres and accepts any width - which means
    # a corpus can end up holding vectors that cannot be compared to each other.
    assert DocumentChunk.__table__.c.embedding.type.dim == EMBEDDING_DIMENSIONS


def test_both_tables_carry_tenant_id() -> None:
    # The RLS policy reads tenant_id off the row itself; it cannot follow a foreign key.
    # A tenant-scoped table missing this column has no isolation at all.
    for model in (Document, DocumentChunk):
        assert "tenant_id" in model.__table__.c


def test_a_source_is_unique_within_a_tenant_per_classification() -> None:
    # Per classification since migration 0012 (EAP-61, ADR-006). Across classifications a
    # re-post could conflict with a row RLS hides from the caller, and the refused update
    # told them the restricted document existed.
    unique = {c.name: c for c in Document.__table__.constraints if isinstance(c, UniqueConstraint)}
    assert list(unique) == ["uq_documents_tenant_id_classification_external_id"]
    assert [col.name for col in unique["uq_documents_tenant_id_classification_external_id"].columns] == [
        "tenant_id",
        "classification",
        "external_id",
    ]

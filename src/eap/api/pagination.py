"""
Keyset cursors for the list endpoints (EAP-37).

/v1/runs and /v1/documents sort by (created_at DESC, id DESC) and page with ?before=<cursor>.
A cursor that carried only created_at could skip or repeat a row when two rows share a
timestamp across a page boundary: "strictly older than T" drops the tied row left on the far
side of the boundary. The cursor this API hands out therefore names both columns, and the
predicate is the row comparison (created_at, id) < (T, I) - on Postgres, exactly the order
the ORDER BY defines.

Wire format: "<created_at ISO-8601 with offset>|<id>". Clients treat it as opaque and send it
back as ?before=. A bare timestamp is still accepted with its old "strictly older" meaning,
for callers that built one by hand; only the cursors this API returns carry the id.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import ColumnElement, tuple_
from sqlalchemy.orm import InstrumentedAttribute

SEPARATOR = "|"


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    # None: a bare timestamp from a caller that built the cursor by hand.
    id: UUID | None


def encode(created_at: datetime, row_id: UUID) -> str:
    return f"{created_at.isoformat()}{SEPARATOR}{row_id}"


def parse(before: str | None) -> Cursor | None:
    """The ?before= value, or a 422 that names what is wrong with it."""
    if before is None:
        return None
    ts_text, _, id_text = before.partition(SEPARATOR)
    try:
        created_at = datetime.fromisoformat(ts_text)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before is not a valid cursor") from e
    if created_at.tzinfo is None:
        # created_at is timestamptz. Comparing it to a naive datetime raises inside asyncpg
        # as a 500; better to tell the caller what is wrong.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before must include a timezone")
    row_id: UUID | None = None
    if id_text:
        try:
            row_id = UUID(id_text)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "before is not a valid cursor") from e
    return Cursor(created_at=created_at, id=row_id)


def older_than(
    cursor: Cursor, created_at: InstrumentedAttribute, row_id: InstrumentedAttribute
) -> ColumnElement[bool]:
    """The WHERE clause for "everything after this cursor" in (created_at DESC, id DESC) order."""
    if cursor.id is None:
        return created_at < cursor.created_at
    return tuple_(created_at, row_id) < tuple_(cursor.created_at, cursor.id)

"""
Language columns and their database guards (EAP-L2).

Two layers, tested separately on purpose. The model layer, with no database, proves the
columns store codes rather than member names, at the width migration 0011 created. The
database layer proves the CHECKs exist and refuse bad values from ANY writer - so those tests
insert with raw SQL, because raw SQL skips every Python-side check, and that is exactly the
writer the CHECKs exist for.

RLS is untouched by 0011 (new columns, same policies), so tenant isolation stays proven where
it already was: test_tenant_isolation (EAP-15) and the vector isolation test (EAP-35).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap.core.db import get_owner_session_factory
from eap.i18n import Language
from eap.models import Agent, AgentRun, DocumentChunk, Tenant, User
from eap.models.document import EMBEDDING_DIMENSIONS

CODES = [language.value for language in Language]

# Every CHECK migration 0011 creates, and the table it belongs to.
CHECKS = {
    "ck_tenants_default_language": "tenants",
    "ck_tenants_supported_languages": "tenants",
    "ck_users_locale": "users",
    "ck_agent_runs_language": "agent_runs",
    "ck_document_chunks_language": "document_chunks",
}

# Retrieval never reads these rows. The column is NOT NULL and fixed-width, so an insert needs
# a vector of the right shape and nothing more. Bound through CAST, as ingest does.
VECTOR = "[" + ",".join(["0.1"] * EMBEDDING_DIMENSIONS) + "]"


# --------------------------------------------------------------------------------------
# Raw-SQL helpers: nothing in Python fills in or checks a column, only the database does
# --------------------------------------------------------------------------------------


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _insert_tenant(**columns: object) -> uuid.UUID:
    names = ["id", "slug", "name", *columns]
    values = ["gen_random_uuid()", ":slug", ":slug", *(f":{name}" for name in columns)]
    sql = f"INSERT INTO tenants ({', '.join(names)}) VALUES ({', '.join(values)}) RETURNING id"
    async with get_owner_session_factory()() as s:
        tenant_id = (await s.execute(text(sql), {"slug": _unique("t"), **columns})).scalar_one()
        await s.commit()
    return tenant_id


async def _seed_owned_rows(tenant_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """An agent and a document - the parents a run and a chunk need."""
    async with get_owner_session_factory()() as s:
        agent_id = (
            await s.execute(
                text(
                    "INSERT INTO agents (id, tenant_id, name, display_name) "
                    "VALUES (gen_random_uuid(), :tid, 'echo', 'Echo') RETURNING id"
                ),
                {"tid": tenant_id},
            )
        ).scalar_one()
        document_id = (
            await s.execute(
                text(
                    "INSERT INTO documents (id, tenant_id, external_id, title) "
                    "VALUES (gen_random_uuid(), :tid, :ext, 'Doc') RETURNING id"
                ),
                {"tid": tenant_id, "ext": _unique("doc")},
            )
        ).scalar_one()
        await s.commit()
    return {"tid": tenant_id, "aid": agent_id, "did": document_id}


# One INSERT per table that carries a language column, with :code as the value under test.
# Omitting the column (the default case) is a separate statement per table below.
INSERT_WITH_CODE = {
    "users": (
        "INSERT INTO users (id, tenant_id, email, password_hash, locale) "
        "VALUES (gen_random_uuid(), :tid, :email, 'not-a-real-hash', :code)"
    ),
    "agent_runs": (
        "INSERT INTO agent_runs (id, tenant_id, agent_id, input, language) "
        "VALUES (gen_random_uuid(), :tid, :aid, '{}', :code)"
    ),
    "document_chunks": (
        "INSERT INTO document_chunks (id, tenant_id, document_id, ordinal, content, embedding, language) "
        "VALUES (gen_random_uuid(), :tid, :did, 0, 'x', CAST(:vec AS vector), :code)"
    ),
}


async def _insert_with_code(table: str, parents: dict[str, uuid.UUID], code: str | None) -> None:
    # One params dict serves all three statements: SQLAlchemy binds only the names a
    # statement uses and drops the rest.
    params = {**parents, "code": code, "email": f"{_unique('u')}@example.com", "vec": VECTOR}
    async with get_owner_session_factory()() as s:
        await s.execute(text(INSERT_WITH_CODE[table]), params)
        await s.commit()


# --------------------------------------------------------------------------------------
# Model layer - no database
# --------------------------------------------------------------------------------------

LANGUAGE_COLUMNS = [
    pytest.param(Tenant.__table__.c.default_language, id="tenants.default_language"),
    pytest.param(User.__table__.c.locale, id="users.locale"),
    pytest.param(AgentRun.__table__.c.language, id="agent_runs.language"),
    pytest.param(DocumentChunk.__table__.c.language, id="document_chunks.language"),
]


@pytest.mark.parametrize("column", LANGUAGE_COLUMNS)
def test_language_columns_store_codes_at_the_migrated_width(column) -> None:
    """'en' rather than 'EN', in a VARCHAR(8) - the exact shape migration 0011 created.

    Store member names and every insert fails the CHECK. Change the width and `alembic
    check` fails CI on all four columns at once.
    """
    assert column.type.enums == CODES
    assert column.type.length == 8
    assert column.type.native_enum is False


def test_only_the_user_locale_may_be_null() -> None:
    """NULL locale means "follow the tenant". NULL anywhere else would just mean "unknown"."""
    assert User.__table__.c.locale.nullable is True
    for column in (
        Tenant.__table__.c.default_language,
        Tenant.__table__.c.supported_languages,
        AgentRun.__table__.c.language,
        DocumentChunk.__table__.c.language,
    ):
        assert column.nullable is False, column


# --------------------------------------------------------------------------------------
# Database layer - the CHECKs exist
# --------------------------------------------------------------------------------------


async def test_all_five_checks_exist_in_the_database() -> None:
    """The guard against the trap this ticket found.

    SQLAlchemy's Enum(native_enum=False) creates no CHECK by itself, so a constraint can be
    believed in for months while the database enforces nothing - v1's status and role columns
    turned out to be in exactly that state. Asserting by name means a dropped or renamed
    constraint fails here, loudly, instead of quietly letting bad rows in.
    """
    async with get_owner_session_factory()() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT conname, conrelid::regclass::text FROM pg_constraint "
                    "WHERE contype = 'c' AND conname = ANY(:names)"
                ),
                {"names": list(CHECKS)},
            )
        ).all()
    assert dict(rows) == CHECKS


# --------------------------------------------------------------------------------------
# Tenants
# --------------------------------------------------------------------------------------


async def _tenant_languages(tenant_id: uuid.UUID) -> tuple[str, list[str]]:
    async with get_owner_session_factory()() as s:
        row = (
            await s.execute(
                text("SELECT default_language, supported_languages FROM tenants WHERE id = :id"),
                {"id": tenant_id},
            )
        ).one()
    return row[0], row[1]


async def test_a_new_tenant_is_english_only() -> None:
    """Every tenant that existed before M6 got exactly this backfill."""
    assert await _tenant_languages(await _insert_tenant()) == ("en", ["en"])


async def test_an_arabic_first_tenant_is_allowed() -> None:
    """Default Arabic with English still offered - the shape a Dubai brokerage will have."""
    tenant_id = await _insert_tenant(default_language="ar", supported_languages=["ar", "en"])
    assert await _tenant_languages(tenant_id) == ("ar", ["ar", "en"])


@pytest.mark.parametrize(
    ("columns", "constraint"),
    [
        pytest.param(
            {"default_language": "fr"},
            "ck_tenants_default_language",
            id="unknown-default",
        ),
        pytest.param(
            # Postgres tests CHECKs in name order, so the default_language one reports first.
            {"default_language": "EN", "supported_languages": ["EN"]},
            "ck_tenants_default_language",
            id="member-name-instead-of-code",
        ),
        pytest.param(
            {"supported_languages": ["en", "fr"]},
            "ck_tenants_supported_languages",
            id="unknown-supported",
        ),
        pytest.param(
            {"supported_languages": []},
            "ck_tenants_supported_languages",
            id="nothing-supported",
        ),
        pytest.param(
            {"supported_languages": ["en", None]},
            "ck_tenants_supported_languages",
            id="null-element",
        ),
        pytest.param(
            # A valid code on its own, but the tenant only supports English.
            {"default_language": "ar"},
            "ck_tenants_supported_languages",
            id="default-not-in-supported",
        ),
    ],
)
async def test_tenant_checks_refuse_bad_values(columns: dict[str, object], constraint: str) -> None:
    with pytest.raises(IntegrityError) as caught:
        await _insert_tenant(**columns)
    assert f'"{constraint}"' in str(caught.value)


# --------------------------------------------------------------------------------------
# Users, runs, chunks
# --------------------------------------------------------------------------------------


async def test_unset_language_columns_get_their_defaults() -> None:
    """A user follows the tenant (NULL); runs and chunks are 'en' until L4/L5 set them.

    The run and chunk INSERTs deliberately name no language column - that is what every
    writer older than M6 does, ingest included, and it must keep working unchanged.
    """
    parents = await _seed_owned_rows(await _insert_tenant())
    async with get_owner_session_factory()() as s:
        await s.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash) "
                "VALUES (gen_random_uuid(), :tid, :email, 'not-a-real-hash')"
            ),
            {"tid": parents["tid"], "email": f"{_unique('u')}@example.com"},
        )
        await s.execute(
            text(
                "INSERT INTO agent_runs (id, tenant_id, agent_id, input) "
                "VALUES (gen_random_uuid(), :tid, :aid, '{}')"
            ),
            {"tid": parents["tid"], "aid": parents["aid"]},
        )
        await s.execute(
            text(
                "INSERT INTO document_chunks (id, tenant_id, document_id, ordinal, content, embedding) "
                "VALUES (gen_random_uuid(), :tid, :did, 0, 'x', CAST(:vec AS vector))"
            ),
            {"tid": parents["tid"], "did": parents["did"], "vec": VECTOR},
        )
        await s.commit()

        locales = (
            await s.execute(text("SELECT locale FROM users WHERE tenant_id = :tid"), parents)
        ).scalars()
        run_languages = (
            await s.execute(text("SELECT language FROM agent_runs WHERE tenant_id = :tid"), parents)
        ).scalars()
        chunk_languages = (
            await s.execute(text("SELECT language FROM document_chunks WHERE tenant_id = :tid"), parents)
        ).scalars()
        assert list(locales) == [None]
        assert list(run_languages) == ["en"]
        assert list(chunk_languages) == ["en"]


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        pytest.param("users", "ck_users_locale", id="users"),
        pytest.param("agent_runs", "ck_agent_runs_language", id="agent_runs"),
        pytest.param("document_chunks", "ck_document_chunks_language", id="document_chunks"),
    ],
)
@pytest.mark.parametrize("bad_code", ["fr", "EN", ""])
async def test_language_checks_refuse_unknown_codes(table: str, constraint: str, bad_code: str) -> None:
    """Unknown language, member name instead of code, and empty string - all refused.

    The empty string matters: it is what a careless form or CSV import produces, and it is
    not NULL, so nothing else would stop it.
    """
    parents = await _seed_owned_rows(await _insert_tenant())
    with pytest.raises(IntegrityError) as caught:
        await _insert_with_code(table, parents, bad_code)
    assert f'"{constraint}"' in str(caught.value)


@pytest.mark.parametrize("code", CODES)
async def test_every_language_member_passes_every_check(code: str) -> None:
    """The enum and the database must agree.

    Add a member to Language without the migration that widens 0011's CHECKs, and this
    fails for the new code - in CI, not at the first real insert.
    """
    tenant_id = await _insert_tenant(default_language=code, supported_languages=[code])
    parents = await _seed_owned_rows(tenant_id)
    for table in INSERT_WITH_CODE:
        await _insert_with_code(table, parents, code)


# --------------------------------------------------------------------------------------
# ORM round trip
# --------------------------------------------------------------------------------------


async def test_the_orm_reads_back_language_members() -> None:
    """Python code will compare with `is Language.AR`. A plain 'ar' string coming back would
    make that comparison silently False, so the members must survive the round trip.

    Chunks are left out on purpose: ingest writes them with raw SQL (see eap.rag.ingest), so
    the ORM never inserts one and the raw-SQL tests above are the ones that cover it.
    """
    async with get_owner_session_factory()() as s:
        tenant = Tenant(slug=_unique("t"), name="ORM round trip")
        s.add(tenant)
        await s.flush()
        agent = Agent(tenant_id=tenant.id, name="echo", display_name="Echo")
        user = User(tenant_id=tenant.id, email=f"{_unique('u')}@example.com", password_hash="not-a-real-hash")
        s.add_all([agent, user])
        await s.flush()
        run = AgentRun(tenant_id=tenant.id, agent_id=agent.id, input={}, language=Language.AR)
        s.add(run)
        await s.commit()
        ids = {"tenant": tenant.id, "user": user.id, "run": run.id}

    async with get_owner_session_factory()() as s:
        tenant = await s.get(Tenant, ids["tenant"])
        user = await s.get(User, ids["user"])
        run = await s.get(AgentRun, ids["run"])

        assert tenant.default_language is Language.EN
        assert tenant.supported_languages == ["en"]
        assert user.locale is None
        assert run.language is Language.AR

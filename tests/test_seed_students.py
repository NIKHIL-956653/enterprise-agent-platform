"""
scripts/seed_students.py (EAP-62): a CSV of students in, member accounts at clearance 1 out.

Every test seeds a suffixed copy of the colleges (as test_demo_seed does) and points the
script at a two-row CSV in a temporary folder, so nothing touches demo-docs/ or the real
demo tenants. The credentials file the script writes is read back by the tests to log in
with - that is the only place a password exists, and the stdout checks prove it stays so.
"""

import stat
import sys
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from scripts import seed_demo, seed_students
from scripts.seed_demo import college_tenants
from sqlalchemy import text

from eap.core.db import dispose_owner_engine, get_owner_session_factory

PASSWORD = "test-only-demo-password"

CSV_TWO = "name,email\nAsha Rao,asha@students.example\nVikram Nair,vikram@students.example\n"


async def _seed_colleges() -> tuple[str, dict[str, str]]:
    suffix = f"-{uuid.uuid4().hex[:8]}"
    ids = await seed_demo.seed(college_tenants(suffix), password=PASSWORD)
    return suffix, {slug: str(tid) for slug, tid in ids.items()}


def _layout(tmp_path: Path, slug: str, body: str = CSV_TWO) -> str:
    folder = tmp_path / slug
    folder.mkdir()
    (folder / "students.csv").write_text(body, encoding="utf-8")
    return str(tmp_path / "{tenant}" / "students.csv")


async def _users(tenant_id: str) -> dict[str, tuple[str, int, str]]:
    async with get_owner_session_factory()() as s:
        rows = (
            await s.execute(
                text("SELECT email, role, clearance, password_hash FROM users WHERE tenant_id = :tid"),
                {"tid": tenant_id},
            )
        ).all()
    return {email: (role, clearance, ph) for email, role, clearance, ph in rows}


async def _login(client: AsyncClient, slug: str, email: str, password: str) -> int:
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": password})
    return r.status_code


# --- the CSV ------------------------------------------------------------------------------


def test_reads_name_and_email_in_any_column_order(tmp_path: Path) -> None:
    p = tmp_path / "s.csv"
    p.write_text("Email,Extra,Name\n  A@X.EXAMPLE ,ignored, Ann \n\n", encoding="utf-8")
    assert seed_students.read_students(p) == [seed_students.Student(name="Ann", email="a@x.example")]


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("name\nAnn\n", "missing: email"),
        ("name,email\nAnn,\n", "both name and email are required"),
        ("name,email\nAnn,not-an-address\n", "is not an email address"),
        ("name,email\nAnn,a@x.example\nAnne,a@x.example\n", "appears twice"),
    ],
)
def test_rejects_a_malformed_csv(tmp_path: Path, body: str, fragment: str) -> None:
    p = tmp_path / "s.csv"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(seed_students.CsvError, match=fragment):
        seed_students.read_students(p)


def test_a_missing_file_is_an_error_not_an_empty_roster(tmp_path: Path) -> None:
    with pytest.raises(seed_students.CsvError, match="no such file"):
        seed_students.read_students(tmp_path / "nope.csv")


# --- the accounts -------------------------------------------------------------------------


async def test_two_rows_become_two_members_at_clearance_one_who_can_log_in(
    client: AsyncClient, tmp_path: Path
) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)

    results = await seed_students.run((engg,), layout, rotate=False, dry_run=False)

    assert [(r.created, r.rotated, r.unchanged, r.skipped) for r in results] == [(2, 0, 0, [])]
    users = await _users(ids[engg])
    assert users["asha@students.example"][:2] == ("member", 1)
    assert users["vikram@students.example"][:2] == ("member", 1)
    # The seed's own two accounts are untouched.
    assert users["admin@engg.edu"][:2] == ("admin", 3)
    assert users["student@engg.edu"][:2] == ("member", 1)

    creds = seed_students.read_credentials(tmp_path / engg / "credentials.txt")
    assert set(creds) == {"asha@students.example", "vikram@students.example"}
    assert creds["asha@students.example"][0] == "Asha Rao"
    for email, (_, password) in creds.items():
        assert len(password) >= 16
        assert await _login(client, engg, email, password) == 200
    # And a student is a student: the file holds no admin line to leak.
    assert "admin@engg.edu" not in creds


async def test_the_credentials_file_is_owner_only(tmp_path: Path) -> None:
    suffix, _ = await _seed_colleges()
    engg = f"engg{suffix}"
    await seed_students.run((engg,), _layout(tmp_path, engg), rotate=False, dry_run=False)
    mode = stat.S_IMODE((tmp_path / engg / "credentials.txt").stat().st_mode)
    if sys.platform != "win32":
        assert mode == 0o600


async def test_rerunning_keeps_passwords_and_rotate_changes_them(client: AsyncClient, tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)
    creds_path = tmp_path / engg / "credentials.txt"

    await seed_students.run((engg,), layout, rotate=False, dry_run=False)
    before = await _users(ids[engg])
    first = seed_students.read_credentials(creds_path)

    # Plain re-run: same hashes, same file, counted as unchanged.
    results = await seed_students.run((engg,), layout, rotate=False, dry_run=False)
    assert (results[0].created, results[0].rotated, results[0].unchanged) == (0, 0, 2)
    assert await _users(ids[engg]) == before
    assert seed_students.read_credentials(creds_path) == first

    # --rotate: new hashes, new passwords on file, the old password no longer logs in.
    results = await seed_students.run((engg,), layout, rotate=True, dry_run=False)
    assert (results[0].created, results[0].rotated, results[0].unchanged) == (0, 2, 0)
    after = await _users(ids[engg])
    second = seed_students.read_credentials(creds_path)
    for email in ("asha@students.example", "vikram@students.example"):
        assert after[email][2] != before[email][2]
        assert second[email][1] != first[email][1]
        assert await _login(client, engg, email, first[email][1]) == 401
        assert await _login(client, engg, email, second[email][1]) == 200


async def test_rerunning_repairs_a_demoted_student_without_touching_the_password(tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)
    await seed_students.run((engg,), layout, rotate=False, dry_run=False)
    before = await _users(ids[engg])

    async with get_owner_session_factory()() as s:
        await s.execute(
            text("UPDATE users SET clearance = 0 WHERE tenant_id = :tid AND email = 'asha@students.example'"),
            {"tid": ids[engg]},
        )
        await s.commit()

    await seed_students.run((engg,), layout, rotate=False, dry_run=False)
    after = await _users(ids[engg])
    assert after["asha@students.example"] == ("member", 1, before["asha@students.example"][2])


async def test_a_staff_email_in_the_csv_is_skipped_not_demoted(tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    body = "name,email\nThe Dean,admin@engg.edu\nAsha Rao,asha@students.example\n"
    layout = _layout(tmp_path, engg, body)
    before = await _users(ids[engg])

    results = await seed_students.run((engg,), layout, rotate=True, dry_run=False)

    assert results[0].created == 1
    assert results[0].skipped == ["admin@engg.edu is admin, left alone"]
    after = await _users(ids[engg])
    assert after["admin@engg.edu"] == before["admin@engg.edu"]
    assert "admin@engg.edu" not in seed_students.read_credentials(tmp_path / engg / "credentials.txt")


async def test_a_dropped_row_keeps_its_account_and_its_line(tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)
    await seed_students.run((engg,), layout, rotate=False, dry_run=False)
    first = seed_students.read_credentials(tmp_path / engg / "credentials.txt")

    (tmp_path / engg / "students.csv").write_text("name,email\nAsha Rao,asha@students.example\n")
    await seed_students.run((engg,), layout, rotate=False, dry_run=False)

    assert "vikram@students.example" in await _users(ids[engg])
    assert seed_students.read_credentials(tmp_path / engg / "credentials.txt") == first


async def test_an_unknown_tenant_or_missing_csv_is_reported_and_the_rest_proceed(tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)
    _layout(tmp_path, f"nope{suffix}")  # a CSV for a tenant that was never seeded

    slugs = (f"nope{suffix}", f"mba{suffix}", engg)
    results = await seed_students.run(slugs, layout, rotate=False, dry_run=False)

    assert results[0].error == f"tenant 'nope{suffix}' does not exist - run scripts/seed_demo.py first"
    assert results[1].error is not None and "no such file" in results[1].error
    assert results[2].ok and results[2].created == 2
    assert "asha@students.example" in await _users(ids[engg])


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    suffix, ids = await _seed_colleges()
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)
    before = await _users(ids[engg])

    results = await seed_students.run((engg,), layout, rotate=False, dry_run=True)

    assert results[0].created == 2
    assert await _users(ids[engg]) == before
    assert not (tmp_path / engg / "credentials.txt").exists()


# --- stdout -------------------------------------------------------------------------------


def test_main_prints_counts_and_the_path_but_never_a_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report is the only thing this script prints; the file is the only place a password goes."""
    import asyncio

    suffix, _ = asyncio.run(_seed_colleges())
    # main() starts its own event loop; the engine the seed above opened belongs to a dead one.
    asyncio.run(dispose_owner_engine())
    engg = f"engg{suffix}"
    layout = _layout(tmp_path, engg)

    code = seed_students.main(["--tenant", engg, "--layout", layout])

    out = capsys.readouterr().out
    assert code == 0
    assert "created 2" in out
    assert str(tmp_path / engg / "credentials.txt") in out
    for _, password in seed_students.read_credentials(tmp_path / engg / "credentials.txt").values():
        assert password not in out

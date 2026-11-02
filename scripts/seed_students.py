"""
Create one member account per student from a CSV, per tenant (EAP-62).

    python scripts/seed_students.py                       # engg, mba, bba; demo-docs/{tenant}/students.csv
    python scripts/seed_students.py --tenant engg         # one college
    python scripts/seed_students.py --rotate              # new passwords for EVERY listed student
    python scripts/seed_students.py --dry-run             # read and validate the CSVs, write nothing

The CSV has a header row with the columns `name,email` (any order, other columns ignored).
Every row becomes a `member` at clearance 1 - a student: sees classification 0-1, never
faculty-only material - in the tenant whose folder the file sits in.

Credentials go to `demo-docs/{tenant}/credentials.txt`, one line per student
(`email<TAB>name<TAB>password`), created with mode 0600. `demo-docs/` is gitignored, and
so is `credentials.txt` by name wherever it sits. The file is the ONLY place a password is
ever written: nothing here prints one to stdout or to a log, and the database holds only
the argon2id hash. Hand the lines out to students yourself; this script does not send mail.

Idempotent. Re-running creates accounts for rows it has not seen and leaves existing
students' passwords alone - their line in credentials.txt is kept as it was. `--rotate`
generates a fresh password for every listed student and rewrites their lines. A row whose
email already belongs to an admin or owner of that tenant is reported and skipped: a CSV
must not be able to demote a staff account to a student, and a student account must not be
able to turn an admin's password over.

Uses the OWNER session, same as scripts/seed_demo.py: the tenant row has to be found by
slug before any tenant context exists. Run `seed_demo.py` first so the colleges exist.
"""

import argparse
import asyncio
import csv
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from eap.core.db import dispose_owner_engine, get_owner_session_factory
from eap.core.security import hash_password

DEFAULT_LAYOUT = "demo-docs/{tenant}/students.csv"
DEFAULT_TENANTS = ("engg", "mba", "bba")
CREDENTIALS_FILE = "credentials.txt"

STUDENT_ROLE = "member"
STUDENT_CLEARANCE = 1  # INTERNAL - see scripts/seed_demo.py and migration 0008

# 12 random bytes -> 16 url-safe characters, ~96 bits. Typed once into a login form, so
# longer only costs the student; shorter would invite guessing on a public URL.
PASSWORD_BYTES = 12

_HEADER = (
    "# Student logins. One per line: email<TAB>name<TAB>password. Written by\n"
    "# scripts/seed_students.py; never committed (gitignored). Keep this file private.\n"
)
_UNRECORDED = "(password not on record here - re-run with --rotate to issue a new one)"


@dataclass(frozen=True)
class Student:
    name: str
    email: str


@dataclass
class TenantResult:
    tenant: str
    created: int = 0
    rotated: int = 0
    unchanged: int = 0
    skipped: list[str] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class CsvError(Exception):
    """The students file is missing, has the wrong columns or holds an unusable row."""


# --- the CSV --------------------------------------------------------------------------------


def read_students(path: Path) -> list[Student]:
    """Parse `name,email` rows. Emails are lower-cased and de-duplicated; blanks are an error."""
    if not path.is_file():
        raise CsvError(f"{path}: no such file")
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = [c.strip().lower() for c in (reader.fieldnames or [])]
        missing = {"name", "email"} - set(columns)
        if missing:
            raise CsvError(
                f"{path}: header must include name and email (missing: {', '.join(sorted(missing))})"
            )
        students: list[Student] = []
        seen: set[str] = set()
        for n, row in enumerate(reader, start=2):
            cleaned = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            name, email = cleaned.get("name", ""), cleaned.get("email", "").lower()
            if not name and not email:
                continue  # a blank line, typically the last one
            if not name or not email:
                raise CsvError(f"{path}:{n}: both name and email are required")
            if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
                raise CsvError(f"{path}:{n}: {email!r} is not an email address")
            if email in seen:
                raise CsvError(f"{path}:{n}: {email} appears twice")
            seen.add(email)
            students.append(Student(name=name, email=email))
    return students


# --- the credentials file -----------------------------------------------------------------


def read_credentials(path: Path) -> dict[str, tuple[str, str]]:
    """email -> (name, password) from an existing file. A missing file is simply empty."""
    if not path.is_file():
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            out[parts[0]] = (parts[1], "\t".join(parts[2:]))
    return out


def write_credentials(path: Path, rows: dict[str, tuple[str, str]]) -> None:
    """Replace the file atomically, owner-readable only, never world- or group-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_HEADER)
        for email in sorted(rows):
            name, password = rows[email]
            f.write(f"{email}\t{name}\t{password}\n")
    # Explicit, for a file that already existed with wider permissions - os.open's mode
    # only applies on creation, and on Windows it is advisory either way.
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


# --- the database -------------------------------------------------------------------------


async def _tenant_id(s: AsyncSession, slug: str) -> UUID | None:
    row = await s.execute(text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug})
    return row.scalar_one_or_none()


async def _existing_users(s: AsyncSession, tenant_id: UUID) -> dict[str, str]:
    """email -> role for every user the tenant already has."""
    rows = (
        await s.execute(text("SELECT email, role FROM users WHERE tenant_id = :tid"), {"tid": tenant_id})
    ).all()
    return {email.lower(): role for email, role in rows}


async def seed_tenant(
    s: AsyncSession,
    *,
    slug: str,
    students: list[Student],
    credentials_path: Path,
    rotate: bool,
    dry_run: bool = False,
) -> TenantResult:
    """
    Upsert one tenant's students. Commits nothing - the caller owns the transaction.

    Three outcomes per row, counted on the result:
      created    no such user: insert as member/1 with a fresh password
      rotated    existing student, --rotate: new password
      unchanged  existing student, no --rotate: role and clearance re-asserted, hash untouched
    A row naming an existing admin or owner is skipped, by email, so a CSV cannot demote
    staff or reset their password.
    """
    result = TenantResult(tenant=slug, skipped=[])
    tenant_id = await _tenant_id(s, slug)
    if tenant_id is None:
        result.error = f"tenant {slug!r} does not exist - run scripts/seed_demo.py first"
        return result

    existing = await _existing_users(s, tenant_id)
    on_file = read_credentials(credentials_path)
    issued: dict[str, tuple[str, str]] = {}

    for student in students:
        role = existing.get(student.email)
        if role is not None and role != STUDENT_ROLE:
            result.skipped.append(f"{student.email} is {role}, left alone")
            continue

        if role is None or rotate:
            password = new_password()
            issued[student.email] = (student.name, password)
            if role is None:
                result.created += 1
            else:
                result.rotated += 1
            if not dry_run:
                await s.execute(
                    text(
                        "INSERT INTO users (id, tenant_id, email, password_hash, role, clearance) "
                        "VALUES (gen_random_uuid(), :tid, :email, :ph, :role, :clr) "
                        "ON CONFLICT (tenant_id, email) DO UPDATE SET "
                        "  password_hash = EXCLUDED.password_hash,"
                        "  role = EXCLUDED.role,"
                        "  clearance = EXCLUDED.clearance,"
                        "  is_active = true"
                    ),
                    {
                        "tid": tenant_id,
                        "email": student.email,
                        "ph": hash_password(password),
                        "role": STUDENT_ROLE,
                        "clr": STUDENT_CLEARANCE,
                    },
                )
        else:
            result.unchanged += 1
            # The password stays whatever it was. Re-asserting role and clearance is the
            # same repair seed_demo.py does: the CSV is the source of truth for "student".
            if not dry_run:
                await s.execute(
                    text(
                        "UPDATE users SET role = :role, clearance = :clr "
                        "WHERE tenant_id = :tid AND email = :email"
                    ),
                    {
                        "tid": tenant_id,
                        "email": student.email,
                        "role": STUDENT_ROLE,
                        "clr": STUDENT_CLEARANCE,
                    },
                )
            prior = on_file.get(student.email)
            issued[student.email] = (student.name, prior[1] if prior else _UNRECORDED)

    if not dry_run:
        # Lines for emails no longer in the CSV are kept: dropping a row from the CSV does
        # not delete the account, so it must not delete the only record of its password.
        merged = {**on_file, **issued}
        write_credentials(credentials_path, merged)
    return result


async def run(tenants: tuple[str, ...], layout: str, *, rotate: bool, dry_run: bool) -> list[TenantResult]:
    results: list[TenantResult] = []
    async with get_owner_session_factory()() as s:
        for slug in tenants:
            csv_path = Path(layout.format(tenant=slug))
            try:
                students = read_students(csv_path)
            except CsvError as e:
                results.append(TenantResult(tenant=slug, error=str(e)))
                continue
            results.append(
                await seed_tenant(
                    s,
                    slug=slug,
                    students=students,
                    credentials_path=csv_path.parent / CREDENTIALS_FILE,
                    rotate=rotate,
                    dry_run=dry_run,
                )
            )
        if dry_run:
            await s.rollback()
        else:
            await s.commit()
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    p.add_argument(
        "--tenant",
        action="append",
        dest="tenants",
        metavar="SLUG",
        help=f"tenant slug; repeatable. Default: {', '.join(DEFAULT_TENANTS)}",
    )
    p.add_argument(
        "--layout",
        default=DEFAULT_LAYOUT,
        help=f"where each tenant's CSV is; {{tenant}} is the slug. Default: {DEFAULT_LAYOUT}",
    )
    p.add_argument("--rotate", action="store_true", help="issue a new password to every listed student")
    p.add_argument("--dry-run", action="store_true", help="read and validate the CSVs; write nothing")
    return p.parse_args(argv)


def report(results: list[TenantResult], layout: str, dry_run: bool) -> None:
    """Counts and file paths only. No password is ever printed - that is the whole rule."""
    for r in results:
        if not r.ok:
            print(f"{r.tenant:<8} ERROR  {r.error}")
            continue
        where = Path(layout.format(tenant=r.tenant)).parent / CREDENTIALS_FILE
        verb = "would write" if dry_run else "wrote"
        print(
            f"{r.tenant:<8} created {r.created:<3} rotated {r.rotated:<3} unchanged {r.unchanged:<3} "
            f"-> {verb} {where}"
        )
        for line in r.skipped or []:
            print(f"{'':<8} skipped: {line}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tenants = tuple(args.tenants) if args.tenants else DEFAULT_TENANTS

    async def _go() -> list[TenantResult]:
        try:
            return await run(tenants, args.layout, rotate=args.rotate, dry_run=args.dry_run)
        finally:
            await dispose_owner_engine()

    results = asyncio.run(_go())
    report(results, args.layout, args.dry_run)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

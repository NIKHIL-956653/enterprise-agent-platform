"""
scripts/ingest_pdfs.py (D2): PDFs in, documents out, through the public API.

The script talks to the app over httpx; here that httpx client is the in-process ASGI one,
so every request below goes through real auth, real RLS and the real ingest path - only
the embedder is faked. PDFs are built byte by byte in this file: real text-drawing
operators, a real /Title in the info dictionary, and blank pages standing in for scans.
"""

import asyncio
import io
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from scripts import ingest_pdfs as script
from sqlalchemy import text

from eap.core.db import get_owner_session_factory
from eap.core.security import hash_password
from eap.rag import ingest

# Bag-of-words over a fixed vocabulary, L2-normalised. Defined once, in test_retrieval.
from tests.test_retrieval import RankingFakeEmbedder

PASSWORD = "test-only-ingest-password"

LECTURE = [
    "Thermodynamics lecture one covers the first law and conservation of energy.",
    "Heat added to a closed system equals the change in internal energy plus work done.",
    "Worked examples include a piston, a kettle and a refrigeration cycle.",
] * 4


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> RankingFakeEmbedder:
    fake = RankingFakeEmbedder()
    monkeypatch.setattr(ingest, "_default_embedder", lambda: fake)
    return fake


# --- a minimal PDF writer --------------------------------------------------------------


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], title: str | None = None) -> bytes:
    """One Helvetica text line per string; an empty list is a blank page (a 'scan')."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in below, once the page tree number is known
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    kids = []
    for lines in pages:
        ops = "".join(f"({_escape(line)}) Tj T* " for line in lines)
        stream = f"BT /F1 10 Tf 12 TL 40 800 Td {ops}ET".encode() if lines else b""
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (tree, font, content)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % tree
    objects[tree - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids),
        len(kids),
    )
    info = add(b"<< /Title (%s) >>" % _escape(title).encode()) if title is not None else None

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    trailer = b"<< /Size %d /Root %d 0 R" % (len(objects) + 1, catalog)
    if info is not None:
        trailer += b" /Info %d 0 R" % info
    out.write(b"trailer\n" + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref)
    return out.getvalue()


def write_pdf(folder: Path, name: str, pages: list[list[str]], title: str | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(make_pdf(pages, title))
    return path


# --- a tenant to ingest into -----------------------------------------------------------


async def _tenant() -> dict[str, str]:
    """A fresh tenant with a clearance-3 admin and a clearance-1 student."""
    slug = f"pdf-{uuid.uuid4().hex[:8]}"
    async with get_owner_session_factory()() as s:
        tid = (
            await s.execute(
                text("INSERT INTO tenants (id, slug, name) VALUES (gen_random_uuid(), :s, :s) RETURNING id"),
                {"s": slug},
            )
        ).scalar_one()
        for email, role, clearance in (("admin@x.edu", "admin", 3), ("student@x.edu", "member", 1)):
            await s.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, role, clearance) "
                    "VALUES (gen_random_uuid(), :tid, :e, :ph, :r, :c)"
                ),
                {"tid": tid, "e": email, "ph": hash_password(PASSWORD), "r": role, "c": clearance},
            )
        await s.commit()
    return {"slug": slug, "tenant_id": str(tid)}


async def _documents(client: AsyncClient, slug: str, email: str = "admin@x.edu") -> list[dict]:
    r = await client.post("/v1/auth/login", json={"tenant_slug": slug, "email": email, "password": PASSWORD})
    token = r.json()["access_token"]
    r = await client.get("/v1/documents", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _chunk_rows(tenant_id: str) -> int:
    async with get_owner_session_factory()() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM document_chunks WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).scalar_one()


async def _ingest(
    client: AsyncClient, slug: str, files: list[Path], classification: int = 0, email="admin@x.edu"
):
    return await script.ingest_tenant(
        client, slug, files, email=email, password=PASSWORD, classification=classification
    )


# --- extraction ------------------------------------------------------------------------


def test_title_comes_from_the_pdf_metadata(tmp_path: Path) -> None:
    path = write_pdf(tmp_path, "thermo-01.pdf", [LECTURE], title="Thermodynamics: Lecture 1")
    doc = script.extract(path)
    assert doc.title == "Thermodynamics: Lecture 1"
    assert "first law" in doc.text
    assert doc.pages == 1


@pytest.mark.parametrize("title", [None, "", "   "])
def test_title_falls_back_to_the_file_name(tmp_path: Path, title: str | None) -> None:
    path = write_pdf(tmp_path, "thermo-01.pdf", [LECTURE], title=title)
    assert script.extract(path).title == "thermo-01"


def test_a_blank_pdf_is_detected_as_having_no_text(tmp_path: Path) -> None:
    doc = script.extract(write_pdf(tmp_path, "scan.pdf", [[], [], []]))
    assert doc.chars == 0
    assert "no extractable text" in script.skip_reason(doc)


def test_near_zero_text_is_also_a_skip(tmp_path: Path) -> None:
    # A scan whose only extractable text is a page number on each page.
    doc = script.extract(write_pdf(tmp_path, "scan.pdf", [["12"], ["13"], ["14"]]))
    assert 0 < doc.chars < script.MIN_CHARS
    assert script.skip_reason(doc) is not None


def test_mostly_blank_pages_are_a_skip(tmp_path: Path) -> None:
    # Enough text in total, but spread over 20 pages - a scanned book with a typed cover.
    doc = script.extract(write_pdf(tmp_path, "book.pdf", [LECTURE[:3]] + [[]] * 19))
    assert doc.chars >= script.MIN_CHARS
    assert "/page" in script.skip_reason(doc)


def test_real_text_is_not_a_skip(tmp_path: Path) -> None:
    assert script.skip_reason(script.extract(write_pdf(tmp_path, "a.pdf", [LECTURE]))) is None


def test_long_text_splits_on_paragraphs_and_keeps_the_bare_name_for_part_one() -> None:
    para = "word " * 1000
    body = "\n\n".join([para.strip()] * 50)  # ~250k characters
    parts = script.split_body(body, limit=script.MAX_BODY)
    assert len(parts) == 2
    assert all(len(p) <= script.MAX_BODY for p in parts)
    assert "".join(parts).replace("\n", "").replace(" ", "") == body.replace("\n", "").replace(" ", "")
    assert script.external_ids("big.pdf", 2) == ["big.pdf", "big.pdf#2"]
    assert script.split_body("short") == ["short"]


# --- through the API -------------------------------------------------------------------


async def test_rerunning_replaces_rather_than_duplicates(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    files = [
        write_pdf(tmp_path, "thermo-01.pdf", [LECTURE, LECTURE], title="Thermodynamics 1"),
        write_pdf(tmp_path, "thermo-02.pdf", [LECTURE]),
    ]

    first = await _ingest(client, t["slug"], files)
    chunks_after_first = await _chunk_rows(t["tenant_id"])
    second = await _ingest(client, t["slug"], files)

    assert [r.status for r in first] == ["ingested", "ingested"]
    assert [r.chunks for r in first] == [r.chunks for r in second]
    assert all(r.chunks > 0 for r in first)
    # The flat chunk count the acceptance criterion asks for, read from the table itself.
    assert chunks_after_first == sum(r.chunks for r in first)
    assert await _chunk_rows(t["tenant_id"]) == chunks_after_first

    docs = await _documents(client, t["slug"])
    assert sorted((d["external_id"], d["title"], d["source"]) for d in docs) == [
        ("thermo-01.pdf", "Thermodynamics 1", "pdf"),
        ("thermo-02.pdf", "thermo-02", "pdf"),
    ]


async def test_a_scanned_pdf_is_skipped_and_never_posted(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    files = [write_pdf(tmp_path, "scan.pdf", [[], []]), write_pdf(tmp_path, "notes.pdf", [LECTURE])]

    results = await _ingest(client, t["slug"], files)

    by_file = {r.file: r for r in results}
    assert by_file["scan.pdf"].status == "skipped"
    assert by_file["scan.pdf"].chars == 0
    assert by_file["notes.pdf"].status == "ingested"
    assert [d["external_id"] for d in await _documents(client, t["slug"])] == ["notes.pdf"]


async def test_an_unreadable_file_is_skipped(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"this is not a pdf")

    [result] = await _ingest(client, t["slug"], [bad])

    assert result.status == "skipped"
    assert "unreadable" in result.note
    assert await _documents(client, t["slug"]) == []


async def test_classification_is_applied_and_hides_faculty_material(
    client: AsyncClient, tmp_path: Path
) -> None:
    t = await _tenant()
    files = [write_pdf(tmp_path, "answer-key.pdf", [LECTURE])]

    [result] = await _ingest(client, t["slug"], files, classification=3)

    assert result.status == "ingested"
    assert [d["classification"] for d in await _documents(client, t["slug"])] == [3]
    assert await _documents(client, t["slug"], "student@x.edu") == []


async def test_above_your_clearance_is_a_reported_failure(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    files = [write_pdf(tmp_path, "answer-key.pdf", [LECTURE])]

    [result] = await _ingest(client, t["slug"], files, classification=3, email="student@x.edu")

    assert result.status == "failed"
    assert "403" in result.note and "clearance" in result.note


async def test_a_bad_password_fails_every_file_without_posting(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    files = [write_pdf(tmp_path, "a.pdf", [LECTURE]), write_pdf(tmp_path, "b.pdf", [LECTURE])]

    results = await script.ingest_tenant(
        client, t["slug"], files, email="admin@x.edu", password="wrong", classification=0
    )

    assert [r.status for r in results] == ["failed", "failed"]
    assert all("login" in r.note for r in results)
    assert await _documents(client, t["slug"]) == []


# --- the command line ------------------------------------------------------------------


async def test_the_layout_flag_and_a_full_run(client: AsyncClient, tmp_path: Path, capsys) -> None:
    t = await _tenant()
    write_pdf(tmp_path / t["slug"], "thermo-01.pdf", [LECTURE])
    write_pdf(tmp_path / t["slug"], "scan.pdf", [[]])
    (tmp_path / t["slug"] / "readme.txt").write_text("not a pdf")
    layout = f"{tmp_path}/{{tenant}}/*.pdf"

    args = script.parse_args(
        ["--tenant", t["slug"], "--layout", layout, "--email", "admin@x.edu", "--password", PASSWORD]
    )
    results = await script.run(args, client=client)
    script.report(results)

    assert sorted((r.file, r.status) for r in results) == [
        ("scan.pdf", "skipped"),
        ("thermo-01.pdf", "ingested"),
    ]
    out = capsys.readouterr().out
    assert "thermo-01.pdf" in out and "scan.pdf" in out and "no extractable text" in out
    assert "1 ingested, 1 skipped" in out


async def test_a_dry_run_posts_nothing(client: AsyncClient, tmp_path: Path) -> None:
    t = await _tenant()
    write_pdf(tmp_path / t["slug"], "thermo-01.pdf", [LECTURE])

    args = script.parse_args(["--tenant", t["slug"], "--layout", f"{tmp_path}/{{tenant}}/*.pdf", "--dry-run"])
    [result] = await script.run(args, client=client)

    assert result.status == "dry-run"
    assert result.chars > 0
    assert await _documents(client, t["slug"]) == []


def test_defaults_match_the_demo_layout() -> None:
    args = script.parse_args([])
    assert args.layout == "demo-docs/{tenant}/*.pdf"
    assert args.classification == 0
    assert script.parse_args(["--classification", "3"]).classification == 3
    with pytest.raises(SystemExit):
        script.parse_args(["--classification", "4"])


def test_there_is_no_built_in_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "dotenv_values", lambda _path: {})
    monkeypatch.delenv("INGEST_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    assert script.resolve_password(None) is None
    with pytest.raises(SystemExit, match="No password"):
        asyncio.run(script.run(script.parse_args(["--tenant", "nobody"])))

    monkeypatch.setenv("DEMO_PASSWORD", "demo")
    assert script.resolve_password(None) == "demo"
    monkeypatch.setenv("INGEST_PASSWORD", "ingest")
    assert script.resolve_password(None) == "ingest"
    assert script.resolve_password("flag") == "flag"

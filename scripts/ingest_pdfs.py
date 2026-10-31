"""
Ingest a folder of PDFs per tenant through the public API.

    python scripts/ingest_pdfs.py                                # engg, mba, bba at classification 0
    python scripts/ingest_pdfs.py --tenant engg --classification 3 --layout "faculty/{tenant}/*.pdf"
    python scripts/ingest_pdfs.py --dry-run                      # extract and report, post nothing

For each tenant: log in AS that tenant, extract each PDF's text with pypdf, POST it to
/v1/documents. It goes through the API, not the database, on purpose: the JWT decides the
tenant and the clearance, RLS checks the write, and the run is exactly what a client sees.
No tenant_id is ever sent - the token carries it.

Re-running is safe. external_id is the file name, and /v1/documents replaces a document's
chunks when the same external_id comes back, so a second run leaves the chunk count flat.

A PDF that yields no text (a scan, an image-only export) is SKIPPED and reported, never
posted. An empty document that looks ingested is worse than a visible failure: the answer
agent would refuse every question about it and nobody would know why.

Credentials: --email defaults to "admin@{tenant}.edu", the seed's convention. The password
is --password, else INGEST_PASSWORD, else DEMO_PASSWORD - each from the environment, then
.env. There is no built-in password; with none of them set the script stops.

Needs pypdf:  pip install -e ".[scripts]"
"""

import argparse
import asyncio
import glob
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import httpx
from dotenv import dotenv_values
from pypdf import PdfReader
from pypdf.errors import PdfReadError

DEFAULT_LAYOUT = "demo-docs/{tenant}/*.pdf"
DEFAULT_TENANTS = ("engg", "mba", "bba")
DEFAULT_EMAIL = "admin@{tenant}.edu"

# Mirrors IngestRequest in eap.api.documents. A longer text is posted in parts rather than
# truncated: truncating would drop the end of a document while reporting it as ingested.
MAX_BODY = 200_000
MAX_TITLE = 300
MAX_EXTERNAL_ID = 200

# Below either floor a file is treated as having no extractable text. Both are counts of
# non-whitespace characters: a scanned page often extracts as a few stray spaces or a page
# number, which must not pass for content.
MIN_CHARS = 100
MIN_CHARS_PER_PAGE = 20


@dataclass
class Extracted:
    title: str
    text: str
    pages: int

    @property
    def chars(self) -> int:
        return len(re.sub(r"\s", "", self.text))


@dataclass
class FileResult:
    tenant: str
    file: str
    status: str  # ingested | skipped | failed | dry-run
    chars: int = 0
    pages: int = 0
    chunks: int = 0
    parts: int = 0
    note: str = ""


class ExtractionError(Exception):
    """The file could not be read as a PDF at all."""


def extract(path: Path) -> Extracted:
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # An empty user password opens most "protected" PDFs (print/copy restrictions).
            reader.decrypt("")
        pages = [page.extract_text() or "" for page in reader.pages]
        meta_title = reader.metadata.title if reader.metadata else None
    except (PdfReadError, OSError, ValueError, KeyError, NotImplementedError) as e:
        raise ExtractionError(f"unreadable PDF: {type(e).__name__}: {e}") from e

    text = "\n\n".join(pages)
    # Postgres text columns reject NUL, and some PDF fonts extract it. Without this the
    # POST fails with a 500 that names no file.
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    title = (meta_title or "").strip() or path.stem
    return Extracted(title=title[:MAX_TITLE], text=text, pages=len(pages))


def skip_reason(
    doc: Extracted, min_chars: int = MIN_CHARS, min_per_page: int = MIN_CHARS_PER_PAGE
) -> str | None:
    if doc.pages == 0:
        return "no pages"
    if doc.chars == 0:
        return "no extractable text (scanned or image-only PDF?)"
    if doc.chars < min_chars:
        return f"only {doc.chars} characters extracted (< {min_chars}; scanned PDF?)"
    if doc.chars / doc.pages < min_per_page:
        return (
            f"only {doc.chars} characters over {doc.pages} pages (< {min_per_page}/page; mostly scanned PDF?)"
        )
    return None


def split_body(text: str, limit: int = MAX_BODY) -> list[str]:
    """Paragraph-boundary split into parts no longer than `limit`. One part when it fits."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def external_ids(filename: str, parts: int) -> list[str]:
    """
    Part 1 is always the bare file name, so a file that grows past the limit keeps its
    original document. Parts 2+ are "name.pdf#2" etc.
    """
    if len(filename) > MAX_EXTERNAL_ID - 4:
        raise ValueError(f"file name longer than {MAX_EXTERNAL_ID - 4} characters")
    return [filename] + [f"{filename}#{i}" for i in range(2, parts + 1)]


class Session:
    """One tenant's login. Re-authenticates once on a 401 - tokens live 30 minutes."""

    def __init__(self, client: httpx.AsyncClient, tenant: str, email: str, password: str) -> None:
        self._client, self._tenant, self._email, self._password = client, tenant, email, password
        self._token: str | None = None

    async def login(self) -> None:
        r = await self._client.post(
            "/v1/auth/login",
            json={"tenant_slug": self._tenant, "email": self._email, "password": self._password},
        )
        if r.status_code != 200:
            raise LoginFailed(f"login as {self._email} on '{self._tenant}' failed: HTTP {r.status_code}")
        self._token = r.json()["access_token"]

    async def post_document(self, payload: dict) -> httpx.Response:
        for attempt in (1, 2):
            if self._token is None:
                await self.login()
            r = await self._client.post(
                "/v1/documents", json=payload, headers={"Authorization": f"Bearer {self._token}"}
            )
            if r.status_code == 401 and attempt == 1:
                self._token = None
                continue
            return r
        return r


class LoginFailed(Exception):
    pass


def _detail(r: httpx.Response) -> str:
    try:
        detail = r.json().get("detail")
    except ValueError:
        detail = None
    return f"HTTP {r.status_code}" + (f": {detail}" if isinstance(detail, str) else "")


async def ingest_file(
    session: Session | None,
    tenant: str,
    path: Path,
    *,
    classification: int,
    source: str = "pdf",
    min_chars: int = MIN_CHARS,
    min_per_page: int = MIN_CHARS_PER_PAGE,
) -> FileResult:
    """Extract one PDF and post it. session=None is a dry run."""
    result = FileResult(tenant=tenant, file=path.name, status="failed")
    try:
        doc = extract(path)
    except ExtractionError as e:
        result.status, result.note = "skipped", str(e)
        return result
    result.chars, result.pages = doc.chars, doc.pages

    reason = skip_reason(doc, min_chars, min_per_page)
    if reason:
        result.status, result.note = "skipped", reason
        return result

    bodies = split_body(doc.text)
    try:
        ids = external_ids(path.name, len(bodies))
    except ValueError as e:
        result.status, result.note = "skipped", str(e)
        return result
    result.parts = len(bodies)

    if session is None:
        result.status = "dry-run"
        result.note = f"title: {doc.title}" + (f"; {len(bodies)} parts" if len(bodies) > 1 else "")
        return result

    for i, (body, xid) in enumerate(zip(bodies, ids, strict=True), start=1):
        title = doc.title if len(bodies) == 1 else f"{doc.title} (part {i}/{len(bodies)})"[:MAX_TITLE]
        r = await session.post_document(
            {
                "title": title,
                "body": body,
                "external_id": xid,
                "source": source,
                "classification": classification,
            }
        )
        if r.status_code != 201:
            result.note = f"part {i}/{len(bodies)}: {_detail(r)}" if len(bodies) > 1 else _detail(r)
            return result
        result.chunks += r.json()["chunk_count"]

    result.status = "ingested"
    result.note = f"title: {doc.title}" + (f"; posted in {len(bodies)} parts" if len(bodies) > 1 else "")
    return result


async def ingest_tenant(
    client: httpx.AsyncClient | None,
    tenant: str,
    files: list[Path],
    *,
    email: str,
    password: str,
    classification: int,
    min_chars: int = MIN_CHARS,
    min_per_page: int = MIN_CHARS_PER_PAGE,
) -> list[FileResult]:
    """Every file for one tenant, under one login. client=None is a dry run."""
    session = None
    if client is not None:
        session = Session(client, tenant, email, password)
        try:
            await session.login()
        except LoginFailed as e:
            return [FileResult(tenant=tenant, file=f.name, status="failed", note=str(e)) for f in files]
    return [
        await ingest_file(
            session, tenant, f, classification=classification, min_chars=min_chars, min_per_page=min_per_page
        )
        for f in files
    ]


def find_files(layout: str, tenant: str) -> list[Path]:
    pattern = layout.format(tenant=tenant)
    return sorted(Path(p) for p in glob.glob(pattern) if p.lower().endswith(".pdf") and Path(p).is_file())


def resolve_password(flag: str | None) -> str | None:
    if flag:
        return flag
    dotenv = dotenv_values(".env")
    for name in ("INGEST_PASSWORD", "DEMO_PASSWORD"):
        value = os.environ.get(name) or dotenv.get(name)
        if value:
            return value
    return None


def report(results: list[FileResult], out: TextIO | None = None) -> None:
    out = out or sys.stdout  # resolved per call, so redirected stdout is honoured
    if not results:
        print("No PDFs found.", file=out)
        return
    fw = max(len("file"), *(len(r.file) for r in results))
    tw = max(len("tenant"), *(len(r.tenant) for r in results))
    print(
        f"{'tenant':<{tw}} {'file':<{fw}} {'pages':>5} {'chars':>9} {'chunks':>6}  status    note", file=out
    )
    for r in results:
        chunks = str(r.chunks) if r.status == "ingested" else "-"
        print(
            f"{r.tenant:<{tw}} {r.file:<{fw}} {r.pages:>5} {r.chars:>9} {chunks:>6}  {r.status:<9} {r.note}",
            file=out,
        )
    counts = {s: sum(r.status == s for r in results) for s in ("ingested", "skipped", "failed", "dry-run")}
    print(
        "\n"
        + ", ".join(f"{n} {s}" for s, n in counts.items() if n)
        + f"; {sum(r.chunks for r in results)} chunks stored",
        file=out,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    p.add_argument(
        "--tenant",
        action="append",
        help=f"tenant slug; repeat for several (default: {', '.join(DEFAULT_TENANTS)})",
    )
    p.add_argument(
        "--layout",
        default=DEFAULT_LAYOUT,
        help=f"glob per tenant, {{tenant}} is replaced by the slug (default: {DEFAULT_LAYOUT})",
    )
    p.add_argument(
        "--classification",
        type=int,
        choices=range(4),
        default=0,
        help="0 public, 1 internal, 2 confidential, 3 restricted (default: 0)",
    )
    p.add_argument("--api-url", default=os.environ.get("EAP_API_URL", "http://localhost:8000"))
    p.add_argument(
        "--email", default=DEFAULT_EMAIL, help=f"login; {{tenant}} is replaced (default: {DEFAULT_EMAIL})"
    )
    p.add_argument("--password", help="default: INGEST_PASSWORD, then DEMO_PASSWORD (environment, then .env)")
    p.add_argument("--min-chars", type=int, default=MIN_CHARS, help="skip files with fewer characters")
    p.add_argument("--min-chars-per-page", type=int, default=MIN_CHARS_PER_PAGE)
    p.add_argument("--dry-run", action="store_true", help="extract and report; post nothing")
    return p.parse_args(argv)


async def run(args: argparse.Namespace, client: httpx.AsyncClient | None = None) -> list[FileResult]:
    password = resolve_password(args.password)
    if password is None and not args.dry_run:
        raise SystemExit("No password: pass --password or set INGEST_PASSWORD / DEMO_PASSWORD.")

    own_client = client is None and not args.dry_run
    if own_client:
        # Embedding a long document is one provider call per batch; give it room.
        client = httpx.AsyncClient(base_url=args.api_url, timeout=httpx.Timeout(300.0))
    results: list[FileResult] = []
    try:
        for tenant in args.tenant or DEFAULT_TENANTS:
            files = find_files(args.layout, tenant)
            if not files:
                print(f"{tenant}: no PDFs match {args.layout.format(tenant=tenant)}", file=sys.stderr)
                continue
            results += await ingest_tenant(
                None if args.dry_run else client,
                tenant,
                files,
                email=args.email.format(tenant=tenant),
                password=password or "",
                classification=args.classification,
                min_chars=args.min_chars,
                min_per_page=args.min_chars_per_page,
            )
    finally:
        if own_client and client is not None:
            await client.aclose()
    return results


def main(argv: list[str] | None = None) -> int:
    results = asyncio.run(run(parse_args(argv)))
    report(results)
    # Skips are reported, not fatal: they are the script working. A failure is not.
    return 1 if any(r.status == "failed" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

"""The hourly archive of closed sessions (P-TR-295, TR-1506).

`session_lines` keeps every claude_code line of a session. A session whose last line is more than
`ARCHIVE_AFTER_DAYS` days old is closed for practical purposes, so once an hour its rows are written
as one gzip JSON-lines file, the file is read back and checked, and only then are the rows deleted
from `session_lines`. Each file is a row in `session_archive_parts`: `(session, part)`, the
`first_seq` to `last_seq` it holds, its line count, size and MD5, and its key. A line that arrives
for an archived session is a row in `session_lines` again (`seq` only grows, so it never reuses a
number), and the next pass writes it as part 2. DuckDB is never touched.

Where the archive lives (read once per pass, from the environment):

- `TARES_ARCHIVE_URL`: `file:///path` (a folder) or `s3://bucket/prefix/`. S3 needs the `archive-s3`
  extra (boto3, imported when the first S3 part is written). `TARES_ARCHIVE_ENDPOINT` and
  `TARES_ARCHIVE_REGION` point it at another S3 (MinIO, Spaces, R2); `TARES_ARCHIVE_ACCESS_KEY_ID`
  and `TARES_ARCHIVE_SECRET_ACCESS_KEY` are the keys, and without them boto3's own chain applies
  (an IAM role). Parts are single-part puts, so the object's ETag is the MD5 of the file; a bucket
  that encrypts with KMS answers another ETag, the check fails and the rows stay (one log line and
  a count in `tares_archive_failures_total`, every pass).
- Unset, a cell whose state is in SQLite uses `<data dir>/archive/`. A cell whose state is in
  Postgres does nothing: a cell without a place to put the lines keeps them.

A file is named `<session>/<part>.jsonl.gz` under the prefix (the session is percent-encoded, so no
id can leave its folder). A failure of any step leaves the rows where they are and prints one line;
the next pass tries the session again. Deleting archive files is not done here.

On Postgres a session is archived in one transaction that starts with
`pg_try_advisory_xact_lock(hashtext('tares_session_archive:' || session))`: two cells (or two
passes) on one database never write the same part, and a session someone else holds is skipped.
SQLite has one writer, so there the rows are read in one transaction, the file written and checked
outside it (a slow upload must not hold the write lock the ingest waits on), and the part inserted
and its rows deleted in a second. Rows never change once written, so the delete takes exactly the
`seq` range that was read.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import os
import urllib.parse
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import delete, exists, func, insert, select, text

from . import metrics
from .envelope import now_utc
from .session_store.tables import session_archive_parts, session_lines, sessions

ARCHIVE_AFTER_DAYS = 7           # a session whose last line is older than this is archived
EVERY = 3600.0                   # seconds between passes
FIRST_AFTER = 300.0              # the first pass waits for the start-up reconcile to mark sessions
PER_PASS = 200                   # sessions moved by one pass


class ArchiveError(Exception):
    """A part that was not stored or did not check out; the rows stay."""


# ── backends ────────────────────────────────────────────────────────────────────
class Backend:
    """Where parts live. `put` returns the etag (the MD5 of the data as hex), `head` the
    `(size, etag)` of a stored key, `get` its bytes."""

    def put(self, key: str, data: bytes) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def head(self, key: str) -> tuple[int, str]:
        raise NotImplementedError


class FileBackend(Backend):
    """A folder. A part is written under a temporary name and renamed, so a reader never sees half
    a file; the etag is the MD5 of what was written."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _path(self, key: str) -> str:
        path = os.path.abspath(os.path.join(self.root, *key.split("/")))
        if os.path.commonpath([self.root, path]) != self.root:
            raise ArchiveError(f"key {key!r} leaves the archive folder")
        return path

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp-{os.getpid()}"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        return hashlib.md5(data).hexdigest()

    def get(self, key: str) -> bytes:
        with open(self._path(key), "rb") as f:
            return f.read()

    def head(self, key: str) -> tuple[int, str]:
        data = self.get(key)
        return len(data), hashlib.md5(data).hexdigest()


class S3Backend(Backend):
    """An S3 bucket (or any S3-compatible store). boto3 is imported here, not at the top, so a
    cell with no S3 archive needs none of it. `client` is for tests."""

    def __init__(self, bucket: str, prefix: str = "", env=None, client=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        env = os.environ if env is None else env
        if client is None:
            try:
                import boto3
            except ImportError as e:
                raise ArchiveError("an s3:// archive needs boto3 (pip install 'tares[archive-s3]')") from e
            client = boto3.client(
                "s3",
                endpoint_url=env.get("TARES_ARCHIVE_ENDPOINT") or None,
                region_name=env.get("TARES_ARCHIVE_REGION") or None,
                aws_access_key_id=env.get("TARES_ARCHIVE_ACCESS_KEY_ID") or None,
                aws_secret_access_key=env.get("TARES_ARCHIVE_SECRET_ACCESS_KEY") or None)
        self.client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    @staticmethod
    def _etag(value) -> str:
        return str(value or "").strip('"')

    def put(self, key: str, data: bytes) -> str:
        # one put, not a multipart upload: a part is far under the size that needs one, and a
        # single put's ETag is the MD5 of the body
        out = self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        return self._etag(out.get("ETag"))

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def head(self, key: str) -> tuple[int, str]:
        out = self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        return int(out["ContentLength"]), self._etag(out.get("ETag"))


def backend_for(url: str | None, data_dir: str | None, postgres: bool, env=None) -> Backend | None:
    """The backend the settings name, or None when there is none: no URL on a Postgres cell, or no
    data directory to default into. An unusable URL raises `ArchiveError`."""
    env = os.environ if env is None else env
    url = (url or "").strip()
    if not url:
        if postgres or not data_dir:
            return None
        return FileBackend(os.path.join(data_dir, "archive"))
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        path = urllib.parse.unquote(parsed.path)
        if not path:
            raise ArchiveError(f"TARES_ARCHIVE_URL {url!r} names no folder")
        return FileBackend(path)
    if parsed.scheme == "s3":
        if not parsed.netloc:
            raise ArchiveError(f"TARES_ARCHIVE_URL {url!r} names no bucket")
        return S3Backend(parsed.netloc, urllib.parse.unquote(parsed.path), env)
    raise ArchiveError(f"TARES_ARCHIVE_URL {url!r}: only file:///path and s3://bucket/prefix/ are known")


# ── one session ─────────────────────────────────────────────────────────────────
def part_key(session: str, part: int) -> str:
    """`<session>/<part>.jsonl.gz`, the session percent-encoded (a leading dot too, so `..` is not
    a folder name)."""
    folder = urllib.parse.quote(session, safe="-_")
    if folder.startswith("."):
        folder = "%2E" + folder[1:]
    return f"{folder or '%00'}/{part}.jsonl.gz"


def encode(rows) -> bytes:
    """One JSON object per row with every column of `session_lines`, in seq order, gzipped. The
    modified time of the gzip header is fixed, so the same rows make the same bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            at = r.at.isoformat() if r.at is not None else None
            gz.write((json.dumps({"session": r.session, "seq": r.seq, "line_key": r.line_key,
                                  "at": at, "type": r.type, "labels": r.labels, "render": r.render,
                                  "payload": r.payload}, ensure_ascii=True, separators=(",", ":"))
                      + "\n").encode("ascii"))
    return buf.getvalue()


def _lock(conn, session: str) -> bool:
    return bool(conn.execute(text("SELECT pg_try_advisory_xact_lock(hashtext('tares_session_archive:' "
                                  "|| CAST(:s AS text)))"), {"s": session}).scalar())


def _check(backend: Backend, key: str, data: bytes, etag: str, md5: str) -> None:
    size, head_etag = backend.head(key)
    if size != len(data) or head_etag != md5 or etag != md5:
        raise ArchiveError(f"{key} did not check out (size {size} of {len(data)}, "
                           f"etag {head_etag or '-'} of {md5})")


def _archive_session(db, backend: Backend, session: str, now: datetime) -> int | None:
    """Move one session's rows into a new part. Returns the part's size, or None when there was
    nothing to do (no rows, or another cell holds the session). Raises on any failure, with the
    rows still in `session_lines` and no part row."""
    postgres = db.dialect == "postgresql"
    with contextlib.ExitStack() as stack:
        if postgres:
            conn = stack.enter_context(db.begin())
            if not _lock(conn, session):
                return None

            def tx():
                return contextlib.nullcontext(conn)
        else:
            tx = db.begin
        t = session_lines.c
        with tx() as c:
            rows = c.execute(select(t.session, t.seq, t.line_key, t.at, t.type, t.labels, t.render,
                                    t.payload).where(t.session == session).order_by(t.seq)).all()
            part = c.execute(select(func.coalesce(func.max(session_archive_parts.c.part), 0)).where(
                session_archive_parts.c.session == session)).scalar_one() + 1
        if not rows:
            return None
        first, last = rows[0].seq, rows[-1].seq
        data = encode(rows)
        md5 = hashlib.md5(data).hexdigest()
        key = part_key(session, part)
        etag = backend.put(key, data)
        _check(backend, key, data, etag, md5)
        with tx() as c:
            if c.execute(select(sessions.c.session).where(sessions.c.session == session)).first() is None:
                return None        # purged while the part was written: nothing to record
            c.execute(insert(session_archive_parts).values(
                session=session, part=part, first_seq=first, last_seq=last, lines=len(rows),
                bytes=len(data), md5=md5, key=key, written_at=now))
            gone = c.execute(delete(session_lines).where(
                t.session == session, t.seq >= first, t.seq <= last)).rowcount
            if gone != len(rows):
                raise ArchiveError(f"{session}: {gone} rows deleted of {len(rows)} archived")
        return len(data)


# ── one pass ────────────────────────────────────────────────────────────────────
def due(db, now: datetime, limit: int = PER_PASS) -> list[str]:
    """The sessions to archive: last line older than `ARCHIVE_AFTER_DAYS`, `lines_complete`, and
    rows still in `session_lines`; the oldest first."""
    cutoff = now - timedelta(days=ARCHIVE_AFTER_DAYS)
    with db.begin() as conn:
        return [r[0] for r in conn.execute(
            select(sessions.c.session).where(
                sessions.c.last_line_at < cutoff, sessions.c.lines_complete.is_(True),
                exists().where(session_lines.c.session == sessions.c.session))
            .order_by(sessions.c.last_line_at, sessions.c.session).limit(limit)).all()]


def archive_pass(store, backend: Backend | None = None, now: datetime | None = None,
                 limit: int = PER_PASS) -> int:
    """Archive up to `limit` due sessions; returns how many were moved. A session that fails keeps
    its rows, costs one log line and one count, and the others carry on."""
    db = store.sessions_db
    if backend is None:
        try:
            backend = backend_for(os.environ.get("TARES_ARCHIVE_URL"), os.path.dirname(
                os.path.abspath(store.path)), db.dialect == "postgresql")
        except Exception as e:
            metrics.archive_failed()
            print(f"taresd: session archive off ({type(e).__name__}: {e})", flush=True)
            return 0
    if backend is None:
        return 0
    now = now or now_utc()
    moved = 0
    for session in due(db, now, limit):
        try:
            size = _archive_session(db, backend, session, now)
        except Exception as e:
            metrics.archive_failed()
            print(f"taresd: session archive of {session} failed ({type(e).__name__}: {e}); "
                  "its lines stay in the store", flush=True)
            continue
        if size is not None:
            moved += 1
            metrics.session_archived(size)
    return moved


async def run(store, stop: asyncio.Event, every: float = EVERY, first: float = FIRST_AFTER) -> None:
    """The hourly loop the daemon starts: wait, run a pass in a thread, wait again. Never stops on
    an error (a pass already catches each session's)."""
    delay = first
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        delay = every
        try:
            await asyncio.to_thread(archive_pass, store)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"taresd: session archive pass failed ({type(e).__name__}: {e})", flush=True)

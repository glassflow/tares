"""The sweep of unused archive files (TR-1664): it removes an old file no part row names, and nothing
else; it never runs against a session being written; on S3 it reads every page of the listing. With a
folder archive, on SQLite and on the Postgres harness. Plain script, exits non-zero on failure.

The cases came from tests/test_session_move_archive.py, which went with the one-time move (TR-1815):
the file a replace leaves behind is made here by renumbering a part, as the move's replace did.
"""
import asyncio
import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select, update

from tares import session_archive
from tares.envelope import Envelope, now_utc
from tares.session_archive import FileBackend, S3Backend
from tares.session_store.tables import session_archive_parts
from tares.store import Store

FAILS = []
SRC = "claude_code"
NOW = now_utc().replace(microsecond=0)
DAY = 86400
ARCHIVE = tempfile.mkdtemp()
os.environ["TARES_ARCHIVE_URL"] = "file://" + ARCHIVE


def ck(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"   {detail}"))
    if not ok:
        FAILS.append(name)


def line(session, at, etype="assistant", text_="t"):
    """One envelope; `at` is how long ago, in seconds."""
    payload = {"uuid": "u-" + uuid.uuid4().hex[:10]}
    return Envelope(source=SRC, source_type="agent_session", key_value=session, event_type=etype,
                    text=text_, event_time=NOW - timedelta(seconds=at), payload=payload,
                    labels={"tares_person": "ann"})


def open_store():
    return Store(os.path.join(tempfile.mkdtemp(), "t"))


def parts_of(s, session):
    with s.sessions_db.begin() as c:
        return c.execute(select(session_archive_parts).where(
            session_archive_parts.c.session == session).order_by(session_archive_parts.c.part)).all()


def replace_part(s, root, session):
    """Part 1 written again as part 2 under a name holding its MD5, the way a replace writes it:
    the row names the new file and the old `<session>/1.jsonl.gz` is left, named by no row."""
    p = parts_of(s, session)[0]
    old = os.path.join(root, *p.key.split("/"))
    data = open(old, "rb").read()
    key = f"{session}/2-{hashlib.md5(data).hexdigest()}.jsonl.gz"
    shutil.copyfile(old, os.path.join(root, *key.split("/")))
    t = session_archive_parts.c
    with s.sessions_db.begin() as c:
        c.execute(update(session_archive_parts).where(t.session == session, t.part == 1)
                  .values(part=2, key=key))


@contextlib.contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield old
    finally:
        setattr(obj, name, old)


# ── the sweep of unused files (TR-1664) ─────────────────────────────────────────
OLD = 8 * DAY


def aged(root, key, age=OLD, data=b"x"):
    """A file `key` under `root`, last written `age` seconds ago; kept as it is when it exists."""
    path = os.path.join(root, *key.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/")
                  for d, _, files in os.walk(root) for f in files)


@contextlib.contextmanager
def own_archive():
    root = tempfile.mkdtemp()
    url = os.environ["TARES_ARCHIVE_URL"]
    os.environ["TARES_ARCHIVE_URL"] = "file://" + root
    try:
        yield root
    finally:
        os.environ["TARES_ARCHIVE_URL"] = url


def part_row(s, session, part, key):
    with s.sessions_db.begin() as c:
        c.execute(insert(session_archive_parts).values(
            session=session, part=part, first_seq=1, last_seq=1, lines=1, bytes=1, md5="m", key=key,
            written_at=NOW))


def sweep_files():
    print("== the sweep removes old files no part row names, and nothing else ==")
    with own_archive() as root:
        s = open_store()
        s.append([line("sw", 20 * DAY - i * 60) for i in range(4)] + [line("sw", 15 * DAY)])
        session_archive._archive_session(s.sessions_db, FileBackend(root), "sw", NOW)
        replace_part(s, root, "sw")
        used = parts_of(s, "sw")[0]
        ck("a replace left the old file", tree(root) == sorted(["sw/1.jsonl.gz", used.key])
           and used.part == 2, tree(root))
        ck("a young unused file stays", session_archive.sweep(s) == 0 and len(tree(root)) == 2)
        orphan_try = "sw/2-" + "a" * 32 + ".jsonl.gz"        # a try of part 2 that did not finish
        keep = ["sw/3.jsonl.gz",                              # after the last recorded part
                "sw/3-" + "b" * 32 + ".jsonl.gz",
                "nobody/1.jsonl.gz",                          # a session with no part here
                "sw/1.jsonl.gz.tmp-123", "sw/notes.txt", "sw/01.jsonl.gz", "sw/1-abc.jsonl.gz",
                "sw/deep/1.jsonl.gz", "1.jsonl.gz", "other.txt",
                "%73w/1.jsonl.gz"]                            # not a name part_key writes
        young = "sw/2-" + "c" * 32 + ".jsonl.gz"
        for key in keep + [orphan_try, used.key, "sw/1.jsonl.gz"]:
            aged(root, key)
        aged(root, young, 6 * DAY)
        everything = tree(root)


        class NoList(FileBackend):
            def files(self):
                raise OSError("listing failed")

        try:
            session_archive.sweep(s, NoList(root))
            raised = False
        except OSError:
            raised = True
        ck("a listing that fails removes nothing", raised and tree(root) == everything)

        def broken():
            raise RuntimeError("no rows")

        out = io.StringIO()
        with patched(s.sessions_db, "read", broken), contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        ck("rows that cannot be read remove nothing, and one line says so", n == 0
           and tree(root) == everything and out.getvalue().count("sweep failed") == 1, out.getvalue())

        class Rewritten(FileBackend):          # listed as old, written again since
            def files(self):
                return [(k, m - timedelta(days=30)) for k, m in super().files()]

        os.utime(os.path.join(root, "sw", "1.jsonl.gz"))
        os.utime(os.path.join(root, *orphan_try.split("/")))
        os.utime(os.path.join(root, *young.split("/")))
        ck("a file written again after the listing stays",
           session_archive.sweep(s, Rewritten(root)) == 0 and tree(root) == everything)
        aged(root, "sw/1.jsonl.gz")
        aged(root, orphan_try)
        aged(root, young, 6 * DAY)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        ck("the replaced file and the old try are removed", n == 2 and tree(root)
           == sorted(set(everything) - {"sw/1.jsonl.gz", orphan_try}), (n, tree(root)))
        ck("one line says how many", "removed 2 unused file(s)" in out.getvalue(), out.getvalue())
        ck("the session still reads", archived_payloads_in(root, s, "sw") is not None)

        # the hourly loop runs it
        aged(root, "sw/1.jsonl.gz")

        async def loop():
            stop = asyncio.Event()
            task = asyncio.create_task(session_archive.run(s, stop, every=0.05, first=0.05))
            for _ in range(100):
                if "sw/1.jsonl.gz" not in tree(root):
                    break
                await asyncio.sleep(0.05)
            stop.set()
            await asyncio.wait_for(task, 5)

        with contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(loop())
        ck("the hourly loop removes it", "sw/1.jsonl.gz" not in tree(root), tree(root))

        # two ids that differ in case only share a folder where the file system ignores case
        part_row(s, "case", 1, "case/1.jsonl.gz")
        part_row(s, "Case", 2, "Case/2.jsonl.gz")

        class OtherCase(FileBackend):
            removed = []

            def files(self):
                return [("Case/1.jsonl.gz", NOW - timedelta(days=30))]

            def modified(self, key):
                return NOW - timedelta(days=30)

            def delete(self, key):
                self.removed.append(key)

        ck("a file another case of the id names stays",
           session_archive.sweep(s, OtherCase(root)) == 0 and OtherCase.removed == [])
        s.close()

    print("== no archive folder yet, and no archive ==")
    with own_archive() as root:
        s = open_store()
        os.environ["TARES_ARCHIVE_URL"] = "file://" + os.path.join(root, "never")
        ck("a folder that is not there is empty", session_archive.sweep(s) == 0)
        with patched(session_archive, "backend_of", lambda store: None):
            ck("with no archive there is nothing to do", session_archive.sweep(s) == 0)
        s.close()


def archived_payloads_in(root, s, session):
    backend = FileBackend(root)
    return [r["payload"] for p in parts_of(s, session) for r in session_archive.read_part(backend, p)]


def sweep_and_writers():
    print("== the sweep never runs against a session being written ==")
    with own_archive() as root:
        s = open_store()
        s.append([line("wr", 20 * DAY - i * 60) for i in range(4)] + [line("wr", 15 * DAY)])
        session_archive._archive_session(s.sessions_db, FileBackend(root), "wr", NOW)
        s.append([line("wr", 14 * DAY)])
        orphan = "wr/1-" + "d" * 32 + ".jsonl.gz"             # unused, numbered within the parts
        for who in ("hourly",):
            aged(root, orphan)
            during = []

            class SweepDuring(FileBackend):
                def put(self, key, data):
                    out = super().put(key, data)
                    t = threading.Thread(target=lambda: during.append(session_archive.sweep(s)),
                                         daemon=True)
                    t.start()
                    t.join(15)
                    during.append(orphan in tree(root))
                    return out

            session_archive._archive_session(s.sessions_db, SweepDuring(root), "wr", NOW)
            ck(f"while the {who} writer is at work, the session's files stay", during[:2] == [0, True],
               during)
            ck(f"after the {who} writer, the sweep removes the unused one",
               session_archive.sweep(s) >= 1 and orphan not in tree(root), tree(root))
        ck("what the rows name is whole", len(archived_payloads_in(root, s, "wr")) == 6)

        # a writer that starts while the sweep is between its check and its delete waits
        aged(root, orphan)
        entered, states, threads = threading.Event(), [], []

        def write():
            with session_archive.writing("another"):
                entered.set()

        class DuringDelete(FileBackend):
            def delete(self, key):
                t = threading.Thread(target=write, daemon=True)
                t.start()
                threads.append(t)
                states.append(entered.wait(0.5))
                super().delete(key)

        n = session_archive.sweep(s, DuringDelete(root))
        for t in threads:
            t.join(10)
        ck("a writer cannot start between the sweep's check and its delete", n == 1
           and states == [False] and entered.is_set(), (n, states))
        ck("nothing is left registered", not session_archive._writing, session_archive._writing)
        s.close()


def sweep_s3():
    print("== the sweep on S3, against botocore's Stubber ==")
    import boto3
    from botocore.stub import Stubber
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="test-id",
                          aws_secret_access_key="test-secret")
    s = open_store()
    part_row(s, "c1", 2, "c1/2.jsonl.gz")
    b = S3Backend("bkt", "pre/fix/", client=client)
    then = datetime.now(timezone.utc) - timedelta(days=30)
    obj = lambda k: {"Key": k, "LastModified": then}
    listing = {"Bucket": "bkt", "Prefix": "pre/fix/"}
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "next",
                                            "Contents": [obj("pre/fix/c1/2.jsonl.gz"),
                                                         obj("pre/fix/c1/readme")]}, listing)
        st.add_response("list_objects_v2", {"IsTruncated": False,
                                            "Contents": [obj("pre/fix/c1/1.jsonl.gz")]},
                        {**listing, "ContinuationToken": "next"})
        st.add_response("head_object", {"LastModified": then},
                        {"Bucket": "bkt", "Key": "pre/fix/c1/1.jsonl.gz"})
        st.add_response("delete_object", {}, {"Bucket": "bkt", "Key": "pre/fix/c1/1.jsonl.gz"})
        n = session_archive.sweep(s, b)
        st.assert_no_pending_responses()
    ck("both pages are read, and only the unused part is deleted, under the prefix", n == 1, n)
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": True, "NextContinuationToken": "next",
                                            "Contents": [obj("pre/fix/c1/1.jsonl.gz")]}, listing)
        st.add_client_error("list_objects_v2", service_error_code="AccessDenied",
                            http_status_code=403)
        out = io.StringIO()
        with patched(session_archive, "backend_of", lambda store: b), contextlib.redirect_stdout(out):
            n = session_archive.sweep_pass(s)
        st.assert_no_pending_responses()          # no delete was asked for
    ck("a listing that fails on a later page deletes nothing", n == 0
       and "AccessDenied" in out.getvalue(), out.getvalue())
    with Stubber(client) as st:
        st.add_response("list_objects_v2", {"IsTruncated": False}, {"Bucket": "bkt", "Prefix": ""})
        ck("an empty bucket with no prefix", S3Backend("bkt", "", client=client).files() == [])
    s.close()


def main():
    t0 = time.perf_counter()
    sweep_files()
    sweep_and_writers()
    sweep_s3()
    print(f"({time.perf_counter() - t0:.1f}s)")
    if FAILS:
        print(f"\n{len(FAILS)} failed: {FAILS}")
        sys.exit(1)
    print("\nall ok")


if __name__ == "__main__":
    main()

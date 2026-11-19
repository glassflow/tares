# Tares enterprise licence, see ee/LICENSE
"""The decisions loop backs off after a failed pass instead of resending every minute (TR-1475).

`tares_ee.brain.decisions.Decisions.run` waits `INTERVAL_SECONDS` after a pass with no failure. After a pass in
which a call to lore failed (a listing, a delete or a put) or which raised, it waits twice the last wait, up to
`BACKOFF_MAX` (30) times the interval; a pass with no failure puts the wait back to the interval. While backing
off, a doc change (`wake`) does not end the wait early; on the interval wait it still does.

In process, with a fake store and a fake lore, the interval 0.1 s. The run is the real `run()`: the test wraps
`_pause` (the one place the loop waits) to record each wait it asks for. The long waits run at a fiftieth of
their length (the wrapper scales what it passes on), so the cap shows in under a second; the scenarios that
count passes against the clock (a wake during a backoff, a wake on the interval wait) run at full length.

The repo loop (`decisions_repo.RepoDecisions`) has no timer: a pass runs when a folder is added, a push lands
or the app starts, and a failed pass is not queued again. The last scenario shows that: a lore that refuses
every put gets one attempt per request and nothing more, so there is no loop there to back off.

Covered: the wait rule as a pure function; which failures count (list, put, a put that raises, delete, a raise out of a pass, a
failure in the pass that deletes after a project turned private); a document lore refuses (400, 413, 422) is put alone, stays out, and is no failure; the first failure doubles the base; the cap;
a clean pass resets; a wake during a backoff starts no pass; a wake on the interval wait does; a raising pass
still backs off and never spins; the loop stops with its task; the repo loop does not retry a failed put.

Run: .venv/bin/python ee/tests/test_brain_decisions_backoff.py
"""
import asyncio, base64, os, sys, time
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

import httpx

from tares_ee.brain import decisions as dec_mod
from tares_ee.brain import decisions_repo as dr
from tares_ee.brain.lore import LoreError

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


BASE = 0.1
SCALE = 0.02   # the long waits of the scaled scenarios run at this share of their length


class Lore:
    """A lore that holds documents in memory and fails the calls named in `mode`: a set of "list", "put",
    "delete", or "raise" (entering the client raises). Counts every call."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.mode: set[str] = set()
        self.calls: list[str] = []

    def __call__(self):
        return self

    async def __aenter__(self):
        if "raise" in self.mode:
            raise RuntimeError("the client could not be made")
        return self

    async def __aexit__(self, *exc):
        return None

    async def list_documents(self, **kw):
        self.calls.append("list")
        if "list" in self.mode:
            raise LoreError("The brain did not answer within 10 seconds")
        return {"documents": [{"external_id": e} for e in self.docs], "next_cursor": None}

    async def put_documents(self, source, documents):
        self.calls.append("put")
        if "put" in self.mode:
            raise LoreError("The brain did not answer within 10 seconds")
        if "putraise" in self.mode:
            raise RuntimeError("the put broke")
        if "refuse" in self.mode and any("bad" in d["external_id"] for d in documents):
            raise LoreError("The brain refused the document", 413)
        for d in documents:
            self.docs[d["external_id"]] = d
        return {}

    async def delete_document(self, source, ext):
        self.calls.append("delete")
        if "delete" in self.mode:
            raise LoreError("The brain did not answer within 10 seconds")
        self.docs.pop(ext, None)


class Store:
    """The few reads `Decisions.wanted` makes: one shared project linked to acme/web with one spec."""

    def __init__(self):
        self.private: set[str] = set()
        self.ids = ["d1"]
        self.body = "text"

    def private_projects(self):
        return [{"id": p} for p in self.private]

    def list_brain_repos(self):
        return [{"repo": "acme/web"}]

    def list_projects(self):
        return [{"id": "p1", "scope": "team"}]

    def get_handover(self, pid):
        return {"repos": [{"name": "acme/web"}]}

    def list_docs(self, pid):
        return [{"id": i, "kind": "spec"} for i in self.ids]

    def get_doc(self, pid, doc_id):
        return {"id": doc_id, "kind": "spec", "title": "Spec", "body": self.body, "updated_by": "Ann",
                "updated_at": "2026-03-01T00:00:00Z"}


def make(interval=BASE):
    dec_mod.INTERVAL_SECONDS = interval
    lore = Lore()
    dec = dec_mod.Decisions(Store(), lore)
    dec.active = lambda: True
    return dec, lore


def instrument(dec, scale=1.0):
    """Record each wait `run` asks for as (seconds, backing_off), and each pass start as a time. The wait
    itself runs through the real `_pause`, `scale` times as long."""
    waits, starts = [], []
    real_pause, real_reconcile = dec._pause, dec.reconcile

    async def pause(seconds, backing_off):
        waits.append((round(seconds, 6), backing_off))
        await real_pause(seconds * scale, backing_off)

    async def reconcile():
        starts.append(time.monotonic())
        return await real_reconcile()

    dec._pause, dec.reconcile = pause, reconcile
    return waits, starts


async def until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.005)
    return cond()


async def stop(dec):
    dec.task.cancel()
    await asyncio.gather(dec.task, return_exceptions=True)
    done = dec.task.done()
    dec.task = None
    return done


def unit():
    print("== the wait rule ==")
    nw = dec_mod.next_wait
    ck("a clean pass waits the base, whatever the last wait was", nw(0.1, False, 0.1) == 0.1 and nw(3.0, False, 0.1) == 0.1)
    ck("the first failure doubles the base", abs(nw(0.1, True, 0.1) - 0.2) < 1e-9)
    ck("a failure doubles the last wait", abs(nw(0.4, True, 0.1) - 0.8) < 1e-9)
    ck("the cap is 30 x the base", dec_mod.BACKOFF_MAX == 30 and abs(nw(2.0, True, 0.1) - 3.0) < 1e-9
       and abs(nw(3.0, True, 0.1) - 3.0) < 1e-9 and nw(1800.0, True, 60.0) == 1800.0 and nw(60.0, True, 60.0) == 120.0)
    ck("the interval is 60 s", dec_mod.INTERVAL_SECONDS == 60.0)


async def which_failures_count():
    print("== which failures make a pass a failed one ==")
    dec, lore = make()
    ck("a clean pass is not failed", await dec.reconcile() is False and "ext" not in lore.docs and len(lore.docs) == 1)
    ck("and sends nothing the second time", await dec.reconcile() is False and lore.calls.count("put") == 1, str(lore.calls))
    dec, lore = make()
    lore.mode = {"list"}
    ck("a listing that fails", await dec.reconcile() is True)
    dec, lore = make()
    lore.mode = {"put"}
    ck("a put that fails", await dec.reconcile() is True and not lore.docs)
    lore.mode = set()
    ck("the documents of a failed put are sent by the next pass, none twice",
       await dec.reconcile() is False and len(lore.docs) == 1 and lore.calls.count("put") == 2)
    dec, lore = make()
    lore.docs["project:gone:doc:x"] = {}
    lore.mode = {"delete"}
    ck("a delete that fails", await dec.reconcile() is True and "project:gone:doc:x" in lore.docs)
    lore.mode = set()
    ck("and is tried again next pass", await dec.reconcile() is False and "project:gone:doc:x" not in lore.docs)
    dec, lore = make()
    await dec.reconcile()
    dec.store.private.add("p1")
    lore.docs["project:p1:doc:d1"] = {}
    dec._known.add("project:p1:doc:d1")
    lore.mode = {"delete"}
    ck("a delete that fails in the privacy check at the end of the pass", await dec.reconcile() is True)
    dec, lore = make()
    lore.mode = {"raise"}
    raised = False
    try:
        await dec.reconcile()
    except RuntimeError:
        raised = True
    ck("a pass that raises goes up to `run`, which handles it", raised)
    dec, lore = make()
    lore.mode = {"putraise"}
    ck("a put that raises something that is not a LoreError", await dec.reconcile() is True and not lore.docs)
    dec, lore = make()
    await dec.reconcile()
    real_wanted = dec.wanted

    def wanted_then_private():
        got = real_wanted()
        dec.store.private.add("p1")      # the project turns private while the pass runs
        return got

    dec.wanted = wanted_then_private
    lore.mode = {"delete"}
    ck("a delete that fails for a project made private during the pass (nothing else to delete)",
       await dec.reconcile() is True and "project:p1:doc:d1" in lore.docs and lore.calls.count("delete") == 1, str(lore.calls))
    lore.mode = set()
    dec.wanted = real_wanted
    ck("and the next pass removes it, no failure", await dec.reconcile() is False and not lore.docs)


async def refused_documents():
    print("== a document lore refuses is not a failed pass ==")
    dec, lore = make()
    dec.store.ids = ["d1", "bad", "d2"]
    lore.mode = {"refuse"}
    ck("one refused document of three: the pass is not failed", await dec.reconcile() is False, str(lore.calls))
    ck("the other two landed", sorted(lore.docs) == ["project:p1:doc:d1", "project:p1:doc:d2"], str(sorted(lore.docs)))
    ck("the batch was tried, then each document alone", lore.calls.count("put") == 4, str(lore.calls))
    before = lore.calls.count("put")
    ck("an unchanged refused document is not sent again", await dec.reconcile() is False and lore.calls.count("put") == before,
       str(lore.calls))
    dec.store.body = "new text"
    ck("a changed one is tried again (and refused again, still not a failure)",
       await dec.reconcile() is False and lore.calls.count("put") > before, str(lore.calls))
    for status in (400, 422):
        dec, lore = make()
        dec.store.ids = ["bad"]
        real = lore.put_documents

        async def put(source, documents, status=status):
            lore.calls.append("put")
            raise LoreError("refused", status)

        lore.put_documents = put
        ck(f"a {status} is a refusal too", await dec.reconcile() is False and lore.calls.count("put") == 2, str(lore.calls))
    for status in (500, 503, None):
        dec, lore = make()
        dec.store.ids = ["d1", "d2"]

        async def put(source, documents, status=status):
            lore.calls.append("put")
            raise LoreError("down", status)

        lore.put_documents = put
        ck(f"a {status} is a failure, and the batch is not split", await dec.reconcile() is True and lore.calls.count("put") == 1,
           str(lore.calls))
    dec, lore = make()
    dec.store.ids = ["d1", "bad"]
    lore.mode = {"refuse"}
    waits, starts = instrument(dec, 0.02)
    dec.task = asyncio.create_task(dec.run())
    ok = await until(lambda: len(waits) >= 3)
    await stop(dec)
    ck("run: a refused document leaves the wait at the base, every pass", ok and all(w == (BASE, False) for w in waits[:3]), str(waits[:4]))


async def doubling_and_cap():
    print("== always failing: the waits double to the cap, a success resets ==")
    for mode, label in (({"list"}, "list"), ({"put"}, "put"), ({"raise"}, "a pass that raises")):
        dec, lore = make()
        lore.mode = mode
        waits, _ = instrument(dec, SCALE)
        dec.task = asyncio.create_task(dec.run())
        ok = await until(lambda: len(waits) >= 9)
        want = [(0.2, True), (0.4, True), (0.8, True), (1.6, True), (3.0, True), (3.0, True), (3.0, True), (3.0, True), (3.0, True)]
        ck(f"{label}: 0.2, 0.4, 0.8, 1.6, then 3.0 and no more", ok and waits[:9] == want, str(waits[:10]))
        if mode == {"list"}:
            lore.mode = set()
            n = len(waits)
            ok = await until(lambda: len(waits) > n)
            ck("a pass with no failure waits the base again", ok and waits[n] == (0.1, False), str(waits[n - 1:n + 2]))
            lore.mode = {"list"}
            ok = await until(lambda: any(b for _s, b in waits[n:]))
            first = next(((s, b) for s, b in waits[n:] if b), None)
            ck("and the next failure starts from the base: 0.2", ok and first == (0.2, True), str(waits[n:n + 6]))
        ck(f"{label}: the loop stops with its task", await stop(dec))


async def wakes():
    print("== a wake while backing off starts no pass; on the interval wait it does ==")
    dec, lore = make()
    lore.mode = {"list"}
    waits, starts = instrument(dec)   # full length: 0.2 then 0.4
    dec.task = asyncio.create_task(dec.run())
    t0 = time.monotonic()
    while len(starts) < 3 and time.monotonic() - t0 < 5:
        dec.wake()   # a document changes every few milliseconds the whole time
        await asyncio.sleep(0.005)
    ck("three passes ran", len(starts) >= 3, str(len(starts)))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    ck("the first backoff held for its 0.2 s whatever woke it", gaps[0] >= 0.19, f"{gaps[0]:.3f}")
    ck("the second held for 0.4 s", len(gaps) > 1 and gaps[1] >= 0.39, str([round(g, 3) for g in gaps]))
    ck("and the waits asked for were 0.2 and 0.4", waits[:2] == [(0.2, True), (0.4, True)], str(waits[:3]))
    ck("the loop stops with its task", await stop(dec))

    dec, lore = make(1.0)
    waits, starts = instrument(dec)
    dec.task = asyncio.create_task(dec.run())
    ok = await until(lambda: len(starts) == 1 and len(waits) == 1)
    ck("a clean pass waits the interval, a wake can end it", ok and waits == [(1.0, False)], str(waits))
    t = time.monotonic()
    dec.wake()
    ok = await until(lambda: len(starts) == 2, 0.5)
    ck("a wake on the interval wait starts the next pass at once", ok and starts[1] - t < 0.3, f"{starts[1] - t:.3f}" if len(starts) > 1 else "no pass")
    ck("the loop stops with its task", await stop(dec))

    dec, lore = make()
    lore.mode = {"list"}
    waits, starts = instrument(dec)
    dec.task = asyncio.create_task(dec.run())
    await until(lambda: len(starts) == 1 and len(waits) == 1)
    lore.mode = set()
    dec.wake()
    await asyncio.sleep(0.12)
    ck("a failed pass's wake did not start a pass inside its 0.2 s wait", len(starts) == 1, str(len(starts)))
    ok = await until(lambda: len(starts) == 2, 1.0)
    ck("the pass after the wait is clean and the wait goes back to the base", ok and await until(lambda: len(waits) == 2)
       and waits[1] == (0.1, False), str(waits))
    ck("the loop stops with its task", await stop(dec))


class Gh:
    """GitHub's routes for one repo of three markdown files in docs/adr."""

    FILES = {f"docs/adr/a{i}.md": f"# a{i}\n" for i in range(3)}

    async def handle(self, request):
        path = request.url.raw_path.decode().split("?")[0]
        q = dict(request.url.params)
        rest = "/".join(path.strip("/").split("/")[3:])
        if rest.startswith("git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": p, "type": "blob", "sha": "blob-" + p} for p in self.FILES],
                                             "truncated": False})
        if rest.startswith("git/blobs/"):
            p = unquote(rest[len("git/blobs/blob-"):])
            return httpx.Response(200, json={"encoding": "base64", "size": len(self.FILES[p]),
                                             "content": base64.encodebytes(self.FILES[p].encode()).decode()})
        if rest.endswith("/pulls"):
            return httpx.Response(200, json=[])
        p = unquote(q.get("path", ""))
        c = {"sha": "c-" + p, "commit": {"author": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"},
                                         "committer": {"name": "Ann A", "date": "2024-01-01T00:00:00Z"}},
             "author": {"login": "ann"}}
        return httpx.Response(200, json=[c] if p in self.FILES else [])


class RepoStore:
    def list_brain_folders(self, repo):
        return [{"path": "docs/adr"}]

    def list_brain_repos(self):
        return [{"repo": "acme/web"}]

    def find_person_by_github(self, login):
        return None


async def repo_loop():
    print("== the repo loop has no timer: a failed put is not retried by itself ==")
    lore = Lore()
    lore.mode = {"put"}

    async def token(repo):
        return "gh-token-" + "for-the-backoff-test"   # gitleaks:allow

    rd = dr.RepoDecisions(RepoStore(), lore, token, transport=httpx.MockTransport(Gh().handle))
    rd.active = lambda: True
    rd.request("acme/web", ["docs/adr"])
    await asyncio.wait_for(rd.wait(), 10)
    first = lore.calls.count("put")
    ck("a request makes one put attempt and the import ends", first == 1 and not rd._tasks, f"{first} {rd._tasks}")
    await asyncio.sleep(0.5)
    ck("nothing sends again by itself while lore refuses", lore.calls.count("put") == first and not rd._tasks,
       f"{lore.calls.count('put')} puts")
    rd.on_push("acme/web", ["docs/adr/a0.md"])
    await asyncio.wait_for(rd.wait(), 10)
    ck("the next trigger (a push) is one more attempt", lore.calls.count("put") == first + 1, str(lore.calls))
    lore.mode = set()
    rd.request("acme/web", ["docs/adr"])
    await asyncio.wait_for(rd.wait(), 10)
    ck("and once lore answers, the documents go in", len(lore.docs) == 3, str(sorted(lore.docs)))


async def main():
    saved = dec_mod.INTERVAL_SECONDS
    try:
        await which_failures_count()
        await refused_documents()
        await doubling_and_cap()
        await wakes()
        await repo_loop()
    finally:
        dec_mod.INTERVAL_SECONDS = saved
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    unit()
    asyncio.run(main())

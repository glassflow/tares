"""The factory routes answer with a constant number of queries, however many tickets a project has
(TR-657), and the batched answers are exactly what the per-ticket code gave: a golden file recorded
from the code before batching.

Run:    .venv/bin/python tests/test_factory_batching.py
Record: .venv/bin/python tests/test_factory_batching.py --record   (only before a refactor, from
        the code whose answers are the reference)
"""
import asyncio
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ  # noqa: E402

scrub_os_environ()
_tmp = tempfile.mkdtemp(prefix="test-factory-batching-")
os.environ["TARES_DB"] = os.path.join(_tmp, "t.duckdb")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for var in ("TARES_AUTH_TOKEN", "TARES_CATALOG", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
            "OPENAI_API_KEY"):
    os.environ.pop(var, None)

import httpx  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "factory_batching")
RECORD = "--record" in sys.argv
# Keys whose values come from the clock; dropped before comparing.
VOLATILE = {"updated_at", "created_at", "at", "reviewed_at", "found_at", "checked_at", "now", "waiting_minutes",
            "waiting_since"}
# Generated ids, replaced by stable names in order of first appearance.
ID = re.compile(r"\b(?:tk|doc|ms|rv|rf|uc)_[0-9a-f]{8,12}\b")

PASS = FAIL = 0


def ck(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def normalize(obj):
    """The answer without clock values, with ids renamed in order of first appearance, so two
    builds of the same project compare equal."""
    names: dict[str, str] = {}

    def rename(s: str) -> str:
        return ID.sub(lambda m: names.setdefault(m.group(0), f"<{m.group(0).split('_')[0]}{len(names)}>"), s)

    def walk(o):
        if isinstance(o, dict):
            return {rename(k): walk(v) for k, v in o.items() if k not in VOLATILE}
        if isinstance(o, list):
            return [walk(v) for v in o]
        if isinstance(o, str):
            return rename(o)
        return o

    return walk(obj)


class CountingCon:
    """Stands in for store.con and counts execute calls (a DuckDB connection's own execute
    cannot be patched)."""

    def __init__(self, con):
        self._con = con
        self.n = 0

    def execute(self, *a, **kw):
        self.n += 1
        return self._con.execute(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._con, name)


def build(store, name: str, n: int) -> str:
    """A factory project with `n` tickets: milestones, dependencies, working docs (one not
    included in the project), stages and PRs, and review rounds with findings fixed later."""
    uid = f"uc_{name}"
    store.create_project(uid, "custom", name, {"objects": []})
    store.set_project_kind(uid, "software_factory")
    ms = [store.create_milestone(uid, f"M{i}. Milestone {i}", goal=f"goal {i}") for i in (1, 2, 3)]
    outside = store.create_doc(None, "working", "A doc the project does not include", "body")
    tids: list[str] = []
    for i in range(n):
        doc = None
        if i % 3 == 0:
            doc = store.create_doc(uid, "working", f"Working: ticket {i}", f"steps for {i}")
        elif i == 4:
            doc = outside
        tid = store.create_ticket(uid, "tares", f"Ticket {i}", status="todo",
                                  working_doc=doc, milestone=ms[i % 3],
                                  depends_on=[tids[i - 1]] if i % 4 == 1 and tids else None)
        tids.append(tid)
        if i % 5 == 0:
            store.update_ticket(tid, status="done")
        if i % 2 == 0:
            stage = ("review", "changes", "doing", "merged")[(i // 2) % 4]
            pr = {"url": f"https://github.com/o/r/pull/{i}", "repo": "o/r", "number": i,
                  "head": f"{i:040x}"}
            store.update_ticket(tid, stage=stage, pr=pr)
        if i % 4 == 0:
            for rnd in range(1 + i % 3):
                store.add_review(uid, tid, f"https://github.com/o/r/pull/{i}", f"{i + rnd:040x}",
                                 ("changes", "pass")[rnd == i % 3], "tests", "", "crew-reviewer",
                                 [{"severity": "major", "file": "a.py", "line": rnd + 1,
                                   "blocking": rnd == 0, "text": f"finding {i}.{rnd}"}],
                                 {"F1": "fixed"} if rnd else {}, lambda t: "bug",
                                 "2026-10-22T10:00:00Z")
    return uid


async def _no_refresh(store, uid):
    return None


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    small, big = build(store, "small", 10), build(store, "big", 30)
    counting = CountingCon(store.con)
    store.con = counting

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 timeout=60) as cx:

        async def get(path):
            before = counting.n
            r = await cx.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:300])
            return r.json(), counting.n - before

        print("== tickets list (TR-657) ==")
        out, _ = await get(f"/api/projects/{big}/tickets")
        got = normalize(out)
        path = os.path.join(FIXTURES, "tickets.json")
        if RECORD:
            os.makedirs(FIXTURES, exist_ok=True)
            with open(path, "w") as f:
                json.dump(got, f, indent=1, sort_keys=True, default=str)
                f.write("\n")
            print(f"  recorded {path}")
        else:
            want = json.load(open(path))
            same = json.loads(json.dumps(got, sort_keys=True, default=str)) == want
            ck("same answer as before batching (golden file)", same,
               "differs from tests/fixtures/factory_batching/tickets.json")
        # The PR refresh the route runs first still reads per ticket with a PR; it is out of this
        # ticket's scope, so the count covers the list itself.
        from tares import factory_api
        refresh, factory_api.refresh_prs = factory_api.refresh_prs, _no_refresh
        try:
            _, q_small = await get(f"/api/projects/{small}/tickets")
            _, q_big = await get(f"/api/projects/{big}/tickets")
        finally:
            factory_api.refresh_prs = refresh
        ck("queries do not grow with the tickets (10 vs 30)", q_small == q_big,
           f"{q_small} queries for 10 tickets, {q_big} for 30")
        print(f"  {q_big} queries for 30 tickets")

    store.con = counting._con
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

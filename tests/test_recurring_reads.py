"""recurring() reads a fixed number of times however many findings a repo has (TR-1605).

The ticket brief, POST .../reviews (which offers rules from the recurring findings) and
/api/recurring-findings all run recurring(). It used to read the ticket once per finding and every
project's full row once; now the finding rows carry the ticket's label and the project names come
in one query. The test seeds 200 findings over 40 tickets in 5 projects (some tickets named by a
Linear identifier, one finding whose project is not its ticket's, one too old, one in another repo),
holds the old function body as the reference and asserts the same answer, and that the route runs at
most 4 statements, and the same number for a repo with 3 findings as for one with 200.

Run: .venv/bin/python tests/test_recurring_reads.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python tests/test_recurring_reads.py
"""
import asyncio
import contextvars
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-recurring-reads-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-recurring"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import factory as factory_mod
from tares.envelope import now_utc

P = F = 0
BUDGET = 4
REPO = "acme/shop"
TEXTS = ["a migration is missing", "no tests for the parser", "unhandled error in the loop",
         "a config key is not documented", "null input crashes", "a race condition on save",
         "the secret is logged", "the README is out of date"]


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def reference(store, repo, hidden=frozenset()):
    """recurring() as it was before TR-1605: one get_ticket per finding, list_projects once."""
    rows = [f for f in store.repo_findings(repo, now_utc() - timedelta(days=factory_mod.RECUR_DAYS))
            if f["project"] not in hidden]
    names = {p["id"]: p["name"] for p in store.list_projects()}
    by: dict[str, dict] = {}
    for f in rows:
        k = by.setdefault(f["kind"], {"kind": f["kind"], "refs": set(), "examples": []})
        t = store.get_ticket(f["project"], f["ticket"])
        k["refs"].add((f["project"], factory_mod.label(t) if t else "?"))
        if len(k["examples"]) < 3:
            k["examples"].append(f["text"][:200])
    out = [{"kind": k["kind"], "count": len(k["refs"]),
            "projects": sorted({p for p, _ in k["refs"]}),
            "tickets": sorted(f"{names.get(p, '?')} {lab}" for p, lab in k["refs"]),
            "examples": k["examples"]} for k in by.values()
           if len(k["refs"]) >= factory_mod.RECUR_MIN]
    return sorted(out, key=lambda r: -r["count"])


_COUNTER: contextvars.ContextVar = contextvars.ContextVar("query_counter", default=None)


def _hit():
    c = _COUNTER.get()
    if c is not None:
        c[0] += 1


class CountingCon:
    """Stands in for the DuckDB connection: counts execute and executemany (as the query-budget test
    does), hands everything else to the real one."""

    def __init__(self, real):
        self._real = real

    def execute(self, *a, **kw):
        _hit()
        return self._real.execute(*a, **kw)

    def executemany(self, *a, **kw):
        _hit()
        return self._real.executemany(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


def install_counters(store):
    shared = store.sc is store.con
    store.con = CountingCon(store.con)
    if shared:
        store.sc = store.con
        return "duckdb"
    for name in ("execute", "executemany"):
        real = getattr(store.sc, name)

        def counted(*a, _real=real, **kw):
            _hit()
            return _real(*a, **kw)
        setattr(store.sc, name, counted)
    return "postgres"


def seed(store):
    """5 projects of 8 tickets, 5 findings each (200), every ticket's pull request in REPO; 3 more
    tickets in another repo."""
    n = 0
    for i in range(5):
        uid = f"uc_rec{i}"
        store.create_project(uid, "custom", f"Recurring project {i}", {}, goal="g")
        store.set_project_kind(uid, "software_factory")
        for j in range(8):
            tid = store.create_ticket(uid, "linear" if j % 3 == 0 else "tares", f"Ticket {j}",
                                      identifier=f"REC{i}-{j + 10}" if j % 3 == 0 else None)
            store.update_ticket(tid, stage="review",
                                pr={"url": f"https://github.com/{REPO}/pull/{n}", "repo": REPO,
                                    "number": n, "head": "abc1234"})
            findings = [{"severity": "P2", "file": None, "line": None, "blocking": False,
                         "text": TEXTS[(i + j + k) % len(TEXTS)] + f" ({n}.{k})"} for k in range(5)]
            store.add_review(uid, tid, f"https://github.com/{REPO}/pull/{n}", "abc1234", "changes",
                             "ran tests", "", "crew-reviewer", findings, {}, factory_mod.finding_kind,
                             now_utc())
            n += 1
    uid = "uc_rec0"
    for j in range(3):
        tid = store.create_ticket(uid, "tares", f"Elsewhere {j}")
        store.update_ticket(tid, stage="review",
                            pr={"url": f"https://github.com/acme/other/pull/{j}", "repo": "acme/other",
                                "number": j, "head": "abc1234"})
        store.add_review(uid, tid, None, "abc1234", "changes", "ran tests", "", "crew-reviewer",
                         [{"severity": "P2", "file": None, "line": None, "blocking": False,
                           "text": "a migration is missing"}], {}, factory_mod.finding_kind, now_utc())
    return n


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}

    async def route(repo):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=root, timeout=60) as cx:
            counter = [0]
            token = _COUNTER.set(counter)
            try:
                r = await cx.get("/api/recurring-findings", params={"repo": repo})
            finally:
                _COUNTER.reset(token)
            return r, counter[0]

    async with app.router.lifespan_context(app):
        tickets = seed(store)
        # a finding that is not on its ticket's project, one older than the window
        first = next(t["id"] for t in store.list_tickets("uc_rec1") if t["number"] == 1)
        old = store.sc.execute("SELECT id FROM review_findings WHERE ticket = ?", [first]).fetchone()[0]
        store.sc.execute("UPDATE review_findings SET project = 'uc_gone' WHERE id = ?", [old])
        older = store.sc.execute("SELECT id FROM review_findings WHERE project = 'uc_rec2' LIMIT 1").fetchone()[0]
        store.sc.execute("UPDATE review_findings SET found_at = ? WHERE id = ?",
                         [now_utc() - timedelta(days=90), older])
        total = store.sc.execute("SELECT count(*) FROM review_findings").fetchone()[0]
        print(f"== {total} findings over {tickets} tickets in 5 projects ==")
        # the findings are in DuckDB, the tickets in the session store (TR-1612)
        here = {t["id"] for i in range(5) for t in store.list_tickets(f"uc_rec{i}")
                if (t["pr"] or {}).get("repo") == REPO}
        found = store.sc.execute("SELECT ticket FROM review_findings").fetchall()
        ck("the cell holds 200 findings on the repo and 3 on another",
           sum(1 for r in found if r[0] in here) == 200 and total == 203, total)

        want = reference(store, REPO)
        ck("the reference finds recurring kinds", len(want) >= 5 and any("?" in " ".join(r["tickets"]) for r in want), want[:2])
        ck("a ticket named by a Linear identifier keeps it",
           any("REC0-10" in t for r in want for t in r["tickets"]), want[0]["tickets"][:3])

        backend = install_counters(store)
        print(f"backend: {backend}")
        r, n = await route(REPO)
        ck("the route answers 200", r.status_code == 200, r.text[:200])
        body = r.json()
        ck("the answer is the old function's, whole", body["recurring"] == want,
           (body["recurring"][:1], want[:1]))
        ck(f"the route runs at most {BUDGET} statements for 200 findings", n <= BUDGET, n)
        print(f"  statements for 200 findings: {n}")
        r3, n3 = await route("acme/other")
        ck("the other repo answers the old function's answer too", r3.json()["recurring"] == reference(store, "acme/other"))
        r0, n0 = await route("acme/none")
        ck("a repo with no findings answers an empty list", r0.json()["recurring"] == [])
        ck("the same statements for 203 findings as for none", n == n0 == n3, (n, n3, n0))
        print(f"  statements for 3 findings: {n3}, for none: {n0}")
        ck("hidden projects are left out as before",
           [(k["kind"], k["count"]) for k in reference(store, REPO, {"uc_rec0"})] != [(k["kind"], k["count"]) for k in want])


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

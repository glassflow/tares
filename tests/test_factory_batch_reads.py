"""The batched reads behind the factory pages answer what the per-item reads did (TR-1472).

`challenge_events_many` is checked against the query `challenge_events` ran before (one name lookup
and one select per ticket), over lines stamped with the project's name, its id, another project's,
and unstamped lines from a session tied to the project or not. `projects_with_choice`,
`members_by_project` and `handovers_by_project` are checked against their one-at-a-time reads, and
`warm_gates` against `outside_gate`, project by project.

Run: .venv/bin/python tests/test_factory_batch_reads.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-batch-reads-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

from tares import factory_api
from tares.daemon import make_app

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def old_challenge_events(store, source, project, ref):
    """What challenge_events ran before TR-1472."""
    with store._sread:
        name = (store.sc.execute("SELECT name FROM usecases WHERE id = ?", [project]).fetchone()
                or [project])[0]
        rows = store.sc.execute(
            "SELECT event_time, event_type, payload, key_value FROM challenges "
            "WHERE source = ? AND (branch = ? OR branch LIKE ?) "
            "AND (lower(tares_project) IN (lower(?), lower(?)) "
            "     OR (tares_project IS NULL AND key_value IN "
            "         (SELECT session FROM project_sessions WHERE project = ?))) "
            "ORDER BY event_time", [source, f"factory/{ref}", f"factory/{ref}-%",
                                    name, project, project]).fetchall()
    out = []
    for at, typ, pj, sid in rows:
        o = json.loads(pj) if isinstance(pj, str) else (pj or {})
        ch = o.get("challenge") if isinstance(o.get("challenge"), dict) else {}
        out.append({"at": at, "type": typ, "session": sid, "challenge": ch})
    return out


def main():
    app = make_app()
    store = app.state.store
    t0 = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    for uid, name in (("uc_a", "Alpha"), ("uc_b", "Beta"), ("uc_c", "Gamma")):
        store.create_project(uid, "software_factory", name, {"objects": []})
    store.sc.execute("INSERT INTO project_sessions (project, session, repo, linked_at) VALUES "
                     "('uc_a', 's-a', 'r', ?), ('uc_b', 's-b', 'r', ?)", [t0, t0])
    rows, n = [], 0

    def line(session, branch, project, typ="challenge_commit"):
        nonlocal n
        n += 1
        rows.append(("cx", session, t0 + timedelta(minutes=n), typ, branch, project,
                     json.dumps({"challenge": {"verdict": "PASS", "n": n}})))

    for branch in ("factory/T1", "factory/T1-2", "factory/T2", "factory/T10"):
        line("s-a", branch, "alpha")            # name, any case
        line("s-a", branch, "uc_a")             # id
        line("s-x", branch, "uc_b")             # another project's stamp
        line("s-a", branch, None)               # unstamped, session tied to uc_a
        line("s-b", branch, None)               # unstamped, session tied to uc_b
        line("s-z", branch, None, "challenge_waived")   # unstamped, tied to nobody
        line("s-a", branch, "ALPHA", "challenge_plan")
    line("s-a", "factory/T3", "uc_a")
    line("s-a", "other/T1", "uc_a")
    store._record_challenges(rows)

    asks = [("uc_a", "T1"), ("uc_a", "T2"), ("uc_b", "T1"), ("uc_b", "T10"), ("uc_c", "T1"),
            ("uc_a", "T3"), ("uc_a", "T99"), ("uc_gone", "T1"), ("uc_a", "T1")]
    got = store.challenge_events_many("cx", asks)
    ck("one entry per distinct pair asked", set(got) == set(asks) and len(got) == len(set(asks)),
       str(sorted(got)))
    for pair in sorted(set(asks)):
        want = old_challenge_events(store, "cx", *pair)
        ck(f"{pair}: the same lines in the same order as the per-ticket read ({len(want)})",
           got[pair] == want, f"{got[pair]} vs {want}")
        ck(f"{pair}: challenge_events answers the same", store.challenge_events("cx", *pair) == want)
    ck("a ref's own branch and its -N branches both count",
       len(got[("uc_a", "T1")]) > len(got[("uc_a", "T2")]) > 0, str(len(got[("uc_a", "T1")])))
    ck("a ref nothing was recorded for is empty", got[("uc_a", "T99")] == [])
    ck("no pairs asked gives {}", store.challenge_events_many("cx", []) == {})
    big = [("uc_a", f"T{i}") for i in range(400)]
    many = store.challenge_events_many("cx", big)
    ck("more refs than one query holds read in chunks, the same answer",
       all(many[p] == old_challenge_events(store, "cx", *p) for p in big[:12]), "differs")

    # projects that hold their own choice, in one query
    store.set_choice("review", "tares", {}, "t")
    store.set_project_choice("uc_b", "review", "tares", {}, "t")
    ck("projects_with_choice names the projects with their own review choice",
       store.projects_with_choice("review", ["uc_a", "uc_b", "uc_c"]) == {"uc_b"})
    ck("projects_with_choice of nothing is empty", store.projects_with_choice("review", []) == set())

    # warm_gates fills the cache with what outside_gate gives for each project alone
    projects = ["uc_a", "uc_b", "uc_c"]
    cache = {}
    factory_api.warm_gates(store, projects, cache)
    for uid in projects:
        alone = factory_api.outside_gate(store, uid)
        ck(f"warm_gates: {uid} has the gate outside_gate gives it alone",
           uid in cache or alone is None or uid == "uc_b", f"{cache} vs {alone}")
        if uid in cache:
            ck(f"warm_gates: {uid} matches outside_gate", cache[uid] == alone, f"{cache[uid]} vs {alone}")
    ck("a project with its own choice is left for outside_gate to resolve", "uc_b" not in cache)
    ck("outside_gate with the cache answers the same as without",
       all(factory_api.outside_gate(store, u, cache) == factory_api.outside_gate(store, u)
           for u in projects))

    # members in one query
    ids = ["uc_a", "uc_b", "uc_c"]
    members = store.members_by_project(ids)
    ck("members_by_project agrees with list_project_members",
       all([m["person_id"] for m in members.get(u, [])]
           == [m["person_id"] for m in store.list_project_members(u)] for u in ids),
       str(members))

    # handovers in one query
    got_h = store.handovers_by_project(ids)
    ck("handovers_by_project agrees with get_handover for each project",
       all(got_h.get(u) == store.get_handover(u) for u in ids), str(got_h))
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()

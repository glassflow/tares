"""The batched reads behind the factory pages answer what the per-item reads did (TR-1472).

`challenge_events_many` is checked against the query `challenge_events` ran before (one name lookup
and one select per ticket), over lines stamped with the project's name, its id, another project's,
and unstamped lines from a session tied to the project or not. `projects_with_choice`,
`members_by_project` and `handovers_by_project` are checked against what was stored (people on one,
two and no project; a hand-over with an override, a later clean one, one never handed over), and
`warm_gates` against `outside_gate`, with an outside tool as the factory's gate and one project that
keeps its own. `autonomy_of`'s memo is checked for two people with different crew settings.

A challenger line whose payload is not JSON, or is JSON but not an object, reads as {} among good
ones: the review queue and the ticket still answer, and the one that is not JSON is logged once, by
its ids alone (TR-1705).

Run: .venv/bin/python tests/test_factory_batch_reads.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
from session_helpers import seed_link
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-batch-reads-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

from sqlalchemy import event, select

from tares import factory_api
from tares.daemon import make_app
from tares.integrations import registry
from tares.integrations.review import ReviewGate
from tares.session_store.tables import challenges as challenges_table


class FakeGate(ReviewGate):
    """An outside review tool, so the factory's gate is not the crew reviewer."""
    name = "fakegate"
    title = "FakeBot"

    def __init__(self, store):
        pass

    async def check_connection(self, settings):
        return True, "ok"


_real_get = registry.get
registry.get = lambda slot, name: FakeGate if (slot, name) == ("review", "fakegate") else _real_get(slot, name)

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def old_challenge_events(store, source, project, ref):
    """What challenge_events ran before TR-1472: one name lookup and one select per ticket. The
    branch, name and session conditions are applied here, with the case rules the old DuckDB query
    had (the branch exact, the stamp in any case)."""
    linked = set(store.sessions_of_projects([project]))      # the session store's rows
    name = (store.get_project(project) or {}).get("name") or project
    t = challenges_table
    with store.sessions_db.read() as conn:
        found = conn.execute(select(t.c.event_time, t.c.event_type, t.c.payload, t.c.key_value,
                                    t.c.branch, t.c.tares_project)
                             .where(t.c.source == source).order_by(t.c.event_time)).fetchall()
    rows = [r[:4] for r in found
            if (r.branch == f"factory/{ref}" or r.branch.startswith(f"factory/{ref}-"))
            and (r.key_value in linked if r.tares_project is None
                 else r.tares_project.lower() in (name.lower(), project.lower()))]
    out = []
    for at, typ, pj, sid in rows:
        o = json.loads(pj or "{}")
        ch = o.get("challenge") if isinstance(o.get("challenge"), dict) else {}
        out.append({"at": at, "type": typ, "session": sid, "challenge": ch})
    return out


# a challenger line's payload as stored: (what it is, the text, what reads back). Not JSON first
UNREADABLE = [("not JSON", "SECRETVERDICT not json"),
              ("a trailing comma, which DuckDB's JSON column takes", '{"challenge": {"title": "SECRETVERDICT"},}'),
              ("cut off", '{"challenge": {"title": "SECRETVERDICT')]
NOT_AN_OBJECT = [("a list", '[1, 2]'), ("a number", "7"), ("a string", '"SECRETVERDICT"'), ("null", "null"),
                 ("true", "true"), ("NaN", "NaN"), ("no payload", None), ("empty", ""), ("blank", "   "),
                 ("an object whose challenge is a list", '{"challenge": [1]}')]


def payloads_that_are_not_objects(app, store, t0):
    """TR-1705: one such line must not fail the writes, the reads or the pages that list them."""
    print("== a challenger line that is not a JSON object ==")
    uid = "uc_odd"
    store.create_project(uid, "custom", "Odd", {}, goal="A project for the test", status="active")
    store.set_project_kind(uid, "software_factory")
    # the review queue is the crew reviewer's: main() made an outside tool the factory's gate
    store.set_project_choice(uid, "review", "tares", {}, "t")
    tid = store.create_ticket(uid, "tares", "A ticket with an odd line", identifier="ODD-1")
    store.update_ticket(tid, stage="review")

    def good(minute, verdict, title):
        return (minute, json.dumps({"challenge": {"verdict": verdict, "sha": f"c{minute}", "findings": [
            {"title": title, "priority": "P1"}]}}))

    odd = UNREADABLE + NOT_AN_OBJECT
    lines = [good(0, "FAIL", "the first finding")]
    lines += [(i + 1, text) for i, (_, text) in enumerate(odd)]
    lines += [good(len(odd) + 1, "FAIL", "the last finding")]
    rows = [("claude_code", "s-odd", t0 + timedelta(minutes=m), "challenge_commit", "factory/ODD-1", uid, text)
            for m, text in lines]

    said: list[str] = []

    class Said(logging.Handler):
        def emit(self, record):
            said.append(record.getMessage())

    handler = Said(level=logging.WARNING)
    logging.getLogger("tares.factory_store").addHandler(handler)
    try:
        wrote = None
        try:
            store._record_challenges(rows)
        except Exception as ex:
            wrote = ex
        ck("every line is written, the odd ones too", wrote is None, repr(wrote))

        def read():
            try:
                return store.challenge_events("claude_code", uid, "ODD-1")
            except Exception as ex:
                return [{"challenge": {"error": repr(ex)}, "at": None}]

        got = read()
        ck("every line comes back, oldest first", [e["at"] for e in got] == [r[2] for r in rows], got)
        first, last = (got[0]["challenge"], got[-1]["challenge"]) if got else ({}, {})
        ck("the good ones as they are",
           (first.get("sha"), last.get("sha")) == ("c0", f"c{len(odd) + 1}"), got)
        for (what, _), e in zip(odd, got[1:-1]):
            ck(f"{what} reads as {{}}", e["challenge"] == {}, e)
        read()      # read() holds a failure back, so the pages below are still asked
        ck("each line that is not JSON is logged once, however often it is read",
           len(said) == len(UNREADABLE), said)
        ck("the log names the line by its ids",
           all(all(x in line for x in ("claude_code", "s-odd", "challenge_commit", "factory/ODD-1"))
               for line in said) and len({line.rsplit(" at ", 1)[1] for line in said}) == len(said), said)
        ck("and quotes nothing of the payload", not any("SECRETVERDICT" in line for line in said), said)
    finally:
        logging.getLogger("tares.factory_store").removeHandler(handler)

    async def pages():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as cx:
                return (await cx.get("/api/review-queue"),
                        await cx.get(f"/api/projects/{uid}/tickets/ODD-1"))

    queue, ticket = asyncio.run(pages())
    ck("the review queue answers 200", queue.status_code == 200, (queue.status_code, queue.text[:300]))
    row = next((q for q in (queue.json()["queue"] if queue.status_code == 200 else [])
                if q["ticket"] == "ODD-1"), None)
    ck("and lists the ticket", row is not None, queue.text[:300])
    ck("the ticket's page answers 200", ticket.status_code == 200, (ticket.status_code, ticket.text[:300]))
    layer = ticket.json()["challenger"] if ticket.status_code == 200 else {}
    ck("with the challenger's rounds: the last review's finding open, the first one's fixed",
       (layer.get("rounds"), [f["title"] for f in layer.get("open", [])], [f["title"] for f in layer.get("fixed", [])])
       == (len(rows), ["the last finding"], ["the first finding"]), layer)


def main():
    app = make_app()
    store = app.state.store
    t0 = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
    for uid, name in (("uc_a", "Alpha"), ("uc_b", "Beta"), ("uc_c", "Gamma")):
        store.create_project(uid, "software_factory", name, {"objects": []})
    seed_link(store, "uc_a", "s-a", "r", t0)
    seed_link(store, "uc_b", "s-b", "r", t0)
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
    # T1's by their first characters, then branches that only look like T1's (TR-1705)
    own = ("factory/T1-", "factory/T1-fix/ü", "factory/T1-\U0001F600")
    near = ("factory/T1.2", "factory/T1+x", "factory/T1 x", "factory/T1_2", "factory/t1", "factory/t1-2",
            "Factory/T1", "factory/T", "xfactory/T1")
    first_edge = n + 1
    for branch in own + near:
        line("s-a", branch, "uc_a")
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
    edge = sorted(e["challenge"]["n"] for e in got[("uc_a", "T1")] if e["challenge"]["n"] >= first_edge)
    ck("exactly the ref's own branches count: nothing of another case, ref or prefix",
       edge == list(range(first_edge, first_edge + len(own))), str(edge))
    if store.sessions_db.dialect == "sqlite":
        # both columns of ix_challenges_branch find the lines, not the source alone (TR-1705)
        asked = []

        def keep(conn, cursor, statement, parameters, context, executemany):
            if "FROM challenges" in statement:
                asked.append((statement, parameters))

        for what, some in (("one ticket", asks[:1]), ("several tickets", asks)):
            asked.clear()
            event.listen(store.sessions_db.engine, "before_cursor_execute", keep)
            store.challenge_events_many("cx", some)
            event.remove(store.sessions_db.engine, "before_cursor_execute", keep)
            with store.sessions_db.engine.connect() as conn:
                plan = [r[3] for st, pr in asked for r in conn.exec_driver_sql("EXPLAIN QUERY PLAN " + st, pr)]
            searches = [x for x in plan if "challenges" in x]
            ck(f"{what}: every search of challenges uses the branch in its index",
               bool(searches) and all("ix_challenges_branch (source=? AND branch>? AND branch<?)" in x
                                      for x in searches), str(plan))
    ck("no pairs asked gives {}", store.challenge_events_many("cx", []) == {})
    big = [("uc_a", f"T{i}") for i in range(400)]
    many = store.challenge_events_many("cx", big)
    ck("more refs than one query holds read in chunks, the same answer",
       all(many[p] == old_challenge_events(store, "cx", *p) for p in big[:12]), "differs")

    # projects that hold their own choice, in one query. The factory's gate is an outside tool
    # (a fake tool); uc_b keeps its own choice, the crew reviewer, so it has no outside gate
    store.set_choice("review", "tares", {}, "t")
    store.set_project_choice("uc_b", "review", "tares", {}, "t")
    store.set_choice("review", "fakegate", {}, "t")
    ck("projects_with_choice names the projects with their own review choice",
       store.projects_with_choice("review", ["uc_a", "uc_b", "uc_c"]) == {"uc_b"})
    ck("projects_with_choice of nothing is empty", store.projects_with_choice("review", []) == set())

    # project_choice_names (TR-1628): the name of each project's own choice, what get_project_choice
    # says for each one alone; a project without a choice is left out, and nothing asked makes no query
    store.set_project_choice("uc_a", "tickets", "linear", {}, "t")
    store.set_project_choice("uc_b", "tickets", "linear", {}, "t")
    for slot in ("tickets", "review", "ci"):
        ids = ["uc_a", "uc_b", "uc_c", "uc_nope", ""]
        one_by_one = {u: store.get_project_choice(u, slot)["name"] for u in ids
                      if u and store.get_project_choice(u, slot)}
        ck(f"project_choice_names gives the {slot} names get_project_choice gives, one project at a time",
           store.project_choice_names(slot, ids) == one_by_one, str(store.project_choice_names(slot, ids)))
    ck("project_choice_names names the project that keeps its own review choice",
       store.project_choice_names("review", ["uc_a", "uc_b", "uc_c"]) == {"uc_b": "tares"})
    ck("project_choice_names of nothing is empty", store.project_choice_names("tickets", []) == {})
    ck("a duplicate uid is asked once", store.project_choice_names("tickets", ["uc_a", "uc_a"]) == {"uc_a": "linear"})
    try:
        store.project_choice_names("memory", ["uc_a"])
        refused = False
    except ValueError:
        refused = True
    ck("a slot a project cannot choose for is refused", refused)
    many = [f"uc_none{i}" for i in range(1200)] + ["uc_a", "uc_b"]
    ck("more projects than one query holds read in chunks, the same answer",
       store.project_choice_names("tickets", many) == {"uc_a": "linear", "uc_b": "linear"})

    # warm_gates fills the cache with what outside_gate gives for each project alone
    projects = ["uc_a", "uc_b", "uc_c"]
    alone = {u: factory_api.outside_gate(store, u) for u in projects}
    ck("the seeded gates differ: an outside tool for two projects, the crew reviewer for uc_b",
       alone["uc_a"] is not None and alone["uc_a"] == ("fakegate", "FakeBot") and alone["uc_c"] == alone["uc_a"]
       and alone["uc_b"] is None, str(alone))
    cache = {}
    factory_api.warm_gates(store, projects, cache)
    ck("warm_gates: a project sharing the factory's gate has it in the cache, as outside_gate gives it",
       cache.get("uc_a") == alone["uc_a"] and cache.get("uc_c") == alone["uc_c"], str(cache))
    ck("a project with its own choice is left for outside_gate to resolve", "uc_b" not in cache)
    ck("outside_gate with the cache answers the same as without",
       all(factory_api.outside_gate(store, u, cache) == alone[u] for u in projects))
    kept = {"uc_a": ("kept", "Kept")}
    factory_api.warm_gates(store, projects, kept)
    ck("warm_gates leaves what the cache already holds", kept["uc_a"] == ("kept", "Kept"))

    # members in one query, with people who are on one project, on two, and on none
    ana = store.create_person("Ana", email="ana@example.com")["id"]
    ben = store.create_person("Ben", email="ben@example.com")["id"]
    store.add_project_member("uc_a", ana, "t")
    store.add_project_member("uc_a", ben, "t")
    store.add_project_member("uc_b", ben, "t")
    ids = ["uc_a", "uc_b", "uc_c"]
    members = store.members_by_project(ids)
    ck("members_by_project names who is on each project",
       {u: sorted(m["person_id"] for m in members.get(u, [])) for u in ids}
       == {"uc_a": sorted([ana, ben]), "uc_b": [ben], "uc_c": []}, str(members))
    ck("members_by_project agrees with list_project_members, in order",
       all([m["person_id"] for m in members.get(u, [])]
           == [m["person_id"] for m in store.list_project_members(u)] for u in ids))
    ck("members_by_project leaves a project nobody is on out, or empty", not members.get("uc_c"))

    # hand-overs: one with an override, then a clean one on another project, one never handed over
    at1 = t0
    at2 = t0 + timedelta(hours=1)
    store.set_handover("uc_a", [{"path": "/r/a", "name": "o/a"}], "L4-ship", "Ana", at1, person_id=ana,
                       crew="crew", override_reason="owner away")
    store.set_handover("uc_b", [{"path": "/r/b", "name": "o/b"}], None, "Ben", at1, person_id=ben,
                       override_reason="late")
    store.set_handover("uc_b", [{"path": "/r/b", "name": "o/b"}], "L3-review", "Ben", at2, person_id=ben)
    got_h = store.handovers_by_project(ids)
    ha, hb = got_h.get("uc_a") or {}, got_h.get("uc_b") or {}
    ck("handovers_by_project holds the projects handed over and not the one never handed over",
       set(got_h) == {"uc_a", "uc_b"}, str(sorted(got_h)))
    ck("handovers_by_project: uc_a has its level, repo, person, crew and its override",
       ha.get("autonomy") == "L4-ship" and ha.get("repo") == "/r/a" and (ha.get("person") or {}).get("id") == ana
       and ha.get("crew") == "crew" and (ha.get("override") or {}).get("reason") == "owner away"
       and ha.get("overridden") is True and [o["reason"] for o in ha.get("overrides") or []] == ["owner away"],
       str(ha))
    ck("handovers_by_project: uc_b's later clean hand-over keeps the override but is not overridden",
       hb.get("autonomy") == "L3-review" and (hb.get("person") or {}).get("id") == ben
       and (hb.get("override") or {}).get("reason") == "late" and hb.get("overridden") is False
       and [o["reason"] for o in hb.get("overrides") or []] == ["late"], str(hb))
    ck("handovers_by_project of one project gives that project only",
       set(store.handovers_by_project(["uc_a"])) == {"uc_a"}
       and store.handovers_by_project([]) == {} and set(store.handovers_by_project()) == {"uc_a", "uc_b"})
    ck("get_handover gives the same hand-over, and None for a project never handed over",
       store.get_handover("uc_a") == ha and store.get_handover("uc_c") is None)
    ck("list_handover_overrides agrees with the override log",
       [o["reason"] for o in store.list_handover_overrides("uc_a")] == ["owner away"])

    # autonomy_of reads each crew setting once and keeps it per person: one memo, two people
    store.set_setting(factory_api.CREW_SETTING, json.dumps({"autonomy": "L4-ship"}))
    store.set_setting(f"{factory_api.CREW_SETTING}:{ana}", json.dumps({"autonomy": "L4-ship"}))
    store.set_setting(f"{factory_api.CREW_SETTING}:{ben}", json.dumps({"autonomy": "L3-review"}))
    store.set_handover("uc_a", [{"path": "/r/a", "name": "o/a"}], None, "Ana", at2, person_id=ana)
    store.set_handover("uc_b", [{"path": "/r/b", "name": "o/b"}], None, "Ben", at2, person_id=ben)
    from tares import merge_queue
    hos = store.handovers_by_project(ids)
    memo = {}
    levels = {u: merge_queue.autonomy_of(store, u, hos, memo) for u in ("uc_a", "uc_b", "uc_a", "uc_b")}
    ck("autonomy_of keeps each person's own crew setting in one memo",
       levels == {"uc_a": "L4-ship", "uc_b": "L3-review"}, str(levels))
    memo2 = {}
    ck("the other order, in a fresh memo, gives the same levels",
       [merge_queue.autonomy_of(store, u, hos, memo2) for u in ("uc_b", "uc_a")] == ["L3-review", "L4-ship"])
    ck("without a memo or a read of the hand-overs the levels are the same",
       [merge_queue.autonomy_of(store, u) for u in ("uc_a", "uc_b")] == ["L4-ship", "L3-review"])
    payloads_that_are_not_objects(app, store, t0)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()

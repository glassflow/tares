"""A deleted private project's cost leaves the totals (TR-1697).

A person who is not on a private project never sees its cost. Deleting the project must not change
that, for anyone: after the delete every viewer reads what that person read before it. Checked on
the Claude Code usage read, the Report's weekly figures, Home's and the Crew page's shares, the
crew's stations and a station's earlier sessions, for replies stamped with the project (by id and by
name) and a station's own unstamped replies, before the delete, arriving after it and after a
restart; and for the unstamped replies a session that joined it, alone or beside an open project,
had before the delete. A deleted public project is counted as before.

Run: .venv/bin/python tests/test_deleted_private_cost.py
"""
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = "root-token-deleted-private"
HOME = tempfile.mkdtemp(prefix="tares-deleted-private-")
os.environ.update({"TARES_DB": os.path.join(HOME, "import.duckdb"),
                   "TARES_CATALOG": os.path.join(HOME, "catalog.yaml"),
                   "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": ROOT,
                   "TARES_EDITION": "factory"})

import httpx
from sqlalchemy import delete, select

from session_helpers import set_cols
from tares import claude_usage as cu
from tares import daemon, factory_home, report
from tares.session_store.tables import session_usage as su

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SECRET, OPEN, OTHER = "uc_a_secret", "uc_b_open", "uc_c_other"   # the private id sorts lowest
NAMES = ("project-secret", SECRET, "a deleted project")


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def station(sid, name, role, project="", ago=60):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": name, "role": role, "parent": "", "factory_station": name}
    return {**o, "tares_project": project} if project else o


def joined(sid, project, ago=60):
    return {"type": "session_project", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "tares_project": project}


def reply(sid, mid, ago, project=""):
    o = {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": "ok",
                     "usage": {"input_tokens": 2, "output_tokens": 10}}}
    return {**o, "tares_project": project} if project else o


@contextlib.asynccontextmanager
async def cell(db: str):
    daemon.DB_PATH = db
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": f"Bearer {ROOT}"}) as cx:
            yield app.state.store, cx


async def ingest(cx, store, lines, costs):
    """The lines through the claude_code ingest, then each reply's cost set to a round number."""
    r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                      headers={"Content-Type": "application/x-ndjson"})
    assert r.status_code < 300, r.text
    for mid, cost in costs.items():
        set_cols(store, "session_usage", [su.c.message == mid], cost_usd=cost)


async def read(cx, who, path):
    factory_home._cache.clear()   # Home keeps its answer for a few seconds
    r = await cx.get(path, headers=who)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r


async def figures(cx, who) -> tuple[dict, str]:
    """Every total and share a viewer reads, as comparable values, and the text of the answers."""
    usage = await read(cx, who, "/api/claude-code/usage")
    home = await read(cx, who, "/api/home")
    page = await read(cx, who, "/api/factory/crew")
    crew = await read(cx, who, "/api/crew")

    def shares(rows):
        return sorted((x["name"], x["kind"], x["project"] or "", x["sessions"], x["replies"],
                       round(x["cost_usd"], 6), round(x["share"] or 0, 6)) for x in rows)

    stations = {s["name"]: {"project": s["project"], "project_name": s["project_name"],
                            "projects": sorted(s["projects"]), "replies": s["usage"]["replies"],
                            "cost_usd": round(s["usage"]["cost_usd"], 6),
                            "line_cost": (s["context"] or {}).get("cost_usd")}
                for s in crew.json()["stations"]}
    got = {"usage, 7 days": shares(usage.json()["projects"]),
           "usage, 5 hour window": shares(usage.json()["window"]["projects"]),
           "Home's week line": home.json()["plan"]["share_text"],
           "Crew page, by project": page.json()["plan"]["by_project"],
           "Crew page's week line": page.json()["plan"]["share_text"],
           "the stations": stations}
    return got, "\n".join(r.text for r in (usage, home, page, crew))


def weeks(store) -> dict:
    """The Report's stored cost by project, over all weeks."""
    report.rebuild_all(store)
    out: dict = {}
    for row in report.read_weeks(store):
        cell_ = out.setdefault(row["project"], {"cost_usd": 0.0, "roles": {}})
        cell_["cost_usd"] = round(cell_["cost_usd"] + row["cost_usd"], 6)
        for role, v in row["cost_by_role"].items():
            cell_["roles"][role] = round(cell_["roles"].get(role, 0) + v["cost_usd"], 6)
    return {k: v for k, v in out.items() if v["roles"]}


def total(rows) -> float:
    return round(sum(r[5] for r in rows), 6)


async def main():
    db = os.path.join(HOME, "cell.duckdb")
    async with cell(db) as (store, cx):
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        assert r.status_code < 300, r.text
        for uid, name in ((SECRET, "project-secret"), (OPEN, "project-open"), (OTHER, "project-other")):
            store.create_project(uid, "custom", name, {}, goal="A project with a cost")
            store.set_project_kind(uid, "software_factory")
        store.set_project_scope(SECRET, "private")
        mia = store.create_person("Mia", "mia@example.com")["id"]
        bo = store.create_person("Bo", "bo@example.com")["id"]
        store.add_project_member(SECRET, mia)

        def key(kid, person):
            token = f"nvf_{kid}_{kid}-secret-for-the-test"
            store.insert_api_key(kid, kid, f"nvf_{kid}", hashlib.sha256(token.encode()).hexdigest(),
                                 ["read"], person_id=person)
            return {"Authorization": f"Bearer {token}"}

        viewers = {"the cell's token": {}, "a member": key("kmia", mia),
                   "a person not on it": key("kbo", bo)}
        outsider = viewers["a person not on it"]

        lines = [
            # the private project's: stamped, its station's own, a session that worked only in it,
            # and another project's station stamping it (by name, and in an earlier session)
            reply("s-stamp", "m-stamp", 55, SECRET),
            station("s-station-old", "secret-build-m1", "builder", SECRET, 200),
            reply("s-station-old", "m-station-old", 190),
            station("s-station", "secret-build-m1", "builder", SECRET, 50),
            reply("s-station", "m-station", 45),
            joined("s-linked", SECRET, 50), reply("s-linked", "m-linked", 45),
            # a session that worked in it and in the open one counts for the lowest id, the private one
            joined("s-both", SECRET, 50), joined("s-both", OPEN, 49), reply("s-both", "m-both", 45),
            station("s-open-old", "open-build-m1", "builder", OPEN, 180),
            reply("s-open-old", "m-open-old", 170, SECRET),
            reply("s-open-old", "m-open-old-own", 169),
            station("s-open", "open-build-m1", "builder", OPEN, 40),
            reply("s-open", "m-open-stamp", 35, "project-secret"),
            reply("s-open", "m-open-own", 34),
            # everybody's
            joined("s-ctl", SECRET, 50), reply("s-ctl", "m-ctl-linked", 45, OPEN),
            reply("s-station", "m-ctl-station", 44, OPEN),
            reply("s-other", "m-other", 45, OTHER),
            reply("s-own", "m-own", 45),
            station("s-orch", "crew-orchestrator", "orchestrator", "", 50), reply("s-orch", "m-orch", 45),
        ]
        await ingest(cx, store, lines, {
            "m-stamp": 100, "m-station": 200, "m-linked": 400, "m-both": 1024, "m-open-stamp": 800,
            "m-open-old": 1600, "m-station-old": 32, "m-open-own": 3, "m-open-old-own": 2,
            "m-ctl-linked": 5, "m-ctl-station": 7, "m-other": 11, "m-own": 13, "m-orch": 17})
        for sid in ("s-station", "s-open", "s-orch"):
            store.set_session_status(sid, cu.compact_status({"session_id": sid, "cost": {"total_cost_usd": 1.25},
                                                             "context_window": {"context_window_size": 200000}}),
                                     datetime.now(timezone.utc))

        print("== before the delete ==")
        before, _ = await figures(cx, outsider)
        member, _ = await figures(cx, viewers["a member"])
        report_before = weeks(store)
        ck("a person not on it reads 58 of cost", total(before["usage, 7 days"]) == 58, before["usage, 7 days"])
        ck("a member reads 4214", total(member["usage, 7 days"]) == 4214, member["usage, 7 days"])
        ck("the Report has the private project's 4156 and 58 beside it",
           report_before.get(SECRET, {}).get("cost_usd") == 4156
           and round(sum(v["cost_usd"] for k, v in report_before.items() if k != SECRET), 6) == 58,
           report_before)
        ck("the Home line is over the 58", before["Home's week line"] == "This week: project-open 29%, project-other 19%",
           before["Home's week line"])
        ids = {s["name"]: s["crew_id"] for s in (await read(cx, {}, "/api/crew")).json()["stations"]}

        r = await cx.delete(f"/api/projects/{SECRET}")
        assert r.status_code < 300, r.text
        kept = {k: v for k, v in report_before.items() if k != SECRET}

        async def after(stage, cx, store):
            for who, headers in viewers.items():
                got, text = await figures(cx, headers)
                for what, want in before.items():
                    ck(f"{stage}, {who}: {what}", got[what] == want, f"\n      got  {got[what]}\n      want {want}")
                found = [n for n in NAMES if n in text]
                ck(f"{stage}, {who}: the project is named nowhere", not found, found)
                base = f"/api/factory/crew/{ids['open-build-m1'] or '-'}/open-build-m1/sessions"
                r = await cx.get(base, headers=headers)
                ck(f"{stage}, {who}: the open station's earlier sessions do not name it",
                   r.status_code == 200 and not [n for n in NAMES if n in r.text], (r.status_code, r.text))
                r = await cx.get(f"/api/factory/crew/{ids['secret-build-m1'] or '-'}/secret-build-m1/sessions",
                                 headers=headers)
                ck(f"{stage}, {who}: the deleted project's station has no earlier sessions to read",
                   r.status_code == 404, (r.status_code, r.text))
            got = weeks(store)
            ck(f"{stage}: the Report counts what it counted for the others, and nothing else",
               got == kept, f"\n      got  {got}\n      want {kept}")

        print("== after the delete ==")
        await after("deleted", cx, store)

        print("== replies that arrive after the delete ==")
        await ingest(cx, store, [
            reply("s-stamp", "m-late-id", 3, SECRET),
            reply("s-late", "m-late-name", 2, "project-secret"),
            reply("s-station", "m-late-station", 1),
        ], {"m-late-id": 64, "m-late-name": 128, "m-late-station": 256})
        await after("late replies", cx, store)
        # two of the stamped ones lose their usage row: the next start counts them again from the
        # events, where the stamp still names the project (by name, and by id)
        again = su.c.message.in_(["m-open-stamp", "m-open-old"])
        with store.sessions_db.begin() as conn:
            conn.execute(delete(su).where(again))

    async with cell(db) as (store, cx):
        print("== after a restart ==")
        with store.sessions_db.begin() as conn:
            back = sorted(conn.execute(select(su.c.message, su.c.project).where(again)).all())
        ck("the start counts the two again, for the project they were stamped with",
           [tuple(r) for r in back] == [("m-open-old", SECRET), ("m-open-stamp", SECRET)], back)
        await after("restarted", cx, store)

        print("== a deleted public project ==")
        r = await cx.delete(f"/api/projects/{OTHER}")
        assert r.status_code < 300, r.text
        got, _ = await figures(cx, {})
        other = [x for x in got["usage, 7 days"] if x[1] == "other"]
        ck("its replies are counted as the sessions' own, as before",
           total(got["usage, 7 days"]) == 58 and [x[5] for x in other] == [24], got["usage, 7 days"])
        report_now = weeks(store)
        ck("and the Report still counts them, for no project",
           report_now.get("", {}).get("cost_usd") == 41 and OTHER not in report_now, report_now)

        print("== a project made again under the id (a setup that failed and is run again) ==")
        store.create_project(SECRET, "custom", "project-secret", {}, goal="A project with a cost")
        store.set_project_scope(SECRET, "private")
        got, _ = await figures(cx, {})
        mine = [x for x in got["usage, 7 days"] if x[2] == SECRET]
        ck("it is a project like any other: its replies count for it",
           len(mine) == 1 and mine[0][5] >= 100, got["usage, 7 days"])
        report_made = weeks(store)
        ck("and the Report counts them for it",
           report_made.get(SECRET, {}).get("cost_usd", 0) >= 100, report_made)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

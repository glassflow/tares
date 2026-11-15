"""set_stage and a merge seen on GitHub go through the project's tickets integration (TR-848).

A ticket's status follows its stage in the system that holds the ticket, through `set_status` of
the integration the project's tickets use, with no look at who owns the ticket. Covered: the
stage to status table (review is in review, merged and shipped are done); a Tares ticket in a
Tares project changes its status in Tares and calls nothing external; a Linear ticket in a Linear
project reaches Linear's In Review, In Progress, To do and Done states; a Tares ticket in a
Linear project, or a Linear ticket in a Tares project, is left alone (no call, no pending row, the
route answers 200); a write that fails never fails the route, keeps the stage, leaves a
pending_writeback row, and the row keeps a sync from changing the stage or status meanwhile; a
write still waiting on Linear (a rate limit) when STATUS_WAIT is up hands over to the row and
lands later on its own; an integration that raises something unexpected leaves a row only when it
retries; a merge seen on GitHub (a PR into a milestone branch counts) moves a Linear ticket to done
and a Tares one too, once; merged_fields gives only {"stage": "merged"}, never reads the owner and
gives nothing for a ticket already merged or a PR not merged; empty and unknown input.

Run: python tests/test_tickets_routing.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-tickets-routing-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-routing"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import factory as factory_mod
from tares import factory_api
from tares import linear
from tares.envelope import now_utc
from tares.integrations import registry
from tares.integrations import tickets as tickets_base
from tares.integrations.types import Status

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


FIX = os.path.join(HERE, "fixtures", "linear")
with open(os.path.join(FIX, "team_full.json")) as f:
    TEAM = json.load(f)
STATE_NAME = {s["id"]: s["name"] for s in TEAM["states"]["nodes"]}

# ── a fake Linear: it answers the two calls a status move makes and records the moves ──────────
LIN = {"moves": [], "mode": "ok", "delay": 0.0, "asked": 0}


async def fake_query(store, gql, variables=None, conn=None):
    LIN["asked"] += 1
    if LIN["delay"]:
        await asyncio.sleep(LIN["delay"])
    if LIN["mode"] == "down":
        raise linear.LinearError("Linear is down")
    if "mutation" in gql:
        LIN["moves"].append((variables["id"], STATE_NAME[variables["state"]]))
        return {"issueUpdate": {"success": True}}
    return {"issue": {"id": variables["id"], "state": {"id": "s-backlog", "name": "Backlog",
                                                       "type": "backlog"}, "team": TEAM}}


linear.query = fake_query


# ── a fake integration to see what the route asks of the integration it is given ────────────────
class Spy(tickets_base.TicketBackend):
    name = "spy"
    calls: list = []
    mode = "ok"

    def __init__(self, store=None):
        if Spy.mode == "boom_init":
            raise RuntimeError("cannot build")
        self.store = store

    async def set_status(self, ticket, status):
        Spy.calls.append((ticket["id"], Status(status).value))
        if Spy.mode == "boom":
            raise RuntimeError("it broke")

    async def retry_pending(self, project):
        return 0


class Plain(tickets_base.TicketBackend):
    """An integration with nothing to retry, as the Tares one: no retry_pending."""
    name = "plain"

    def __init__(self, store=None):
        self.store = store

    async def set_status(self, ticket, status):
        if Spy.mode == "boom":
            raise RuntimeError("it broke")

_real_get = registry.get
FAKES = {"spy": Spy, "plain": Plain}
registry.get = lambda slot, name: FAKES.get(name) if slot == "tickets" and name in FAKES else _real_get(slot, name)


def pending(store, tid):
    return store.get_pending_writeback(tid)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})

        async def new_project(name, tickets_with=None):
            r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": []})
            uid = r.json()["id"]
            if tickets_with:
                store.set_project_choice(uid, "tickets", tickets_with, {}, by="test")
            return uid

        def ticket(uid, owner, title, pr=None, **kw):
            if owner == "linear":
                kw.setdefault("external_id", "lin-" + title.lower().replace(" ", "-"))
                kw.setdefault("identifier", "LIN-" + str(abs(hash(title)) % 1000))
            tid = store.create_ticket(uid, owner, title, "todo", **kw)
            if pr:
                store.update_ticket(tid, pr=pr)
            return store.get_ticket(uid, tid)

        async def stage(uid, ref, to, **body):
            return await cx.post(f"/api/projects/{uid}/tickets/{ref}/stage", json={"stage": to, **body})

        print("== the table ==")
        ck("every stage asks for a status", set(factory_mod.STATUS_OF_STAGE) == set(factory_mod.STAGES))
        ck("todo is todo", factory_mod.STATUS_OF_STAGE["todo"] == "todo")
        ck("doing, changes and blocked are in progress", {factory_mod.STATUS_OF_STAGE[s] for s in
           ("doing", "changes", "blocked")} == {"in_progress"})
        ck("review is in review", factory_mod.STATUS_OF_STAGE["review"] == "in_review")
        ck("merged and shipped are done", {factory_mod.STATUS_OF_STAGE[s] for s in
           ("merged", "shipped")} == {"done"})
        ck("every value is a Status", all(Status(v) for v in factory_mod.STATUS_OF_STAGE.values()))

        print("== a Tares ticket in a Tares project ==")
        uid = await new_project("plain")
        t = ticket(uid, "tares", "Plain one")
        LIN["asked"] = 0
        r = await stage(uid, t["id"], "doing")
        ck("doing: status in progress", r.status_code == 200 and r.json()["stage"] == "doing"
           and r.json()["status"] == "in_progress", r.text)
        r = await stage(uid, t["id"], "review")
        ck("review: Tares has no in review, so in progress", r.json()["stage"] == "review"
           and r.json()["status"] == "in_progress", r.text)
        r = await stage(uid, t["id"], "merged")
        ck("merged: done", r.json()["stage"] == "merged" and r.json()["status"] == "done", r.text)
        ck("nothing external was called", LIN["asked"] == 0 and not LIN["moves"])
        ck("no pending row", pending(store, t["id"]) is None)

        print("== a Linear ticket in a Linear project ==")
        lp = await new_project("with linear", "linear")
        lt = ticket(lp, "linear", "Linear one")
        LIN["moves"].clear()
        for to, state in (("doing", "In Progress"), ("review", "In Review"), ("changes", "In Progress"),
                          ("merged", "Done"), ("todo", "Todo")):
            r = await stage(lp, lt["id"], to)
            ck(f"{to} -> {state} in Linear, route 200",
               r.status_code == 200 and r.json()["stage"] == to and LIN["moves"][-1] == (lt["external_id"], state),
               f"{r.status_code} {LIN['moves'][-1:]}")
        ck("each stage made one move", len(LIN["moves"]) == 5, LIN["moves"])
        ck("no pending row after the moves landed", pending(store, lt["id"]) is None)

        print("== a ticket the project's integration does not hold ==")
        LIN["moves"].clear()
        mine = ticket(lp, "tares", "Mine in linear project")
        seen = []
        for to in ("doing", "review", "merged", "todo"):
            r = await stage(lp, mine["id"], to)
            seen.append((r.status_code, r.json()["stage"], r.json()["status"]))
        ck("a Tares ticket in a Linear project: stage set, status follows in Tares", seen == [
           (200, "doing", "in_progress"), (200, "review", "in_progress"), (200, "merged", "done"),
           (200, "todo", "todo")], seen)
        ck("it calls nothing in Linear and leaves no row", not LIN["moves"] and pending(store, mine["id"]) is None)
        foreign = ticket(uid, "linear", "Linear in tares project")
        r = await stage(uid, foreign["id"], "review")
        ck("a Linear ticket in a Tares project: 200, stage set, status left as it was",
           r.status_code == 200 and r.json()["stage"] == "review" and r.json()["status"] == "todo", r.text)
        ck("and no row", pending(store, foreign["id"]) is None)

        print("== a cell-wide choice of Linear, a project not linked ==")
        store.set_choice("tickets", "linear", {}, by="test")
        try:
            cw = await new_project("cell wide linear")
            ct = ticket(cw, "tares", "Tares ticket, linear cell wide")
            seen = []
            for to in ("doing", "review", "merged"):
                r = await stage(cw, ct["id"], to)
                seen.append((r.status_code, r.json()["stage"], r.json()["status"]))
            ck("a Tares ticket follows its stage in Tares", seen == [
               (200, "doing", "in_progress"), (200, "review", "in_progress"), (200, "merged", "done")], seen)
        finally:
            store.set_choice("tickets", "tares", {}, by="test")

        print("== a write that fails ==")
        LIN["mode"] = "down"
        r = await stage(lp, lt["id"], "review")
        row = pending(store, lt["id"])
        ck("the route answers 200 with the stage set", r.status_code == 200 and r.json()["stage"] == "review", r.text)
        ck("a pending row holds the status wanted and the error", row is not None
           and row["status"] == "in_review" and row["integration"] == "linear"
           and "Linear is down" in row["last_error"], row)
        ok = store.update_ticket(lt["id"], status="done", stage=None, unless_pending=True)
        again = store.get_ticket(lp, lt["id"])
        ck("a sync write keeps its hands off while the row is there", ok is False
           and again["stage"] == "review" and again["status"] != "done", again)
        LIN["mode"] = "ok"
        r = await stage(lp, lt["id"], "merged")
        ck("the next stage change lands and clears the row", r.status_code == 200
           and pending(store, lt["id"]) is None and LIN["moves"][-1][1] == "Done")

        print("== a write that waits on a rate limit ==")
        factory_api.STATUS_WAIT = 0.2
        LIN["delay"] = 3.0     # far above STATUS_WAIT: a route that waited would take 6 s, a slow runner adds well under 1 (TR-1337)
        LIN["moves"].clear()
        t0 = time.monotonic()
        r = await stage(lp, lt["id"], "review")
        took = time.monotonic() - t0
        row = pending(store, lt["id"])
        ck("the route did not wait for it", r.status_code == 200 and took < 2.0, f"{took:.2f}s")
        ck("a row holds the wanted status meanwhile", row is not None and row["status"] == "in_review", row)
        for _ in range(200):   # a Linear move makes two calls, so the write lands about 6 s after it started; poll, up to 10 s
            if LIN["moves"] and pending(store, lt["id"]) is None:
                break
            await asyncio.sleep(0.05)
        ck("the write landed on its own", LIN["moves"] == [(lt["external_id"], "In Review")], LIN["moves"])
        ck("and cleared the row", pending(store, lt["id"]) is None, pending(store, lt["id"]))
        factory_api.STATUS_WAIT = 10.0
        LIN["delay"] = 0.0

        print("== an integration that raises something unexpected ==")
        sp = await new_project("spy project", "spy")
        st = ticket(sp, "tares", "Spied")
        Spy.calls.clear()
        r = await stage(sp, st["id"], "review")
        ck("the integration was asked, with in_review", Spy.calls == [(st["id"], "in_review")], Spy.calls)
        ck("no row after a clean write", pending(store, st["id"]) is None)
        Spy.mode = "boom"
        r = await stage(sp, st["id"], "merged")
        row = pending(store, st["id"])
        ck("it raised: the route still answers 200, the stage is set", r.status_code == 200
           and r.json()["stage"] == "merged", r.text)
        ck("an integration that retries gets a row", row is not None and row["status"] == "done"
           and row["integration"] == "spy", row)
        Spy.mode = "boom_init"
        r = await stage(sp, st["id"], "doing")
        ck("one that cannot even be built never fails the route", r.status_code == 200
           and r.json()["stage"] == "doing", r.text)
        Spy.mode = "ok"
        pp = await new_project("plain project", "plain")
        pt = ticket(pp, "tares", "No retry")
        Spy.mode = "boom"
        r = await stage(pp, pt["id"], "review")
        ck("an integration with nothing to retry leaves no row", r.status_code == 200
           and pending(store, pt["id"]) is None)
        Spy.mode = "ok"

        print("== write_status on its own, and on bad input ==")
        Spy.calls.clear()
        await factory_api.write_status(store, sp, st, "nonsense")
        await factory_api.write_status(store, sp, {}, "review")
        await factory_api.write_status(store, sp, None, "review")
        ck("an unknown stage or no ticket writes nothing", Spy.calls == [])
        await factory_api.write_status(store, "no-such-project", st, "review")
        ck("an unknown project does not raise", True)
        r = await stage(uid, "T999", "doing")
        ck("an unknown ticket -> 404", r.status_code == 404, r.text)
        r = await stage(uid, t["id"], "")
        ck("an empty stage -> 400", r.status_code == 400, r.text)
        r = await stage(uid, t["id"], "flying")
        ck("an unknown stage -> 400", r.status_code == 400, r.text)

        print("== a merge seen on GitHub ==")
        mp = {"merged": True, "state": "closed", "base": "factory-modular/m2", "repo": "acme/app",
              "number": 7}
        ck("merged_fields: only the stage", factory_api.merged_fields({"stage": "doing"}, mp) == {"stage": "merged"})
        ck("it never reads the owner", all(factory_api.merged_fields({"stage": "review", "owner": o}, mp)
           == {"stage": "merged"} for o in ("tares", "linear", "github", None)))
        ck("a ticket merged or shipped already gives nothing", factory_api.merged_fields({"stage": "merged"}, mp) == {}
           and factory_api.merged_fields({"stage": "shipped"}, mp) == {})
        ck("a PR not merged gives nothing", factory_api.merged_fields({"stage": "doing"}, {**mp, "merged": False}) == {}
           and factory_api.merged_fields({"stage": "doing"}, {}) == {})
        ck("a PR into a milestone branch counts, whatever the base", all(
           factory_api.merged_fields({"stage": "doing"}, {**mp, "base": b}) == {"stage": "merged"}
           for b in ("factory-modular/m2", "main", None)))

        async def fake_read_pr(_store, repo, number, prev=None):
            return {**(prev or {}), "repo": repo, "number": number, "merged": True, "state": "closed",
                    "base": "factory-modular/m2", "checked_at": now_utc().isoformat()}

        real_read, factory_api.factory_github.read_pr = factory_api.factory_github.read_pr, fake_read_pr
        try:
            lp2 = await new_project("linear merge", "linear")
            a = ticket(lp2, "linear", "Merged on github", pr={"repo": "acme/app", "number": 7, "checked_at": None},
                       repo="acme/app")
            b = ticket(lp2, "tares", "A tares ticket there", pr={"repo": "acme/app", "number": 8, "checked_at": None},
                       repo="acme/app")
            LIN["moves"].clear()
            store.update_ticket(a["id"], stage="review")
            store.update_ticket(b["id"], stage="review")
            await factory_api._refresh(store, lp2)
            got = store.get_ticket(lp2, a["id"])
            ck("a Linear ticket seen merged is merged and moved to Done in Linear", got["stage"] == "merged"
               and LIN["moves"] == [(a["external_id"], "Done")], (got["stage"], LIN["moves"]))
            ck("a Tares ticket of that project is merged and calls nothing", store.get_ticket(lp2, b["id"])["stage"] == "merged"
               and len(LIN["moves"]) == 1)
            await factory_api._refresh(store, lp2)
            ck("a second refresh writes nothing more", len(LIN["moves"]) == 1, LIN["moves"])

            tp = await new_project("tares merge")
            c = ticket(tp, "tares", "Tares merged", pr={"repo": "acme/app", "number": 9, "checked_at": None},
                       repo="acme/app")
            store.update_ticket(c["id"], stage="review")
            await factory_api._refresh(store, tp)
            got = store.get_ticket(tp, c["id"])
            ck("a Tares ticket seen merged is done", got["stage"] == "merged" and got["status"] == "done", got)

            LIN["mode"] = "down"
            d = ticket(lp2, "linear", "Merged while linear is down", pr={"repo": "acme/app", "number": 10, "checked_at": None},
                       repo="acme/app")
            store.update_ticket(d["id"], stage="review")
            await factory_api._refresh(store, lp2)
            got, row = store.get_ticket(lp2, d["id"]), pending(store, d["id"])
            ck("Linear down: the ticket is still merged and a row waits for done", got["stage"] == "merged"
               and row is not None and row["status"] == "done", (got["stage"], row))
            LIN["mode"] = "ok"

            e = ticket(lp2, "linear", "Linked by the route")
            LIN["moves"].clear()
            r = await stage(lp2, e["id"], "review", pr="https://github.com/acme/app/pull/11")
            ck("set_stage with a merged PR: merged, and Done in Linear", r.status_code == 200
               and r.json()["stage"] == "merged" and LIN["moves"] == [(e["external_id"], "Done")],
               (r.text[:200], LIN["moves"]))
        finally:
            factory_api.factory_github.read_pr = real_read

    print(f"\n{P} passed, {F} failed")
    return F


sys.exit(1 if asyncio.run(main()) else 0)

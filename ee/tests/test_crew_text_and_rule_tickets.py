# Tares enterprise licence, see ee/LICENSE
"""The crew's messages on a ticket and the tickets a rule offer lists, as each reader gets them:
taken from the sessions and projects the reader can open, before and after a private project is
deleted.

The cell is the one of test_private_station_reads: Kim's crew works on Atlas (a team project) and on
Zephyrvault, which is private to Lee, with Mo a member of it. Kim owns the crew and is not on
Zephyrvault; Ann, a key with no person and a key of Atlas are neither.

    python ee/tests/test_crew_text_and_rule_tickets.py
"""
import asyncio
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_station_reads as reads   # its licensed cell, people and projects
from test_private_station_reads import SECRET

OUTSIDERS = ("ann", "bot", "atlaskey")
READERS = OUTSIDERS + ("kim", "lee", "mo", "root")
HEADING = "## The crew's latest messages about it"

# session -> (its station or None, the station's project, what else ties it to the private project)
SESSIONS = {
    "msg-open": ("atlas-build-talk", "atlas", None),
    "msg-free": (None, None, None),
    "msg-vault": ("vault-build-talk", "vault", None),
    "msg-both": ("atlas-build-both", "atlas", "reply"),
    "msg-linked": (None, None, "link"),
}
# oldest first: (tag, session or None, seconds after T0, sent to, the text before the tag)
MESSAGES = [
    ("from-open", "msg-open", 10, "crew-orchestrator", "{t}"),
    ("free-open", "msg-free", 15, "crew-orchestrator", "Atlas {t}"),
    ("no-session", None, 18, "crew-orchestrator", "Atlas {t}"),
    ("by-name", "msg-vault", 30, "crew-orchestrator", "Atlas {t} " + SECRET),
    ("to-builder", "msg-vault", 40, "atlas-build-talk", "{t} " + SECRET),
    ("from-builder", "msg-both", 50, "crew-orchestrator", "{t} " + SECRET),
    ("no-station", "msg-linked", 60, "crew-orchestrator", "Atlas {t} " + SECRET),
]
ORDER = [m[0] for m in MESSAGES]
OPEN = {"from-open", "free-open", "no-session"}
CREW = {"by-name", "to-builder", "from-builder"}     # sent by sessions of Kim's crew
ALL = set(ORDER)

WHOLE = ["Atlas T1", "Zephyrvault T1", "Zephyrvault T2"]
PART = ["Atlas T1"]

TALLY = {"messages": [0, 0], "rules": [0, 0]}


def ck(part, label, cond, detail=""):
    TALLY[part][0 if cond else 1] += 1
    print(("ok   " if cond else "FAIL ") + f"[{part}] {label}"
          + ("" if cond else f"  {str(detail)[:300]}"))


def body_of(tag, label):
    text = next(m[4] for m in MESSAGES if m[0] == tag)
    return f"[TF:UPDATE] {text.format(t=label)} {tag}"


def lines(sid, atlas, vault, label):
    station, project, tie = SESSIONS[sid]

    def head(s):
        return {"sessionId": sid, "cwd": "/w/crew",
                "timestamp": reads.iso(reads.T0 + timedelta(seconds=s))}

    out = []
    if station:
        out.append({**head(0), "type": "session_station", "station": station, "role": "builder",
                    "parent": "crew-orchestrator", "factory_station": station,
                    "factory_crew": "crew", "crew": "crew", "factory_machine": "kim-mbp",
                    "machine": "kim-mbp",
                    "tares_project": vault if project == "vault" else atlas})
    out.append({**head(1), "type": "user", "message": {"role": "user", "content": "go"}})
    for tag, by, s, to, _ in MESSAGES:
        if by != sid:
            continue
        out.append({**head(s), "type": "assistant", "message": {
            "id": f"m-{tag}", "role": "assistant", "content": [
                {"type": "tool_use", "id": f"t-{tag}", "name": "SendMessage",
                 "input": {"to": to, "message": body_of(tag, label)}}]}})
    if tie == "reply":
        # one reply with token counts that carries the vault: the session worked there too
        out.append({**head(70), "type": "assistant", "tares_project": vault, "message": {
            "id": f"v-{sid}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": f"{SECRET} for the vault"}],
            "usage": {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 10,
                      "cache_read_input_tokens": 100}}})
    return out


def without_session(store, label):
    """A message recorded with no session, written as session_ingest writes its rows."""
    from sqlalchemy import insert
    from tares.session_store.tables import session_messages
    with store.sessions_db.begin() as conn:
        conn.execute(insert(session_messages).values(
            tool_use_id="t-no-session", source="claude_code", session=None,
            at=reads.T0 + timedelta(seconds=18), to_station="crew-orchestrator", type="UPDATE",
            text=body_of("no-session", label)))


async def check_messages(app, world, when, wanted, base, label, empty):
    for who in READERS:
        want = wanted(who)
        at = f"{who}, {when}"
        r = await reads.get(app, world, who, f"{base}/{label}/messages")
        rows = r.json().get("messages") if r.status_code == 200 else None
        got = [m["text"].split()[-1] for m in rows or []]
        ck("messages", f"{at}: the ticket's messages are those of the sessions they can open",
           got == [t for t in ORDER if t in want], (r.status_code, got))
        r = await reads.get(app, world, who, f"{base}/{label}/builder-brief")
        if r.status_code != 200:
            ck("messages", f"{at}: the builder brief refuses this key and gives no message",
               who == "atlaskey" and r.status_code == 403 and "[TF:" not in r.text,
               (r.status_code, r.text))
        else:
            section = r.json()["brief"].partition(HEADING)[2].partition("\n## ")[0]
            got = [t for t in ORDER if f" {t}" in section]
            ck("messages", f"{at}: the builder brief gives the latest five of those",
               got == [t for t in ORDER if t in want][-5:], (r.status_code, got))
        r = await reads.get(app, world, who, f"{base}/{empty}/messages")
        ck("messages", f"{at}: a ticket nobody wrote about has none",
           r.status_code == 200 and r.json() == {"messages": []}, (r.status_code, r.text))


def offer(store, kind, project=None):
    from tares.envelope import now_utc
    return store.upsert_rule_proposal(f"example-org/{kind}", kind, project,
                                      "project" if project else "all",
                                      f"Check for {kind}.", WHOLE, now_utc())["id"]


def home_offers(store, pids):
    """{offer id: the project its item carries} for those of `pids` that are among the rule items
    every Home list is built from."""
    from tares import factory_waiting
    from tares.envelope import now_utc
    items = {i["id"]: i for i in factory_waiting.rules_to_decide(store, None, now_utc())}
    return {p: (items[f"rule:{p}"].get("project") or {}).get("id")
            for p in pids if f"rule:{p}" in items}


async def check_offers(app, world, when, wanted, ids):
    """The offers `ids` in the list of every state, the open ones also in the list of open offers
    and on the Crew page."""
    for who in READERS:
        want = wanted(who)
        at = f"{who}, {when}"
        for name, path, params, key in (
                ("the open offers", "/api/rule-proposals", {}, "proposals"),
                ("the offers of every state", "/api/rule-proposals", {"state": ""}, "proposals"),
                ("the Crew page", "/api/factory/crew", {}, "rule_proposals")):
            r = await reads.get(app, world, who, path, **params)
            if r.status_code != 200:
                ck("rules", f"{at}: {name} refuses this key and names no ticket",
                   who == "atlaskey" and r.status_code in (401, 403, 404)
                   and "Zephyrvault" not in r.text, (r.status_code, r.text))
                continue
            got = {p["id"]: p["tickets"] for p in r.json()[key]}
            shown = [i for i, state in ids.items()
                     if state == "open" or name == "the offers of every state"]
            ck("rules", f"{at}: {name} lists the tickets they can read",
               bool(shown) and all(got.get(i) == want for i in shown),
               {i: got.get(i) for i in shown})


async def decide(app, world, who, pid, decision, want, when):
    async with reads.client(app, world["who"][who]) as cx:
        r = await cx.post(f"/api/rule-proposals/{pid}/{decision}", json={})
    got = r.json().get("tickets") if r.status_code == 200 else None
    ck("rules", f"{who}, {when}: the answer to {decision} lists the tickets they can read",
       got == want, (r.status_code, r.text))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        world = await reads.seed(app)
        who, atlas, vault = world["who"], world["atlas"], world["vault"]
        base = f"/api/projects/{atlas}/tickets"
        labels = []
        async with reads.client(app, who["root"]) as cx:
            for title in ("Talked about", "Quiet"):
                r = await cx.post(base, json={"title": title})
                assert r.status_code == 201, r.text
                labels.append(r.json()["label"])
        label, empty = labels
        every = [x for sid in SESSIONS for x in lines(sid, atlas, vault, label)]
        async with reads.client(app, {**who["kim"], "Content-Type": "application/x-ndjson"}) as cx:
            r = await cx.post("/ingest/claude_code", content=reads.nd(every))
            assert r.status_code < 300, r.text
        await asyncio.sleep(0.5)
        store.link_session(vault, "msg-linked", None)
        without_session(store, label)

        ids = {offer(store, "missing-tests"): "open"}
        for name, decision, want in (("ann", "reject", PART), ("ann", "accept", PART),
                                     ("mo", "reject", WHOLE)):
            pid = offer(store, f"{name}-{decision}")
            await decide(app, world, name, pid, decision, want, "before the delete")
            ids[pid] = decision
        late = offer(store, "decided-later")
        ids[late] = "open"
        own, shared = offer(store, "own-project", vault), next(iter(ids))
        async with reads.client(app, who["root"]) as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": "Orchard",
                                                     "objects": [], "kind": "software_factory"})
            assert r.status_code in (200, 201), r.text
            team = r.json()["id"]
            kept = offer(store, "team-project", team)
            r = await cx.delete(f"/api/projects/{team}")
            assert r.status_code < 300 and store.get_project(team) is None, r.text
        home = home_offers(store, (own, shared, kept))
        ck("rules", "before the delete: Home's items carry a project's offer with the project",
           home == {own: vault, shared: None, kept: None}, str(home))

        def before(name):
            if name in OUTSIDERS:
                return OPEN
            return OPEN | CREW if name == "kim" else ALL

        await check_messages(app, world, "before the delete", before, base, label, empty)
        await check_offers(app, world, "before the delete",
                           lambda n: PART if n in OUTSIDERS + ("kim",) else WHOLE, ids)

        async with reads.client(app, who["lee"]) as cx:
            r = await cx.delete(f"/api/projects/{vault}")
        assert r.status_code < 300 and store.get_project(vault) is None, r.text

        # deleted, the project is read by nobody; Kim still reads the sessions of their own crew
        await check_messages(app, world, "after the delete",
                             lambda n: OPEN | CREW if n == "kim" else OPEN, base, label, empty)
        await check_offers(app, world, "after the delete", lambda n: PART, ids)
        for name in READERS:
            r = await reads.get(app, world, name, "/api/rule-proposals", state="")
            ck("rules", f"{name}, after the delete: the offers name only projects they can read",
               "Zephyrvault" not in r.text, r.text)
        after = home_offers(store, (own, shared, kept))
        ck("rules", "after the delete: Home's items leave out the deleted project's own offer "
           "and keep the others", after == {shared: None, kept: None}, str(after))
        await decide(app, world, "root", late, "reject", PART, "after the delete")
        ids[late] = "reject"

        async with reads.client(app, who["kim"]) as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": "Zephyrvault",
                                                     "objects": [], "kind": "software_factory"})
        assert r.status_code in (200, 201), r.text
        await check_offers(app, world, "a team project has the name", lambda n: WHOLE, ids)
        r = await reads.get(app, world, "ann", f"{base}/{label}/messages")
        ck("messages", "with a new project of the name, ann gets the messages of open sessions",
           r.status_code == 200 and SECRET not in r.text, (r.status_code, json.dumps(r.text)))

    for part, (passed, failed) in TALLY.items():
        print(f"[{part}] {passed} passed, {failed} failed")
    passed, failed = (sum(t[i] for t in TALLY.values()) for i in (0, 1))
    print(f"{passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)

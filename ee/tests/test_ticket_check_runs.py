"""A ticket's checks as each reader gets them: the state of every check, and the recorded text
taken from the sessions the reader may open. Read for a ticket a station holds and for one nobody
holds.

    python ee/tests/test_ticket_check_runs.py
"""
import asyncio
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_private_station_reads as reads   # its licensed cell, people and projects
from test_private_station_reads import SECRET

STATION = "atlas-build-checks"
OPEN, CLOSED = "chk-open", "chk-vault"       # CLOSED also works for the private project
GOOD = "3 passed"
# session -> (seconds after T0, command, what it printed, failed)
RUNS = {
    OPEN: [(10, "make check-open", "open run: 1 failed", True),
           (20, "make check-both", "open older run: 2 failed", True)],
    CLOSED: [(30, "make check-closed", f"{SECRET} run: 1 failed", True),
             (40, "make check-both", f"{SECRET} newer run: 1 failed", True),
             (50, "make check-good", f"{SECRET} run: {GOOD}", False)],
}
STATES = {"make check-open": "differs", "make check-closed": "differs",
          "make check-both": "differs", "make check-good": "matched"}
# command -> the recorded text of a reader who may open both sessions, and of one who may not
ALL = {"make check-open": "open run", "make check-closed": f"{SECRET} run",
       "make check-both": f"{SECRET} newer run", "make check-good": None}
SOME = {"make check-open": "open run", "make check-closed": None,
        "make check-both": None, "make check-good": None}
TALLY = [0, 0]


def ck(label, cond, detail=""):
    TALLY[0 if cond else 1] += 1
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else f"  {detail}"))


def lines(sid, atlas, vault):
    def head(s):
        return {"sessionId": sid, "cwd": "/w/crew",
                "timestamp": reads.iso(reads.T0 + timedelta(seconds=s))}

    out = [{**head(0), "type": "session_station", "station": STATION, "role": "builder",
            "parent": "crew-orchestrator", "factory_station": STATION, "factory_crew": "crew",
            "crew": "crew", "factory_machine": "kim-mbp", "machine": "kim-mbp",
            "tares_project": atlas}]
    for s, command, printed, failed in RUNS[sid]:
        call = f"{sid}-{s}"
        out.append({**head(s), "type": "assistant", "message": {
            "id": f"m-{call}", "role": "assistant", "content": [
                {"type": "tool_use", "id": call, "name": "Bash", "input": {"command": command}}]}})
        out.append({**head(s + 1), "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call, "content": printed, "is_error": failed}]}})
    if sid == CLOSED:
        # one reply with token counts that carries the vault: the session worked there too
        out.append({**head(60), "type": "assistant", "tares_project": vault, "message": {
            "id": f"v-{sid}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": f"{SECRET} for the vault"}],
            "usage": {"input_tokens": 2, "output_tokens": 3, "cache_creation_input_tokens": 10,
                      "cache_read_input_tokens": 100}}})
    return out


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        world = await reads.seed(app)
        who, atlas, vault = world["who"], world["atlas"], world["vault"]
        every = [x for sid in RUNS for x in lines(sid, atlas, vault)]
        async with reads.client(app, {**who["kim"], "Content-Type": "application/x-ndjson"}) as cx:
            r = await cx.post("/ingest/claude_code", content=reads.nd(every))
            assert r.status_code < 300, r.text
        await asyncio.sleep(0.5)
        for sid in RUNS:   # a ticket nobody holds is read against the project's linked sessions
            app.state.store.link_session(atlas, sid, None)

        tickets = {}
        base = f"/api/projects/{atlas}/tickets"
        checks = [{"command": c, "result": GOOD} for c in STATES]
        async with reads.client(app, who["root"]) as cx:
            for kind in ("held", "unheld"):
                r = await cx.post(base, json={"title": f"A {kind} ticket"})
                assert r.status_code == 201, r.text
                ref = tickets[kind] = r.json()["label"]
                if kind == "held":
                    r = await cx.post(f"{base}/{ref}/assign", json={"holder": STATION})
                    assert r.status_code == 200, r.text
                r = await cx.post(f"{base}/{ref}/checks", json={"checks": checks})
                assert r.status_code == 201, r.text

        readers = [(name, ALL) for name in ("kim", "lee", "mo", "root")]
        readers += [(name, SOME) for name in ("ann", "bot", "atlaskey")]
        for name, want in readers:
            for kind, ref in tickets.items():
                async with reads.client(app, who[name]) as cx:
                    r = await cx.get(f"{base}/{ref}")
                body = r.json() if r.status_code == 200 else {}
                got = {c["command"]: c for c in body.get("checks") or []}
                at = f"{name}, {kind} ticket"
                ck(f"{at}: every check has its state",
                   {k: c["match"] for k, c in got.items()} == STATES,
                   (r.status_code, {k: c["match"] for k, c in got.items()}))
                for command, text in want.items():
                    said = (got.get(command) or {}).get("recorded", "no such check")
                    ck(f"{at}: {command}: the recorded text",
                       said is None if text is None else text in str(said), said)
                if want is SOME:
                    whole = json.dumps(body.get("checks"))
                    ck(f"{at}: the checks hold text of readable sessions only", SECRET not in whole,
                       whole[:200])

    print(f"{TALLY[0]} passed, {TALLY[1]} failed")
    return TALLY[1]


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)

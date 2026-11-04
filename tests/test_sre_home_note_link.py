"""The SRE home links a raised check to its incident, not to the check (TR-894).

"Read the note" on the home opens the incident page, which takes an incident id. The home used to
send the id of the check (the agent run), so the page said no incident had that id. Each raised row
now carries `incident_id`: the incident the check's note was filed under, or null when it was
filed under none (then the row has nothing to open).

Run: .venv/bin/python tests/test_sre_home_note_link.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _env import scrub_os_environ
scrub_os_environ()
sys.path.insert(0, os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-sre-note-link-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-sre-note-link"
os.environ["TARES_EDITION"] = "sre"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"
os.environ["TARES_WATCH_SECONDS"] = "3600"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import PROJECT, build

UID = "prj-" + PROJECT
from tares import incidents, sre_home

P = F = 0
UTC = timezone.utc


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    hdr = {"Authorization": f"Bearer {TOKEN}"}

    async def get(path):
        sre_home.clear_cache()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.get(path)

    async with app.router.lifespan_context(app):
        base = datetime.now(UTC).replace(microsecond=0)
        incidents.clock = lambda: base
        build(store, base, watcher=True, investigator=True)
        app.state.runtime.reload_catalog()

        def check(rid, service, minutes):
            at = base - timedelta(minutes=minutes)
            store.start_agent_run(rid, "watcher", "watch-prod", "", service, "h", project=UID)
            store.finish_agent_run(rid, "ok", finding=f"{service} returns errors.", outcome="finding",
                                   verdict="investigate")
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])
            return at

        at = check("chk-filed", "edge-proxy", 20)
        inc = store.create_incident("edge-proxy", UID, at)
        store.add_incident_note(inc["id"], "chk-filed", at)
        check("chk-loose", "docs-site", 10)

        r = await get("/api/home")
        ck("the home answers", r.status_code == 200, r.text[:120])
        rows = {x["id"]: x for x in r.json()["raised"]}
        ck("both checks are listed", set(rows) == {"chk-filed", "chk-loose"}, str(list(rows)))
        row = rows.get("chk-filed", {})
        ck("a check filed under an incident carries that incident's id",
           row.get("incident_id") == inc["id"], str(row))
        ck("which is not the check's own id", row.get("incident_id") != row.get("id"), str(row))
        ck("a check filed under none has no incident id",
           "incident_id" in rows.get("chk-loose", {}) and rows["chk-loose"]["incident_id"] is None,
           str(rows.get("chk-loose")))

        r = await get(f"/api/sre/incidents/{row.get('incident_id')}")
        ck("the incident page opens with the id the row carries", r.status_code == 200, r.text[:120])
        r = await get("/api/sre/incidents/chk-filed")
        ck("and says no incident has the check's id (the link the row used to make)",
           r.status_code == 404, f"{r.status_code} {r.text[:120]}")

    print(f"\n{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)

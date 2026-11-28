"""A crew member stuck on a prompt in its terminal waits for you (TR-1461).

The plugin reports `waiting` when Claude Code puts up a permission prompt, a question or a
dialog, and `idle` when a turn ends (claude-plugin/scripts/ship.py, state_for). Here the plugin's
own mapping makes the session_state lines, the claude_code ingest takes them in, and GET /api/home
on a cell of one shows:

  - a waiting crew member as one "terminal" item: "Answer <name> in its terminal", the reason as
    its detail, a link to the member on the Crew page and nothing to say yes or no with;
  - nothing for one that is idle, working, or silent long enough to read as quiet;
  - the item gone once the member works again, and a new prompt as a new item.

Run: .venv/bin/python tests/test_terminal_waiting.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-terminal-waiting-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-terminal"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import ship
from tares import factory_home as fh
from tares import words

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, project="", ago=30):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": "", "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def hook(sid, event, ago, **fields):
    """The session_state line the plugin sends for a hook event, from its own mapping."""
    state, reason = ship.state_for(event, {"hook_event_name": event, **fields})
    assert state, (event, fields)
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "state": state, "reason": reason}


async def main():
    from tares.daemon import make_app
    app = make_app()
    cx = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})

    async def push(*lines):
        async with cx() as c:
            r = await c.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                             headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code < 300, r.text

    async def stuck() -> dict:
        fh.clear_cache()
        async with cx() as c:
            r = await c.get("/api/home")
        assert r.status_code == 200, r.text
        return {i["holds_up"]: i for i in r.json()["waiting_for_you"] if i["kind"] == "terminal"}

    async with app.router.lifespan_context(app):
        shop = "uc_shop"
        app.state.store.create_project(shop, "Shop", goal="Sell things")
        app.state.store.set_project_kind(shop, "software_factory")

        print("== at work: nothing waits ==")
        await push(st_line("x1", "crew-releaser", "releaser", ago=20), hook("x1", "PostToolUse", 5),
                   st_line("b1", "shop-build-m1", "builder", "Shop", ago=20), hook("b1", "PostToolUse", 5))
        ck("no terminal item while both work", await stuck() == {})

        print("== a permission prompt in the releaser's terminal ==")
        await push(hook("x1", "PermissionRequest", 4, tool_name="Bash"))
        items = await stuck()
        ck("one item, for the releaser", sorted(items) == ["crew-releaser"], sorted(items))
        x = items.get("crew-releaser", {})
        ck("its ask names the member and where to answer", x.get("ask") == "Answer crew-releaser in its terminal", x)
        ck("the plugin's reason is the detail", x.get("detail") == "Claude Code needs your permission to use Bash.", x)
        ck("on no project: Every project", x.get("project") is None and x.get("title") == "Every project", x)
        acts = x.get("actions") or []
        ck("one action, a link to the member on the Crew page",
           len(acts) == 1 and acts[0].get("href", "").startswith("/crew?member=crew-releaser&crew=")
           and acts[0]["primary"], acts)
        ck("never yes or no: nothing to do on the page", not any("do" in a for a in acts), acts)
        ck("it holds the member up", x.get("blocking") is True, x)
        ck("the person of the cell has it", x.get("for_people") == ["local"], x.get("for_people"))
        first_id = x.get("id")
        ck("the same item on the next read", (await stuck()).get("crew-releaser", {}).get("id") == first_id)

        print("== a turn that ended is not a prompt ==")
        await push(hook("b1", "Stop", 4))
        ck("an idle builder makes no item", sorted(await stuck()) == ["crew-releaser"])

        print("== a dialog in the builder's terminal, on its project ==")
        await push(hook("b1", "Notification", 3, notification_type="elicitation_dialog",
                        message="Claude Code needs your input"))
        b = (await stuck()).get("shop-build-m1", {})
        ck("the builder's item carries its project", b.get("project") == {"id": shop, "name": "Shop"}
           and b.get("title") == "Shop", b)
        ck("its reason is the notice", b.get("detail") == "Claude Code needs your input.", b)

        print("== back at work: the item goes ==")
        await push(hook("x1", "PostToolUse", 2))
        items = await stuck()
        ck("the releaser's item is gone, the builder's stays", sorted(items) == ["shop-build-m1"], sorted(items))

        print("== a new prompt is a new item ==")
        await push(hook("x1", "PermissionRequest", 1, tool_name="Write"))
        x2 = (await stuck()).get("crew-releaser", {})
        ck("the releaser waits again, under a new id", x2.get("id") and x2["id"] != first_id, (first_id, x2.get("id")))
        ck("with the new reason", x2.get("detail") == "Claude Code needs your permission to use Write.", x2)

        print("== silent past the window: quiet, not waiting ==")
        await push(st_line("r1", "crew-reviewer", "reviewer", ago=200),
                   hook("r1", "PermissionRequest", 150, tool_name="Bash"))
        ck("a prompt nobody heard from in 150 minutes makes no item", "crew-reviewer" not in await stuck())

        print("== a build with no request (the Slack reader) passes the person's hidden projects ==")
        from tares import factory_owners as owners_mod
        from tares import factory_waiting as fw
        from tares.store import now_utc
        row = {"name": "vault-build-m1", "crew_id": "c1", "state": "waiting", "state_at": now_utc(),
               "state_reason": "Waiting for the Vault launch codes", "project": "uc_vault",
               "project_name": "Vault", "projects": ["uc_vault"], "person": {"id": "local"}}
        own = owners_mod.Owners(app.state.store)
        seen = fw.station_items([row], own, now_utc())
        ck("seen: the reason and the project", seen[0]["detail"] == "Waiting for the Vault launch codes."
           and seen[0]["project"]["name"] == "Vault", seen[0])
        hid = fw.station_items([row], own, now_utc(), hidden={"uc_vault"})
        ck("hidden: neither the reason nor the project",
           "Vault" not in json.dumps(hid[0]) and hid[0]["project"] is None, hid[0])
        row2 = {**row, "project": "uc_shop2", "project_name": "Shop2", "projects": ["uc_shop2", "uc_vault"]}
        hid = fw.station_items([row2], own, now_utc(), hidden={"uc_vault"})
        ck("a member that also worked on a hidden project says nothing of its reason",
           "Vault" not in json.dumps(hid[0]), hid[0])

        print("== the words ==")
        items = list((await stuck()).values())
        text = [s for i in items for s in (i["id"], i["kind"], i["label"], i["ask"], i["detail"], i["meta"],
                                           i["title"], *(a["label"] for a in i["actions"]))]
        bad = [(s, words.offenders(s)) for s in text if words.offenders(s)]
        ck("every string passes the words check", not bad, bad)
        ck("no em dash", not [s for s in text if "—" in s])
        ck("the kind is one Home knows", all(i["kind"] in fh.KINDS for i in items))

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())

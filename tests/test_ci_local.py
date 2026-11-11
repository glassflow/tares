"""Local checks as the ci integration (TR-854).

One in-process cell. A builder station ran its project's commands in a recorded session and
recorded each as a check on its ticket. Covered:
 - `LocalCI.state` answers `none` with no required commands (null, empty, blank and non-list input
   included), takes the commands from the project when the caller passes none, and prefers the
   ones the caller passes;
 - a command not recorded on the head is pending (and says so); recorded only on an older commit
   is pending with "run them again"; a check with no commit is not on any head;
 - every command recorded on the head and matching the session is success; a short head counts;
   a chain (`cd app && pytest -q`) stands for its last step;
 - a check that says it failed, a result the session recording contradicts and a command the
   session never ran are failure, with the command in `failing`; one failure wins over a pending
   command; the latest check on the head decides;
 - nothing to compare with (a project with no session) is pending, never success; no ticket or no
   head is pending; a missing store raises;
 - the store: required commands are cleaned (blank, repeats, spacing), replaced, cleared, and a
   value that is not a list of strings is refused;
 - the route PUT/GET /api/projects/{uid}/required-checks: an admin and the lead write, another
   person, a read key and a station do not; a bad body is 422; the set_required_checks tool;
 - local is a built-in `ci` integration and needs no settings.

Run: .venv/bin/python tests/test_ci_local.py
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

_TMP = tempfile.mkdtemp(prefix="tares-ci-local-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-ci-local"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares.integrations import registry
from tares.integrations.builtin.ci_local import LocalCI, OLDER_COMMIT
from tares.integrations.types import PRRef

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


HEAD = "aaaaaaa1111111111111111111111111111111aa"
OLD = "bbbbbbb2222222222222222222222222222222bb"
PYTEST, RUFF, LINT = "pytest -q", "ruff check .", "npm run lint"


def run_lines(session, calls):
    """The session lines of a builder that ran each (id, command, output, is_error)."""
    out = []
    for i, (cmd, output, err) in enumerate(calls):
        uid = f"{session}-{i}"
        out.append({"type": "assistant", "sessionId": session, "cwd": "/w/shop", "timestamp": ago(0),
                    "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": uid, "name": "Bash", "input": {"command": cmd}}]}})
        item = {"type": "tool_result", "tool_use_id": uid, "content": output}
        if err:
            item["is_error"] = True
        out.append({"type": "user", "sessionId": session, "cwd": "/w/shop", "timestamp": ago(0),
                    "message": {"role": "user", "content": [item]}})
    return out


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    headers = dict(root)

    def mk(timeout=10):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers=dict(headers))

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    m.TARESD = mf.TARESD = "http://t"
    m._cx = mf._cx = mk

    async def ingest(lines):
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        empty = json.loads(await m.create_project("Empty", "No sessions"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        await mf.write_milestone("Store", "", [], project="Shop")
        await mf.write_milestone("Store", "", [], project="Empty")
        for t in ("Catalog", "Cart", "Checkout"):
            await m.write_ticket(t, milestone="Store", project="Shop")
        await m.write_ticket("Lonely", milestone="Store", project="Empty")
        as_station("crew-orchestrator", "orchestrator")
        await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "doing", project="Shop")
        await ingest([{"type": "session_station", "sessionId": "b1", "cwd": "/w/shop",
                       "timestamp": ago(30), "station": "shop-build-m1", "role": "builder",
                       "parent": "crew-orchestrator", "tares_project": "Shop"}]
                     + run_lines("b1", [(PYTEST, "14 passed in 1.2s", False),
                                        ("cd app && " + RUFF, "All checks passed!", False),
                                        (LINT, "3 errors", True)]))

        ci = LocalCI(store)

        def pr(ticket="T1", head=HEAD, project=shop):
            return PRRef("acme/shop", 7, head, "main", "https://github.com/acme/shop/pull/7",
                         ticket, project)

        async def record(*checks, ticket="T1", project="Shop"):
            for c in checks:
                await mf.add_check(ticket, c[0], c[1], c[2], project=project)
                await asyncio.sleep(0.02)

        print("== no required commands ==")
        for label, req in (("null", None), ("empty", []), ("blank strings", ["", "   "]),
                           ("a string, not a list", "pytest -q"), ("numbers", [1, 2])):
            s = await ci.state(pr(), req)
            ck(f"required {label} and none on the project is none",
               s.state == "none" and s.head == HEAD and s.summary, s)

        print("== nothing recorded yet ==")
        s = await ci.state(pr(), [PYTEST, RUFF])
        ck("pending, naming what is missing",
           s.state == "pending" and PYTEST in s.summary and RUFF in s.summary and not s.failing, s)

        print("== recorded on the head ==")
        await record((PYTEST, "14 passed", HEAD))
        s = await ci.state(pr(), [PYTEST, RUFF])
        ck("one of two is still pending", s.state == "pending" and RUFF in s.summary
           and PYTEST not in s.summary, s)
        await record((RUFF, "All checks passed!", HEAD[:9]))
        s = await ci.state(pr(), [PYTEST, RUFF])
        ck("both on the head (one by a short hash) is success",
           s.state == "success" and s.head == HEAD and s.failing == [], s)
        ck("the commands match when the caller spaces them oddly",
           (await ci.state(pr(), ["  pytest   -q ", "ruff check ."])).state == "success")
        ck("a chain in the recording stands for its last step: ruff ran as `cd app && ruff check .`",
           (await ci.state(pr(), [RUFF])).state == "success")
        ck("a repeated required command counts once",
           (await ci.state(pr(), [PYTEST, PYTEST])).state == "success")

        print("== the project's own list ==")
        ck("no list yet", store.get_required_checks(shop) == [])
        ck("set returns the cleaned list",
           store.set_required_checks(shop, ["  pytest   -q", "", PYTEST, RUFF], "ann") == [PYTEST, RUFF])
        s = await ci.state(pr(), None)
        ck("with none passed, the project's list decides", s.state == "success", s)
        s = await ci.state(pr(), [LINT])
        ck("what the caller passes wins over the project's list: lint was never recorded",
           s.state == "pending" and LINT in s.summary, s)
        ck("an empty list from the caller falls back to the project's",
           (await ci.state(pr(), [])).state == "success")

        print("== an older commit ==")
        s = await ci.state(pr(head=OLD), [PYTEST, RUFF])
        ck("checks only on another commit are pending, run them again",
           s.state == "pending" and OLDER_COMMIT in s.summary and s.head == OLD, s)
        await record((LINT, "clean", ""), ("make e2e", "all green", None))
        s = await ci.state(pr(), [LINT])
        ck("a check with no commit is on no head", s.state == "pending" and LINT in s.summary, s)

        print("== failures ==")
        await record((PYTEST, "2 failed, 12 passed", HEAD))
        s = await ci.state(pr(), [PYTEST, RUFF])
        ck("a check that says it failed is failure, naming the command",
           s.state == "failure" and s.failing == [PYTEST], s)
        await record((PYTEST, "14 passed", HEAD))
        ck("the latest on the head decides: a pass after the failure is success",
           (await ci.state(pr(), [PYTEST, RUFF])).state == "success")
        await record((PYTEST, "20 passed", HEAD))
        s = await ci.state(pr(), [PYTEST, RUFF])
        ck("a count the session recording contradicts is failure",
           s.state == "failure" and s.failing == [PYTEST], s)
        await record((PYTEST, "14 passed", HEAD))
        await record(("make e2e", "all green", HEAD))
        s = await ci.state(pr(), ["make e2e"])
        ck("a command the session never ran is failure", s.state == "failure" and s.failing == ["make e2e"], s)
        await record((LINT, "clean", HEAD))
        s = await ci.state(pr(), [LINT])
        ck("a misreported result (the recording shows 3 errors) is failure",
           s.state == "failure" and s.failing == [LINT], s)
        await record((LINT, "3 errors", HEAD))
        s = await ci.state(pr(), [LINT])
        ck("an honest failed result, which the recording backs, is still failure",
           s.state == "failure" and s.failing == [LINT], s)
        await record((LINT, "clean", HEAD))
        s = await ci.state(pr(), [LINT, PYTEST, "never recorded"])
        ck("a failure wins over a command still pending",
           s.state == "failure" and s.failing == [LINT], s)
        s = await ci.state(pr(), [PYTEST, "never recorded"])
        ck("pending when nothing failed but one is missing", s.state == "pending", s)

        print("== nothing to compare with ==")
        await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7", project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        await mf.assign_ticket("T1", "shop-build-m9", project="Empty")
        as_station("shop-build-m9", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "doing", project="Empty")
        await mf.add_check("T1", PYTEST, "14 passed", HEAD, project="Empty")
        s = await ci.state(pr("T1", project=empty), [PYTEST])
        ck("a ticket with no session recording is pending, never success",
           s.state == "pending" and "recording" in s.summary, s)
        s = await ci.state(pr("T99", project=shop), [PYTEST])
        ck("a ticket that does not exist is pending", s.state == "pending" and s.head == HEAD, s)
        s = await ci.state(pr("", project=shop), [PYTEST])
        ck("a pull request with no ticket is pending", s.state == "pending", s)
        s = await ci.state(pr(project=""), [PYTEST])
        ck("a pull request with no project is pending", s.state == "pending", s)
        s = await ci.state(pr(head=""), [PYTEST])
        ck("a pull request with no head is pending", s.state == "pending" and s.head == "", s)
        ck("required but nothing recorded for another ticket is pending",
           (await ci.state(pr("T2"), [PYTEST])).state == "pending")
        try:
            await LocalCI().state(pr(), [PYTEST])
            ck("a store-less integration raises", False)
        except RuntimeError as e:
            ck("a store-less integration raises", "store" in str(e), str(e))
        ok, msg = await LocalCI().check_connection({})
        ck("the connection test needs no store", ok and "checks" in msg, msg)

        print("== the store ==")
        ck("None cleans to nothing", store.clean_commands(None) == [])
        ck("clean keeps order, drops blanks and repeats",
           store.clean_commands(["b  x", " a", "", "b x", "a"]) == ["b x", "a"])
        for bad in ("pytest", [1], [["a"]], {"a": 1}):
            try:
                store.clean_commands(bad)
                ck(f"{bad!r} is refused", False)
            except ValueError:
                ck(f"{bad!r} is refused", True)
        ck("a replace changes the list", store.set_required_checks(shop, [LINT], "ann") == [LINT]
           and store.get_required_checks(shop) == [LINT])
        ck("an empty list clears it", store.set_required_checks(shop, [], "ann") == []
           and store.get_required_checks(shop) == [])
        ck("another project is untouched", store.get_required_checks(empty) == [])
        ck("a project that does not exist has none", store.get_required_checks("nope") == [])

        print("== the route and the tool ==")
        ann = store.create_person("Ann", email="ann@example.com")
        ben = store.create_person("Ben", email="ben@example.com")
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=ann["id"])

        async def key(name, scopes, person=None):
            async with mk() as cx:
                body = {"name": name, "scopes": scopes}
                if person:
                    body["person_id"] = person["id"]
                return (await cx.post("/api/keys", json=body)).json()["secret"]

        as_station()
        reader, ann_k, ben_k = (await key("reader", ["read"]), await key("ann", ["read", "ingest"], ann),
                                await key("ben", ["read", "ingest"], ben))

        def client(token, **kw):
            hdr = {"Authorization": f"Bearer {token}", **kw}
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

        url = "/api/projects/p1/required-checks"
        async with client(TOKEN) as cx:
            r = await cx.get(url)
            ck("a fresh project reads an empty list", r.status_code == 200 and r.json()["commands"] == [], r.text)
            r = await cx.put(url, json={"commands": ["pytest -q", " ruff  check . ", ""]})
            ck("an admin writes, the answer is the cleaned list",
               r.status_code == 200 and r.json()["commands"] == ["pytest -q", "ruff check ."], r.text)
            r = await cx.get(url)
            ck("a read shows it", r.json()["commands"] == ["pytest -q", "ruff check ."], r.text)
            for bad in ({"commands": "pytest"}, {"commands": [1]}, {"commands": {"a": 1}}, {}):
                r = await cx.put(url, json=bad if bad else {"commands": None, "x": 1})
                if bad == {}:
                    ck("null commands clear the list", r.status_code == 200 and r.json()["commands"] == [], r.text)
                    await cx.put(url, json={"commands": ["pytest -q"]})
                else:
                    ck(f"{bad} is 422", r.status_code == 422, f"{r.status_code} {r.text}")
            ck("a bad body left the list as it was", (await cx.get(url)).json()["commands"] == ["pytest -q"])
            ck("an unknown project is 404", (await cx.put("/api/projects/nope/required-checks",
                                                          json={"commands": []})).status_code == 404)
        async with client(ann_k) as cx:
            r = await cx.put(url, json={"commands": ["make test"]})
            ck("the project's lead writes", r.status_code == 200 and r.json()["commands"] == ["make test"], r.text)
        for who, k, kw in (("another person", ben_k, {}), ("a read key", reader, {}),
                           ("a station", TOKEN, {"X-Tares-Station": "shop-build-m1", "X-Tares-Role": "builder"})):
            async with client(k, **kw) as cx:
                r = await cx.put(url, json={"commands": ["rm -rf /"]})
                ck(f"{who} may not write", r.status_code == 403, f"{r.status_code} {r.text}")
        ck("nothing a refused caller sent was stored", store.get_required_checks("p1") == ["make test"])

        as_station()
        out = await mf.set_required_checks(["pytest -q", "ruff check ."], project="Shop")
        ck("the tool sets the list", "pytest -q" in out and "ruff check ." in out
           and store.get_required_checks(shop) == [PYTEST, RUFF], out)
        out = await mf.set_required_checks([], project="Shop")
        ck("the tool clears it", store.get_required_checks(shop) == [], out)
        out = await mf.set_required_checks(["x"], project="No such project")
        ck("the tool says so for an unknown project", store.get_required_checks(shop) == [] and out, out)

        print("== a built-in ci integration ==")
        cls = registry.get("ci", "local")
        ck("local is installed for the ci slot", cls is LocalCI, cls)
        ck("it takes no settings and is not ee", cls.settings_schema == [] and cls.ee is False
           and cls.title == "Local checks")


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

"""A project across several repos, and the crew's list of projects (M8: TR-456, TR-462, TR-463).

Tickets carry the GitHub repo they change; the hand-over keeps every repo a project spans (a later
one adds, never replaces) and the autonomy it was handed over at; /api/crew/projects is the list
the orchestrator works from (repos with their tickets, progress, the milestone it is on); the
project's repos and the review queue name each ticket's repo; a station reaches the Tares in
TARES_FACTORY_URL with the token in TARES_FACTORY_TOKEN_FILE, whatever the plugin option says.

Run: .venv/bin/python tests/test_factory_repos.py
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-factory-repos-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-repos"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory as factory_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def rules():
    print("== repo names and hand-over lists ==")
    ck("owner/name stays", factory_mod.repo_ok("glassflow/api") == "glassflow/api")
    ck("a github URL becomes owner/name",
       factory_mod.repo_ok("https://github.com/glassflow/api.git") == "glassflow/api")
    ck("a git remote becomes owner/name",
       factory_mod.repo_ok("git" + "@github.com:glassflow/web.git") == "glassflow/web")
    try:
        factory_mod.repo_ok("just-a-name")
        ck("a bare name is refused", False)
    except Exception as e:
        ck("a bare name is refused", "owner/name" in str(e), e)
    got = factory_mod.handover_repos(["/w/api", {"path": "/w/web/", "name": "g/web"},
                                      {"path": "/w/api", "name": "g/api"}])
    ck("paths and names merge per repo, in order",
       got == [{"path": "/w/api", "name": "g/api"}, {"path": "/w/web", "name": "g/web"}], got)
    more = factory_mod.merge_repos(got, factory_mod.handover_repos([{"path": "/w/chart",
                                                                     "name": "g/chart"}]))
    ck("a later list adds", [r["name"] for r in more] == ["g/api", "g/web", "g/chart"], more)


def station_url():
    print("== a station reaches the crew's Tares, whatever the plugin option says ==")
    tok = os.path.join(_TMP, "token")
    with open(tok, "w") as f:
        f.write("station-token\n")
    env = {**os.environ, "TARESD_URL": "http://127.0.0.1:8787", "TARES_AUTH_TOKEN": "",
           "TARES_FACTORY_URL": "http://127.0.0.1:8797/", "TARES_FACTORY_TOKEN_FILE": tok}
    out = subprocess.run([sys.executable, "-c", "import tares.mcp_server as m; "
                          "print(m.TARESD); print(m.AUTH_TOKEN)"],
                         cwd=ROOT, env=env, capture_output=True, text=True).stdout.split()
    ck("the MCP proxy uses TARES_FACTORY_URL", out[:1] == ["http://127.0.0.1:8797"], out)
    ck("and the token from the file", out[1:2] == ["station-token"], out)
    env.pop("TARES_FACTORY_URL")
    env.pop("TARES_FACTORY_TOKEN_FILE")
    out = subprocess.run([sys.executable, "-c", "import tares.mcp_server as m; print(m.TARESD)"],
                         cwd=ROOT, env=env, capture_output=True, text=True).stdout.split()
    ck("outside a station TARESD_URL still counts", out == ["http://127.0.0.1:8787"], out)
    import ship
    saved = {k: os.environ.get(k) for k in ("TARES_FACTORY_URL", "TARES_FACTORY_TOKEN_FILE",
                                            "CLAUDE_PLUGIN_OPTION_TARES_URL")}
    try:
        os.environ.update(TARES_FACTORY_URL="http://127.0.0.1:8797", TARES_FACTORY_TOKEN_FILE=tok,
                          CLAUDE_PLUGIN_OPTION_TARES_URL="http://127.0.0.1:8787")
        cfg = ship.config()
        ck("the shipper sends to the crew's Tares", cfg["base"] == "http://127.0.0.1:8797", cfg)
        ck("with the station's token", cfg["headers"].get("Authorization") == "Bearer station-token",
           cfg["headers"])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(root))
    m._cx = mf._cx = mk

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        other = json.loads(await m.create_project("Notes", "Keep notes"))["id"]

        print("== tickets carry the repo they change ==")
        await mf.write_milestone("API", project=shop)
        await mf.write_milestone("UI", project=shop)
        t1 = json.loads(await m.write_ticket("Orders endpoint", milestone="API",
                                             repo="glassflow/factory-test-api", project=shop))
        ck("write_ticket keeps the repo", t1.get("repo") == "glassflow/factory-test-api", t1)
        t2 = json.loads(await m.write_ticket("Order form", milestone="UI", depends_on=["T1"],
                                             repo="https://github.com/glassflow/factory-test-web",
                                             project=shop))
        ck("a github URL is kept as owner/name", t2.get("repo") == "glassflow/factory-test-web", t2)
        t3 = json.loads(await m.write_ticket("Shared README", milestone="API", project=shop))
        ck("a ticket may name no repo", t3.get("repo") is None, t3)
        bad = await m.write_ticket("x", repo="not a repo", project=shop)
        ck("a bad repo is refused", "owner/name" in bad, bad)
        lst = json.loads(await m.list_tickets(shop))["tickets"]
        ck("list_tickets says each ticket's repo",
           [t["repo"] for t in lst] == ["glassflow/factory-test-api", "glassflow/factory-test-web",
                                        None], lst)
        one = json.loads(await m.get_ticket("T2", shop))
        ck("get_ticket says it too", one.get("repo") == "glassflow/factory-test-web", one)
        ch = json.loads(await m.write_ticket("", id="T3", repo="glassflow/factory-test-api",
                                             project=shop))
        ck("a ticket's repo can be set later", ch.get("repo") == "glassflow/factory-test-api", ch)

        print("== the hand-over keeps every repo ==")
        async with mk() as cx:
            r = await cx.get("/api/crew/projects")
            ck("nothing handed over yet", r.json()["projects"] == [], r.text)
        out = await mf.hand_over("Shop", repos=[{"path": "/w/api", "name": "glassflow/factory-test-api"}])
        ck("hand_over names the repo it recorded", "repo: /w/api (glassflow/factory-test-api)" in out, out)
        ck("no crew running: the person starts it", "tares factory crew up" in out, out)
        out = await mf.hand_over("Shop", repos=[{"path": "/w/web", "name": "glassflow/factory-test-web"}],
                                 autonomy="L3-review")
        async with mk() as cx:
            ho = (await cx.get(f"/api/projects/{shop}/crew")).json()["handover"]
        ck("a second hand-over adds its repo",
           [x["path"] for x in ho["repos"]] == ["/w/api", "/w/web"], ho)
        ck("the stricter autonomy is kept", ho["autonomy"] == "L3-review", ho)
        bad = await mf.hand_over("Shop", autonomy="L5-dark")
        ck("an autonomy the crew does not have is refused", "unknown autonomy" in bad, bad)
        async with mk() as cx:
            ho = (await cx.get(f"/api/projects/{shop}/crew")).json()["handover"]
        ck("a refused hand-over changes nothing", len(ho["repos"]) == 2
           and ho["autonomy"] == "L3-review", ho)
        await mf.hand_over("Notes", repo="/w/notes")
        async with mk() as cx:
            ho = (await cx.get(f"/api/projects/{other}/crew")).json()["handover"]
        ck("the older one-path form still works",
           ho["repos"] == [{"path": "/w/notes", "name": None}] and ho["repo"] == "/w/notes", ho)

        print("== the crew's projects ==")
        async with mk() as cx:
            ps = (await cx.get("/api/crew/projects")).json()["projects"]
        ck("both projects, oldest hand-over first", [p["name"] for p in ps] == ["Shop", "Notes"], ps)
        shop_row = ps[0]
        ck("progress: none of three done", (shop_row["tickets_done"], shop_row["tickets_total"])
           == (0, 3), shop_row)
        ck("the milestone it is on", (shop_row["milestone"] or {}).get("name") == "API", shop_row)
        by_name = {r["name"]: r for r in shop_row["repos"]}
        ck("each repo lists its tickets",
           by_name["glassflow/factory-test-api"]["tickets"] == ["T1", "T3"]
           and by_name["glassflow/factory-test-web"]["tickets"] == ["T2"], shop_row["repos"])
        ck("with its folder on the crew's machine",
           by_name["glassflow/factory-test-api"]["path"] == "/w/api", by_name)
        ck("and the autonomy", shop_row["autonomy"] == "L3-review", shop_row)
        tool = json.loads(await mf.crew_projects())
        ck("the orchestrator's tool reads the same list", [p["name"] for p in tool["projects"]]
           == ["Shop", "Notes"], tool)
        async with mk() as cx:
            r = await cx.get("/api/crew/projects")
            st = await cx.get("/api/crew/crew-orchestrator")
        ck("the station route still answers for a station", st.status_code == 404
           and "station" in st.text, st.text)

        print("== the project page's repos ==")
        async with mk() as cx:
            rr = (await cx.get(f"/api/projects/{shop}/repos")).json()["repos"]
            none = await cx.post("/api/projects", json={"template": "custom", "name": "Empty", "objects": []})
            empty = (await cx.get(f"/api/projects/{none.json()['id']}/repos")).json()["repos"]
        ck("two repos with their tickets", [(r["name"], r["tickets"]) for r in rr]
           == [("glassflow/factory-test-api", ["T1", "T3"]),
               ("glassflow/factory-test-web", ["T2"])], rr)
        ck("no repos for a project nobody handed over", empty == [], empty)

        print("== the review queue names the repo ==")
        async with mk() as cx:
            await cx.post(f"/api/projects/{shop}/tickets/T2/stage", json={"stage": "doing"})
            await cx.post(f"/api/projects/{shop}/tickets/T2/stage", json={"stage": "review"})
            q = (await cx.get("/api/review-queue")).json()["queue"]
            brief = (await cx.get(f"/api/projects/{shop}/tickets/T2/brief")).json()["brief"]
        ck("the queue item says its repo", q and q[0].get("repo") == "glassflow/factory-test-web", q)
        ck("the brief says it too", "Repo: glassflow/factory-test-web." in brief, brief[:400])


rules()
station_url()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

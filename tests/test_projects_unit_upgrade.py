"""The upgrade to "projects are the unit" (TR-327, TR-328): views fold into triggers, every object
lands in a project, and a second start changes nothing.

Builds a database the way a cell had it while views existed (catalog_views, a `view` column on
catalog_triggers, a custom project that lists a view, a template project, objects in no project, a
handoff trigger over the findings source filtered to another agent, a schedule trigger), opens it
with today's Store, and checks the result against the contract. Then starts the daemon on it. Last,
imports catalogs exported while views existed (a committed fixture, and the real export named by
TARES_LEGACY_CATALOG when that file is present) into a fresh store and checks every trigger has
sources and a project and nothing names a view.

Run: .venv/bin/python tests/test_projects_unit_upgrade.py   (no external services needed)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb
import yaml

PASS = FAIL = 0
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "legacy_catalog_with_views.yaml")
REAL_EXPORT = os.getenv(
    "TARES_LEGACY_CATALOG",
    "/private/tmp/claude-501/-Users-ashishbagri-WorkData-source-github-glassflow-tares/"
    "5cf507c5-16a0-4b3b-9851-93e4aee1668c/scratchpad/upgrade/glassflow-web-catalog.yaml")


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


# The catalog tables as a cell had them while views existed (their migrated shape: owned_by,
# customized and the agent columns were there already). Everything else the Store creates.
OLD_DDL = """
CREATE TABLE catalog_sources (
  name TEXT PRIMARY KEY, type TEXT, connector TEXT, poll TEXT, config JSON,
  paused BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, ingest_key TEXT,
  owned_by TEXT, customized BOOLEAN);
CREATE TABLE catalog_views (
  name TEXT PRIMARY KEY, key_field TEXT, sources JSON, filters JSON, created_by TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, owned_by TEXT, customized BOOLEAN);
CREATE TABLE catalog_triggers (
  name TEXT PRIMARY KEY, view TEXT, condition JSON, emit JSON, cooldown TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, paused BOOLEAN DEFAULT FALSE,
  owned_by TEXT, customized BOOLEAN);
CREATE TABLE catalog_agents (
  name TEXT PRIMARY KEY, trigger TEXT, prompt TEXT, slack_webhook TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, model TEXT, slack_channel TEXT,
  webhook_url TEXT, webhook_token TEXT, mcp_servers JSON, max_rounds INTEGER,
  budget_usd DOUBLE, owned_by TEXT, customized BOOLEAN);
CREATE TABLE mcp_servers (
  name TEXT PRIMARY KEY, url TEXT, auth_header TEXT, auth_value TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, owned_by TEXT, customized BOOLEAN, headers JSON);
CREATE TABLE usecases (
  id TEXT PRIMARY KEY, recipe TEXT, name TEXT, params JSON, status TEXT,
  created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, last_error TEXT);
CREATE TABLE usecase_objects (
  usecase_id TEXT, kind TEXT, key TEXT, name TEXT, customized BOOLEAN, created_at TIMESTAMPTZ,
  PRIMARY KEY (usecase_id, kind, key));
CREATE TABLE usecase_log (usecase_id TEXT, logged_at TIMESTAMPTZ, action TEXT, detail TEXT);
CREATE TABLE trigger_state (
  trigger TEXT, key_value TEXT, last_fired TIMESTAMPTZ, PRIMARY KEY (trigger, key_value));
CREATE TABLE query_log (
  id TEXT PRIMARY KEY, view TEXT, key_value TEXT, time_window TEXT, rows_returned INTEGER,
  client TEXT, queried_at TIMESTAMPTZ);
"""

CUSTOM, TPL = "uc_custom0001", "uc_template01"
LABELLED = {"labels": [{"name": "service", "field": "service", "primary": True}]}
COUNT = {"aggregate": "count", "predicate": "> 0", "window": "5m"}


def build_old_db(path: str) -> None:
    con = duckdb.connect(path)
    for stmt in OLD_DDL.strip().split(";"):
        if stmt.strip():
            con.execute(stmt)

    def src(name, connector="webhook", config=None, owner=None):
        con.execute("INSERT INTO catalog_sources VALUES (?, 'x', ?, '5s', ?, FALSE, now(), now(), ?, ?, FALSE)",
                    [name, connector, json.dumps(config or {}), f"ik_{name}", owner])

    def view(name, sources, key_field="service", filters=None, owner=None):
        con.execute("INSERT INTO catalog_views VALUES (?, ?, ?, ?, 'human', now(), now(), ?, FALSE)",
                    [name, key_field, json.dumps(sources), json.dumps(filters or []), owner])

    def trig(name, view_name, condition=None, owner=None):
        con.execute("INSERT INTO catalog_triggers VALUES (?, ?, ?, '{}', '5m', now(), now(), FALSE, ?, FALSE)",
                    [name, view_name, json.dumps(condition or COUNT), owner])

    def agent(name, trigger, servers=(), owner=None):
        con.execute("INSERT INTO catalog_agents VALUES (?, ?, 'look', '', now(), now(), '', '', '', '', ?, NULL, NULL, ?, FALSE)",
                    [name, trigger, json.dumps(list(servers)), owner])

    def mcp(name, owner=None):
        con.execute("INSERT INTO mcp_servers VALUES (?, 'https://example.invalid/mcp', '', '', now(), now(), ?, FALSE, '{}')",
                    [name, owner])

    def row(uid, kind, key, name):
        con.execute("INSERT INTO usecase_objects VALUES (?, ?, ?, ?, FALSE, now())", [uid, kind, key, name])

    # a custom project that lists a source, a view and a trigger; a template project that owns
    # a full plan
    con.execute("INSERT INTO usecases VALUES (?, 'custom', 'checkout', ?, 'active', now(), now(), NULL)",
                [CUSTOM, json.dumps({"objects": [{"kind": "source", "name": "logs"},
                                                 {"kind": "view", "name": "v_logs"},
                                                 {"kind": "trigger", "name": "t_logs"}]})])
    con.execute("INSERT INTO usecases VALUES (?, 'ai_sre_demo', 'demo', '{}', 'active', now(), now(), NULL)",
                [TPL])
    src("logs", config=LABELLED, owner=CUSTOM)
    src("findings", connector="finding")
    src("loose", config=LABELLED)
    src("tpl_src", config=LABELLED, owner=TPL)
    view("v_logs", ["logs"], filters=[{"field": "service", "op": "neq", "value": "healthcheck"}],
         owner=CUSTOM)
    view("v_escalate", ["findings"], filters=[{"field": "agent", "op": "eq", "value": "watcher"},
                                             {"field": "verdict", "op": "eq", "value": "investigate"}])
    view("v_tpl", ["tpl_src"], owner=TPL)
    trig("t_logs", "v_logs", owner=CUSTOM)
    trig("t_escalate", "v_escalate")
    trig("t_watch", "v_logs", condition={"every": "10m", "summary_by": ["service"]})
    trig("t_tpl", "v_tpl", owner=TPL)
    trig("t_ghost", "ghost_view")
    agent("a_logs", "t_logs", servers=["m_logs"])
    agent("a_rca", "t_escalate", servers=["m_used"])
    agent("a_tpl", "t_tpl", owner=TPL)
    mcp("m_logs")
    mcp("m_used")
    mcp("m_free")
    for kind, name in (("source", "logs"), ("view", "v_logs"), ("trigger", "t_logs")):
        row(CUSTOM, kind, f"{kind}:{name}", name)
    for kind, key, name in (("source", "alerts", "tpl_src"), ("view", "view", "v_tpl"),
                            ("trigger", "trigger", "t_tpl"), ("agent", "agent", "a_tpl")):
        row(TPL, kind, key, name)
    # the schedule's last tick, recorded under its view's name
    con.execute("INSERT INTO trigger_state VALUES ('t_watch', 'v_logs', now())")
    con.execute("INSERT INTO query_log VALUES ('q1', 'v_logs', 'checkout', '15m', 3, 'mcp', now())")
    con.close()


SNAPSHOT = {
    "catalog_sources": "name, owned_by",
    "catalog_triggers": "name, sources, filters, key_field, condition, paused, owned_by",
    "catalog_agents": "name, trigger, owned_by",
    "mcp_servers": "name, owned_by",
    "usecases": "id, recipe, name, params, status",
    "usecase_objects": "usecase_id, kind, key, name",
    "settings": "key, value",
    "trigger_state": "trigger, key_value",
}


def snapshot(store) -> dict:
    return {t: sorted(store.con.execute(f"SELECT {cols} FROM {t}").fetchall(), key=str)
            for t, cols in SNAPSHOT.items()}


def check_invariants(store, where: str) -> None:
    """Every trigger, agent and MCP server in exactly one existing project, listed there; every
    source in at least one; every trigger's sources members of its project; no view anywhere."""
    projects = {p["id"] for p in store.list_projects()}
    rows = store.con.execute("SELECT usecase_id, kind, name FROM usecase_objects").fetchall()
    listed = {(u, k, n) for u, k, n in rows}
    bad = []
    for kind, items in (("trigger", store.list_catalog_triggers()), ("agent", store.list_catalog_agents()),
                        ("mcp_server", store.list_mcp_servers())):
        for x in items:
            if x.get("owned_by") not in projects or (x["owned_by"], kind, x["name"]) not in listed:
                bad.append(f"{kind}:{x['name']}->{x.get('owned_by')}")
    check(f"{where}: every trigger, agent and MCP server is in one project and listed there",
          not bad, str(bad))
    members = store.source_memberships()
    orphans = [s["name"] for s in store.list_catalog_sources() if not members.get(s["name"])]
    check(f"{where}: every source is in a project", not orphans, str(orphans))
    missing = [(t["name"], s) for t in store.list_catalog_triggers() for s in t["sources"]
               if t["owned_by"] not in members.get(s, [])]
    check(f"{where}: every trigger's sources are members of its project", not missing, str(missing))
    agents_off = [a["name"] for a in store.list_catalog_agents()
                  if a["owned_by"] != next((t["owned_by"] for t in store.list_catalog_triggers()
                                            if t["name"] == a["trigger"]), None)]
    check(f"{where}: every agent is in its trigger's project", not agents_off, str(agents_off))
    check(f"{where}: no view rows, no view objects in a custom project",
          not [r for r in rows if r[1] == "view"]
          and not [o for p in store.list_projects() for o in (p["params"].get("objects") or [])
                   if o.get("kind") == "view"])


def upgrade_checks():
    from tares.config import catalog_from_db, validate_agent_dict
    from tares.store import Store
    print("== upgrade of a database from when views existed ==")
    path = os.path.join(tempfile.mkdtemp(), "old.duckdb")
    build_old_db(path)
    store = Store(path)
    tables = {r[0] for r in store.con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    tcols = {r[0] for r in store.con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'catalog_triggers'").fetchall()}
    check("catalog_views is dropped", "catalog_views" not in tables, str(sorted(tables)))
    check("catalog_triggers lost its view column and has sources, filters, key_field",
          "view" not in tcols and {"sources", "filters", "key_field"} <= tcols, str(tcols))
    trig = {t["name"]: t for t in store.list_catalog_triggers()}
    check("a trigger took its view's sources, filters and key_field",
          trig["t_logs"]["sources"] == ["logs"] and trig["t_logs"]["key_field"] == "service"
          and trig["t_logs"]["filters"] == [{"field": "service", "op": "neq", "value": "healthcheck"}],
          str(trig["t_logs"]))
    check("the handoff trigger keeps the agent filter on findings",
          trig["t_escalate"]["sources"] == ["findings"]
          and {"field": "agent", "op": "eq", "value": "watcher"} in trig["t_escalate"]["filters"],
          str(trig["t_escalate"]))
    check("a trigger whose view was already gone is paused with no sources",
          trig["t_ghost"]["sources"] == [] and trig["t_ghost"]["paused"] is True, str(trig["t_ghost"]))
    last = store.con.execute("SELECT key_value FROM trigger_state WHERE trigger = 't_watch'").fetchall()
    check("the schedule's last tick moved from the view's name to the trigger's",
          last == [("t_watch",)], str(last))
    q = store.list_queries()
    check("the query log kept its rows under scope", q and q[0]["scope"] == "v_logs" and "view" not in q[0],
          str(q))

    default = store.get_setting("default_project")
    projects = {p["id"]: p for p in store.list_projects()}
    check("a default project was created and recorded",
          default in projects and projects[default]["template"] == "default"
          and projects[default]["name"] == "Default", str(projects.get(default)))
    check("triggers: one in the custom project stays, one in the template stays, the rest go to default",
          trig["t_logs"]["owned_by"] == CUSTOM and trig["t_tpl"]["owned_by"] == TPL
          and trig["t_escalate"]["owned_by"] == default and trig["t_watch"]["owned_by"] == default
          and trig["t_ghost"]["owned_by"] == default, str({k: v["owned_by"] for k, v in trig.items()}))
    ag = {a["name"]: a["owned_by"] for a in store.list_catalog_agents()}
    check("an agent with no project follows its trigger; an owned one stays",
          ag == {"a_logs": CUSTOM, "a_rca": default, "a_tpl": TPL}, str(ag))
    mc = {m["name"]: m["owned_by"] for m in store.list_mcp_servers()}
    check("an MCP server with no project goes where its agents are, else to default",
          mc == {"m_logs": CUSTOM, "m_used": default, "m_free": default}, str(mc))
    members = store.source_memberships()
    # logs is also read by t_watch, which went to the default project: it is a member of both
    check("sources: members where they were, and of every project whose trigger reads them; "
          "a loose one joins the default",
          sorted(members.get("logs")) == sorted([CUSTOM, default]) and members.get("tpl_src") == [TPL]
          and members.get("findings") == [default] and members.get("loose") == [default], str(members))
    custom = projects[CUSTOM]["params"]["objects"]
    check("the custom project's list lost its view and gained what followed its trigger",
          {"kind": "view", "name": "v_logs"} not in custom
          and {"kind": "agent", "name": "a_logs"} in custom
          and {"kind": "mcp_server", "name": "m_logs"} in custom, str(custom))
    tpl_keys = {(o["kind"], o["key"]) for o in store.list_project_objects(TPL)}
    check("the template project keeps its plan keys, minus the view",
          tpl_keys == {("source", "alerts"), ("trigger", "trigger"), ("agent", "agent")}, str(tpl_keys))
    check_invariants(store, "after the upgrade")
    cat = catalog_from_db(store)
    triggers = {t["name"]: t for t in store.list_catalog_triggers()}
    try:
        validate_agent_dict({"name": "a_rca", "trigger": "t_escalate", "prompt": "p"},
                            set(triggers), triggers)
        check("the loop guard still sees the handoff as a handoff", True)
    except Exception as e:
        check("the loop guard still sees the handoff as a handoff", False, str(e))
    check("the typed catalog carries sources and projects",
          next(t for t in cat.triggers if t.name == "t_logs").sources == ["logs"]
          and next(t for t in cat.triggers if t.name == "t_logs").project == CUSTOM)

    before = snapshot(store)
    store.con.close()
    store = Store(path)
    check("a second start changes nothing", snapshot(store) == before,
          json.dumps({k: [v for v in snapshot(store)[k] if v not in before[k]] for k in before}, default=str)[:600])
    store.con.close()
    return path, default


async def daemon_checks(path, default):
    print("== the daemon starts on the upgraded database ==")
    os.environ["TARES_DB"] = path
    os.environ["TARES_CATALOG"] = os.path.join(os.path.dirname(path), "none.yaml")
    import importlib
    import httpx
    import tares.daemon as daemon
    importlib.reload(daemon)
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            ps = (await cx.get("/api/projects")).json()["projects"]
            check("the default project is listed first and flagged",
                  ps[0]["id"] == default and ps[0]["default"] is True
                  and sum(1 for p in ps if p["default"]) == 1, json.dumps(ps)[:300])
            custom = next(p for p in ps if p["id"] == CUSTOM)
            check("the custom project lists no view",
                  not any(o["kind"] == "view" for o in custom["objects"]), json.dumps(custom["objects"]))
            t = {x["name"]: x for x in (await cx.get("/api/triggers")).json()}["t_logs"]
            check("GET /api/triggers speaks the new shape",
                  t["project"] == CUSTOM and t["sources"] == ["logs"] and "view" not in t, str(t))
            r = await cx.get("/api/views")
            check("/api/views is gone", r.status_code == 404, r.text[:100])
            src = {s["name"]: s for s in (await cx.get("/api/sources")).json()}
            check("GET /api/sources carries the memberships", src["loose"]["projects"] == [default])
            st = app.state.store
            r = await cx.post("/ingest/ik_logs", json={"service": "checkout", "msg": "boom"})
            check("ingest into a folded trigger's source works", r.status_code == 202, r.text[:200])
            check("the folded trigger fires on its sources",
                  st.last_fired("t_logs", "checkout") is not None)
            r = await cx.post("/ingest/ik_logs", json={"service": "healthcheck", "msg": "ok"})
            check("and its filters still keep out what the view kept out",
                  r.status_code == 202 and st.last_fired("t_logs", "healthcheck") is None)
            y = (await cx.get("/api/catalog/export")).text
            doc = yaml.safe_load(y)
            check("the export has no views and every trigger names its project and sources",
                  "views" not in doc and all(t.get("sources") and t.get("project") and "view" not in t
                                             for t in doc["triggers"]),
                  y[:400])
            check("the trigger left with no sources is left out of the export (it would not import)",
                  not any(t["name"] == "t_ghost" for t in doc["triggers"]))
            check("the export does not list the default project",
                  not any(p.get("template") == "default" for p in doc.get("projects") or []))


def legacy_import_checks(label: str, text: str):
    from tares.config import import_catalog_dict
    from tares.projects import Engine
    from tares.store import Store
    print(f"== legacy import: {label} ==")
    raw = yaml.safe_load(text)
    check(f"{label}: the file really is legacy (views: and triggers naming views)",
          raw.get("views") and all(t.get("view") for t in raw["triggers"]))
    store = Store(os.path.join(tempfile.mkdtemp(), "import.duckdb"))
    # the shared-code-context projects check their GitHub credential exists before they start
    creds = {m.group(0) for m in __import__("re").finditer(r"[a-z0-9-]+-github-token", text)}
    for c in creds:
        store.upsert_github_credential(c, "tok", "token", "", "")
    counts = import_catalog_dict(store, raw, engine=Engine(store))
    check(f"{label}: imported", counts["triggers"] == len(raw["triggers"])
          and counts["projects"] == len(raw["projects"]) and "views" not in counts, str(counts))
    by_name = {v["name"]: v for v in raw["views"]}
    trig = {t["name"]: t for t in store.list_catalog_triggers()}
    # a shared code context trigger now also says it counts commits (its GitHub sources report
    # pull requests too); that one filter is the template's, not the view's
    commit = {"field": "event_type", "op": "eq", "value": "commit"}
    wrong = [n for n, t in raw_triggers(raw) if trig[n]["sources"] != by_name[t["view"]]["sources"]
             or [f for f in trig[n]["filters"] if f != commit]
             != (by_name[t["view"]].get("filters") or [])]
    check(f"{label}: every trigger has its view's sources and filters", not wrong, str(wrong))
    projects = {p["id"] for p in store.list_projects()}
    check(f"{label}: every trigger has sources and a project",
          all(t["sources"] and t["owned_by"] in projects for t in trig.values()),
          str({n: (t["sources"], t["owned_by"]) for n, t in trig.items()}))
    check_invariants(store, label)
    names = {p["name"]: p for p in store.list_projects()}
    check(f"{label}: the declared projects exist besides the default one",
          {p["name"] for p in raw["projects"]} <= set(names)
          and sum(1 for p in store.list_projects() if p["template"] == "default") == 1, str(list(names)))
    custom = [p for p in store.list_projects() if p["template"] == "custom"]
    check(f"{label}: the custom project's view objects were dropped, its trigger kept",
          custom and all(o["kind"] != "view" for p in custom for o in p["params"]["objects"])
          and any(o["kind"] == "trigger" for p in custom for o in p["params"]["objects"]), str(custom)[:300])
    blob = json.dumps({"t": store.list_catalog_triggers(), "p": store.list_projects(),
                       "o": store.con.execute("SELECT kind FROM usecase_objects").fetchall()}, default=str)
    check(f"{label}: nothing references a view", '"view"' not in blob and "attach_view" not in blob,
          blob[:200])
    from tares.config import export_db_to_yaml
    again = yaml.safe_load(export_db_to_yaml(store))
    check(f"{label}: its export imports cleanly into another fresh store",
          reimport(again, creds), "")
    store.con.close()


def raw_triggers(raw):
    return [(t["name"], t) for t in raw["triggers"]]


def reimport(doc, creds) -> bool:
    from tares.config import import_catalog_dict
    from tares.projects import Engine
    from tares.store import Store
    s = Store(os.path.join(tempfile.mkdtemp(), "again.duckdb"))
    for c in creds:
        s.upsert_github_credential(c, "tok", "token", "", "")
    import_catalog_dict(s, doc, engine=Engine(s))
    ok = (len(s.list_catalog_triggers()) == len(doc["triggers"])
          and all(t["owned_by"] for t in s.list_catalog_triggers()))
    s.con.close()
    return ok


def main():
    path, default = upgrade_checks()
    asyncio.run(daemon_checks(path, default))
    legacy_import_checks("committed fixture", open(FIXTURE).read())
    if os.path.exists(REAL_EXPORT):
        legacy_import_checks("real export", open(REAL_EXPORT).read())
    else:
        print(f"  (no real export at {REAL_EXPORT}; set TARES_LEGACY_CATALOG to run it too)")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

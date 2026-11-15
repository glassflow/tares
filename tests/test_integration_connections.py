"""Credentials live only at the factory (TR-1380).

Covered:
 - the store: several connections per slot with one default; the first connection made is the
   default unless asked otherwise; `default=False` keeps the old one; set_default moves the pointer
   and refuses an integration that is not connected; memory, monitoring and runtime keep exactly one
   connection (a second replaces the first); an integration with no integration settings counts as
   connected; deleting a connection is refused while it is the default, while a project uses it and
   while a guardrail pair uses it, and the message names what uses it; a free connection deletes;
   a project row holds only plain fields (a secret key or an address key raises ValueError);
   stored_settings is the factory connection overlaid with the project's own fields;
 - the effective read: a project's choice of an integration that is no longer connected falls
   through to the factory's default with the reason "not connected";
 - the migration of a cell from before (`_factory_upgrade`): a choice's settings become a
   connection and the choice keeps only the pointer; a project's secret and address keys move to the
   factory connection and its plain fields stay; an existing connection is not overwritten; an
   integration that is not installed treats every key as a secret; a guardrail pair's settings move
   the same way and the pair keeps none; pairs that are then twins become one; a second run changes
   nothing; no secret value is printed;
 - the API: connect several, `default: false`, the default route, the delete route (422 naming the
   user, 404 for a connection that is not there, the entry back with `deleted`); every entry carries
   `connections` redacted; a project write with a secret key, an address key or an integration that
   is not connected is 422 and names the factory's integrations; a pair with integration settings is
   422; who may: an admin, and a crew station gets 403 on every new route while it may still read
   the effective integrations.

Run: python tests/test_integration_connections.py
With CI's Postgres run: TARES_TEST_PG=<dsn> PYTHONPATH=tests/pg_harness python tests/test_integration_connections.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

_TMP = tempfile.mkdtemp(prefix="tares-integration-connections-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-connections"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import licence
from tares.integrations import registry
from tares.integrations.ci import CISource
from tares.integrations.guardrails import Guardrail
from tares.integrations.review import ReviewGate
from tares.integrations.tickets import TicketBackend
from tares.integrations.types import Field
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SECRETS = ["CSECRET-alpha-1", "CSECRET-beta-2", "CSECRET-gamma-3", "CSECRET-old-4", "CSECRET-pair-5",
           "CSECRET-gone-6", "CSECRET-proj-7", "CSECRET-pair-8"]


class TixA(TicketBackend):
    name = "tixa"
    title = "Tickets A"
    settings_schema = [Field("url", "Address", "url"), Field("token", "Token", "secret"),
                       Field("project", "Project key", "text")]


class TixB(TicketBackend):
    name = "tixb"
    title = "Tickets B"
    settings_schema = [Field("url", "Address", "url"), Field("token", "Token", "secret"),
                       Field("project", "Project key", "text")]


class TixC(TicketBackend):
    name = "tixc"
    title = "Tickets C"
    settings_schema = [Field("token", "Token", "secret")]


class RevA(ReviewGate):
    name = "reva"
    title = "Review A"
    settings_schema = [Field("token", "Token", "secret")]


class CiPlain(CISource):
    name = "ciplain"
    title = "Plain CI"
    settings_schema = []


class CiBranch(CISource):
    name = "cibranch"
    title = "Branch CI"
    settings_schema = [Field("branch", "Branch", "text")]


def rail(name, schema):
    class Rail(Guardrail):
        async def check(self, stage, payload):
            return None
    Rail.name = name
    Rail.title = f"Rail {name}"
    Rail.settings_schema = schema
    return Rail


RSCHEMA = [Field("url", "Address", "url"), Field("token", "Token", "secret"), Field("mode", "Mode", "text")]

registry.load()
for cls in (TixA, TixB, TixC):
    registry._loaded[("tickets", cls.name)] = cls
registry._loaded[("review", "reva")] = RevA
registry._loaded[("ci", "ciplain")] = CiPlain
registry._loaded[("ci", "cibranch")] = CiBranch
for n in ("rail_a", "rail_b", "rail_c"):
    registry._loaded[("guardrails", n)] = rail(n, RSCHEMA)

licence.ee_allowed = lambda feature="enterprise": False


def raises(fn, *a, **kw):
    """The ValueError message, or None when nothing was raised."""
    try:
        fn(*a, **kw)
    except ValueError as e:
        return str(e)
    return None


def conn_names(s, slot):
    return [c["name"] for c in s.list_connections(slot)]


def default_of(s, slot):
    return [c["name"] for c in s.list_connections(slot) if c["default"]]


# ── the store ──────────────────────────────────────────────────────────────────
print("== several connections and the default ==")
s = Store(os.path.join(_TMP, "store.duckdb"))
ck("nothing connected: no connections", s.list_connections("tickets") == [])
v = s.set_choice("tickets", "tixa", {"url": "https://a.example/api", "token": SECRETS[0], "project": "A"}, "ann")
ck("the first connection made is the default", v["default"] is True and v["name"] == "tixa", v)
ck("its view is redacted", v["settings"].get("token") == "" and v["secrets"] == {"token": {"has_value": True}}
   and SECRETS[0] not in json.dumps(v, default=str), v)
v = s.set_choice("tickets", "tixb", {"url": "https://b.example/api", "token": SECRETS[1], "project": "B"}, "ann",
                 default=False)
ck("a second connection with default=False keeps the old default", v["default"] is False
   and default_of(s, "tickets") == ["tixa"] and conn_names(s, "tickets") == ["tixa", "tixb"], v)
ck("each connection keeps its own secret", s.stored_settings("tickets", "tixa")["token"] == SECRETS[0]
   and s.stored_settings("tickets", "tixb")["token"] == SECRETS[1])
ck("get_choice is the default's view", s.get_choice("tickets")["name"] == "tixa")
ck("get_connection reads one, redacted, with its default flag",
   s.get_connection("tickets", "tixb")["default"] is False
   and s.get_connection("tickets", "tixb")["settings"]["token"] == ""
   and s.get_connection("tickets", "nope") is None)
v = s.set_choice("tickets", "tixc", {"token": SECRETS[2]}, "ann")
ck("connecting with the default left on makes it the default", v["default"] is True
   and default_of(s, "tickets") == ["tixc"] and conn_names(s, "tickets") == ["tixa", "tixb", "tixc"], v)
v = s.set_default("tickets", "tixa", "ann")
ck("set_default moves the pointer and keeps every connection", v["name"] == "tixa" and v["default"] is True
   and default_of(s, "tickets") == ["tixa"] and len(conn_names(s, "tickets")) == 3, v)
ck("set_default refuses an integration that is not connected, and says where to connect it",
   "not connected" in (raises(s.set_default, "tickets", "nope", "ann") or "")
   and "factory's integrations" in (raises(s.set_default, "tickets", "nope", "ann") or ""))
ck("a refused set_default changed nothing", default_of(s, "tickets") == ["tixa"])
ck("set_default for monitoring is refused", raises(s.set_default, "monitoring", "off", "ann") is not None)

print("== the slots with one connection ==")
s.set_choice("memory", "tares", {}, "ann")
ck("memory holds one connection", conn_names(s, "memory") == ["tares"])
s.set_choice("memory", "other", {"x": 1}, "ann")
ck("a second memory connection replaces the first and is the default",
   conn_names(s, "memory") == ["other"] and default_of(s, "memory") == ["other"], s.list_connections("memory"))
s.set_choice("runtime", "claude_code", {}, "ann")
s.set_choice("runtime", "other_rt", {}, "ann")
ck("runtime holds one connection", conn_names(s, "runtime") == ["other_rt"])
ck("monitoring has no row: nothing listed while tracing is off", s.list_connections("monitoring") == [])

print("== connected ==")
ck("a connection row is connected", s.is_connected("tickets", "tixb"))
ck("an integration with no integration settings needs no connection", s.is_connected("ci", "ciplain"))
ck("an integration with settings and no connection is not connected", not s.is_connected("review", "reva"))
s.set_choice("review", "reva", {"token": SECRETS[2]}, "ann", default=False)
ck("connecting it with default=False connects it and points no default at it", s.is_connected("review", "reva")
   and default_of(s, "review") == [], s.list_connections("review"))
ck("the slot's built-in default counts as connected", s.is_connected("ci", "local"))

print("== a project holds plain fields only ==")
r = raises(s.set_project_choice, "p1", "tickets", "tixa", {"token": SECRETS[6]}, "ann")
ck("a secret key raises and names the factory", r is not None and "token is a secret" in r
   and "factory's integrations" in r and SECRETS[6] not in r, r)
r = raises(s.set_project_choice, "p1", "tickets", "tixa", {"url": "https://x.example"}, "ann")
ck("an address key raises", r is not None and "url is an address" in r, r)
ck("a refused write left no row", s.get_project_choice("p1", "tickets") is None)
v = s.set_project_choice("p1", "tickets", "tixb", {"project": "PROJ"}, "ann")
ck("a plain field is kept", v["settings"] == {"project": "PROJ"}, v)
ck("stored_settings with the project is the connection overlaid with the project's field",
   s.stored_settings("tickets", "tixb", "p1")
   == {"url": "https://b.example/api", "token": SECRETS[1], "project": "PROJ"}, s.stored_settings("tickets", "tixb", "p1"))
ck("without the project it is the connection as saved",
   s.stored_settings("tickets", "tixb")["project"] == "B")
ck("another integration for that project is not overlaid",
   s.stored_settings("tickets", "tixa", "p1")["project"] == "A")

print("== deleting a connection ==")
ck("the default cannot be deleted, and the message names why",
   "default for tickets" in (raises(s.delete_connection, "tickets", "tixa") or ""))
r = raises(s.delete_connection, "tickets", "tixb")
ck("a connection a project uses cannot be deleted, and the message names the project",
   r is not None and "p1" in r and "project" in r, r)
ck("it is still there", "tixb" in conn_names(s, "tickets"))
ck("a connection nothing uses deletes", s.delete_connection("tickets", "tixc") is True
   and "tixc" not in conn_names(s, "tickets"))
ck("deleting what is not there says so (False)", s.delete_connection("tickets", "tixc") is False)
s.clear_project_choice("p1", "tickets")
ck("once the project lets go it deletes", s.delete_connection("tickets", "tixb") is True)
s.set_choice("guardrails", "rail_a", {"url": "https://g.example/", "token": SECRETS[4]}, "ann")
s.set_choice("guardrails", "rail_b", {"url": "https://g2.example/", "token": SECRETS[5]}, "ann", default=False)
pair, how = s.save_guardrail_pair("factory", None, "pre_build", "rail_b", "block", by="ann")
r = raises(s.delete_connection, "guardrails", "rail_b")
ck("a connection a guardrail pair uses cannot be deleted, and the message names the pair",
   how == "added" and r is not None and pair["id"] in r and "pair" in r, (how, r))
s.delete_guardrail_pair(pair["id"])
ck("once the pair is gone it deletes", s.delete_connection("guardrails", "rail_b") is True)

print("== a project's choice of what is no longer connected ==")
from tares.integrations import resolve
s.set_choice("tickets", "tixb", {"url": "https://b.example/api", "token": SECRETS[1], "project": "B"}, "ann",
             default=False)
s.set_project_choice("p2", "tickets", "tixb", {}, "ann")
e = resolve.effective("tickets", "p2", store=s)
ck("while connected the project's choice applies", e.source == "project" and e.integration == "tixb", e)
with s._swrite:    # as a restore or an older cell could leave it: the row without the connection
    s.sc.execute("DELETE FROM integration_connections WHERE slot = ? AND name = ?", ["tickets", "tixb"])
e = resolve.effective("tickets", "p2", store=s)
ck("with no connection it falls through to the factory's default, reason not connected",
   e.integration == "tixa" and e.source == "factory" and e.reason == "not connected", e)
ck("and shows no secret", SECRETS[1] not in repr(e) and SECRETS[0] not in repr(e), e)


# ── the migration ──────────────────────────────────────────────────────────────
print("== the migration of a cell from before ==")
m = Store(os.path.join(_TMP, "old.duckdb"))
at = "2026-01-01 00:00:00"


def put(sql, args):
    with m._swrite:
        m.sc.execute(sql, args)


def old_choice(slot, name, settings):
    put("INSERT INTO integration_choices (slot, name, settings, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
        [slot, name, json.dumps(settings), at, "old-ann"])


def old_project(uid, slot, name, settings):
    put("INSERT INTO project_integrations (project_uid, slot, name, settings, updated_at, updated_by) "
        "VALUES (?, ?, ?, ?, ?, ?)", [uid, slot, name, json.dumps(settings), at, "old-lead"])


def old_pair(pid, scope, uid, hook, name, settings, on_fail="block"):
    put("INSERT INTO guardrail_pairs (id, scope, project_uid, hook, integration, settings, on_fail, created_at, "
        "created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [pid, scope, uid, hook, name, json.dumps(settings), on_fail, at, "old-ann"])


def table(sql, args=()):
    with m._sread:
        return m.sc.execute(sql, list(args)).fetchall()


def upgrade():
    with m._swrite:
        m._factory_upgrade()


# tickets: the factory's choice held its settings; a project held a token, an address and a plain field
old_choice("tickets", "tixa", {"url": "https://a.example/api", "token": SECRETS[3], "project": "FAC"})
# a plain-only row for that same integration that sorts BEFORE p1: it must not make the connection empty first
old_project("p00", "tickets", "tixb", {"project": "ZERO"})
old_project("p1", "tickets", "tixb", {"url": "https://own.example/api", "token": SECRETS[6], "project": "OWN"})
# a project row for an integration the factory already has a connection for: its secret is not copied
old_choice("review", "reva", {"token": SECRETS[2]})
old_project("p1", "review", "reva", {"token": SECRETS[7]})
# a project row for an integration that is not installed: every key is a secret
old_project("p3", "ci", "gone_ci", {"url": "https://gone.example", "branch": "main"})
# a project row with only plain fields: nothing moves
old_project("p4", "tickets", "tixa", {"project": "P4"})
# a project row with only plain fields for an integration the factory never connected: it still needs a connection
old_project("p5", "ci", "cibranch", {"branch": "release"})
# guardrail pairs: two the same with settings, one for another hook, one with no settings
old_pair("GP1", "factory", None, "pre_build", "rail_a", {"url": "https://g.example/", "token": SECRETS[4]})
old_pair("GP2", "factory", None, "pre_build", "rail_a", {"url": "https://g.example/", "token": SECRETS[4]})
old_pair("GP3", "factory", None, "pre_merge", "rail_a", {"url": "https://g.example/", "token": SECRETS[4]}, "warn")
old_pair("GP4", "project", "p1", "pre_build", "rail_c", {})
old_pair("GP5", "project", "p1", "pre_build", "rail_c", {})
# monitoring never moves: it lives in the tracing settings
old_choice("monitoring", "otlp", {"endpoint": "https://m.example"})

upgrade()

conns = {(r[0], r[1]): json.loads(r[2]) for r in table("SELECT slot, name, settings FROM integration_connections")}
ck("the factory's choice became a connection with its settings",
   conns.get(("tickets", "tixa")) == {"url": "https://a.example/api", "token": SECRETS[3], "project": "FAC"},
   list(conns))
ck("the connection keeps who and when", table("SELECT updated_by FROM integration_connections WHERE slot = "
                                                "'tickets' AND name = 'tixa'") == [("old-ann",)])
ck("the choice kept only the pointer", {r[0]: (r[1], json.loads(r[2])) for r in table(
   "SELECT slot, name, settings FROM integration_choices")}.get("tickets") == ("tixa", {}))
ck("the project's secret and address moved to the factory connection of the same integration",
   conns.get(("tickets", "tixb")) == {"url": "https://own.example/api", "token": SECRETS[6]}, list(conns))
pj = {(r[0], r[1]): json.loads(r[2]) for r in table("SELECT project_uid, slot, settings FROM project_integrations")}
ck("the project kept its plain field only", pj[("p1", "tickets")] == {"project": "OWN"}, pj)
ck("a plain-only row sorting first keeps its field and does not take the credential with it",
   pj[("p00", "tickets")] == {"project": "ZERO"}, pj)
ck("a connection that already existed is not overwritten by a project's copy",
   conns.get(("review", "reva")) == {"token": SECRETS[2]} and pj[("p1", "review")] == {}, (conns.get(("review", "reva")), pj))
ck("an integration that is not installed has every key treated as a secret: both leave the project",
   pj[("p3", "ci")] == {} and conns.get(("ci", "gone_ci")) == {"url": "https://gone.example", "branch": "main"},
   (pj, conns.get(("ci", "gone_ci"))))
ck("a project row with only plain fields keeps them",
   pj[("p4", "tickets")] == {"project": "P4"} and pj[("p5", "ci")] == {"branch": "release"}, pj)
ck("a project row with only plain fields still gets an (empty) connection to stand on",
   conns.get(("ci", "cibranch")) == {} and m.is_connected("ci", "cibranch"), conns.get(("ci", "cibranch")))
from tares.integrations import resolve as _resolve
_e = _resolve.effective("ci", "p5", store=m)
ck("so the project keeps its pick instead of falling back as not connected",
   _e.integration == "cibranch" and _e.source == "project" and _e.reason != "not connected", _e)
ck("the guardrail's pair settings became its connection", conns.get(("guardrails", "rail_a"))
   == {"url": "https://g.example/", "token": SECRETS[4]})
pairs = table("SELECT id, hook, integration, on_fail, settings FROM guardrail_pairs ORDER BY id")
ck("no pair keeps a setting", all(json.loads(p[4]) == {} for p in pairs), pairs)
ck("pairs that were the same are one, the oldest stays; a different on_fail or hook is kept",
   [p[0] for p in pairs] == ["GP1", "GP3", "GP4"], pairs)
ck("monitoring has no connection row and its choice row is left as it was",
   not any(k[0] == "monitoring" for k in conns))
ck("a connection made from a project row is not the default",
   {r[0]: r[1] for r in table("SELECT slot, name FROM integration_choices")}.get("tickets") == "tixa")

before = (sorted(table("SELECT slot, name, settings, updated_by FROM integration_connections")),
          sorted(table("SELECT project_uid, slot, name, settings FROM project_integrations")),
          sorted(table("SELECT slot, name, settings FROM integration_choices")),
          sorted(table("SELECT id, hook, integration, on_fail, settings FROM guardrail_pairs")))
upgrade()
after = (sorted(table("SELECT slot, name, settings, updated_by FROM integration_connections")),
         sorted(table("SELECT project_uid, slot, name, settings FROM project_integrations")),
         sorted(table("SELECT slot, name, settings FROM integration_choices")),
         sorted(table("SELECT id, hook, integration, on_fail, settings FROM guardrail_pairs")))
ck("a second run changes nothing", before == after)
ck("what the migration made reads back through the store, redacted",
   m.get_connection("tickets", "tixa")["settings"]["token"] == "" and m.get_connection("tickets", "tixa")["default"] is True
   and SECRETS[3] not in json.dumps(m.list_connections("tickets"), default=str))
ck("the project still runs with the factory's connection and its own field",
   m.stored_settings("tickets", "tixb", "p1")
   == {"url": "https://own.example/api", "token": SECRETS[6], "project": "OWN"}, m.stored_settings("tickets", "tixb", "p1"))
# a pair added after the upgrade that carries settings from an old cell: the connection already there wins
old_pair("GP9", "factory", None, "pre_release", "rail_a", {"url": "https://other.example/", "token": SECRETS[7]})
upgrade()
ck("a pair's settings never replace an existing connection",
   m.stored_settings("guardrails", "rail_a") == {"url": "https://g.example/", "token": SECRETS[4]}
   and json.loads(table("SELECT settings FROM guardrail_pairs WHERE id = 'GP9'")[0][0]) == {})


# ── the API ────────────────────────────────────────────────────────────────────
ALL = []


def ru(r):
    ALL.append(r.text)
    return r


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def mk(token=TOKEN, station=None, role="builder"):
        hdr = {"Authorization": f"Bearer {token}"}
        if station:
            hdr.update({"X-Tares-Station": station, "X-Tares-Role": role})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=hdr)

    async with app.router.lifespan_context(app):
        ann = store.create_person("Ann", email="ann@example.com")
        store.create_project("p1", "custom", "alpha", {}, lead_person_id=ann["id"])
        async with mk() as root:
            ann_k = (await root.post("/api/keys", json={"name": "ann", "scopes": ["read", "ingest"],
                                                         "person_id": ann["id"]})).json()["secret"]
            print("== connecting several through the API ==")
            A = {"name": "tixa", "settings": {"url": "http://93.184.216.34/api", "token": SECRETS[0], "project": "A"}}
            B = {"name": "tixb", "settings": {"url": "http://93.184.216.40/api", "token": SECRETS[1], "project": "B"},
                 "default": False}
            r = ru(await root.put("/api/factory/integrations/tickets", json=A))
            ck("the first connection is the default", r.status_code == 200 and r.json()["choice"]["name"] == "tixa"
               and r.json()["effective"]["integration"] == "tixa", r.text)
            r = ru(await root.put("/api/factory/integrations/tickets", json=B))
            conns = {c["name"]: c for c in r.json()["connections"]}
            ck("default false adds a second and keeps the default", r.status_code == 200
               and set(conns) == {"tixa", "tixb"} and conns["tixa"]["default"] is True and conns["tixb"]["default"] is False
               and r.json()["effective"]["integration"] == "tixa", r.text)
            ck("each connection is {name, settings, secrets, updated_at, updated_by, default}, redacted",
               all({"name", "settings", "secrets", "updated_at", "updated_by", "default"} <= set(c) for c in conns.values())
               and conns["tixb"]["settings"]["token"] == "" and conns["tixb"]["secrets"] == {"token": {"has_value": True}}, conns)
            ck("the old keys are still there", {"slot", "available", "choice", "effective"} <= set(r.json()))
            r = ru(await root.put("/api/factory/integrations/tickets", json={**B, "default": "yes"}))
            ck("default must be true or false", r.status_code == 422 and "default" in r.text, r.text)
            lst = ru(await root.get("/api/factory/integrations")).json()["slots"]
            ck("the list carries connections for every slot", all("connections" in s for s in lst)
               and len(next(s for s in lst if s["slot"] == "tickets")["connections"]) == 2)
            ck("memory, monitoring and runtime list at most one",
               all(len(s["connections"]) <= 1 for s in lst if s["slot"] in ("memory", "monitoring", "runtime")))

            print("== the default route ==")
            r = ru(await root.put("/api/factory/integrations/tickets/default", json={"name": "tixb"}))
            ck("the default moves", r.status_code == 200 and r.json()["effective"]["integration"] == "tixb"
               and [c["name"] for c in r.json()["connections"] if c["default"]] == ["tixb"], r.text)
            r = ru(await root.put("/api/factory/integrations/tickets/default", json={"name": "tixc"}))
            ck("an integration that is not connected is 422 and says where to connect it", r.status_code == 422
               and "tixc is not connected" in r.text and "factory's integrations" in r.text, r.text)
            r = ru(await root.put("/api/factory/integrations/tickets/default", json={}))
            ck("no name is 422", r.status_code == 422, r.text)
            r = ru(await root.put("/api/factory/integrations/nope/default", json={"name": "x"}))
            ck("an unknown slot is 404", r.status_code == 404, r.text)

            print("== a project names a connected integration ==")
            async with mk(ann_k) as cx:
                r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={"name": "tixa", "settings": {"project": "PX"}}))
                ck("a connected integration and a plain field", r.status_code == 200
                   and r.json()["effective"]["source"] == "project" and r.json()["choice"]["settings"] == {"project": "PX"}, r.text)
                for label, st, needle in (("a secret key", {"token": SECRETS[6]}, "token is a secret"),
                                          ("an address key", {"url": "http://93.184.216.50/api"}, "url is an address")):
                    r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={"name": "tixa", "settings": st}))
                    ck(f"{label} is 422 and names the factory's integrations", r.status_code == 422
                       and needle in r.text and "factory's integrations" in r.text and SECRETS[6] not in r.text, r.text)
                r = ru(await cx.put("/api/projects/p1/integrations/tickets", json={"name": "tixc", "settings": {}}))
                ck("an integration that is not connected is 422", r.status_code == 422
                   and "tixc is not connected" in r.text, r.text)
                ck("the refused writes changed nothing", store.get_project_choice("p1", "tickets")["settings"] == {"project": "PX"})

            print("== deleting a connection through the API ==")
            r = ru(await root.delete("/api/factory/integrations/tickets/connections/tixb"))
            ck("the default is 422 and the message names it", r.status_code == 422 and "default for tickets" in r.text, r.text)
            r = ru(await root.delete("/api/factory/integrations/tickets/connections/tixa"))
            ck("one a project uses is 422 and names the project", r.status_code == 422 and "project" in r.text
               and store.get_project_choice("p1", "tickets")["name"] == "tixa", r.text)
            r = ru(await root.delete("/api/factory/integrations/tickets/connections/nothere"))
            ck("one that is not there is 404", r.status_code == 404 and "not connected" in r.text, r.text)
            r = ru(await root.delete("/api/factory/integrations/nope/connections/x"))
            ck("an unknown slot is 404", r.status_code == 404, r.text)
            await root.delete("/api/projects/p1/integrations/tickets")
            r = ru(await root.delete("/api/factory/integrations/tickets/connections/tixa"))
            ck("a free connection deletes and the entry comes back", r.status_code == 200 and r.json()["deleted"] is True
               and [c["name"] for c in r.json()["connections"]] == ["tixb"] and "slot" in r.json(), r.text)

            print("== a guardrail pair holds no integration settings ==")
            await root.put("/api/factory/integrations/guardrails", json={
                "name": "rail_a", "settings": {"url": "http://93.184.216.34/hook", "token": SECRETS[4]}})
            r = ru(await root.post("/api/factory/guardrails/pairs", json={
                "hook": "pre_build", "integration": "rail_a", "settings": {"token": SECRETS[7]}}))
            ck("a pair with integration settings is 422 and names the factory's integrations", r.status_code == 422
               and "integration settings" in r.text and "factory's integrations" in r.text and SECRETS[7] not in r.text, r.text)
            r = ru(await root.post("/api/factory/guardrails/pairs", json={"hook": "pre_build", "integration": "rail_a"}))
            ck("a pair with none is added and reads no settings", r.status_code == 201
               and r.json()["pair"]["settings"] == {} and r.json()["pair"]["secrets"] == {}, r.text)
            pid = r.json()["pair"]["id"]
            r = ru(await root.post("/api/factory/guardrails/pairs", json={"hook": "pre_build", "integration": "rail_b"}))
            ck("a pair for an integration that is not connected is 422", r.status_code == 422 and "not connected" in r.text, r.text)
            r = ru(await root.delete("/api/factory/integrations/guardrails/connections/rail_a"))
            ck("its connection cannot be deleted while the pair uses it (the default blocks it first, then the pair)",
               r.status_code == 422, r.text)
            await root.put("/api/factory/integrations/guardrails", json={
                "name": "rail_c", "settings": {"url": "http://93.184.216.40/hook", "token": SECRETS[5]}})
            r = ru(await root.delete("/api/factory/integrations/guardrails/connections/rail_a"))
            ck("not the default any more, the pair still blocks it and is named", r.status_code == 422 and pid in r.text, r.text)
            await root.delete(f"/api/factory/guardrails/pairs/{pid}")
            r = ru(await root.delete("/api/factory/integrations/guardrails/connections/rail_a"))
            ck("with the pair removed it deletes", r.status_code == 200, r.text)

            print("== who may ==")
            async with mk(ann_k) as cx:
                for label, call in (
                        ("connect", cx.put("/api/factory/integrations/tickets", json=A)),
                        ("set the default", cx.put("/api/factory/integrations/tickets/default", json={"name": "tixb"})),
                        ("delete a connection", cx.delete("/api/factory/integrations/tickets/connections/tixb"))):
                    r = ru(await call)
                    ck(f"a person's key without admin may not {label}", r.status_code == 403, r.status_code)
            async with mk(station="shop-build-x", role="builder") as cx:
                for label, call in (
                        ("connect", cx.put("/api/factory/integrations/tickets", json=A)),
                        ("set the default", cx.put("/api/factory/integrations/tickets/default", json={"name": "tixb"})),
                        ("delete a connection", cx.delete("/api/factory/integrations/tickets/connections/tixb")),
                        ("list connections", cx.get("/api/factory/integrations"))):
                    r = ru(await call)
                    ck(f"a crew station may not {label}", r.status_code == 403, r.status_code)
                r = ru(await cx.get("/api/factory/integrations/effective"))
                ck("a crew station may read the effective integrations", r.status_code == 200
                   and "connections" not in r.text, r.text[:200])
            ck("the default is as it was after the refusals", store.get_choice("tickets")["name"] == "tixb")

    print("== no secret in any response ==")
    blob = "\n".join(ALL)
    ck("no secret value appears in any response", not any(sv in blob for sv in SECRETS))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

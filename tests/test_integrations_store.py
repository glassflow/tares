"""Factory integration connections and a project's own fields (TR-839, TR-1380).

Covered: the factory's connection per integration and a project's own choice (tickets, review, ci
only) round trip; a read returns each secret as "" with a `secrets` map saying whether one is set;
a blank or missing secret on write keeps the stored value, JSON null clears it, a value replaces
it; each integration is its own connection, so no secret travels between two; an integration that
is not installed has every stored value redacted and keeps them on a blank write; a project row
holds only plain fields: a secret or an address raises ValueError, and the factory's connection
is what a project runs with; a project slot other than tickets, review or ci raises ValueError;
clear_project_choice reports whether a row existed; the stored JSON holds the secret itself
(like connector secrets), the API-facing reads never do.

Run: python tests/test_integrations_store.py
With CI's Postgres run: TARES_TEST_PG=<dsn> PYTHONPATH=tests/pg_harness python tests/test_integrations_store.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from tares.integrations import registry
from tares.integrations.base import Integration
from tares.integrations.tickets import TicketBackend
from tares.integrations.types import Field
from tares.store import Store


class FakeTickets(TicketBackend):
    name = "faketix"
    title = "Fake tickets"
    settings_schema = [Field("url", "URL", "url"), Field("token", "Token", "secret"),
                       Field("key", "Key", "secret"), Field("branch", "Branch", "text")]


class OtherTickets(TicketBackend):
    name = "othertix"
    title = "Other tickets"
    settings_schema = [Field("token", "Token", "secret")]


registry.load()
registry._loaded[("tickets", "faketix")] = FakeTickets
registry._loaded[("tickets", "othertix")] = OtherTickets

tmp = tempfile.mkdtemp(prefix="tares-integrations-store-")
s = Store(os.path.join(tmp, "t.duckdb"))


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError:
        return True
    return False


def stored(table, **where):
    """The JSON settings as stored, bypassing every redacting read."""
    cond = " AND ".join(f"{k} = ?" for k in where)
    with s._sread:
        r = s.sc.execute(f"SELECT settings FROM {table} WHERE {cond}", list(where.values())).fetchone()
    return json.loads(r[0]) if r else None


# ── the factory's choice ───────────────────────────────────────────────────────
ck("no choice reads None", s.get_choice("tickets") is None)
r = s.set_choice("tickets", "faketix", {"url": "x", "token": "tok-1"}, "ashish")
ck("set_choice returns the redacted view",
   r["settings"] == {"url": "x", "token": ""} and r["name"] == "faketix"
   and r["secrets"] == {"key": {"has_value": False}, "token": {"has_value": True}}
   and r["updated_by"] == "ashish" and r["updated_at"], r)
g = s.get_choice("tickets")
ck("get_choice matches the set view", g["settings"] == {"url": "x", "token": ""}
   and g["secrets"]["token"] == {"has_value": True}, g)
FT = dict(slot="tickets", name="faketix")
ck("the secret is in storage and in no read", stored("integration_connections", **FT)["token"] == "tok-1"
   and "tok-1" not in json.dumps(g, default=str))
ck("the choice row is only the pointer", stored("integration_choices", slot="tickets") == {})

s.set_choice("tickets", "faketix", {"url": "y", "token": ""}, "ashish")
ck("blank secret keeps the stored value", stored("integration_connections", **FT) == {"url": "y", "token": "tok-1"},
   stored("integration_connections", **FT))
s.set_choice("tickets", "faketix", {"url": "y"}, "ashish")
ck("missing secret keeps the stored value", stored("integration_connections", **FT)["token"] == "tok-1")
s.set_choice("tickets", "faketix", {"url": "y", "token": "tok-2"}, "ashish")
ck("a new value replaces it", stored("integration_connections", **FT)["token"] == "tok-2")
r = s.set_choice("tickets", "faketix", {"url": "y", "token": None}, "ashish")
ck("null clears it", "token" not in stored("integration_connections", **FT)
   and r["secrets"]["token"] == {"has_value": False}, stored("integration_connections", **FT))
s.set_choice("tickets", "faketix", {"token": "tok-3", "key": "k-1"}, "ashish")
s.set_choice("tickets", "othertix", {"token": ""}, "ashish")
ck("another integration starts from nothing: no secret travels",
   stored("integration_connections", slot="tickets", name="othertix") == {},
   stored("integration_connections", slot="tickets", name="othertix"))
ck("the first one stays connected with its own secrets",
   stored("integration_connections", **FT) == {"token": "tok-3", "key": "k-1"}, stored("integration_connections", **FT))
s.set_choice("tickets", "faketix", {"token": ""}, "ashish")
ck("choosing it again keeps its own secrets, not the other's",
   stored("integration_connections", **FT) == {"token": "tok-3", "key": "k-1"}
   and stored("integration_connections", slot="tickets", name="othertix") == {})

ck("a name is required", raises(s.set_choice, "tickets", " ", {}, "a"))
ck("an unknown slot raises", raises(s.set_choice, "nope", "x", {}, "a") and raises(s.get_choice, "nope"))
s.set_choice("memory", "anything", {"a": 1}, "ashish")
ck("the factory takes every slot", s.get_choice("memory")["name"] == "anything")

# ── an integration that is not installed ───────────────────────────────────────
s.set_choice("tickets", "gone", {"url": "u", "token": "t"}, "ashish")
g = s.get_choice("tickets")
ck("not installed: every value redacted", g["settings"] == {"url": "", "token": ""}
   and g["secrets"] == {"token": {"has_value": True}, "url": {"has_value": True}}, g)
s.set_choice("tickets", "gone", {"url": "", "token": ""}, "ashish")
GONE = dict(slot="tickets", name="gone")
ck("not installed: a blank write keeps every stored value",
   stored("integration_connections", **GONE) == {"url": "u", "token": "t"})
s.set_choice("tickets", "gone", {"url": None}, "ashish")
ck("not installed: null still clears one", stored("integration_connections", **GONE) == {"token": "t"})

# ── a project's own choice: plain fields only ─────────────────────────────────
s.set_choice("tickets", "faketix", {"url": "factory", "token": "factory-secret"}, "ashish")
ck("no project row reads None", s.get_project_choice("p1", "tickets") is None)
r = s.set_project_choice("p1", "tickets", "faketix", {"branch": "main"}, "ashish")
ck("a project row holds its plain fields only",
   stored("project_integrations", project_uid="p1", slot="tickets") == {"branch": "main"}
   and r["settings"] == {"branch": "main"} and r["project_uid"] == "p1", r)
ck("the factory connection is untouched", stored("integration_connections", **FT)["token"] == "factory-secret")
for bad in ({"token": "own"}, {"token": ""}, {"key": "k"}, {"url": "https://x.example"}, {"url": ""}):
    try:
        s.set_project_choice("p1", "tickets", "faketix", bad, "ashish")
        ck(f"a project refuses {sorted(bad)}", False, "accepted")
    except ValueError as e:
        ck(f"a project refuses {sorted(bad)}, naming the factory",
           "factory's integrations" in str(e) and "own" not in str(e), str(e))
ck("a refused write leaves the row as it was",
   stored("project_integrations", project_uid="p1", slot="tickets") == {"branch": "main"})
s.set_project_choice("p1", "tickets", "faketix", {"branch": "dev"}, "ashish")
g = s.get_project_choice("p1", "tickets")
ck("project read shows the plain field and no secret set",
   g["settings"] == {"branch": "dev"} and not any(v["has_value"] for v in g["secrets"].values()), g)
ck("another project has no row", s.get_project_choice("p2", "tickets") is None)
for slot in ("memory", "monitoring", "guardrails", "runtime", "bogus"):
    ck(f"project slot {slot} raises in all three methods",
       raises(s.set_project_choice, "p1", slot, "x", {}, "a") and raises(s.get_project_choice, "p1", slot)
       and raises(s.clear_project_choice, "p1", slot))
for slot in ("review", "ci"):
    s.set_project_choice("p1", slot, "x", {}, "ashish")
    ck(f"project slot {slot} is accepted", s.get_project_choice("p1", slot)["name"] == "x")
ck("an integration that is not installed takes no field: every key counts as a secret",
   raises(s.set_project_choice, "p1", "ci", "x", {"a": "b"}, "a"))
ck("clear reports a row", s.clear_project_choice("p1", "tickets") is True
   and s.get_project_choice("p1", "tickets") is None)
ck("clear on none reports False", s.clear_project_choice("p1", "tickets") is False)
ck("the factory default is still there after clear", s.get_choice("tickets")["name"] == "faketix")

# ── a project row: switching integration, empty name ───────────────────────────
s.set_project_choice("p3", "tickets", "faketix", {"branch": "b3"}, "ashish")
r = s.set_project_choice("p3", "tickets", "othertix", {}, "ashish")
ck("a project switching integration carries no field",
   stored("project_integrations", project_uid="p3", slot="tickets") == {} and r["settings"] == {}, r)
s.set_project_choice("p3", "tickets", "faketix", {}, "ashish")
ck("switching a project back does not bring the old field back",
   stored("project_integrations", project_uid="p3", slot="tickets") == {})
ck("a project name is required", raises(s.set_project_choice, "p3", "tickets", "", {}, "a")
   and raises(s.set_project_choice, "p3", "tickets", "  ", {}, "a")
   and raises(s.set_project_choice, "p3", "tickets", None, {}, "a"))
ck("a refused empty name leaves the row as it was", s.get_project_choice("p3", "tickets")["name"] == "faketix")

# ── settings that are not an object (F2) ───────────────────────────────────────
for bad in ([], ["a"], "text", 5, True):
    ck(f"settings {bad!r} is refused with the intended message",
       raises(s.set_choice, "tickets", "faketix", bad, "a")
       and raises(s.set_project_choice, "p3", "tickets", "faketix", bad, "a"))
try:
    s.set_choice("tickets", "faketix", ["a"], "a")
except ValueError as e:
    ck("the message says settings must be an object", "must be an object" in str(e), str(e))
s.set_choice("tickets", "othertix", {"token": ""}, "a")   # a row with nothing stored
ck("None settings means none", s.set_choice("tickets", "othertix", None, "a")["settings"] == {}
   and stored("integration_connections", slot="tickets", name="othertix") == {})

# ── a stored key the schema does not list reads as a secret (F3) ───────────────
s.set_choice("tickets", "faketix", {}, "a")
with s._swrite:
    s.sc.execute("UPDATE integration_connections SET settings = ? WHERE slot = ? AND name = ?",
                 [json.dumps({"url": "u", "token": "t", "legacy": "old-secret"}), "tickets", "faketix"])
g = s.get_choice("tickets")
ck("an unlisted stored key reads as a secret: value hidden, flag set",
   g["settings"] == {"url": "u", "token": "", "legacy": ""}
   and g["secrets"]["legacy"] == {"has_value": True} and "old-secret" not in json.dumps(g, default=str), g)
ck("a listed plain field still reads as itself", g["settings"]["url"] == "u")

# ── stored_settings: the connection, plus a project's own fields ──────────────
s.set_choice("tickets", "faketix", {"url": "u9", "token": "t9"}, "ashish")
ck("stored_settings reads the factory connection", s.stored_settings("tickets", "faketix") == {"url": "u9", "token": "t9"})
ck("stored_settings of another integration is its own connection", s.stored_settings("tickets", "othertix") == {})
ck("stored_settings of an integration never connected is empty", s.stored_settings("tickets", "nobody") == {})
s.set_project_choice("p3", "tickets", "faketix", {"branch": "b3"}, "ashish")
ck("stored_settings with a project is the connection overlaid with the project's fields",
   s.stored_settings("tickets", "faketix", "p3") == {"url": "u9", "token": "t9", "branch": "b3"}
   and s.stored_settings("tickets", "faketix", "p-none") == {"url": "u9", "token": "t9"})
s.set_project_choice("p3", "tickets", "othertix", {}, "ashish")
ck("a project that chose another integration adds nothing to this one",
   s.stored_settings("tickets", "faketix", "p3") == {"url": "u9", "token": "t9"})
with s._swrite:
    s.sc.execute("UPDATE project_integrations SET name = ?, settings = ? WHERE project_uid = ? AND slot = ?",
                 ["faketix", json.dumps({"branch": "b3", "token": "old-own", "url": "old-url"}), "p3", "tickets"])
ck("a secret or an address left in a project row from before is never read",
   s.stored_settings("tickets", "faketix", "p3") == {"url": "u9", "token": "t9", "branch": "b3"})
ck("stored_settings refuses a slot a project cannot have", raises(s.stored_settings, "memory", "x", "p3")
   and raises(s.stored_settings, "bogus", "x"))

# ── the registry is read before the write lock (F4) ────────────────────────────
seen = []
real = registry.get
def spy(slot, name):
    seen.append(s._swrite._is_owned() if hasattr(s._swrite, "_is_owned") else None)
    return real(slot, name)
registry.get = spy
try:
    s.set_choice("tickets", "faketix", {"url": "f4"}, "ashish")
    s.set_project_choice("p3", "tickets", "faketix", {"branch": "f4"}, "ashish")
    s.stored_settings("tickets", "faketix", "p3")
finally:
    registry.get = real
ck("the registry is read outside the state write lock", seen and not any(seen), seen)


# ── survives a reopen ──────────────────────────────────────────────────────────
path = os.path.join(tmp, "t.duckdb")
s2 = Store(path)
ck("rows survive a reopen", s2.get_choice("tickets")["name"] == "faketix"
   and s2.get_project_choice("p1", "ci")["name"] == "x")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

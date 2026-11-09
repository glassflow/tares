"""Factory integration choices and their integration settings (TR-839).

Covered: the factory's choice per slot and a project's own choice (tickets, review, ci only)
round trip; a read returns each secret as "" with a `secrets` map saying whether one is set; a
blank or missing secret on write keeps the stored value, JSON null clears it, a value replaces it;
choosing another integration keeps no secret of the old one; an integration that is not installed
has every stored value redacted and keeps them on a blank write; a project row never holds a value
taken from the factory row; a project slot other than tickets, review or ci raises ValueError;
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
                       Field("key", "Key", "secret")]


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
ck("the secret is in storage and in no read", stored("integration_choices", slot="tickets")["token"] == "tok-1"
   and "tok-1" not in json.dumps(g, default=str))

s.set_choice("tickets", "faketix", {"url": "y", "token": ""}, "ashish")
ck("blank secret keeps the stored value", stored("integration_choices", slot="tickets") == {"url": "y", "token": "tok-1"},
   stored("integration_choices", slot="tickets"))
s.set_choice("tickets", "faketix", {"url": "y"}, "ashish")
ck("missing secret keeps the stored value", stored("integration_choices", slot="tickets")["token"] == "tok-1")
s.set_choice("tickets", "faketix", {"url": "y", "token": "tok-2"}, "ashish")
ck("a new value replaces it", stored("integration_choices", slot="tickets")["token"] == "tok-2")
r = s.set_choice("tickets", "faketix", {"url": "y", "token": None}, "ashish")
ck("null clears it", "token" not in stored("integration_choices", slot="tickets")
   and r["secrets"]["token"] == {"has_value": False}, stored("integration_choices", slot="tickets"))
s.set_choice("tickets", "faketix", {"token": "tok-3", "key": "k-1"}, "ashish")
s.set_choice("tickets", "othertix", {"token": ""}, "ashish")
ck("another integration starts from nothing: no secret travels",
   stored("integration_choices", slot="tickets") == {}, stored("integration_choices", slot="tickets"))
s.set_choice("tickets", "faketix", {"token": ""}, "ashish")
ck("switching back does not bring the old secret back", stored("integration_choices", slot="tickets") == {})

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
ck("not installed: a blank write keeps every stored value",
   stored("integration_choices", slot="tickets") == {"url": "u", "token": "t"})
s.set_choice("tickets", "gone", {"url": None}, "ashish")
ck("not installed: null still clears one", stored("integration_choices", slot="tickets") == {"token": "t"})

# ── a project's own choice ─────────────────────────────────────────────────────
s.set_choice("tickets", "faketix", {"url": "factory", "token": "factory-secret"}, "ashish")
ck("no project row reads None", s.get_project_choice("p1", "tickets") is None)
r = s.set_project_choice("p1", "tickets", "faketix", {"url": "proj", "token": ""}, "ashish")
ck("a blank secret in a project row does not pick up the factory's",
   stored("project_integrations", project_uid="p1", slot="tickets") == {"url": "proj"}
   and r["settings"] == {"url": "proj"} and r["secrets"]["token"] == {"has_value": False}
   and r["project_uid"] == "p1", r)
ck("the factory row is untouched", stored("integration_choices", slot="tickets")["token"] == "factory-secret")
s.set_project_choice("p1", "tickets", "faketix", {"url": "proj", "token": "own"}, "ashish")
s.set_project_choice("p1", "tickets", "faketix", {"url": "proj2", "token": ""}, "ashish")
ck("a project keeps its own secret on a blank write",
   stored("project_integrations", project_uid="p1", slot="tickets") == {"url": "proj2", "token": "own"})
g = s.get_project_choice("p1", "tickets")
ck("project read is redacted", g["settings"] == {"url": "proj2", "token": ""}
   and g["secrets"]["token"] == {"has_value": True} and "own" not in json.dumps(g, default=str), g)
ck("another project has no row", s.get_project_choice("p2", "tickets") is None)
for slot in ("memory", "monitoring", "guardrails", "runtime", "bogus"):
    ck(f"project slot {slot} raises in all three methods",
       raises(s.set_project_choice, "p1", slot, "x", {}, "a") and raises(s.get_project_choice, "p1", slot)
       and raises(s.clear_project_choice, "p1", slot))
for slot in ("review", "ci"):
    s.set_project_choice("p1", slot, "x", {"a": "b"}, "ashish")
    ck(f"project slot {slot} is accepted", s.get_project_choice("p1", slot)["name"] == "x")
ck("clear reports a row", s.clear_project_choice("p1", "tickets") is True
   and s.get_project_choice("p1", "tickets") is None)
ck("clear on none reports False", s.clear_project_choice("p1", "tickets") is False)
ck("the factory row is still there after clear", s.get_choice("tickets")["name"] == "faketix")

# ── a project row: switching integration, empty name (review of TR-839, F1) ────
s.set_project_choice("p3", "tickets", "faketix", {"url": "u3", "token": "p3-secret", "key": "p3-key"}, "ashish")
r = s.set_project_choice("p3", "tickets", "othertix", {"token": ""}, "ashish")
ck("a project switching integration carries no secret",
   stored("project_integrations", project_uid="p3", slot="tickets") == {}
   and r["secrets"] == {"token": {"has_value": False}} and "p3-secret" not in json.dumps(r, default=str), r)
s.set_project_choice("p3", "tickets", "faketix", {"token": ""}, "ashish")
ck("switching a project back does not bring the old secret back",
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
   and stored("integration_choices", slot="tickets") == {})

# ── a stored key the schema does not list reads as a secret (F3) ───────────────
s.set_choice("tickets", "faketix", {}, "a")
with s._swrite:
    s.sc.execute("UPDATE integration_choices SET settings = ? WHERE slot = ?",
                 [json.dumps({"url": "u", "token": "t", "legacy": "old-secret"}), "tickets"])
g = s.get_choice("tickets")
ck("an unlisted stored key reads as a secret: value hidden, flag set",
   g["settings"] == {"url": "u", "token": "", "legacy": ""}
   and g["secrets"]["legacy"] == {"has_value": True} and "old-secret" not in json.dumps(g, default=str), g)
ck("a listed plain field still reads as itself", g["settings"]["url"] == "u")

# ── stored_settings: the raw row, same integration only ────────────────────────
s.set_choice("tickets", "faketix", {"url": "u9", "token": "t9"}, "ashish")
ck("stored_settings reads the factory row", s.stored_settings("tickets", "faketix") == {"url": "u9", "token": "t9"})
ck("stored_settings of another integration is empty", s.stored_settings("tickets", "othertix") == {})
s.set_project_choice("p3", "tickets", "faketix", {"url": "p3u", "token": "p3t"}, "ashish")
ck("stored_settings with a project reads the project row, never the factory's",
   s.stored_settings("tickets", "faketix", "p3") == {"url": "p3u", "token": "p3t"}
   and s.stored_settings("tickets", "faketix", "p-none") == {})
ck("stored_settings refuses a slot a project cannot have", raises(s.stored_settings, "memory", "x", "p3")
   and raises(s.stored_settings, "bogus", "x"))

# ── the registry is read before the write lock (F4) ────────────────────────────
import threading
seen = []
real = s._secret_fields
def spy(slot, name):
    seen.append(s._swrite._is_owned() if hasattr(s._swrite, "_is_owned") else None)
    return real(slot, name)
s._secret_fields = spy
s.set_choice("tickets", "faketix", {"url": "f4"}, "ashish")
s.set_project_choice("p3", "tickets", "faketix", {"url": "f4"}, "ashish")
s._secret_fields = real
ck("_secret_fields runs outside the state write lock", seen and not any(seen), seen)


# ── survives a reopen ──────────────────────────────────────────────────────────
path = os.path.join(tmp, "t.duckdb")
s2 = Store(path)
ck("rows survive a reopen", s2.get_choice("tickets")["name"] == "faketix"
   and s2.get_project_choice("p1", "ci")["name"] == "x")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

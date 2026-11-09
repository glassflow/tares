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
    ck(f"project slot {slot} raises in all four methods",
       raises(s.set_project_choice, "p1", slot, "x", {}, "a") and raises(s.get_project_choice, "p1", slot)
       and raises(s.clear_project_choice, "p1", slot))
for slot in ("review", "ci"):
    s.set_project_choice("p1", slot, "x", {"a": "b"}, "ashish")
    ck(f"project slot {slot} is accepted", s.get_project_choice("p1", slot)["name"] == "x")
ck("clear reports a row", s.clear_project_choice("p1", "tickets") is True
   and s.get_project_choice("p1", "tickets") is None)
ck("clear on none reports False", s.clear_project_choice("p1", "tickets") is False)
ck("the factory row is still there after clear", s.get_choice("tickets")["name"] == "faketix")

# ── survives a reopen ──────────────────────────────────────────────────────────
path = os.path.join(tmp, "t.duckdb")
s2 = Store(path)
ck("rows survive a reopen", s2.get_choice("tickets")["name"] == "faketix"
   and s2.get_project_choice("p1", "ci")["name"] == "x")

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

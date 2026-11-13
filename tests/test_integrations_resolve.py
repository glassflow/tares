"""Which integration a slot uses (TR-840).

Covered: every source of `effective` (project, factory, carried, default) and the order between
them; the built-in default of each of the seven slots; a project override only for tickets, review
and ci; a project override's settings are its own stored row and never hold a factory value, even
when the project row leaves a field blank; a stored name that is not installed resolves to the
slot's default with reason "not installed"; an ee integration without a licence resolves to the
default with reason "needs an enterprise licence" and with one resolves to itself; a bad project
choice goes straight to the default, not to the factory's; a stored choice never raises; secrets
read as "" (as the store reads them); an unknown slot raises ValueError.

Run: python tests/test_integrations_resolve.py
With CI's Postgres run: TARES_TEST_PG=<dsn> PYTHONPATH=tests/pg_harness python tests/test_integrations_resolve.py
"""
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


from tares import licence
from tares.integrations import registry, resolve
from tares.integrations.resolve import DEFAULTS, Effective, effective
from tares.integrations.ci import CISource
from tares.integrations.review import ReviewGate
from tares.integrations.tickets import TicketBackend
from tares.integrations.types import Field
from tares.store import Store


class FakeTickets(TicketBackend):
    name = "faketix"
    title = "Fake tickets"
    settings_schema = [Field("url", "URL", "url"), Field("token", "Token", "secret")]


class FakeReview(ReviewGate):
    name = "fakegate"
    title = "Fake review gate"
    settings_schema = [Field("url", "URL", "url"), Field("token", "Token", "secret")]


class FakeCI(CISource):
    name = "fakeci"
    title = "Fake CI"
    settings_schema = [Field("url", "URL", "url"), Field("token", "Token", "secret")]


class PaidTickets(TicketBackend):
    name = "paidtix"
    title = "Paid tickets"
    ee = True
    settings_schema = [Field("token", "Token", "secret")]


registry.load()
registry._loaded[("tickets", "faketix")] = FakeTickets
registry._loaded[("tickets", "paidtix")] = PaidTickets
registry._loaded[("review", "fakegate")] = FakeReview
registry._loaded[("ci", "fakeci")] = FakeCI

licensed = False
licence.ee_allowed = lambda feature="enterprise": licensed

tmp = tempfile.mkdtemp(prefix="tares-integrations-resolve-")
s = Store(os.path.join(tmp, "t.duckdb"))


def eff(slot, uid=None):
    return effective(slot, uid, store=s)


# ── default ────────────────────────────────────────────────────────────────────
ck("the defaults are the agreed ones", DEFAULTS == {
    "tickets": "tares", "review": "tares", "ci": "local", "memory": "tares",
    "monitoring": "off", "guardrails": "tares", "runtime": "claude_code"}, DEFAULTS)
for slot, name in DEFAULTS.items():
    e = eff(slot)
    ck(f"nothing stored: {slot} is the default", e == Effective(name, {}, "default", ""), e)
ck("a project with nothing stored gets the default too", eff("ci", "p1").source == "default")

# ── factory ────────────────────────────────────────────────────────────────────
s.set_choice("tickets", "faketix", {"url": "factory-url", "token": "factory-secret"}, "a")
e = eff("tickets")
ck("factory choice", e.integration == "faketix" and e.source == "factory" and e.reason == ""
   and e.settings == {"url": "factory-url", "token": ""}, e)
ck("a project without its own row follows the factory", eff("tickets", "p1") == e, eff("tickets", "p1"))
s.set_choice("memory", "anything", {"a": 1}, "a")
ck("a name not installed in a slot with no override: default, not installed",
   eff("memory") == Effective("tares", {}, "default", "not installed"), eff("memory"))

# ── project ────────────────────────────────────────────────────────────────────
s.set_project_choice("p1", "tickets", "faketix", {"url": "proj-url"}, "a")
e = eff("tickets", "p1")
ck("project override", e.integration == "faketix" and e.source == "project" and e.reason == "", e)
ck("a project's settings are only its own row, no factory value",
   e.settings == {"url": "proj-url"} and "factory-secret" not in repr(e) and "factory-url" not in repr(e), e)
ck("the factory's view is unchanged", eff("tickets").settings == {"url": "factory-url", "token": ""})
ck("another project does not see p1's row", eff("tickets", "p2").source == "factory")
ck("no project given: the factory's choice", eff("tickets", None).source == "factory")
ck("the project beats the factory even when names differ",
   (s.set_choice("tickets", "othername", {}, "a"), eff("tickets", "p1").source)[1] == "project")
s.set_choice("tickets", "faketix", {"url": "factory-url", "token": "factory-secret"}, "a")

# a slot a project cannot override (memory) ignores the project id
ck("memory has no project override", eff("memory", "p1") == eff("memory"))

# ── carried ────────────────────────────────────────────────────────────────────
real_carried = resolve.carried
ck("carried finds nothing for a project with no links (tests/test_integrations_carry.py covers the rest)",
   real_carried("tickets", "p1", s) is None and real_carried("monitoring", None, s) is None)
s.clear_project_choice("p1", "tickets")
resolve.carried = lambda slot, uid, store=None: ("faketix", {"url": "linked"}) if slot == "review" else None
registry._loaded[("review", "faketix")] = FakeTickets   # the class is only looked up by slot and name
e = eff("review", "p1")
ck("carried is used when nothing is chosen", e == Effective("faketix", {"url": "linked"}, "carried", ""), e)
s.set_choice("review", "faketix", {"url": "chosen"}, "a")
ck("the factory beats carried", eff("review", "p1").source == "factory")
resolve.carried = lambda slot, uid, store=None: ("gone", {}) if slot == "ci" else None
ck("a carried name that is not installed: default, not installed",
   eff("ci", "p1") == Effective("local", {}, "default", "not installed"), eff("ci", "p1"))
resolve.carried = real_carried
del registry._loaded[("review", "faketix")]

# every project slot overrides the same way: review and ci as well as tickets
for slot, name in (("review", "fakegate"), ("ci", "fakeci")):
    s.set_choice(slot, name, {"url": f"{slot}-factory-url", "token": f"{slot}-factory-secret"}, "a")
    ck(f"{slot}: a project without a row follows the factory", eff(slot, "p5").source == "factory")
    s.set_project_choice("p5", slot, name, {"url": f"{slot}-proj-url"}, "a")
    e = eff(slot, "p5")
    ck(f"{slot}: project override", e.integration == name and e.source == "project" and e.reason == "", e)
    ck(f"{slot}: the settings are only the project's row, no factory value",
       e.settings == {"url": f"{slot}-proj-url"} and "factory" not in repr(e), e)
    ck(f"{slot}: another project still follows the factory", eff(slot, "p6").source == "factory")
    s.clear_project_choice("p5", slot)
    e = eff(slot, "p5")
    ck(f"{slot}: after clearing, the factory's choice is back",
       e.source == "factory" and e.settings == {"url": f"{slot}-factory-url", "token": ""}, e)

# ── not installed ──────────────────────────────────────────────────────────────
s.set_project_choice("p3", "tickets", "gone", {"url": "x", "token": "y"}, "a")
e = eff("tickets", "p3")
ck("project name not installed: default, not installed, and no stored value leaks",
   e == Effective("tares", {}, "default", "not installed"), e)
ck("a bad project choice does not fall back to the factory's", e.source == "default")
s.set_choice("tickets", "gone", {}, "a")
e = eff("tickets")
ck("factory name not installed: default, not installed", e == Effective("tares", {}, "default", "not installed"), e)
s.set_choice("tickets", "faketix", {"url": "factory-url", "token": "factory-secret"}, "a")

# ── ee ─────────────────────────────────────────────────────────────────────────
s.set_choice("tickets", "paidtix", {"token": "t"}, "a")
e = eff("tickets")
ck("ee without a licence: default, needs an enterprise licence",
   e == Effective("tares", {}, "default", "needs an enterprise licence"), e)
s.set_project_choice("p4", "tickets", "paidtix", {"token": "t"}, "a")
ck("ee project override without a licence: default too",
   eff("tickets", "p4") == Effective("tares", {}, "default", "needs an enterprise licence"), eff("tickets", "p4"))
licensed = True
e = eff("tickets")
ck("ee with a licence resolves to itself", e.integration == "paidtix" and e.source == "factory" and e.reason == "", e)
ck("ee project override with a licence", eff("tickets", "p4").source == "project")
licensed = False
ck("the licence is read on each call, so losing it falls back at once",
   eff("tickets", "p4").reason == "needs an enterprise licence")
ck("a class that is not ee needs no licence", (s.set_choice("tickets", "faketix", {}, "a"), eff("tickets").source)[1] == "factory")

# ── never raises, bad input ────────────────────────────────────────────────────
s.set_choice("tickets", "x" * 300, {}, "a")
ck("an odd stored name does not raise", eff("tickets").reason == "not installed")
try:
    eff("nope")
    ck("an unknown slot raises ValueError", False)
except ValueError:
    ck("an unknown slot raises ValueError", True)
e = Effective("a", {"k": 1}, "factory")
try:
    e.integration = "b"
    ck("Effective is frozen", False)
except Exception:
    ck("Effective is frozen", True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

"""The integrations package (TR-838): types and slot interfaces import, and the registry finds
integrations from BUILTINS and from the `tares.integrations` entry point group.

Covered: every slot has an interface that subclasses Integration; the types hold their fields;
a built-in and an entry point integration are both returned; a broken entry point, a class of the
wrong slot and a class that is not an integration are skipped with a log line and never raise; the
same class from both sources appears once with no log line; a different class on a taken (slot,
name) is skipped with one log line naming both, and the first loaded (BUILTINS) wins. The fake
entry point is a temporary *.dist-info folder on sys.path.

Run: python tests/test_integrations_registry.py
"""
import asyncio, logging, os, sys, tempfile, textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import scrub_os_environ
scrub_os_environ()

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


from tares.integrations import registry, types
from tares.integrations.base import Integration
from tares.integrations.tickets import TicketBackend
from tares.integrations.review import ReviewGate
from tares.integrations.ci import CISource
from tares.integrations.memory import MemoryStore
from tares.integrations.monitoring import Monitoring
from tares.integrations.guardrails import Guardrail
from tares.integrations.runtime import Runtime

# ── interfaces and types ───────────────────────────────────────────────────────
SEVEN = ("tickets", "review", "ci", "memory", "monitoring", "guardrails", "runtime")
ck("seven slots in the registry", registry.SLOTS == SEVEN, registry.SLOTS)
for slot, base in registry.SLOT_BASES.items():
    ck(f"{slot}: interface subclasses Integration and names its slot",
       issubclass(base, Integration) and base.slot == slot)

pr = types.PRRef("o/r", 3, "abc")
ck("PRRef defaults", (pr.repo, pr.number, pr.head, pr.base, pr.url, pr.ticket, pr.project_uid)
   == ("o/r", 3, "abc", "", "", "", ""))
ck("Verdict findings default is its own list",
   types.Verdict("none").findings == [] and types.Verdict("none").findings is not types.Verdict("none").findings)
ck("CIState, Decision, LaunchSpec, TicketRef fields",
   types.CIState("success").failing == [] and types.Decision(True).warn_only is False
   and types.LaunchSpec().command == [] and types.TicketRef("x").status == "")
ck("Status has the four stages", [s.name for s in types.Status] == ["todo", "in_progress", "in_review", "done"])
ck("Field kinds are the six", types.FIELD_KINDS == ("text", "secret", "url", "select", "bool", "mapping"))

async def raises(coro):
    try:
        await coro
    except NotImplementedError:
        return True
    return False

def run(coro):
    return asyncio.run(coro)

ck("async methods raise NotImplementedError", all(run(raises(c)) for c in [
    Integration().check_connection({}), TicketBackend().sync(None),
    TicketBackend().set_status(None, types.Status.done), TicketBackend().create(None, [], []),
    TicketBackend().update(None, {}), TicketBackend().link(None, {}),
    ReviewGate().verdict(pr), ReviewGate().findings(pr), CISource().state(pr, []),
    MemoryStore().add("s", "l"), MemoryStore().recall("s", "q"), Guardrail().check("pre_merge", {})]))
ck("the two monitoring methods and launch_spec are plain functions", not asyncio.iscoroutinefunction(Monitoring.exporter_config)
   and not asyncio.iscoroutinefunction(Monitoring.station_env)
   and not asyncio.iscoroutinefunction(Runtime.launch_spec))
for label, fn in (("exporter_config", lambda: Monitoring().exporter_config()),
                  ("station_env", lambda: Monitoring().station_env({})),
                  ("launch_spec", lambda: Runtime().launch_spec("builder", "s", {}))):
    try:
        fn(); ok = False
    except NotImplementedError:
        ok = True
    ck(f"{label} raises NotImplementedError", ok)

# ── the registry ───────────────────────────────────────────────────────────────
class Capture(logging.Handler):
    def __init__(self):
        super().__init__(); self.lines = []
    def emit(self, record):
        self.lines.append(record.getMessage())

cap = Capture()
logging.getLogger("tares.integrations").addHandler(cap)
logging.getLogger("tares.integrations").setLevel(logging.DEBUG)
logging.getLogger("tares.integrations").propagate = False

MODULE = textwrap.dedent('''
    from tares.integrations.ci import CISource
    from tares.integrations.memory import MemoryStore
    from tares.integrations.review import ReviewGate
    from tares.integrations.base import Integration

    class FakeCI(CISource):
        name = "fake"
        title = "Fake CI"

    class FakeMemory(MemoryStore):
        name = "fake"
        title = "Fake memory"

    class ClashCI(CISource):          # same (slot, name) as FakeCI, a different class
        name = "fake"

    class WrongSlot(Integration):     # an Integration that is not any slot's interface
        slot = "ci"
        name = "wrong"

    class MislabelledReview(CISource):  # says review, builds on ci
        slot = "review"
        name = "mislabelled"

    class Nameless(ReviewGate):
        pass

    NotAnIntegration = object
''')

tmp = tempfile.mkdtemp(prefix="tares-integ-")
with open(os.path.join(tmp, "fake_integ_mod.py"), "w") as f:
    f.write(MODULE)

def dist(name, points):
    d = os.path.join(tmp, f"{name}-0.0.dist-info")
    os.makedirs(d)
    with open(os.path.join(d, "METADATA"), "w") as f:
        f.write(f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0\n")
    with open(os.path.join(d, "entry_points.txt"), "w") as f:
        f.write("[tares.integrations]\n" + "".join(f"{k} = {v}\n" for k, v in points.items()))

sys.path.insert(0, tmp)

# The registry reads entry points through `metadata.entry_points`, which scans all of sys.path, so
# the entry points of an installed Tares (tickets.tares, tickets.linear) would join every case.
# The cases see only the dist-info folders of their own temporary folder.
import importlib.metadata, types as _types
def _only_tmp(group):
    return [ep for d in importlib.metadata.distributions(path=[tmp])
            for ep in d.entry_points if ep.group == group]
registry.metadata = _types.SimpleNamespace(entry_points=_only_tmp)

def fresh(builtins, points):
    """A clean registry for one case: the dist-info set is rewritten and the logs are emptied."""
    for entry in os.listdir(tmp):
        if entry.endswith(".dist-info"):
            for sub in os.listdir(os.path.join(tmp, entry)):
                os.remove(os.path.join(tmp, entry, sub))
            os.rmdir(os.path.join(tmp, entry))
    if points:
        dist("fakeinteg", points)
    registry.BUILTINS[:] = builtins
    cap.lines.clear()
    registry.load(refresh=True)

import fake_integ_mod as M

# built-ins and an entry point integration both come back
fresh(["fake_integ_mod:FakeMemory"], {"fake-ci": "fake_integ_mod:FakeCI"})
ck("a built-in is returned", registry.get("memory", "fake") is M.FakeMemory)
ck("an entry point integration is returned", registry.get("ci", "fake") is M.FakeCI)
ck("available(slot) lists the slot's classes", registry.available("ci") == [M.FakeCI]
   and registry.available("memory") == [M.FakeMemory])
ck("available of an empty or unknown slot is []", registry.available("tickets") == []
   and registry.available("nope") == [])
ck("get of a missing name is None", registry.get("ci", "missing") is None)
ck("a clean load logs nothing", cap.lines == [], cap.lines)

# a broken entry point is skipped with a log line, and the good one still loads
fresh([], {"broken": "no_such_module_xyz:Nope", "good": "fake_integ_mod:FakeCI",
           "missing-attr": "fake_integ_mod:NoSuchClass"})
ck("a broken entry point does not stop the good one", registry.get("ci", "fake") is M.FakeCI)
ck("each broken entry point is logged once",
   sum("broken" in l for l in cap.lines) == 1 and sum("missing-attr" in l for l in cap.lines) == 1, cap.lines)
ck("the log line says the cause", any("no_such_module_xyz" in l or "ModuleNotFoundError" in l for l in cap.lines), cap.lines)

# the same class from BUILTINS and from an entry point counts once, silently
fresh(["fake_integ_mod:FakeCI"], {"fake-ci": "fake_integ_mod:FakeCI"})
ck("the same class from both sources appears once", registry.available("ci") == [M.FakeCI])
ck("and logs nothing", cap.lines == [], cap.lines)

# a different class on a taken (slot, name): the first loaded wins, one line names both
fresh(["fake_integ_mod:FakeCI"], {"clash": "fake_integ_mod:ClashCI"})
ck("BUILTINS win over an entry point", registry.get("ci", "fake") is M.FakeCI)
ck("the clash is logged once, naming both", len(cap.lines) == 1
   and "FakeCI" in cap.lines[0] and "ClashCI" in cap.lines[0], cap.lines)
ck("the clashing class is not listed", registry.available("ci") == [M.FakeCI])

fresh([], {"clash": "fake_integ_mod:ClashCI", "fake-ci": "fake_integ_mod:FakeCI"})
ck("between two entry points the first loaded wins and the other is logged",
   registry.get("ci", "fake") in (M.FakeCI, M.ClashCI) and len(cap.lines) == 1, cap.lines)

# a class that is not an integration of its slot is skipped with a line
fresh(["fake_integ_mod:WrongSlot", "fake_integ_mod:MislabelledReview",
       "fake_integ_mod:Nameless", "fake_integ_mod:NotAnIntegration"], {})
ck("wrong-slot, mislabelled, nameless and non-integration classes are all skipped",
   all(registry.available(s) == [] for s in SEVEN), {s: registry.available(s) for s in SEVEN})
ck("each skip is logged", len(cap.lines) == 4, cap.lines)

# a built-in path that does not import is logged, not raised
fresh(["no_such_module_xyz:Nope"], {})
ck("a broken built-in path is logged and skipped", len(cap.lines) == 1 and registry.available("ci") == [], cap.lines)

# the cache holds until refreshed
fresh(["fake_integ_mod:FakeCI"], {})
registry.BUILTINS[:] = []
ck("load is cached until refresh", registry.get("ci", "fake") is M.FakeCI)
registry.load(refresh=True)
ck("refresh looks again", registry.get("ci", "fake") is None)

registry.BUILTINS[:] = []
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

"""idle_wake is on unless someone set it to false, on both sides.

The cell (`tares/factory.py` crew_effective, `tares/factory_api.py` crew_settings) serves true for a setting
nobody set, and a person's own layer never covers the team's false with a default they did not choose. The
crew's client (`tares/crew/client.py`) reads a null or missing idle_wake as on too. A saved false stays false.

Run: python tests/test_crew_wake_default.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tares import factory, factory_api
from tares.crew import client as crew_client

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class Store:
    """The two calls crew_settings makes: get_setting by key."""
    def __init__(self, **kv):
        self.kv = {k: json.dumps(v) for k, v in kv.items()}

    def get_setting(self, key):
        return self.kv.get(key)


PERSON = {"id": "p1"}
TEAM = factory_api.CREW_SETTING
MINE = f"{TEAM}:p1"

print("== the cell ==")
ck("a cell with nothing saved serves idle_wake on", factory_api.crew_settings(Store())["idle_wake"] is True)
ck("the stored layer still says unset: nothing is written on the person's behalf",
   factory_api.stored_crew_setting(Store(), TEAM)["idle_wake"] is None)
ck("a saved null is on too", factory_api.crew_settings(Store(**{TEAM: {"idle_wake": None}}))["idle_wake"] is True)
ck("a saved false stays false", factory_api.crew_settings(Store(**{TEAM: {"idle_wake": False}}))["idle_wake"] is False)
ck("a saved true stays true", factory_api.crew_settings(Store(**{TEAM: {"idle_wake": True}}))["idle_wake"] is True)
ck("the team's false is not covered by a person who never set it",
   factory_api.crew_settings(Store(**{TEAM: {"idle_wake": False}, MINE: {"autonomy": "L3-review"}}),
                             PERSON)["idle_wake"] is False)
ck("a person's own false beats the team's true",
   factory_api.crew_settings(Store(**{TEAM: {"idle_wake": True}, MINE: {"idle_wake": False}}),
                             PERSON)["idle_wake"] is False)
ck("a person with nothing set follows the default: on", factory_api.crew_settings(Store(), PERSON)["idle_wake"] is True)
ck("the home card's fallback says on", factory.crew_effective(factory.CREW_DEFAULTS)["idle_wake"] is True)
ck("crew_effective leaves the other settings as they are",
   factory.crew_effective({"autonomy": None, "pace": None})["pace"] is None
   and factory.crew_effective({"idle_wake": False})["idle_wake"] is False)
ck("saving another setting does not write idle_wake", factory.crew_settings_ok({"builders_max": 3}, {})["idle_wake"] is None)
ck("clear puts idle_wake back to unset (on)",
   factory.crew_settings_ok({"clear": ["idle_wake"]}, {"idle_wake": False})["idle_wake"] is None)

print("== the crew's client ==")
ck("its defaults have idle_wake on", crew_client.settings_defaults()["idle_wake"] is True)


class Raw(crew_client.TaresClient):
    def __init__(self, raw):
        super().__init__(env={})
        self._raw = raw

    def settings_raw(self):
        return self._raw


for label, raw, want in (("missing", {}, True), ("null", {"idle_wake": None}, True),
                         ("true", {"idle_wake": True}, True), ("false", {"idle_wake": False}, False)):
    ck(f"idle_wake {label} reads as {'on' if want else 'off'}", Raw(raw).crew_settings()["idle_wake"] is want)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

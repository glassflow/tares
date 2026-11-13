"""What is left of the crew's rules helpers (TR-864, tares/crew/rules.py) now that a start takes its whole launch,
rules included, from the cell (TR-870; tests/test_crew_launch_spec.py covers that).

Covered: `fetch` asks Tares for a station's rules and names the station and its role (a person's key alone is
refused), a cell too old to have the route is refused with the upgrade message; `saved` and `usable` read the
rules a station last started with from its saved launch spec and treat a spec that is junk, or holds no rules,
or lost its deny side as none; `shipped` renders the defaults the plugin ships and is an error when the file is
missing or empty; `added` and the watch's regrant prompt quote the rules that are new since that start.

A fake Tares of tests/factory/launch_support.py answers; nothing of the person's is read or written.

Run: python tests/test_crew_rules.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-rules-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.update(HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")
(SCRATCH / "home").mkdir()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

from tares.crew import launch as L  # noqa: E402
from tares.crew import regrant, rules  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
KIT = str(ROOT / "claude-plugin")
GRANT = '- "the releaser may restart the staging database" (2026-01-02)\n'


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


_n = [0]


def crew_with(world):
    """A fresh crew home and a client of a fake Tares with this world; returns (tares, client)."""
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {})
    tares = LS.FakeTares(world)
    tares.__enter__()
    os.environ["TARESD_URL"] = tares.url
    return tares, TaresClient(CrewPaths.at(tmp / "crew"), env=dict(os.environ))


def raises(fn):
    try:
        fn()
    except CrewError as e:
        return str(e)
    return None


print("the rules a station last started with")


def spec_with(allow, soft_deny, station="crew-reviewer"):
    """A crew home whose saved launch spec for `station` has these rule sentences; returns its paths."""
    tares, c = crew_with({})
    tares.__exit__(None, None, None)
    c.paths.launch_dir.mkdir(parents=True, exist_ok=True)
    spec = {"command": [], "env": {}, "settings": {"autoMode": {"environment": ["$defaults", "env line"],
            "allow": ["$defaults", *allow], "soft_deny": ["$defaults", *soft_deny]}}}
    c.paths.launch_file(station).write_text(json.dumps(spec), encoding="utf-8")
    return c.paths


got = rules.saved(spec_with(["Kept for {CREW_HOME}."], ["Kept deny."]), "crew-reviewer", {"CREW_HOME": "/crew"})
ck("saved reads the sentences of the saved spec, $defaults left out and the machine's placeholders filled",
   got == {"allow": ["Kept for /crew."], "soft_deny": ["Kept deny."]}, str(got))
moved = spec_with(["Reads Tares at http://old:1."], ["Kept deny."])
spec_doc = json.loads(moved.launch_file("crew-reviewer").read_text(encoding="utf-8"))
spec_doc["env"] = {"TARESD_URL": "http://old:1"}
moved.launch_file("crew-reviewer").write_text(json.dumps(spec_doc), encoding="utf-8")
got_moved = rules.saved(moved, "crew-reviewer", {}, "http://new:2")
ck("a Tares that moved is named as it is now, so its sentence is not mistaken for a new one",
   got_moved == {"allow": ["Reads Tares at http://new:2."], "soft_deny": ["Kept deny."]}, str(got_moved))
ck("a station with no saved spec has none", rules.saved(spec_with(["a"], ["b"]), "crew-releaser") is None)
junk = spec_with(["a"], ["b"])
junk.launch_file("crew-reviewer").write_text("not json {", encoding="utf-8")
ck("a spec that is not JSON counts as none", rules.saved(junk, "crew-reviewer") is None)
ck("a spec with no rules in it counts as none too", rules.saved(spec_with([], []), "crew-reviewer") is None)
ck("a spec with allow sentences but no deny side is none (the production guard is part of the rules)",
   rules.saved(spec_with(["Do a thing."], []), "crew-reviewer") is None)
ck("usable: both kinds of sentence, and nothing else is", rules.usable(got) and not rules.usable(None)
   and not rules.usable({"allow": ["a"]}) and not rules.usable({"allow": [" "], "soft_deny": ["b"]}))
ck("fill puts in the names it has and leaves the others", rules.fill("{human} on {other}", {"human": "Pat"}) == "Pat on {other}")

print("the shipped defaults")
rev = rules.shipped(KIT, "reviewer")
ck("a reviewer is given the reviewer's rule and not the releaser's",
   any("factory/review" in s for s in rev["allow"]) and not any("kind cluster" in s for s in rev["allow"]))
ck("...with no grant in them", rev["soft_deny"] and not any("granted this" in s for s in rev["allow"]))
ck("an unknown role still gets the rules for every station", rules.shipped(KIT, "")["allow"] != [])
bare = SCRATCH / "bare-kit"
bare.mkdir()
msg = raises(lambda: rules.shipped(str(bare), "builder"))
ck("a plugin without the shipped file is an error that says what to do", msg is not None and "Reinstall the plugin" in msg, str(msg))
(bare / "templates").mkdir()
(bare / "templates" / "default-rules.json").write_text('{"defaults": []}', encoding="utf-8")
ck("a shipped file with no defaults is the same error", "Reinstall the plugin" in (raises(lambda: rules.shipped(str(bare), "builder")) or ""))

print("a call that says which station it is")
tares, c = crew_with({})
try:
    try:
        c.tares_http("GET", "/api/factory/guardrails/rules?station=crew-reviewer&role=reviewer")
        bare_call = None
    except Exception as e:   # noqa: BLE001
        bare_call = getattr(e, "status", None)
    ck("the fake Tares, like the real route, refuses a rules call that names no station (403)", bare_call == 403, str(bare_call))
    sent = rules.fetch(c, "crew-reviewer", "", "reviewer")
    ck("rules.fetch names the station and its role, so a person's key is answered", sent.get("station") == "crew-reviewer" and sent.get("role") == "reviewer", str(sent))
finally:
    tares.__exit__(None, None, None)


tares, c = crew_with({"grants": {"all": GRANT, "projects": {}}})
try:
    got = rules.fetch(c, "crew-reviewer", "", "reviewer")
    ck("the rules are Tares's, the grant among them, with the {human} still in", any("{human} granted this on 2026-01-02" in s and "staging database" in s for s in got["allow"]), str(got)[:200])
    tares.world["capabilities"] = ["integrations", "merge_queue"]
    c._caps = None
    msg = raises(lambda: rules.fetch(c, "crew-reviewer", "", "reviewer"))
    ck("a cell without guardrail_rules is refused with the upgrade message", msg and "missing guardrail_rules" in msg and "Upgrade Tares" in msg, str(msg))
finally:
    c.close()
    tares.__exit__(None, None, None)

print("what a regrant quotes")
old = {"allow": ["a"], "soft_deny": ["b Pat"]}
new = {"rules": [{"text": "a", "source": "default"}, {"text": "b {human}", "source": "grant"}, {"text": "c {human}", "source": "grant"},
                 {"text": "d", "source": "own"}]}
V = {"human": "Pat"}
ck("added: what is in the new rules and not in the last start, filled", rules.added(old, new, V) == ["c Pat", "d"], str(rules.added(old, new, V)))
ck("added: without a last start, only the grants", rules.added(None, new, V) == ["b Pat", "c Pat"], str(rules.added(None, new, V)))
ck("added: nothing new is nothing", rules.added({"allow": ["a", "b Pat", "c Pat", "d"], "soft_deny": []}, new, V) == [])
ck("added: an empty answer adds nothing", rules.added(old, {}, V) == [])

tares, c = crew_with({"grants": {"all": GRANT, "projects": {}}})
try:
    c.prime()
    c.paths.launch_dir.mkdir(parents=True, exist_ok=True)
    c.paths.launch_file("crew-reviewer").write_text(json.dumps({"command": [], "env": {}, "settings": {"autoMode": {
        "allow": ["$defaults", "x"], "soft_deny": ["$defaults", "y"]}}}), encoding="utf-8")
    L.state.stations_put("crew-reviewer", {"role": "reviewer"}, paths=c.paths)
    quoted = regrant._new_rules(c, c.paths, "crew-reviewer", "")
    ck("_new_rules: the sentences added since the last start, with the person's name", any("Pat granted this on 2026-01-02" in s for s in quoted), str(quoted)[:200])
    ck("...and none of the defaults of the last start are mistaken for new ones", len(quoted) >= 1)
    tares.world["capabilities"] = ["integrations"]
    c._caps = None
    ck("_new_rules: a Tares that cannot give the rules gives none (the prompt then says the change without quoting)",
       regrant._new_rules(c, c.paths, "crew-reviewer", "") == [])
finally:
    c.close()
    tares.__exit__(None, None, None)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)

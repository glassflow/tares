"""The rules a station starts under come from Tares and are never empty (TR-864, tares/crew/rules.py).

Tares up: the settings file holds the sentences Tares renders, and a copy of the answer is kept in
<crew home>/rules/<station>.json. Tares down (or answering with an error): the copy; with no usable copy, the
defaults shipped in claude-plugin/templates/default-rules.json. A Tares that answers with no rules, and a cell
too old to have the route, stop the start with a message. The watch's regrant prompt quotes the rules that are
new since the station's last start.

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
VALUES = {"human": "Pat", "home": "/h", "tares": "http://t", "stations_file": "/h/stations.json", "repos": "the repos",
          "where": "the repo", "wt_repos": "the repos", "cluster": "kind", "prod_pattern": "prod"}


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


def start(world, station="crew-reviewer", role="reviewer", before=None):
    """for_station against a fake Tares; returns (result or None, the CrewError text or "", the client, the lines said)."""
    tares, c = crew_with(world)
    said = []
    c.say = said.append
    try:
        if before:
            before(c)
        try:
            return rules.for_station(c, station, "", role, KIT, VALUES), "", c, said
        except CrewError as e:
            return None, str(e), c, said
    finally:
        tares.__exit__(None, None, None)


def raises(fn):
    try:
        fn()
    except CrewError as e:
        return str(e)
    return None


print("Tares answers")
got, err, c, said = start({"grants": {"all": GRANT, "projects": {}}})
ck("the rules are Tares's, the grant among them, {human} filled in",
   got and got["source"] == "tares" and any("Pat granted this on 2026-01-02" in s and "staging database" in s for s in got["allow"]), str(got))
ck("the cell's own deny rule is in soft_deny, {prod_pattern} filled by Tares", got and any("pattern" in s and "prod" in s for s in got["soft_deny"]))
ck("the sentences carry no {human} or {home} left over", got and not any("{human}" in s or "{home}" in s for s in got["allow"] + got["soft_deny"]))
copy = c.paths.rules_file("crew-reviewer")
ck("a copy of the answer is kept at rules/<station>.json", copy.is_file())
kept = json.loads(copy.read_text(encoding="utf-8")) if copy.is_file() else {}
ck("...as Tares gave it, placeholders and all", any("{human}" in s for s in kept.get("allow", [])))
ck("a reviewer is given the reviewer's rule and not the releaser's",
   got and any("factory/review" in s for s in got["allow"]) and not any("kind cluster" in s for s in got["allow"]))
rel, err, c2, _ = start({}, "crew-releaser", "releaser")
ck("a releaser is given the releaser's rule", rel and any("kind" in s and "cluster" in s for s in rel["allow"]), err)

print("Tares does not answer")
copy_doc = {"allow": ["Kept sentence for {human}."], "soft_deny": ["Kept deny."], "rules": [
    {"id": "x", "kind": "allow", "source": "default", "text": "Kept sentence for {human}."}]}


def put_copy(client):
    client.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    client.paths.rules_file("crew-reviewer").write_text(json.dumps(copy_doc), encoding="utf-8")


got, err, c, said = start({"health": False}, before=put_copy)
ck("Tares down with a copy: the copy, filled in", got and got["source"] == "copy" and got["allow"] == ["Kept sentence for Pat."], str(got) + err)
got, err, c, said = start({"rules_status": 500}, before=put_copy)
ck("Tares answering with an error and a copy: the copy, and it says so",
   got and got["source"] == "copy" and any("could not read the rules" in s for s in said), str(got) + err + str(said))
got, err, c, said = start({"health": False})
ck("Tares down, no copy: the defaults the plugin ships, for the role",
   got and got["source"] == "shipped" and got["allow"] and got["soft_deny"] and any("factory/review" in s for s in got["allow"]), str(got) + err)
ck("...with no grant in them", got and not any("granted this" in s for s in got["allow"]))
ck("...and no copy is written from them", not c.paths.rules_file("crew-reviewer").exists())


def put_junk(client):
    client.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    client.paths.rules_file("crew-reviewer").write_text("not json {", encoding="utf-8")


got, err, c, said = start({"health": False}, before=put_junk)
ck("a copy that is not JSON counts as no copy: the shipped defaults", got and got["source"] == "shipped", str(got) + err)


def put_empty(client):
    client.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    client.paths.rules_file("crew-reviewer").write_text(json.dumps({"allow": [], "soft_deny": [], "rules": []}), encoding="utf-8")


got, err, c, said = start({"health": False}, before=put_empty)
ck("a copy with no rules in it counts as no copy too", got and got["source"] == "shipped", str(got) + err)
ck("an unknown role still gets the rules for every station", rules.shipped(KIT, "")["allow"] != [])

print("never no rules")
got, err, c, said = start({"rules": {"station": "s", "allow": [], "soft_deny": [], "rules": []}}, before=put_copy)
ck("Tares answering with no rules stops the start, even with a copy", got is None and "never starts without rules" in err, err)
ck("...and does not overwrite the copy with nothing", json.loads(c.paths.rules_file("crew-reviewer").read_text(encoding="utf-8")) == copy_doc)
got, err, c, said = start({"rules": ["not", "a", "doc"]}, before=put_copy)
ck("an answer that is not rules is read as no answer: the copy", got and got["source"] == "copy", str(got) + err)
bare = SCRATCH / "bare-kit"
bare.mkdir()
msg = raises(lambda: rules.shipped(str(bare), "builder"))
ck("a plugin without the shipped file is an error that says what to do", msg is not None and "Reinstall the plugin" in msg, str(msg))
(bare / "templates").mkdir()
(bare / "templates" / "default-rules.json").write_text('{"defaults": []}', encoding="utf-8")
ck("a shipped file with no defaults is the same error", "Reinstall the plugin" in (raises(lambda: rules.shipped(str(bare), "builder")) or ""))

print("an older Tares")
got, err, c, said = start({"capabilities": ["integrations", "merge_queue"]}, before=put_copy)
ck("a cell without guardrail_rules stops the start with the upgrade message, copy or not",
   got is None and "missing guardrail_rules" in err and "Upgrade Tares" in err, err)

print("settings of a start")
tares, c = crew_with({"grants": {"all": GRANT, "projects": {}}})
try:
    c.prime()
    path = L.write_settings(c, "crew-reviewer", "reviewer", "", "", "")
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    auto = doc.get("autoMode", {})
    ck("write_settings puts Tares's sentences in autoMode", any("staging database" in s for s in auto.get("allow", [])), str(auto)[:200])
    ck("...with the person's name in place of {human}", any("Pat granted" in s for s in auto.get("allow", [])))
    ck("...and the deny sentence in soft_deny", bool(auto.get("soft_deny")))
    ck("...and the copy kept", c.paths.rules_file("crew-reviewer").is_file())
    tares.world["capabilities"] = ["integrations"]
finally:
    c.close()
    tares.__exit__(None, None, None)

print("what a regrant quotes")
old = {"rules": [{"text": "a", "source": "default"}, {"text": "b {human}", "source": "grant"}]}
new = {"rules": [{"text": "a", "source": "default"}, {"text": "b {human}", "source": "grant"}, {"text": "c {human}", "source": "grant"},
                 {"text": "d", "source": "own"}]}
ck("added: what is in the new rules and not in the copy", rules.added(old, new) == ["c {human}", "d"], str(rules.added(old, new)))
ck("added: without a copy, only the grants", rules.added(None, new) == ["b {human}", "c {human}"], str(rules.added(None, new)))
ck("added: nothing new is nothing", rules.added(new, new) == [])
ck("added: an empty answer adds nothing", rules.added(old, {}) == [])

tares, c = crew_with({"grants": {"all": GRANT, "projects": {}}})
try:
    c.prime()
    c.paths.rules_dir.mkdir(parents=True, exist_ok=True)
    c.paths.rules_file("crew-reviewer").write_text(json.dumps({"allow": ["x"], "soft_deny": [], "rules": [
        {"text": "x", "source": "default"}]}), encoding="utf-8")
    L.state.stations_put("crew-reviewer", {"role": "reviewer"}, paths=c.paths)
    quoted = regrant._new_rules(c, c.paths, "crew-reviewer", "")
    ck("_new_rules: the sentences added since the copy, with the person's name", any("Pat granted this on 2026-01-02" in s for s in quoted), str(quoted)[:200])
    ck("...and none of the defaults that were already in the copy's place are mistaken for new ones", len(quoted) >= 1)
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

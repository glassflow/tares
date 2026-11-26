"""A station's rules as its runtime reads them (tares/crew/role_text.py and claude-plugin/runtimes, TR-1598).

The rules have one source, the kit's skills. Claude Code reads them as they are; a Codex station gets a
rendering that names nothing only Claude Code has.

 a. Claude Code: the rendering of every skills file is the file, byte for byte, and no manual is made;
 b. Codex, the builder: the manual holds the protocol, the builder's manual and their pages, names nothing
    of role_text.CLAUDE_ONLY, has no front matter, keeps the text both runtimes share word for word, and
    says how a Codex builder reports (through Tares) and that it holds no schedule;
 c. the two cannot drift apart unseen: a source that changed under an edit, an edit that is gone (so a
    Claude-only name is left), a broken edits file and a page that does not exist all stop the rendering
    with the reason; every edits file belongs to a file some role reads;
 d. another role is added by a table and its edits, with no code: shown on a kit copy;
 e. a role with no rendering has no manual, and the launch says so in its boot prompt;
 f. the launch: a Codex builder's plan carries its identity and then the manual, with nothing left to
    fill; an orchestrator's carries its identity alone; a kit whose rules cannot be rendered stops the
    plan; a Claude Code plan has no such place;
 g. the prompts: Claude Code's boot prompt and idle-wake note are as they were; a Codex station's name
    nothing of CLAUDE_ONLY, no READY, and tell it of a halt.

Run: python tests/test_role_text.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KIT = ROOT / "claude-plugin"
SCRATCH = Path(tempfile.mkdtemp(prefix="trt-", dir="/tmp"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
HOME = SCRATCH / "home"
HOME.mkdir()
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1")

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))
import _crew_guard  # noqa: E402

_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run (TR-1164)

from tares.crew import launch as L  # noqa: E402
from tares.crew import role_text as rt  # noqa: E402
from tares.crew import state  # noqa: E402
from tares.crew.client import TaresClient  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
_n = [0]


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def named(text):
    """The names of CLAUDE_ONLY in `text`, found here by the test's own search."""
    return [n for n in rt.CLAUDE_ONLY if n.lower() in text.lower()]


def kit_copy():
    """A copy of the kit's skills and runtimes to break."""
    _n[0] += 1
    kit = SCRATCH / f"kit{_n[0]}"
    shutil.copytree(KIT / "skills", kit / "skills")
    shutil.copytree(KIT / "runtimes", kit / "runtimes")
    return kit


def refused(fn, *args):
    try:
        fn(*args)
    except (rt.RenderError, CrewError) as e:
        return str(e)
    return None


def edit_file(kit, name, old, new):
    path = Path(kit) / name
    text = path.read_text(encoding="utf-8")
    assert old in text, (name, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


SKILLS = sorted(str(p.relative_to(KIT / "skills")) for p in (KIT / "skills").rglob("*.md"))

print("a. Claude Code reads the skills as they are")
same = [n for n in SKILLS if rt.render_file(KIT, rt.CLAUDE_CODE, n).encode("utf-8") == (KIT / "skills" / n).read_bytes()]
ck(f"the rendering of each of the {len(SKILLS)} skills files is the file, byte for byte",
   len(SKILLS) > 20 and same == SKILLS, sorted(set(SKILLS) - set(same)))
ck("no manual is made for a Claude Code station: it loads the skills itself",
   all(rt.manual(KIT, rt.CLAUDE_CODE, role) == "" for role in ("builder", "orchestrator", "reviewer", "releaser"))
   and rt.roles(KIT, rt.CLAUDE_CODE) == {})

print("b. the builder's manual on Codex")
manual = rt.manual(KIT, rt.CODEX, "builder")
table = rt.roles(KIT, rt.CODEX)
ck("the builder has a manual on Codex, made of the protocol, the builder's manual and their pages",
   table["builder"]["parts"] == ["factory-protocol/SKILL.md", "station-builder/SKILL.md"]
   and all(f"# Page `{p.rsplit('/', 1)[-1]}`" in manual for p in table["builder"]["pages"])
   and manual.index("# Factory protocol") < manual.index("# Builder station") < manual.index("# Page `report.md`"),
   table)
ck("it names nothing only Claude Code has", named(manual) == [] and rt.named(manual) == [], named(manual))
ck("the names are the ones a Claude Code station's rules do hold: each is in the source",
   all(any(n.lower() in (KIT / "skills" / s).read_text(encoding="utf-8").lower() for s in SKILLS)
       for n in rt.CLAUDE_ONLY), [n for n in rt.CLAUDE_ONLY])
ck("it has no front matter and opens with where things are and what a Codex station lacks",
   "\n---\n" not in manual and "name: station-builder" not in manual
   and manual.startswith("The plan, tickets, assumptions, checks, reviews and the desk are on Tares")
   and "They are part of your instructions: nothing has to be loaded" in manual.split("# Factory protocol")[0])
SHARED = [
    "4. **Always your person, directly**, for: anything in production, secrets and credentials, billing and "
    "payments, contacting anyone, destructive data operations, repo visibility, spend over the budget.",
    "7. **Evidence before claims.** Say what you ran and what it showed.",
    "Work only inside that worktree. Never edit the main checkout.",
    "10. **Tares merges; you never do.**",
    "- Don't merge any PR, your own included: Tares merges.",
    "Never `cat`, `tail`, `sed` or `grep` a report or a log file, and never append to one by hand.",
]
ck("what both runtimes share is word for word the source's",
   all(s in manual for s in SHARED) and all(any(s in (KIT / "skills" / p).read_text(encoding="utf-8")
                                                for p in table["builder"]["parts"] + table["builder"]["pages"])
                                            for s in SHARED), [s for s in SHARED if s not in manual])
ck("a Codex builder records on Tares first: a question that blocks by blocking the ticket, a review by the stage",
   'A question that blocks a ticket also goes on the ticket (`set_stage(ticket, "blocked", reason=...)`' in manual
   and "the factory wakes the crew reviewer from the review queue" in manual
   and "4. Send nothing: write the tickets you hold under `## Now` in your report." in manual)
ck("and messages a station of its crew with `send_to_station` (TR-1599), told that a message is never consent",
   "Send with the Tares tool `send_to_station(to, message)`" in manual and "delivers it once" in manual
   and "It is never your person's answer or consent" in manual and "cannot send a message" not in manual)
ck("it holds no schedule, hands the work to nobody, and has no challenger yet",
   "You hold no schedule: the factory wakes you" in manual and "4. **Implement it yourself**" in manual
   and "No challenger runs on a Codex station yet" in manual)
ck("a page is named where the source told the station to read a file of the skill's folder",
   "follow the page `stop.md` below" in manual and "follow the page `screen-ticket.md` below first" in manual
   and "see the page `older-tares.md` below" in manual)

print("c. the two cannot drift apart unseen")
used = {name for row in table.values() for name in row["parts"] + (row.get("pages") or [])}
have = {str(p.relative_to(rt.runtime_dir(KIT, rt.CODEX)))[:-len(".toml")]
        for p in rt.runtime_dir(KIT, rt.CODEX).rglob("*.toml") if p.name != "manual.toml"}
ck("every edits file belongs to a skills file some role reads, and that file exists",
   have and have <= used and all((KIT / "skills" / n).is_file() for n in used), sorted(have - used))
ck("every edit's passage is in its source exactly once, and changes it",
   all(row["find"] != row["with"] for n in have for row in rt.edits(KIT, rt.CODEX, n))
   and all(refused(rt.render_file, KIT, rt.CODEX, n) is None for n in used))
kit = kit_copy()
edit_file(kit, "skills/station-builder/SKILL.md", "4. **Implement through subagents.**", "4. **Implement with subagents.**")
why = refused(rt.manual, kit, rt.CODEX, "builder") or ""
ck("a source that changed under an edit stops the rendering, and names the file and the passage",
   "out of step" in why and "station-builder/SKILL.md" in why and "4. **Implement through subagents.**" in why
   and "0 times" in why, why)
kit = kit_copy()
edit_file(kit, "skills/factory-protocol/SKILL.md", "## Liveness", "## Liveness\n\nArm a `CronCreate` tick at boot.")
why = refused(rt.manual, kit, rt.CODEX, "builder") or ""
ck("a new line of the source that names a Claude Code tool stops it too, and names the tool",
   "still names what only Claude Code has" in why and "CronCreate" in why and "factory-protocol/SKILL.md" in why, why)
kit = kit_copy()
(rt.runtime_dir(kit, rt.CODEX) / "station-builder" / "stop.md.toml").unlink()
why = refused(rt.manual, kit, rt.CODEX, "builder") or ""
ck("so does an edits file that is gone", "stop.md" in why and "CronDelete" in why, why)
kit = kit_copy()
(rt.runtime_dir(kit, rt.CODEX) / "station-builder" / "stop.md.toml").write_text("[[edit]]\nfind = 'x\n")
ck("and one that is not valid TOML, or holds no edit, or an edit without its two texts",
   "not valid TOML" in (refused(rt.manual, kit, rt.CODEX, "builder") or ""))
(rt.runtime_dir(kit, rt.CODEX) / "station-builder" / "stop.md.toml").write_text("# nothing\n")
ck("  (no edit)", "holds no [[edit]]" in (refused(rt.manual, kit, rt.CODEX, "builder") or ""))
(rt.runtime_dir(kit, rt.CODEX) / "station-builder" / "stop.md.toml").write_text("[[edit]]\nfind = 'x'\n")
ck("  (half an edit)", "needs a `find` passage and a `with` text" in (refused(rt.manual, kit, rt.CODEX, "builder") or ""))
kit = kit_copy()
(kit / "skills" / "station-builder" / "stop.md").unlink()
ck("a page that does not exist stops it", "cannot read" in (refused(rt.manual, kit, rt.CODEX, "builder") or ""))

print("d. another role is added by a table and its edits")
kit = kit_copy()
(kit / "skills" / "station-tester").mkdir()
(kit / "skills" / "station-tester" / "SKILL.md").write_text(
    "---\nname: station-tester\ndescription: x\n---\n\n# Tester station\n\nSay so with `SendMessage`. Test things.\n")
with open(rt.runtime_dir(kit, rt.CODEX) / "manual.toml", "a", encoding="utf-8") as f:
    f.write('\n[roles.tester]\nparts = ["factory-protocol/SKILL.md", "station-tester/SKILL.md"]\n')
ck("without its edits the new role is refused for what its file still names",
   "station-tester/SKILL.md" in (refused(rt.manual, kit, rt.CODEX, "tester") or ""))
(rt.runtime_dir(kit, rt.CODEX) / "station-tester").mkdir()
(rt.runtime_dir(kit, rt.CODEX) / "station-tester" / "SKILL.md.toml").write_text(
    "[[edit]]\nfind = '''Say so with `SendMessage`. '''\nwith = ''''''\n")
tester = rt.manual(kit, rt.CODEX, "tester")
ck("with them it has a manual, with no code changed: the protocol, then its own file, no pages",
   tester.endswith("# Tester station\n\nTest things.\n") and "# Factory protocol" in tester and "# Page" not in tester
   and named(tester) == [], tester[-200:])
ck("the builder's is untouched by it", rt.manual(kit, rt.CODEX, "builder") == manual)

print("e. every crew role on Codex (TR-1599), and a role with no rendering")
roles = {r: rt.manual(KIT, rt.CODEX, r) for r in ("orchestrator", "reviewer", "releaser")}
ck("the orchestrator, the reviewer and the releaser have a manual on Codex, naming nothing of CLAUDE_ONLY",
   sorted(table) == ["builder", "orchestrator", "releaser", "reviewer"]
   and all(len(m) > 5000 and named(m) == [] for m in roles.values()), {r: named(m) for r, m in roles.items()})
ck("none holds a schedule; each is woken by the factory",
   all("You hold no schedule" in m and "CronCreate" not in m for m in roles.values()))
ck("the orchestrator: the person answers on the desk in the console, which records the answer; it asks nothing in its turns",
   "they answer on the desk, in the Tares console" in roles["orchestrator"] and "Never record an answer yourself" in roles["orchestrator"]
   and "crew attach orchestrator" not in roles["orchestrator"] and "PushNotification" not in roles["orchestrator"]
   and "The console records their answer." in roles["orchestrator"])
ck("the reviewer: a station of its own, and no second model, said in what it did not verify",
   "a conversation of your own" in roles["reviewer"] and "no second-model check: one Codex reviewer" in roles["reviewer"]
   and "subagent" not in roles["reviewer"].lower())
ck("the orchestrator asks for approval to run a `tares factory` command that reaches Tares (its sandbox has no network)",
   "this command reaches Tares: run it asking for approval (escalated permissions" in roles["orchestrator"])
ck("it settles a batch of assumptions with decide_assumption, never by recording the answer itself",
   "decide_assumption(A<n>" in roles["orchestrator"] and "`kept=[...]`" not in roles["orchestrator"])
ck("a takeover's list goes on the desk: the person's own session is not a station",
   "no message reaches it" in roles["orchestrator"] and "Reply to the session named in the `TAKEOVER`" not in roles["orchestrator"])
ck("the reviewer knows it runs outside any repository", "You run outside any repository" in roles["reviewer"])
ck("the releaser: woken for a RELEASE, which is kept while it is stopped",
   "the factory wakes you when a `RELEASE` reaches you" in roles["releaser"])
bare = kit_copy()
shutil.copytree(KIT / "hooks", bare / "hooks")   # a kit the launch takes: it checks the hooks are there
edit_file(bare, "runtimes/codex/manual.toml", "[roles.reviewer]", "[roles.unused]")
ck("a role the kit's table does not name has no manual on Codex", rt.manual(bare, rt.CODEX, "reviewer") == "")
ck("a runtime the kit has no folder for has none either", rt.manual(KIT, "other", "builder") == "" and rt.roles(KIT, "other") == {})

print("f. the launch")


def crew(runtime="codex"):
    _n[0] += 1
    tmp = SCRATCH / f"t{_n[0]}"
    tmp.mkdir()
    LS.make_crew(tmp, {"token": "tok-fake-role-text-0123456789", "stations": {}})
    # the cell's runtime slot, as GET /api/factory/integrations/effective answers it
    slot = {"slots": [{"slot": "runtime", "integration": runtime, "title": runtime, "source": "factory", "reason": ""}]}
    tares = LS.FakeTares({"runtime": runtime, "routes": {L.RUNTIME_SLOT: (200, slot)}})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    client = TaresClient(paths, env={**os.environ, "TARESD_URL": tares.url}, say=lambda *_: None)
    client.prime()
    return tares, paths, client


tares, paths, client = crew()
plan = L.launch_plan(client, "shop-build-api", "builder", "", "", "", "gpt-5.6-luna", "low")
told = plan["settings"]["thread"]["developerInstructions"]
ck("a Codex builder's plan carries who it is and then its manual, with nothing left to fill",
   told == "You are a tares-factory station: role builder, session name shop-build-api. Act only within your role."
   "\n\n" + manual.rstrip() and "{ROLE_MANUAL}" not in told and named(told) == [], told[:200])
ck("the manual is the one launch.role_manual gives, a blank line first", L.role_manual(client, "builder") == "\n\n" + manual.rstrip())
plan = L.launch_plan(client, "crew-orchestrator", "orchestrator", "", "", "", "gpt-5.6-luna", "low")
told = plan["settings"]["thread"]["developerInstructions"]
ck("an orchestrator's carries who it is and then its manual (TR-1599)",
   told == "You are a tares-factory station: role orchestrator, session name crew-orchestrator. Act only within your role."
   "\n\n" + roles["orchestrator"].rstrip() and named(told) == [], told[:200])
state.stations_put("shop-build-api", {"role": "builder", "runtime": "codex"}, paths)
plan = L.launch_plan(client, "shop-build-api-look", "helper", "shop-build-api", "", "", "gpt-5.6-luna", "low")
ck("a helper's carries the manual of the station it works under",
   plan["settings"]["thread"]["developerInstructions"].endswith("\n\n" + manual.rstrip()))
broken = kit_copy()
shutil.copytree(KIT / "hooks", broken / "hooks")
edit_file(broken, "skills/station-builder/stop.md", "`CronDelete` your tick", "`CronDelete` the tick")
os.environ["TARES_PLUGIN_DIR"] = str(broken)
why = refused(L.launch_plan, client, "shop-build-api", "builder", "", "", "", "gpt-5.6-luna", "low") or ""
del os.environ["TARES_PLUGIN_DIR"]
ck("a kit whose rules cannot be rendered stops the plan: no station starts on rules half Claude Code's",
   why.startswith("cannot give a builder station its rules on Codex:") and "stop.md" in why, why)
tares.__exit__(None, None, None)

tares, paths, client = crew("claude_code")
plan = L.launch_plan(client, "shop-build-api", "builder", "", "", "", "sonnet", "low")
ck("a Claude Code plan has no such place: its station loads the skills",
   "ROLE_MANUAL" not in str(plan) and "station-builder" in " ".join(plan["args"]), plan["args"])

print("g. the prompts")
BOOT = ("Boot as the builder station: load the skills factory-protocol and station-builder, follow their Boot "
        "sections, and send crew-orchestrator READY when you are set.")
ck("Claude Code's boot prompt is as it was, with and without a task",
   L.boot_prompt(client, "builder") == BOOT and L.boot_prompt(client, "builder", "T1\n") == BOOT + "\n\nYour task:\nT1"
   and L.boot_prompt(client, "builder", runtime="claude_code") == BOOT, L.boot_prompt(client, "builder"))
ck("and so is its idle-wake note",
   L.idle_wake_note() == "idle_wake is on: the factory wakes you when there is work and holds your tick, so do not arm a "
   "CronCreate tick (if you hold one, delete it: CronList, then CronDelete).")
codex_boot = L.boot_prompt(client, "builder", "T1", runtime="codex")
ck("a Codex builder's boot prompt points at its instructions, and names no skill, no READY, nothing of Claude Code's",
   codex_boot == "Boot as the builder station: your operating rules are in your instructions (the factory protocol, the "
   "builder manual and their pages). Follow their Boot sections.\n\nYour task:\nT1" and named(codex_boot) == [], codex_boot)
os.environ["TARES_PLUGIN_DIR"] = str(bare)
no_manual = L.boot_prompt(client, "reviewer", runtime="codex")
del os.environ["TARES_PLUGIN_DIR"]
ck("a role with no manual on Codex is told so",
   no_manual == "Boot as the reviewer station. There is no operating manual for "
   "your role on Codex yet: do only what this prompt and the ones after it ask.", no_manual)
ck("a reviewer's boot prompt points at its instructions",
   L.boot_prompt(client, "reviewer", runtime="codex").startswith("Boot as the reviewer station: your operating rules are in "
   "your instructions"))
ck("the idle-wake note of a Codex station names no schedule tool",
   named(L.idle_wake_note("codex")) == [] and "the factory wakes you" in L.idle_wake_note("codex"), L.idle_wake_note("codex"))
state.halt_write("the person said stop", paths=paths)
halted = L.boot_prompt(client, "builder", runtime="codex")
ck("a Codex station is told of a halt at its start, which a Claude Code one hears from its hook",
   "The crew is halted (the person said stop): start nothing new until it is resumed." in halted
   and "halted" not in L.boot_prompt(client, "builder"), halted)
tares.__exit__(None, None, None)

tares, paths, client = crew()
first = L.boot_prompt(client, "builder", "T1")
ck("a new Codex crew, with no station recorded yet, gets the Codex prompt at its first start: the cell says the runtime",
   first == codex_boot and named(first) == [] and "load the skills" not in first and "READY" not in first
   and "CronCreate" not in first and L.planned_runtime(client) == "codex", first)
tares.__exit__(None, None, None)

tares, paths, client = crew("claude_code")
ck("a new Claude Code crew gets Claude Code's", L.boot_prompt(client, "builder") == BOOT)
tares.world["routes"][L.RUNTIME_SLOT] = (500, {"detail": "down"})
ck("a cell that cannot say leaves the prompt Claude Code's, as before", L.planned_runtime(client) == ""
   and L.boot_prompt(client, "builder") == BOOT)
tares.__exit__(None, None, None)

tares, paths, client = crew()
state.stations_put("crew-orchestrator", {"role": "orchestrator", "runtime": "codex"}, paths)
ck("with no runtime given, a recorded Codex crew gets the Codex prompt",
   L.boot_prompt(client, "builder").startswith("Boot as the builder station: your operating rules are in your instructions"))
tares.__exit__(None, None, None)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

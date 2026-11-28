"""`tares capabilities` (TR-1415): the list of machine commands (tares/crew/capabilities.py COMMANDS) is complete.

The definition of done for a new machine command is a name in COMMANDS. This test reads the commands the CLI
really registers, the `tares factory` registry (PORTED in tares/crew/dispatch.py) and the top-level words of
tares/cli.py (build_parser), and holds each one against two explicit tables below: MACHINE, registry word to
dotted name, and NOT_MACHINE, registry word to the reason it has no name. It fails when

  - a registered command is in neither table (a new command: decide, and write it down here);
  - a table names a command that is not registered any more;
  - a command is in both tables;
  - COMMANDS lacks a name MACHINE gives, or holds a name no registered command has (a stale name), or repeats one.

The checks run on the real registry, and again on a copy with one thing wrong each, to show they can fail.

The behaviour of `tares capabilities --json` (the document, a cell that answers or not) is in tests/test_crew_contract.py.

Run: python tests/test_crew_capabilities.py
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)
from _env import scrub_os_environ  # noqa: E402

scrub_os_environ()
_crew_guard.scratch_home()   # the registry names below include crew commands: nothing here may reach the real home (TR-1164)
_crew_guard.install()

from tares import cli  # noqa: E402
from tares.crew import capabilities, dispatch  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:600]))


# `tares factory <words>` commands that have `--json` and a schema in tares/crew/schemas: key in PORTED -> dotted name.
MACHINE = {
    ("status",): "factory.status",
    ("roster",): "factory.roster",
    ("desk",): "factory.desk",
    ("events",): "factory.events",
    ("doctor",): "factory.doctor",
    ("projects",): "factory.projects",
    ("setup",): "factory.setup",
    ("setup", "apply"): "factory.setup.apply",
    ("settings",): "factory.settings",
    ("tidy",): "factory.tidy",
    ("restart",): "factory.restart",
    ("revive",): "factory.revive",
    ("retire",): "factory.retire",
    ("crew", "status"): "factory.crew.status",
    ("crew", "desk"): "factory.crew.desk",
    ("crew", "setup"): "factory.crew.setup",
    ("crew", "up"): "factory.crew.up",
    ("crew", "down"): "factory.crew.down",
    ("crew", "halt"): "factory.crew.halt",
    ("crew", "resume"): "factory.crew.resume",
    ("crew", "freeze"): "factory.crew.freeze",
    ("crew", "unfreeze"): "factory.crew.unfreeze",
    ("crew", "update"): "factory.crew.update",
    ("crew", "restart"): "factory.crew.restart",
    ("crew", "watch"): "factory.crew.watch",
}

# The rest of PORTED, each with why it is not a machine command.
NOT_MACHINE = {
    ("watch",): "the same command as crew watch (the machine name is factory.crew.watch)",
    ("down",): "the same command as crew down (the machine name is factory.crew.down)",
    ("halt",): "the same command as crew halt (the machine name is factory.crew.halt)",
    ("resume",): "the same command as crew resume (the machine name is factory.crew.resume)",
    ("crew", "roster"): "the README names roster only, not crew roster",
    ("attach",): "opens a terminal on a station, no --json",
    ("crew", "attach"): "opens a terminal on a station, no --json",
    ("logs",): "prints a station's log, no --json",
    ("crew", "logs"): "prints a station's log, no --json",
    ("report",): "its --json only shapes a refusal and the README table does not list it",
    ("spawn",): "starts a station for the crew itself, no --json",
    ("launch-plan",): "prints a launch plan for the crew itself, no --json",
    ("heavy",): "a heavy-job lock for stations, no --json",
    ("with-secrets",): "runs a command with project secrets, no --json",
    ("secrets",): "manages project secrets for a person, no --json",
    ("allow",): "a person's permission edit, no --json",
    ("git-credential",): "git's credential protocol on stdin, not a JSON command",
    ("gh",): "runs gh with a fresh token, its output is gh's",
    ("finish",): "hands a project back, text only",
    ("context",): "prints a station's context for a person, text only",
    ("plan",): "starts a planning session, no --json",
    ("spec",): "starts a spec session, no --json",
    ("session",): "starts a session, no --json",
    ("outcome",): "a Codex station's own account of a call cut off by a crash, text only (TR-1593)",
    ("second-look",): "a reviewer's or a spec session's look by a separate Codex session, text only (TR-1599)",
    ("runtime",): "what new crews start on and what this crew runs on, text only; status --json has it (TR-1600)",
    ("_mcp-url",): "internal, prints one address for the plugin",
    ("_parse-bg-id",): "internal, parses Claude Code output",
}

# Top-level words of `tares` (tares/cli.py): machine commands.
TOP_MACHINE = {"login": "login", "logout": "logout", "capabilities": "capabilities"}
TOP_NOT_MACHINE = {
    "up": "starts the daemon",
    "mcp": "starts the MCP server",
    "hook": "starts a plugin hook",
    "factory": "the prefix of every factory command above",
}


def top_words() -> dict:
    """{word: its argparse subparser} from the real `tares` parser."""
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def problems(ported, words, commands, machine=MACHINE, not_machine=NOT_MACHINE, top_machine=TOP_MACHINE,
             top_not=TOP_NOT_MACHINE) -> list:
    """What is wrong with the registry (ported keys, top-level words) against the tables and COMMANDS."""
    found = []
    for key in sorted(set(ported) - set(machine) - set(not_machine)):
        found.append(f"factory command {' '.join(key)!r} is registered but is in neither MACHINE nor NOT_MACHINE")
    for key in sorted(set(machine) - set(ported)):
        found.append(f"MACHINE names {' '.join(key)!r}, which is not registered")
    for key in sorted(set(not_machine) - set(ported)):
        found.append(f"NOT_MACHINE names {' '.join(key)!r}, which is not registered")
    for key in sorted(set(machine) & set(not_machine)):
        found.append(f"{' '.join(key)!r} is in both tables")
    for word in sorted(set(words) - set(top_machine) - set(top_not)):
        found.append(f"top-level word {word!r} is registered but is in neither TOP_MACHINE nor TOP_NOT_MACHINE")
    for word in sorted((set(top_machine) | set(top_not)) - set(words)):
        found.append(f"a top-level table names {word!r}, which is not registered")
    for word in sorted(set(top_machine) & set(top_not)):
        found.append(f"top-level {word!r} is in both tables")
    want = list(machine.values()) + list(top_machine.values())
    for name in sorted(set(want) - set(commands)):
        found.append(f"COMMANDS lacks {name!r}")
    for name in sorted(set(commands) - set(want)):
        found.append(f"COMMANDS holds {name!r}, which no registered machine command has")
    for name in sorted({n for n in commands if list(commands).count(n) > 1}):
        found.append(f"COMMANDS lists {name!r} more than once")
    return found


PORTED = set(dispatch.PORTED)
WORDS = top_words()
COMMANDS = list(capabilities.COMMANDS)
README = (ROOT / "tares" / "crew" / "schemas" / "README.md").read_text(encoding="utf-8")

print("== the registry and COMMANDS agree")
found = problems(PORTED, WORDS, COMMANDS)
ck("every registered command is a machine command or left out with a reason, and COMMANDS is exactly the machine ones", not found, found)
ck("the registry is read: the factory words and the top-level words are not empty",
   len(PORTED) > 20 and {"login", "logout", "up"} <= set(WORDS) and "status" not in WORDS, (len(PORTED), sorted(WORDS)))
ck("a machine command's name is `factory.` and its words, or the top-level word",
   all(name == "factory." + ".".join(key) for key, name in MACHINE.items())
   and all(word == name for word, name in TOP_MACHINE.items()), "")
ck("every NOT_MACHINE entry has a reason", all(r.strip() for r in list(NOT_MACHINE.values()) + list(TOP_NOT_MACHINE.values())), "")
ck("the top-level machine commands take --json",
   all("--json" in WORDS[w]._option_string_actions for w in TOP_MACHINE if w in WORDS), "")
ck("setup and setup apply are listed, each under its own name",
   "factory.setup.apply" in COMMANDS and "factory.setup" in COMMANDS, "")
ck("the README tells the rule", "a new machine command" in README and "`COMMANDS`" in README, "")

print("== the check fails for what it is there to catch")
fake = problems(PORTED | {("crew", "later")}, WORDS, COMMANDS)
ck("a new registered factory command with no entry fails", any("'crew later'" in p for p in fake), fake)
fake = problems(PORTED, dict(WORDS, newword=None), COMMANDS)
ck("a new top-level word with no entry fails", any("'newword'" in p for p in fake), fake)
fake = problems(PORTED | {("later",)}, WORDS, COMMANDS, machine={**MACHINE, ("later",): "factory.later"})
ck("a new machine command in MACHINE but not in COMMANDS fails", any("lacks 'factory.later'" in p for p in fake), fake)
fake = problems(PORTED, WORDS, [c for c in COMMANDS if c != "factory.crew.up"])
ck("a name removed from COMMANDS fails", any("lacks 'factory.crew.up'" in p for p in fake), fake)
fake = problems(PORTED, WORDS, COMMANDS + ["factory.gone"])
ck("a stale name in COMMANDS fails", any("holds 'factory.gone'" in p for p in fake), fake)
fake = problems(PORTED, WORDS, COMMANDS + ["login"])
ck("a repeated name fails", any("more than once" in p for p in fake), fake)
fake = problems(PORTED - {("crew", "up")}, WORDS, COMMANDS)
ck("a command removed from the registry but still in the tables fails", any("'crew up'" in p for p in fake), fake)
fake = problems(PORTED, {k: v for k, v in WORDS.items() if k != "login"}, COMMANDS)
ck("a top-level word removed from the CLI but still in the tables fails", any("'login'" in p for p in fake), fake)
fake = problems(PORTED, WORDS, COMMANDS, not_machine={**NOT_MACHINE, ("status",): "x"})
ck("a command in both tables fails", any("both tables" in p for p in fake), fake)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

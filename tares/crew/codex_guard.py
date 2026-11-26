"""The factory guard on a Codex station (TR-1594): `tares hook codex-guard`, the hook a Codex station runs.

The rules are the Claude plugin's `hooks/guard.py`, loaded from the station's kit and asked as they are: Codex
hands a `PreToolUse` hook the payload Claude Code does (`tool_name` "Bash" or `mcp__server__tool`,
`tool_input.command`, `cwd`) and takes the same deny answer (TR-1576). This file adds what a Codex station
needs and a Claude Code station does not:

  * A command let through runs with its standard input closed. Codex can start a command and type into it
    later (`write_stdin`), and what it types is shown to no hook: a station could start `bash` and type
    `git push` past a halt. So the allow answer carries the command with `exec </dev/null` in front. A shell
    that waits for input ends at once and nothing typed later reaches anything; a pipe, a here-document and
    the exit code are untouched. A station then has what a Claude Code station has: one checked command per
    call. `asked` gives a stored command back without the added line.

  * A record that the guard ran. Codex skips a hook it was not told to trust, without an error, and the
    command runs. So every run of this hook appends one line to stations/<station>.codex.guard in the crew
    home, and the crew compares (`tares.crew.codex_station`): a turn must leave its line before the model
    is asked (the same command is registered for `UserPromptSubmit`, which Codex runs at the start of every
    turn), and every command or MCP call a turn made must have its line, saying it was let through. A
    station that fails either is stopped.

    The line of a call that is let through also holds its folder and what was asked (the command, or another
    tool's input; see `started`): Codex lists a call only once it has ended, and not at all when the
    app-server died while it ran.

  * Failing shut. A hook that ends with 1 lets the call through on Codex, as on Claude Code. Whatever goes
    wrong in here (a kit without its guard, a record that cannot be written) ends with 2, which blocks the
    call: a Codex station whose guard cannot run does no work.

  * No repeat of a call whose outcome is not known (TR-1593). A turn cut off by a crash can leave a command
    that was let through and never reported an end: it may have pushed, or opened a pull request, or done
    nothing. The crew writes such calls to stations/<station>.codex.unknown.json
    (`tares.crew.codex_station`), and this hook refuses the same command again until the station has looked
    at what it left and said so (`tares factory outcome`). Looking is not refused: only the same command is.

What the guard does not stop, on Codex as on Claude Code: it reads the text of a command, for a station that
cooperates. A script file with a push in it is not read. On Codex, a command that opens the terminal for
itself (`bash </dev/tty`) gets around the closed input; a station has no terminal (its app-server is started
without one), so there is none to open.

Reads the hook payload on stdin. Standard library only, and nothing else of the package: a hook starts at
every tool call (tests/test_hook_start.py holds the limit for `tares hook`).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys
import time
from pathlib import Path

CLOSE_STDIN = "exec </dev/null\n"   # the line put in front of a command that is let through
RECORD = "codex.guard"              # stations/<station>.codex.guard in the crew home
BLOCK = 2                           # the exit code that blocks the call (1 would let it through)
UNKNOWN = "codex.unknown.json"      # stations/<station>.codex.unknown.json: calls whose outcome is not known
KEPT = 4096                         # characters of a command, or of another tool's input, the record keeps


def record_file(home: str, station: str) -> str:
    return os.path.join(home, "stations", f"{station}.{RECORD}")


def note(env, line: dict) -> None:
    """Append one line to the station's record. Raises OSError when it cannot be written: the caller blocks."""
    home, station = env.get("FACTORY_HOME", ""), env.get("FACTORY_STATION", "")
    if not home or not station:
        raise OSError("FACTORY_HOME and FACTORY_STATION are not both set")
    data = (json.dumps({"at": round(time.time(), 3), **line}, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(record_file(home, station), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)   # one write of one short line: lines of two hooks at once do not mix
    finally:
        os.close(fd)


def read_record(path) -> list:
    """The lines of a station's record, oldest first; a line that is not a JSON object is passed over."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    out = []
    for raw in text.split("\n"):
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict):
            out.append(doc)
    return out


def asked(command: str) -> str:
    """A command as the station asked for it: the added line taken off. Takes the bare text, or the form
    Codex stores (`/bin/zsh -lc '<text>'`), which is given back as the bare text when it carries the line."""
    if not isinstance(command, str):
        return ""
    if command.startswith(CLOSE_STDIN):
        return command[len(CLOSE_STDIN):]
    for word in _words(command):
        if word.startswith(CLOSE_STDIN):
            return word[len(CLOSE_STDIN):]
    return command


def input_closed(item: dict) -> bool:
    """Does a stored command (a `commandExecution` item) carry the added line? Looked for in the command
    Codex ran (`command`, the text wrapped in the login shell) and in what Codex read out of it
    (`commandActions`)."""
    command = item.get("command") if isinstance(item, dict) else None
    if isinstance(command, str) and (command.startswith(CLOSE_STDIN)
                                     or any(w.startswith(CLOSE_STDIN) for w in _words(command))):
        return True
    actions = item.get("commandActions") if isinstance(item, dict) else None
    return any(isinstance(a, dict) and isinstance(a.get("command"), str) and a["command"].startswith(CLOSE_STDIN)
               for a in actions or [])


def _words(command: str) -> list:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def load_rules(guard_py):
    """The plugin's guard.py as a module (its rules are asked, never copied)."""
    spec = importlib.util.spec_from_file_location("tares_guard_rules", str(guard_py))
    if spec is None or spec.loader is None:
        raise OSError(f"cannot load {guard_py}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def unknown_file(home: str, station: str) -> str:
    return os.path.join(home, "stations", f"{station}.{UNKNOWN}")


def read_unknown(path) -> list:
    """The calls whose outcome is not known, [] for no file or one that cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def repeats(env, command: str):
    """The unknown call (its row) when `command` is one whose earlier run never reported an end, else None."""
    home, station = env.get("FACTORY_HOME", ""), env.get("FACTORY_STATION", "")
    want = " ".join(asked(command).split())
    if not home or not station or not want:
        return None
    for row in read_unknown(unknown_file(home, station)):
        if row.get("command") and row.get("call") and " ".join(str(row["command"]).split()) == want:
            return row
    return None


def repeat_reason(call: str) -> str:
    """What a station is told when a repeat is refused. The call is named by its own id, which does not
    change when another call is settled first (a place in a list would)."""
    return (f"This command was started in a turn that was cut off, and what it did is not known (call {call}). "
            "Running it again could do it twice. Look at what it left first (git status and git log, the remote, the "
            "pull requests, the ticket), then say what you found: "
            f"`tares factory outcome {call} done \"<what you found>\"` when it had done its work, or "
            f"`tares factory outcome {call} not-done \"<what you found>\"` when it had not. After that it is let through.")


def started(tool: str, cmd, cwd, tool_input=None) -> dict:
    """What the record keeps of a call that is let through, besides who and when: the folder (`cwd`), and
    what was asked. For a shell command that is `command`, as the station asked for it; for any other tool
    (a patch, an MCP call) it is `input`, the tool's input as Codex handed it to the hook. One shape for
    every tool: {at, turn, call, tool, said, cwd, command or input, cut}.

    Why it is kept: Codex lists a call only once it has ended. A command that was running when the app-server
    died is in no stored conversation, and a call that waits for an approval is told in full only to whoever
    was connected when it began. This line is the one durable place that saw the call before it ran (the
    recorder, TR-1595, and the approvals of TR-1593 read it). What is longer than KEPT characters is cut and
    the line says so (`cut`): an input that was cut is kept as the start of its JSON text."""
    line = {}
    if isinstance(cwd, str) and cwd:
        line["cwd"] = cwd
    if tool == "Bash":
        if isinstance(cmd, str) and cmd:
            asked_for = asked(cmd)
            line["command"] = asked_for[:KEPT]
            if len(asked_for) > KEPT:
                line["cut"] = True
        return line
    if tool_input is not None:
        text = json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False)
        if len(text) > KEPT:
            line.update(input=text[:KEPT], cut=True)
        else:
            line["input"] = tool_input
    return line


def run(guard_py, env, raw: str, out) -> int:
    """One run of the hook: `raw` is the payload, `out` takes the answer's bytes. Returns the exit code."""
    if not env.get("FACTORY_ROLE"):   # the person's own session: never touched
        return 0
    data = _payload(raw)
    turn = _text(data.get("turn_id"))
    if data.get("hook_event_name") == "UserPromptSubmit":
        note(env, {"turn": turn, "armed": True})
        return 0
    rules = load_rules(guard_py)
    tool, cmd = rules.parse(raw)
    call = _text(data.get("tool_use_id"))
    try:
        rules.decide(env, tool, cmd, rules.payload_cwd(raw))
    except rules.Deny as d:
        reason = str(d)
        note(env, {"turn": turn, "call": call, "tool": tool, "said": "deny"})
        out.write(rules.deny_output(reason))
        out.flush()
        rules.log_deny(env, tool, reason)
        return 0
    again = repeats(env, cmd) if tool == "Bash" and cmd else None
    if again:
        note(env, {"turn": turn, "call": call, "tool": tool, "said": "deny"})
        out.write(rules.deny_output(repeat_reason(again["call"])))
        out.flush()
        return 0
    note(env, {"turn": turn, "call": call, "tool": tool, "said": "allow", **started(tool, cmd, rules.payload_cwd(raw), data.get("tool_input"))})
    if tool == "Bash" and cmd and not cmd.startswith(CLOSE_STDIN):
        answer = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                         "updatedInput": {"command": CLOSE_STDIN + cmd}}}
        out.write((json.dumps(answer) + "\n").encode("utf-8"))
        out.flush()
    return 0


def main(guard_py: Path) -> int:
    """`tares hook codex-guard`: the payload on stdin, the answer on stdout. Anything that goes wrong blocks."""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        return run(guard_py, os.environ, raw, sys.stdout.buffer)
    except Exception as e:   # noqa: BLE001 - failing shut is the point: 1 would let the call through
        print(f"tares hook codex-guard: the guard could not run ({type(e).__name__}: {e}); the call is blocked",
              file=sys.stderr)
        return BLOCK

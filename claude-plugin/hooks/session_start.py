"""SessionStart hook: tells a tares-factory crew station who it is on start, resume and after compaction,
and a person's own session bound to a project (`tares factory session`, TARES_PROJECT set) which project
it works on (or, with TARES_SESSION_KIND=contributor, that it only contributes to it).
Silent in every other session. The Python twin of session-start.sh: the same words, no jq.

Started by `tares hook session-start` (it runs this file in process), or by hand with python3.
Standard library only, and 3.9-safe: it runs on a person's machine, not in the repo's venv.

Input: the hook's JSON on stdin; only "source" is read (startup, resume, compact, clear; startup when
absent or unreadable). Output: one JSON object on stdout, or nothing. The compat line (the installed
tares is older than this plugin's min_tares) goes after whatever the session is told, and is the only
thing said in a session that would otherwise hear nothing. It never costs more than about two seconds
and never fails the hook: when the check cannot run, only that line is dropped.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

# "compact" + "d" read "compactd" in the bash hook
PAST = {"resume": "resumed", "compact": "compacted", "clear": "cleared"}
COMPAT_SECONDS = 2.0  # the longest the compat check may take when it has to ask a `tares` on PATH


def env(name, default=""):
    """${NAME:-default}: the default when the variable is unset or empty."""
    return os.environ.get(name) or default


def read_source(raw: bytes) -> str:
    """jq -r '.source // "startup"': startup when the input is not a JSON object or has no source.
    (jq says nothing for an empty input and the bash script then took it for a resume; startup is the
    sensible reading, and Claude Code always sends an object.)"""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return "startup"
    value = data.get("source") if isinstance(data, dict) else None
    if value is None or value is False:
        return "startup"
    return value if isinstance(value, str) else json.dumps(value)


def plugin_root() -> str:
    return env("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def compat_line(root: str) -> str:
    """One line when the installed tares is older than the plugin's min_tares, else "". In process
    when tares is importable (it is, under `tares hook`); else by asking the `tares` on PATH, at most
    COMPAT_SECONDS, first line only, its stderr dropped. Any failure is ""."""
    try:
        from tares.crew.version import check_plugin_compat
    except Exception:
        check_plugin_compat = None
    if check_plugin_compat is not None:
        try:
            lines = (check_plugin_compat(root) or "").splitlines()
            return lines[0] if lines else ""
        except Exception:
            return ""
    tares = shutil.which("tares")
    if not tares:
        return ""
    try:
        done = subprocess.run([tares, "hook", "compat", "--plugin-root", root], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=COMPAT_SECONDS)
    except Exception:  # a timeout, a missing file, a refusal to run
        return ""
    if done.returncode != 0:  # a failing tares has nothing we can trust to say
        return ""
    lines = done.stdout.decode("utf-8", errors="replace").splitlines()
    return lines[0].strip() if lines else ""


def halt_reason(path: str) -> str:
    """jq -r '.reason // ""' on the HALT file; empty when it is not JSON."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    value = data.get("reason") if isinstance(data, dict) else None
    if value is None or value is False:
        return ""
    return (value if isinstance(value, str) else json.dumps(value)).rstrip("\n")


def station_skill(role: str) -> str:
    """The station skill to name: the role's own, and for a helper its parent's."""
    if role != "helper":
        return role
    crew = env("FACTORY_CREW", "crew")
    parent = os.environ.get("FACTORY_PARENT", "")
    for who in ("orchestrator", "reviewer", "releaser"):
        if parent.startswith(f"{crew}-{who}"):
            return parent[len(crew) + 1:]
    if "-build-" in parent:
        return "builder"
    return role


def context_for(source: str) -> str:
    """What this session is told, or "" when it is told nothing."""
    role = os.environ.get("FACTORY_ROLE", "")
    project = os.environ.get("TARES_PROJECT", "")
    if not role:
        if not project:
            return ""
        if os.environ.get("TARES_SESSION_KIND") == "contributor":
            # `tares factory spec <project>` run by someone who does not lead it: answer, check; never build
            text = (f"This session contributes to the Tares project `{project}` led by someone else, on "
                    f"{env('TARES_FACTORY_URL', 'the Tares')} through the Tares MCP tools. Follow the "
                    "`spec-project` skill, section Contributor. Do not build tickets.")
            if source != "startup":
                text += (" This session was resumed or compacted: load the `spec-project` skill again, "
                         "section Contributor, and carry on from its steps.")
            return text
        text = (f"This session works on the Tares project `{env('TARES_PROJECT_NAME', project)}` "
                f"(id `{project}`) by itself (tares factory session), on {env('TARES_FACTORY_URL', 'the Tares')} "
                "through the Tares MCP tools. Follow the `session` skill.")
        if source == "startup":
            if os.environ.get("TARES_SESSION_TAKEOVER"):
                text += (" The person took this project over from the crew: do the skill's "
                         "\"Taking over\" steps first.")
        else:
            text += (" This session was resumed or compacted: load the `session` skill again, re-read the "
                     "project's tickets and the working doc of the one in progress, then carry on where you "
                     "left off (a takeover you already asked for is not asked again).")
        return text

    text = f"You are the {role} station `{env('FACTORY_STATION', '?')}` of the tares-factory crew."
    if project:
        text += (f" You work on project `{env('TARES_PROJECT_NAME', project)}` (id `{project}`) "
                 f"in {env('FACTORY_REPO', '?')}.")
    if os.environ.get("FACTORY_PARENT"):
        text += f" You were started by `{os.environ['FACTORY_PARENT']}`."
    text += (f" The plan, tickets, assumptions, checks, reviews and the desk are on Tares "
             f"({env('TARES_FACTORY_URL', 'the crew Tares')}), through the Tares MCP tools.")
    text += f" Your rules are in the skills `factory-protocol` and `station-{station_skill(role)}`."
    if source != "startup":
        text += (f" This session was {PAST.get(source, source + 'd')}: load both skills again, re-read your report and your work on "
                 "Tares, check CronList and re-arm your schedule if it is missing (if get_crew_settings shows "
                 "idle_wake true, arm none: the factory wakes you), then carry on where your report left off.")
    home = os.environ.get("FACTORY_HOME", "")
    if home and os.path.isfile(os.path.join(home, "HALT")):
        text += (f" The crew is halted ({halt_reason(os.path.join(home, 'HALT'))}): start nothing new until "
                 "it is resumed.")
    return text


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except (OSError, ValueError, AttributeError):
        raw = b""
    source = read_source(raw)
    text = context_for(source)
    line = compat_line(plugin_root())
    if line:
        # after whatever the session is told; alone in a session that would hear nothing
        text = f"{text} {line}" if text else line
    if not text:
        return 0
    out = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                     ensure_ascii=False)
    sys.stdout.buffer.write(out.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

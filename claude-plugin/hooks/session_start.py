"""SessionStart hook: tells a tares-factory crew station who it is on start, resume and after compaction,
and a person's own session bound to a project (`tares factory session`, TARES_PROJECT set) which project
it works on (or, with TARES_SESSION_KIND=contributor, that it only contributes to it).
Where the cell reports a code brain with a ready repo (`GET /api/brain` on TARES_FACTORY_URL), any of
these sessions is also told to ask the brain before it reads code (TR-1202). Any shell with
TARES_FACTORY_URL set makes that one call (a cell without a brain, or a slow one, changes nothing). Silent
in every other session. It replaced session-start.sh (TR-1103): the same words, no jq.

Started by `tares hook session-start` (it runs this file in process), or by hand with python3.
Standard library only, and 3.9-safe: it runs on a person's machine, not in the repo's venv.

Input: the hook's JSON on stdin; only "source" is read (startup, resume, compact, clear; startup when
absent or unreadable). Output: one JSON object on stdout, or nothing. The compat line (the installed
tares is older than this plugin's min_tares) goes after whatever the session is told, and is the only
thing said in a session that would otherwise hear nothing. It never costs more than about two seconds
and never fails the hook: when the check cannot run, only that line is dropped.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request

# "compact" + "d" read "compactd" in the bash hook
# The same words as tares.crew.lifecycle.REPORT_READ: the hook stands alone, so test_crew_lifecycle checks they match.
REPORT_READ = ("re-read your report (a short state file; for older events use `tares factory report grep`, "
               "never cat or tail a file)")
PAST = {"resume": "resumed", "compact": "compacted", "clear": "cleared"}
COMPAT_SECONDS = 2.0  # the longest the compat check may take when it has to ask a `tares` on PATH
BRAIN_SECONDS = 1.5   # the longest the brain check may take (TR-1202); the hook as a whole has 5 seconds
BRAIN_ADVICE = ("This cell has a code brain with ready repos{repos}. Before reading code, call `search_brain` "
                "or `ask_brain` (Tares MCP tools); use grep only for repos those tools say are not ready, and "
                "to confirm an exact line before editing it.")


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
        proc = subprocess.Popen([tares, "hook", "compat", "--plugin-root", root], stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception:  # a missing file, a refusal to run
        return ""
    try:
        stdout, _ = proc.communicate(timeout=COMPAT_SECONDS)
    except Exception:  # a timeout: stop it and do not wait for the pipe, a grandchild may hold it open (Windows)
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            proc.kill()
        except Exception:
            pass
        return ""
    if proc.returncode != 0:  # a failing tares has nothing we can trust to say
        return ""
    lines = stdout.decode("utf-8", errors="replace").splitlines()
    return lines[0].strip() if lines else ""


def crew_bearer(root: str, url: str) -> str:
    """The token the plugin's MCP header helper would present to `url`, or "" (it never fails)."""
    try:
        path = os.path.join(root, "scripts", "mcp_headers.py")
        spec = importlib.util.spec_from_file_location("tares_plugin_mcp_headers", path)
        if spec is None or spec.loader is None:
            return ""
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        return helper.crew_token() or helper.saved_key(url.rstrip("/") + "/mcp")
    except Exception:
        return ""


def brain_ready_repos(root: str, url: str) -> list:
    """The names of the ready repos `GET /api/brain` on `url` lists, or [] (it may raise: brain_line catches)."""
    req = urllib.request.Request(url.rstrip("/") + "/api/brain", headers={"Accept": "application/json"})
    token = crew_bearer(root, url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=BRAIN_SECONDS) as resp:
        data = json.loads(resp.read(1 << 20).decode("utf-8", errors="replace"))
    if not isinstance(data, dict) or data.get("configured") is not True:
        return []
    rows = data.get("repos")
    return [str(r.get("repo")) for r in rows if isinstance(r, dict) and r.get("state") == "ready" and r.get("repo")] \
        if isinstance(rows, list) else []


def brain_line(root: str) -> str:
    """One paragraph telling the session to ask the code brain first, only when the cell reports one
    (`GET /api/brain` says configured with at least one ready repo), else "". The cell is
    TARES_FACTORY_URL, the one the crew uses; with it unset, in a session that is not the crew's or a
    spec session's, nothing is asked. Any failure (no brain, 404, auth, a slow or dead cell) gives "":
    a cell without a brain changes nothing. The whole check has BRAIN_SECONDS: a socket timeout covers
    one read, not a body that arrives a byte at a time or a lookup that hangs, so the call runs in a
    daemon thread the hook stops waiting for."""
    url = os.environ.get("TARES_FACTORY_URL", "").strip()
    if not url:
        return ""
    found: list = []

    def work() -> None:
        try:
            found.append(brain_ready_repos(root, url))
        except Exception:
            pass

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(BRAIN_SECONDS)
    ready = found[0] if found else []
    if not ready:
        return ""
    names = ", ".join(ready[:5]) + (f" and {len(ready) - 5} more" if len(ready) > 5 else "")
    return BRAIN_ADVICE.format(repos=f" ({names})")


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
        if source == "compact":
            text += (" This session was compacted: your skills are re-attached, so load one again only if its "
                     "re-attached copy says it was truncated or you cannot find a rule you need; a page is never "
                     "re-attached, so Read it again when a situation needs it. Then " + REPORT_READ + " and your work on Tares, check "
                     "get_crew_settings: arm no schedule of your own unless idle_wake is false (the factory wakes you); "
                     "when it is false, check CronList and re-arm your schedule if it is missing. Then carry on "
                     "where your report left off.")
        else:
            text += (f" This session was {PAST.get(source, source + 'd')}: load both skills again, " + REPORT_READ + " and your work on "
                     "Tares, check get_crew_settings: arm no schedule of your own unless idle_wake is false (the "
                     "factory wakes you); when it is false, check CronList and re-arm your schedule if it is missing. "
                     "Then carry on where your report left off.")
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
    brain = brain_line(plugin_root())
    if brain:
        text = f"{text} {brain}" if text else brain
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

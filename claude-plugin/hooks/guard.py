"""PreToolUse guard for factory stations: one owner per action type, halt, heavy commands.

The Python twin of guard.sh (TR-971), run as `tares hook guard`. It reads the hook payload on stdin and
prints a deny decision, or nothing to allow. Sessions without FACTORY_ROLE (the person's own) are never
touched. A payload that is not JSON, or has no tool name, is allowed, as with jq in guard.sh.

Standard library only and nothing from the tares package: a hook starts at every tool call.

The regular expressions are the ones guard.sh hands to perl and grep -E, matched one line at a time
as grep does, so a decision here is the decision there. A change to either file goes into both until
guard.sh is removed (TR-1103).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time


def _re(pattern: str, flags: int = 0):
    """A pattern with \\w, \\s, \\d and \\b read as ASCII, as perl and grep -E read them in guard.sh
    (Python's default is Unicode: an accented letter would count as a word character here)."""
    return re.compile(pattern, flags | re.ASCII)


# A quoted string, to be skipped as a unit (the scoped s flag lets a backslash escape a newline in it).
QUOTED = r"""(?s:"(?:[^"\\]|\\.)*"|'[^']*')"""
# An option that carries prose: -m, --message, -b, --body, -t, --title, -n, --notes.
FLAG = r"(^|\s)(-m|--message|-b|--body|-t|--title|-n|--notes)(\s+|=)"
HEREDOC_MESSAGE = _re(
    FLAG + r"""\"\$\(cat\s*<<-?\s*(['"])(\w+)\4[ \t]*\n(?:(?!\n\t*\5\n).)*?\n\t*\5\n\s*\)\"""", re.S)
QUOTED_MESSAGE = _re(FLAG + r"""("(?:[^"\\]|\\.)*"|'[^']*')""")
SPAWN_TASK = _re(r"""(\bfactory\s+(?:spawn|revive|restart)\b)((?:[^\n;&|"']|"(?:[^"\\]|\\.)*"|'[^']*')*)""")
SPAWN_QUOTED = _re(r"""("(?:[^"\\]|\\.)*"|'[^']*')""")
READER_FOLDER = _re(
    "(" + QUOTED + r")|((?:^|[;&|(`\n])[ \t]*(?:ls|cd|pushd|cat|head|tail|less|more|wc|stat|file|du|tree|grep|test|realpath)"
    r"""(?:[ \t]+(?:[^\s;&|()`"'\\]|\\.|""" + QUOTED + r""")+)*?[ \t]+)(?:[^\s;&|()`"']*/)?factory(?=[ \t])""",
    re.M)
COMMAND_SPLIT = _re(r"\|\||&&|[;&|]")
# The quoted-string scans rescan the rest of the command at every quote, so a command with thousands of
# quotes takes seconds (a hook gets 5 s, and a timeout lets the command through). Above this many
# characters they are skipped: nothing is blanked, so the guard sees more of the text and refuses more,
# never less. guard.sh has no such limit.
MAX_QUOTE_SCAN = 10000

# The factory CLI by its name, whatever its path or quoting: `factory x`, `/abs/bin/factory x`,
# `tares factory x`, `(factory x)`, `$(factory x)`, `"factory" x`, `bash -c "factory x"`. A leading dot
# (`.factory`) is a folder, not the command. FACTORY_END closes a verb: anything but a letter, digit,
# `_` or `-`, or the end.
FACTORY_CLI = r"""(^|[;&|\s(`])["']?([^\s;&|"']*/)?factory["']?\s+"""
FACTORY_END = r"([^\w-]|$)"

KUBECTL = r"kubectl(\s+[^\s;&|]+)*\s+(apply|rollout|delete|scale|set|patch|replace|create)(\s|$)"
HELM = r"helm(\s+[^\s;&|]+)*\s+(install|upgrade|uninstall|rollback)(\s|$)"

HALTED = _re(
    r"(gh\s+pr\s+merge|git\s+push|git\s+tag\s+[^-]|gh\s+release\s+create|" + KUBECTL + "|" + HELM
    + r"|claude\s+(--bg|--background))")
HALTED_SESSION = _re(FACTORY_CLI + "(spawn|revive)" + FACTORY_END)
HEAVY = _re(
    r"(^|\s)(docker\s+(build|buildx)|govulncheck|kind\s+create\s+cluster|trivy|snyk\s+test)(\s|$)")
HEAVY_WRAPPER = _re(r"^\s*factory-heavy(\s|$)")
FINISH = _re(FACTORY_CLI + "finish" + FACTORY_END)
PR_REVIEW = _re(r"gh\s+pr\s+review")
PR_MERGE = _re(r"gh\s+pr\s+merge|gh\s+api[^;&|]*pulls/[0-9]+/merge")
STATUSES = _re(r"statuses/")
GH_API = _re(r"gh\s+api")
TAGS = _re(
    r"(git\s+tag\s+[^-]|git\s+push[^;&|]*[\s:](--tags|refs/tags|v[0-9]))|gh\s+release\s+(create|edit|delete)")
DEPLOY = _re("(" + KUBECTL + "|" + HELM + ")")
CLAUDE_STATIONS = _re(r"(^|[;&|\s])claude\s+(--bg|--background|stop|kill|rm|respawn)(\s|$)")
STATION_VERBS = _re(
    FACTORY_CLI + r"((spawn|retire|revive|restart|down|halt)|crew\s+(up|down|halt|resume|freeze|unfreeze|update|restart))"
    + FACTORY_END)

# Posting to people only; reading Slack, and every other tool, stays open to all stations.
SLACK_SEND = _re(
    r"mcp__.*slack.*send.*|mcp__.*slack.*schedule.*|mcp__.*slack.*post.*|mcp__.*slack.*update.*message.*", re.S)

MESSAGE_PEOPLE = ("Crew stations don't message people. The person reads the crew in the orchestrator's session "
                  "and on Tares; send the orchestrator an ASK instead.")
HEAVY_REASON = ("Heavy command: run it through `factory-heavy <timeout-seconds> <command...>` so only one runs "
                "at a time with a hard timeout.")
FINISH_REASON = ("Only the person marks a project done from their terminal. Ask them on the desk, record their "
                 "answer with desk_answer, then call finish_project.")
REVIEW_REASON = ("Factory sessions share one GitHub account and GitHub refuses self-approval. The reviewer posts "
                 "a PR comment plus the factory/review commit status instead.")


class Deny(Exception):
    """A rule fired: the reason goes to the model and the event log."""


def jq_text(value, default: str = "") -> str:
    """What `jq -r '.x // "default"'` prints: the default for null or false, a string as is (an empty
    one too), anything else as jq's indented JSON."""
    if value is None or value is False:
        return default
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def parse(raw: str):
    """(tool, command) of the payload, or ("", "") when jq would fail on it. Like `$(...)`, drops trailing newlines."""
    try:
        data = json.loads(raw)
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    tool = jq_text(data.get("tool_name"))
    inp = data.get("tool_input")
    cmd = jq_text(inp.get("command")) if isinstance(inp, dict) else ""
    return tool.rstrip("\n"), cmd.rstrip("\n")


def station_role(env) -> str:
    """The role the station acts under. A helper works under its parent: <crew>-<role>, or a builder
    named <project>-build-<slug> (<crew>-<project>-build-<slug> in a crew with its own name)."""
    role = env.get("FACTORY_ROLE", "")
    if role != "helper":
        return role
    crew = env.get("FACTORY_CREW") or "crew"
    parent = env.get("FACTORY_PARENT", "")
    for owner in ("orchestrator", "reviewer", "releaser"):
        if parent.startswith(crew + "-" + owner):
            return parent[len(crew) + 1:]
    if "-build-" in parent:
        return "builder"
    return role


def blank_prose(cmd: str) -> str:
    """The command with the text that is only prose blanked out, so "fix the gh pr merge race" in a
    commit message does not trip a rule.

    The quoted message of a commit, PR body, title or release notes, and the task or prompt handed to
    factory spawn, revive or restart, are prose. A double-quoted one that substitutes a command ($(...) or
    backticks) runs that command, so it stays visible, except the "$(cat <<'EOF' ... EOF)" form, whose
    quoted delimiter keeps the body literal. A folder named factory handed to a reader (`ls ~/factory down`)
    is an argument, not the CLI. Only readers that neither run their arguments nor print text for a shell to
    run are listed: find, xargs, env, the shells, echo, printf and rg (--pre) are not. The reader must sit
    at the start of a command outside any quote, and its arguments are plain words or whole quoted strings.
    """
    text = cmd + "\n"  # guard.sh hands perl a here-string, which ends in a newline

    def message(m):
        return m.group(1) + m.group(2) + " _"

    def keep_substitution(q: str) -> bool:
        return q.startswith('"') and ("$(" in q or "`" in q)

    def quoted_message(m):
        return m.group(0) if keep_substitution(m.group(4)) else m.group(1) + m.group(2) + " _"

    def spawn_task(m):
        rest = SPAWN_QUOTED.sub(lambda q: q.group(1) if keep_substitution(q.group(1)) else "_", m.group(2))
        return m.group(1) + rest

    def reader(m):
        return m.group(1) if m.group(1) is not None else m.group(2) + "_"

    text = HEREDOC_MESSAGE.sub(message, text)
    scan_quotes = len(text) <= MAX_QUOTE_SCAN
    if scan_quotes:
        text = QUOTED_MESSAGE.sub(quoted_message, text)
    text = SPAWN_TASK.sub(spawn_task, text)
    if scan_quotes:
        text = READER_FOLDER.sub(reader, text)
    return text.rstrip("\n")


def grep(pattern, text: str) -> bool:
    """grep -Eq: does any line of the text match."""
    return any(pattern.search(line) for line in text.split("\n"))


def halt_reason(path: str) -> str:
    """The reason in the HALT file, as `jq -r '.reason // "no reason given"'` prints it ("" for a bad file)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return jq_text(data.get("reason"), "no reason given")


def granted(env, role: str, cmd: str) -> bool:
    """The person allowed exactly this command for this station in the console (TR-644): a line of the
    grants the station started with holds the whole command in backticks and names the station, or
    its role as the console's sentence does ("The releaser may run ..."). Lifts only the one-owner
    rules. A command over several lines never matches."""
    home, station = env.get("FACTORY_HOME", ""), env.get("FACTORY_STATION", "")
    if not home or not station or "\n" in cmd:
        return False
    # the console keeps a grant's words with runs of white space closed up
    want = "`" + " ".join(cmd.split()) + "`"
    if want == "``":
        return False
    try:
        with open(os.path.join(home, "stations", station + ".grants"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    return any(want in line and (station in line or f"the {role}" in line.lower()) for line in lines)


def decide(env, tool: str, cmd: str) -> None:
    """Raise Deny with the reason when the station may not run this tool call; return when it may."""
    role = station_role(env)

    def owner_only(owner: str, action: str) -> None:
        if role != owner and not granted(env, role, cmd):
            raise Deny(f"In this factory only the {owner} station may {action}. "
                       "Send it a message instead (see the factory-protocol skill), "
                       "or ask the person to allow this exact command in the console.")

    if SLACK_SEND.fullmatch(tool):
        raise Deny(MESSAGE_PEOPLE)
    if tool != "Bash":
        return
    scan = blank_prose(cmd)

    home = env.get("FACTORY_HOME", "")
    halt_file = os.path.join(home, "HALT") if home else ""
    if halt_file and os.path.isfile(halt_file):
        if grep(HALTED, scan) or grep(HALTED_SESSION, scan):
            raise Deny(f"The crew is halted ({halt_reason(halt_file)}). Finish your current step safely and "
                       "start nothing new; `factory crew resume` lifts the halt.")

    # Heavy commands must go through factory-heavy (one at a time, hard timeout), checked per command in
    # a chain, so `factory-heavy 9 true; docker build .` does not carry the second one past the lock.
    for part in COMMAND_SPLIT.sub("\n", scan).split("\n"):
        if HEAVY_WRAPPER.search(part):
            continue
        if HEAVY.search(part):
            raise Deny(HEAVY_REASON)

    if grep(FINISH, scan):
        raise Deny(FINISH_REASON)
    if grep(PR_REVIEW, scan):
        raise Deny(REVIEW_REASON)
    if grep(PR_MERGE, scan):
        owner_only("builder", "merge pull requests")
    if grep(STATUSES, scan) and grep(GH_API, scan):
        owner_only("reviewer", "set commit statuses")
    if grep(TAGS, scan):
        owner_only("releaser", "create tags or releases")
    if grep(DEPLOY, scan):
        owner_only("releaser", "deploy, roll out or roll back")
    if grep(CLAUDE_STATIONS, scan):
        owner_only("orchestrator", "start, stop or revive stations")
    if grep(STATION_VERBS, scan):
        owner_only("orchestrator", "start, stop or revive stations")


def deny_output(reason: str) -> bytes:
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                  "permissionDecisionReason": reason}}
    return (json.dumps(out, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def log_deny(env, tool: str, reason: str) -> None:
    """Append a guard-deny line to the crew's events.jsonl; the log is a courtesy, so it never fails the hook."""
    home = env.get("FACTORY_HOME", "")
    if not home or not os.path.isdir(home):
        return
    line = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "station": env.get("FACTORY_STATION") or "?",
            "event": "guard-deny", "tool": tool, "reason": reason}
    try:
        with open(os.path.join(home, "events.jsonl"), "ab") as f:
            f.write((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    except OSError:
        pass


def main() -> int:
    env = os.environ
    if not env.get("FACTORY_ROLE"):
        return 0
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    tool, cmd = parse(raw)
    try:
        decide(env, tool, cmd)
    except Deny as d:
        reason = str(d)
        sys.stdout.buffer.write(deny_output(reason))
        sys.stdout.buffer.flush()
        log_deny(env, tool, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())

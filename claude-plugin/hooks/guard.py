"""PreToolUse guard for factory stations: one owner per action type, halt, heavy commands.

Run as `tares hook guard` (TR-971; it replaced guard.sh, TR-1103). It reads the hook payload on stdin and
prints a deny decision, or nothing to allow. Sessions without FACTORY_ROLE (the person's own) are never
touched. A payload that is not JSON, or has no tool name, is allowed.

Standard library only and nothing from the tares package: a hook starts at every tool call.

The regular expressions are the ones guard.sh used to hand to perl and grep -E, matched one line at a time
as grep did, so a decision is the one guard.sh made. tests/guard_cases.jsonl and
tests/guard_extra_cases.jsonl pin them.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time


def _re(pattern: str, flags: int = 0):
    """A pattern with \\w, \\s, \\d and \\b read as ASCII, as perl and grep -E read them in the old guard.sh
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
# never less. the old guard.sh had no such limit.
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
HEAVY_WRAPPER = _re(r"^\s*([^\s;&|\"']*/)?tares\s+factory\s+heavy(\s|$)")
FINISH = _re(FACTORY_CLI + "finish" + FACTORY_END)
PR_REVIEW = _re(r"gh\s+pr\s+review")
PR_MERGE = _re(r"gh\s+pr\s+merge|gh\s+api[^;&|]*pulls/[0-9]+/merge")
STATUSES = _re(r"statuses/")
GH_API = _re(r"gh\s+api")
# A tag made, changed or deleted, or a push that names one. `git tag -l` and the other listing forms stay open; a
# flag that writes (-d, -f, -a, -s, -u, -m, -F, -e and the long forms) is a tag command too. A push names a tag
# when it says --tags, --follow-tags, --mirror, refs/tags, or a ref like v1.2 or paper-tokens-v3 (also as `:name`,
# which deletes it). A branch such as `vladimir/tr-1-slug` has a slash and is not one, nor is `docs-v2-notes`: a name
# ending in `-v<digits>` is.
TAGS = _re(
    r"(git\s+tag\s+[^-]|git\s+tag\b[^;&|]*\s(-[a-zA-Z0-9]*[dfasumFe][a-zA-Z0-9]*|--(delete|force|annotate|sign|local-user|message|file|edit))(\s|=|$)"
    r"|git\s+push[^;&|]*[\s:+](--tags|--follow-tags|--mirror|refs/tags|v[0-9]|[\w.-]*-v[0-9][0-9.]*(?:-?(?:rc|alpha|beta|pre|dev)[0-9.]*|[a-z])?(?![\w-])))"
    r"|gh\s+release\s+(create|edit|delete)")
DEPLOY = _re("(" + KUBECTL + "|" + HELM + ")")
CLAUDE_STATIONS = _re(r"(^|[;&|\s])claude\s+(--bg|--background|stop|kill|rm|respawn)(\s|$)")
STATION_VERBS = _re(
    FACTORY_CLI + r"((spawn|retire|revive|restart|down|halt)|crew\s+(up|down|halt|resume|freeze|unfreeze|update|restart))"
    + FACTORY_END)

# Posting to people only; reading Slack, and every other tool, stays open to all stations.
SLACK_SEND = _re(
    r"mcp__.*slack.*send.*|mcp__.*slack.*schedule.*|mcp__.*slack.*post.*|mcp__.*slack.*update.*message.*", re.S)

# Slack stays read only whatever its server calls its tools: of an MCP server with slack in its name (the official
# plugin is `mcp__plugin_slack_slack__<tool>`, a third-party one `mcp__<name>__<tool>`), only the tools listed here
# run; a write tool, an unknown verb or a tool added later is refused. Claude Code has no per-tool allow list for a
# server and its auto mode leaves a tool no rule names to a classifier, so the list is kept here, where a hook
# decides in every permission mode. A read tool missing from it is refused until it is added.
SLACK_READ_TOOLS = frozenset((
    "slack_read_channel", "slack_read_thread", "slack_read_user_profile", "slack_read_file", "slack_read_canvas",
    "slack_search_public", "slack_search_public_and_private", "slack_search_channels", "slack_search_users",
    "slack_list_channel_members", "slack_get_reactions",
    "conversations_history", "conversations_replies", "conversations_search_messages", "channels_list"))

MESSAGE_PEOPLE = ("Crew stations don't message people. The person reads the crew in the orchestrator's session "
                  "and on Tares; send the orchestrator an ASK instead.")
SLACK_READ_ONLY = ("Crew stations read Slack and never write it, and only the Slack read tools are open to them. "
                   "This tool is not one of them; send the orchestrator an ASK instead.")
HEAVY_REASON = ("Heavy command: run it through `tares factory heavy <timeout-seconds> <command...>` so only one runs "
                "at a time with a hard timeout.")
FINISH_REASON = ("Only the person marks a project done from their terminal. Ask them on the desk, record their "
                 "answer with desk_answer, then call finish_project.")
REVIEW_REASON = ("Factory sessions share one GitHub account and GitHub refuses self-approval. The reviewer posts "
                 "a PR comment plus the factory/review commit status instead.")


def slack_not_read(tool: str) -> bool:
    """Whether the tool is one of an MCP server with slack in its name (any case) that is not on the read list.
    A name with several `__` can be read more than one way (`mcp__a__b__c` is tool `b__c` of server `a`, or tool `c`
    of server `a__b`), so every way it splits is tried and the tool is a read only if each Slack reading of it is."""
    if not tool.startswith("mcp__"):
        return False
    rest = tool[5:]
    cuts = [i for i in range(len(rest)) if rest.startswith("__", i)]
    if not cuts:
        return "slack" in rest.lower()
    return any("slack" in rest[:i].lower() and rest[i + 2:] not in SLACK_READ_TOOLS for i in cuts)


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


def payload_cwd(raw: str) -> str:
    """The folder the tool call runs in, as the payload says; "" when it does not say."""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    cwd = data.get("cwd") if isinstance(data, dict) else None
    return cwd if isinstance(cwd, str) else ""


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
    text = cmd + "\n"  # guard.sh handed perl a here-string, which ends in a newline

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


# A grant that names a tag (TR-1574): a backticked clause `tag: <owner>/<repo> <pattern>` in a line of the grants the
# station started with. The pattern is a tag name, or a name with <N> for a number (`paper-tokens-v<N>`). Prose in a
# grant that only mentions a tag lifts nothing.
TAG_GRANT = _re(r"`tag: ([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*) ([A-Za-z0-9][\w.-]*(?:<N>[\w.-]*)*)`")
# A GitHub remote URL as git spells it (https, scp-like ssh, ssh://), matched from its first character so a path or
# a host that only contains `github.com` is not one.
GITHUB_REMOTE = _re(
    r"(?:https://(?:[\w.~%-]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?")
# What a tag command may be made of: words, `/`, `.`, `@`, `~`, `-`, spaces and `&&`. No quote, `$`, `;`, `|`,
# redirect, newline, `:` (a refspec) or `=`: what is not plain is refused, never guessed at.
PLAIN_COMMAND = _re(r"[\w./@~ &-]+")
SHA = _re(r"[0-9a-f]{7,40}")
# The longest `&&` chain a tag command may be: cd, fetch, tag, push and ls-remote twice over still fit.
MAX_TAG_SEGMENTS = 8
# Seconds after which no further folder is looked up (each lookup is two git calls of 1.5 s at most): the hook's own
# 5 s limit fails open, so the guard must answer before it.
TAG_GIT_BUDGET = 1.5


def crew_has_releaser(env) -> bool:
    """Whether the crew has a releaser station (a role `releaser` in the roster on disk). A roster that can't be
    read counts as one: the tag rule stays shut when it can't be sure."""
    home = env.get("FACTORY_HOME", "")
    try:
        with open(os.path.join(home, "stations.json"), encoding="utf-8", errors="replace") as f:
            roster = json.load(f)
    except (OSError, ValueError):
        return True
    if not isinstance(roster, dict):
        return True
    return any(isinstance(s, dict) and s.get("role") == "releaser" for s in roster.values())


def tag_grants(env) -> list:
    """(owner/repo, compiled pattern of the tag name) of each clause in the station's grants (see TAG_GRANT)."""
    home, station = env.get("FACTORY_HOME", ""), env.get("FACTORY_STATION", "")
    if not home or not station:
        return []
    try:
        with open(os.path.join(home, "stations", station + ".grants"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    return [(repo.lower(), re.compile(r"[1-9][0-9]*".join(re.escape(x) for x in name.split("<N>")), re.ASCII))
            for repo, name in TAG_GRANT.findall(text)]


def origin_repo(folder: str) -> str:
    """owner/repo of the GitHub remote `origin` of the repository at folder, read from its config (no network);
    "" when there is none. Every URL origin has counts, the ones it fetches from and the ones it pushes to (a
    `pushurl` can name another repo than `url`, and `url` can be set twice): they must all be GitHub URLs of one
    repo, or there is no answer."""
    repos = set()
    for flags in ([], ["--push"]):
        try:
            out = subprocess.run(["git", "-C", folder, "remote", "get-url", "--all"] + flags + ["origin"],
                                 capture_output=True, text=True, timeout=1.5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
        for url in out.split("\n"):
            if not url:
                continue
            m = GITHUB_REMOTE.fullmatch(url.strip())
            if not m:
                return ""
            repos.add((m.group(1) + "/" + m.group(2)).lower())
    return repos.pop() if len(repos) == 1 else ""


def tag_granted(env, role: str, cmd: str, cwd: str) -> bool:
    """A builder of a crew without a releaser may tag what a grant of its project names (TR-1574): the command is
    only `git tag <name> <sha>`, `git push origin <name>`, `git fetch origin`, `git ls-remote --tags origin <name>`
    and `cd <folder>`, joined with &&, every name is one a grant names for the repo `origin` of the folder it runs
    in (each call of the usual two-step flow is decided on its own). A flag, a refspec, `--tags` or a second remote
    is never in that list, so deleting, moving and bulk pushes stay refused."""
    if role != "builder" or crew_has_releaser(env) or not PLAIN_COMMAND.fullmatch(cmd):
        return False
    grants = tag_grants(env)
    if not grants:
        return False
    segments = cmd.split("&&")
    if len(segments) > MAX_TAG_SEGMENTS:
        return False
    folder = cwd or os.getcwd()
    asked = []  # (folder, tag name) of each call that has to be granted
    # First the shape of every segment, with no git call: a long chain that ends in a refused one is refused at once,
    # not after one `git remote` call per segment (a hook gets 5 s, and a timeout lets the command through).
    for seg in segments:
        words = seg.split()
        if not words or "&" in seg:
            return False
        if words[0] == "cd" and len(words) == 2:
            # `..` is refused: the shell's cd follows it in the path as typed, git -C in the real folder, so after a
            # link the two name different repos.
            # A word that starts with `-` is an option or `cd -` (OLDPWD) to the shell, never a folder named so.
            if ".." in words[1].split("/") or words[1].startswith("-"):
                return False
            folder = os.path.join(folder, os.path.expanduser(words[1]))
            continue
        if words == ["git", "fetch", "origin"]:
            continue
        if len(words) == 4 and words[:2] == ["git", "tag"] and SHA.fullmatch(words[3]):
            name = words[2]
        elif len(words) == 4 and words[:3] == ["git", "push", "origin"]:
            name = words[3]
        elif len(words) == 5 and words[:4] == ["git", "ls-remote", "--tags", "origin"]:
            name = words[4]
        else:
            return False
        asked.append((folder, name))
    # One answer per folder, and a deadline for all of them: a hook gets 5 s and a timeout lets the command through.
    deadline = time.monotonic() + TAG_GIT_BUDGET
    repos = {}
    for folder, name in asked:
        if folder not in repos:
            if time.monotonic() > deadline:
                return False
            repos[folder] = origin_repo(folder)
        if not any(repos[folder] == r and p.fullmatch(name) for r, p in grants):
            return False
    return bool(asked)


def decide(env, tool: str, cmd: str, cwd: str = "") -> None:
    """Raise Deny with the reason when the station may not run this tool call; return when it may."""
    role = station_role(env)

    def owner_only(owner: str, action: str) -> None:
        if role != owner and not granted(env, role, cmd):
            raise Deny(f"In this factory only the {owner} station may {action}. "
                       "Send it a message instead (see the factory-protocol skill), "
                       "or ask the person to allow this exact command in the console.")

    if SLACK_SEND.fullmatch(tool):
        raise Deny(MESSAGE_PEOPLE)
    if slack_not_read(tool):
        raise Deny(SLACK_READ_ONLY)
    if tool != "Bash":
        return
    scan = blank_prose(cmd)

    home = env.get("FACTORY_HOME", "")
    halt_file = os.path.join(home, "HALT") if home else ""
    if halt_file and os.path.isfile(halt_file):
        if grep(HALTED, scan) or grep(HALTED_SESSION, scan):
            raise Deny(f"The crew is halted ({halt_reason(halt_file)}). Finish your current step safely and "
                       "start nothing new; `tares factory crew resume` lifts the halt.")

    # Heavy commands must go through `tares factory heavy` (one at a time, hard timeout), checked per command in
    # a chain, so `tares factory heavy 9 true; docker build .` does not carry the second one past the lock.
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
    if grep(TAGS, scan) and not tag_granted(env, role, cmd, cwd):
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
        decide(env, tool, cmd, payload_cwd(raw))
    except Deny as d:
        reason = str(d)
        sys.stdout.buffer.write(deny_output(reason))
        sys.stdout.buffer.flush()
        log_deny(env, tool, reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())

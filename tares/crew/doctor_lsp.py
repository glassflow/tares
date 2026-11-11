"""Code intelligence and cmux checks of `doctor`, and the offer `crew up` makes (TR-970, ports bash lsp_*,
cmux_* and doctor_cmux / doctor_lsp / lsp_offer).

The languages of each handed-over repo are read from its manifests (at the root or one folder down, never
inside node_modules, .git, .factory, .venv or venv). A language has a server for Claude Code when an
enabled user-scope plugin and its binary are both there. `crew up` calls `lsp_offer` (Up.lsp_offer in up.py);
it takes its inputs as arguments.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

from . import claude
from .errors import CrewError
from .paths import ME

MARKETPLACE = "claude-plugins-official"
PRUNE = ("node_modules", ".git", ".factory", ".venv", "venv")

# language, manifest file names, plugin, binary, install command (bash lsp_langs, in this order)
LANGS = (
    ("Python", ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt"), "pyright-lsp",
     "pyright-langserver", "npm install -g pyright"),
    ("TypeScript", ("tsconfig.json",), "typescript-lsp", "typescript-language-server",
     "npm install -g typescript-language-server typescript"),
    ("JavaScript", ("package.json",), "typescript-lsp", "typescript-language-server",
     "npm install -g typescript-language-server typescript"),
    ("Go", ("go.mod",), "gopls-lsp", "gopls", "go install golang.org/x/tools/gopls@latest"),
    ("Rust", ("Cargo.toml",), "rust-analyzer-lsp", "rust-analyzer", "rustup component add rust-analyzer"),
)
BY_NAME = {row[0]: row for row in LANGS}

CMUX_ON = ("cmux shows \"Waiting for you\" about a minute after every station's turn ends, with nothing for you to do. "
           "To stop it and keep the permission alerts, set \"notifications\": {\"agentIdleReminder\": false} in %s "
           f"(it applies to all your cmux agents; {ME} never edits it)")
CMUX_OFF = ("cmux's idle reminder is off, so a station between turns does not ask for you; permission alerts "
            "stay on")
CMUX_UNREADABLE = ("could not parse %s; set \"notifications\": {\"agentIdleReminder\": false} there so cmux "
                   "stops saying \"Waiting for you\" after every station turn")

PLUGIN_LIST_TIMEOUT = 60


# ---------- languages of a repo ----------


def _names(row) -> tuple:
    return row[1]


def detect(repo: str) -> list:
    """The languages a repo's manifests show, in table order, each plugin once (a tsconfig.json makes it
    TypeScript, so a package.json next to it is not also JavaScript). Looks at the repo's root and one folder
    down, files only, without following links; the folders in PRUNE are skipped."""
    found = set()
    try:
        top = list(os.scandir(repo))
    except OSError:
        return []

    def note(entry) -> None:
        try:
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            return
        if is_file:
            found.add(entry.name)

    subdirs = []
    for e in top:
        note(e)
        try:
            if e.is_dir(follow_symlinks=False) and e.name not in PRUNE:
                subdirs.append(e.path)
        except OSError:
            pass
    for d in subdirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    note(e)
        except OSError:
            continue
    seen, langs = set(), []
    for row in LANGS:
        if row[2] in seen:
            continue
        if found.intersection(_names(row)):
            langs.append(row[0])
            seen.add(row[2])
    return langs


# ---------- what Claude Code has ----------


def read_plugins(claude_path: str = ""):
    """[(id, projectPath)] for each enabled plugin `claude plugin list --json` shows, or None when claude cannot
    list them (no claude on PATH, a failing command, a timeout, output that is not a list of plugins): that
    means unknown. A missing claude must not end the doctor's report, so CrewError is unknown too."""
    try:
        r = claude.run_claude(["plugin", "list", "--json"], claude_path or None, PLUGIN_LIST_TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError, CrewError):
        return None
    if r.returncode != 0:
        return None
    try:
        rows = json.loads(r.stdout)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    out = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if row.get("enabled") is True:
            path = row.get("projectPath")
            out.append((str(row.get("id")), "" if path is None else str(path)))
    return out


def have_bin(name: str, env: dict) -> bool:
    """FACTORY_LSP_PATH, when set and not empty, is the only place to look (tests); otherwise PATH."""
    search = env.get("FACTORY_LSP_PATH") or env.get("PATH")
    return shutil.which(name, path=search) is not None


def state(plugins, plugin: str, binary: str, env: dict) -> str:
    """ready, no-plugin, no-binary, neither, or unknown (the plugins could not be listed). Only a user-scope
    plugin counts (no projectPath): stations run in the crew home and in worktrees, never in the repo path."""
    if plugins is None:
        return "unknown"
    want = f"{plugin}@{MARKETPLACE}"
    has_plugin = any(i == want and p == "" for i, p in plugins)
    has_bin = have_bin(binary, env)
    if has_plugin and has_bin:
        return "ready"
    if has_plugin:
        return "no-binary"
    return "no-plugin" if has_bin else "neither"


def fix(st: str, plugin: str, install: str) -> list:
    """The commands that make the language server work."""
    cmds = []
    if st in ("no-binary", "neither"):
        cmds.append(install)
    if st in ("no-plugin", "neither"):
        cmds.append(f"claude plugin install {plugin}@{MARKETPLACE}")
    return cmds


def why(st: str, plugin: str, binary: str) -> str:
    if st == "no-binary":
        return f"the {plugin} plugin is on, {binary} is not on PATH"
    if st == "no-plugin":
        return f"{binary} is installed, the {plugin} plugin is not on"
    return f"neither the {plugin} plugin nor {binary} is there"


def doctor_rows(rows, plugins, env: dict) -> list:
    """[(level, project, lang, message, fix commands)] in bash's order, for each (project, repo)."""
    out = []
    for project, repo in rows:
        if not os.path.isdir(repo):
            continue
        for lang in detect(repo):
            _n, _m, plugin, binary, install = BY_NAME[lang]
            st = state(plugins, plugin, binary, env)
            if st == "ready":
                out.append(("ok", project, lang, f"project {project}: {lang} in {repo} has a language server for "
                            f"Claude Code ({plugin} plugin, {binary} on PATH)", []))
            elif st == "unknown":
                out.append(("warn", project, lang, f"project {project}: {lang} in {repo}: cannot tell whether "
                            "Claude Code has a language server, because claude plugin list --json failed", []))
            else:
                cmds = fix(st, plugin, install)
                shown = " then ".join(f"`{c}`" for c in cmds)
                out.append(("warn", project, lang, f"project {project}: {lang} in {repo} has no language server for "
                            f"Claude Code, so the LSP tool cannot answer for it ({why(st, plugin, binary)}). "
                            f"Run {shown}; {ME} crew up offers it", cmds))
    return out


def missing(rows, plugins, env: dict) -> list:
    """(lang, plugin, binary, install, state, repo) for each repo language with no working server; what `crew up`
    offers. Unknown (plugins could not be listed) is left out: nothing is offered then."""
    out = []
    for _project, repo in rows:
        if not os.path.isdir(repo):
            continue
        for lang in detect(repo):
            _n, _m, plugin, binary, install = BY_NAME[lang]
            st = state(plugins, plugin, binary, env)
            if st not in ("ready", "unknown"):
                out.append((lang, plugin, binary, install, st, repo))
    return out


# ---------- cmux ----------


def cmux_config(env: dict) -> str:
    return os.path.join(claude._home(env), ".config", "cmux", "cmux.json").replace("\\", "/")


def cmux_idle_reminder(env: dict) -> str:
    """on, off or unreadable (JSONC: whole-line comments and trailing commas are fine); "" when cmux has no
    config here, or the file is empty."""
    path = cmux_config(env)
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return "unreadable"
    text = "".join(ln for ln in text.splitlines(keepends=True) if not re.match(r"^[ \t\r\f\v]*//", ln))
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    if not text.strip():
        return ""
    try:
        doc = json.loads(text)
    except ValueError:
        return "unreadable"
    # jq reads `.notifications.agentIdleReminder` of an object; anything else is an error there
    if not isinstance(doc, dict):
        return "unreadable"
    n = doc.get("notifications")
    if n is not None and not isinstance(n, dict):
        return "unreadable"
    return "off" if isinstance(n, dict) and n.get("agentIdleReminder") is False else "on"


def cmux_check(env: dict):
    """(level, message) for doctor, or None when cmux has no config here."""
    st = cmux_idle_reminder(env)
    if st == "on":
        return "warn", CMUX_ON % cmux_config(env)
    if st == "off":
        return "ok", CMUX_OFF
    if st == "unreadable":
        return "warn", CMUX_UNREADABLE % cmux_config(env)
    return None


# ---------- the offer crew up makes ----------


def offer(missing_rows, assume_yes: bool, claude_path: str = "", say=None, err=None, ask=None,
          run_install=None, run_claude_install=None) -> list:
    """Recommend the missing language servers, and install them only on a yes. Returns the plugins installed.
    err prints a line to stderr, say prints a `crew up` progress line, ask(prompt) returns the answer or None
    for a closed input, run_install(command) runs an install command and returns True on success,
    run_claude_install(plugin_id) does the same for `claude plugin install`."""
    if not missing_rows:
        return []
    err = err or (lambda text: print(text, file=sys.stderr))
    say = say or (lambda text: print(f"{ME}: {text}", file=sys.stderr))
    err("\nCode intelligence: Claude Code can find a definition, its references and its callers through a language "
        "server,\nso builders and the reviewer search less and read less. These repos have none wired in yet:")
    for lang, plugin, _bin, install, st, repo in missing_rows:
        err(f"  {lang} in {repo}")
        for cmd in fix(st, plugin, install):
            err(f"      {cmd}")
    if assume_yes:
        say("not installing language servers without a yes: run the commands above yourself, or run crew up "
            "without --yes")
        return []
    answer = (ask or _ask)("Run these now?\n  recommended: yes\n  press enter to take it, or type no\n> ")
    if answer is None:
        answer = "no"   # no answer at all (a closed input) is a no
    if (answer or "yes") not in ("y", "Y", "yes", "Yes"):
        say(f"no language servers installed; {ME} doctor lists what is missing")
        return []
    run_install = run_install or _run_install
    run_claude_install = run_claude_install or (lambda pid: _claude_install(pid, claude_path))
    done, seen = [], set()
    for _lang, plugin, binary, install, st, _repo in missing_rows:
        if plugin in seen:
            continue
        seen.add(plugin)
        if st in ("no-binary", "neither"):
            say(f"installing {binary}: {install}")
            if not run_install(install):
                say(f"could not install {binary}; install it yourself ({install})")
                continue
        if st in ("no-plugin", "neither"):
            say(f"installing the {plugin} plugin")
            if run_claude_install(f"{plugin}@{MARKETPLACE}"):
                done.append(plugin)
            else:
                say(f"could not install the {plugin} plugin; run: claude plugin install {plugin}@{MARKETPLACE}")
    return done


def _ask(prompt: str):
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    return line.rstrip("\n") if line else None


def _run_install(command: str) -> bool:
    """Run an install command through the shell ("bash -c" there; the platform shell where bash is not)."""
    bash = shutil.which("bash")
    cmd = [bash, "-c", command] if bash else command
    try:
        return subprocess.run(cmd, shell=not bash, stdin=subprocess.DEVNULL, stdout=2).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _claude_install(plugin_id: str, claude_path: str = "") -> bool:
    try:
        return claude.run_claude(["plugin", "install", plugin_id], claude_path or None, 300).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

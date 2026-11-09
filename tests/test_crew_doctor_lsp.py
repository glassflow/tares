"""Code intelligence and cmux checks of `tares factory doctor`, in Python (TR-970), and the offer `crew up`
makes for a missing language server.

The parity harness (tests/factory/parity.py 'lsp_*' and 'doctor_cmux') compares doctor's lines with bash's on a
real fake crew. This file covers what the harness cannot reach:

  - detect(): the manifests at the root and one folder down, the pruned folders, JavaScript against
    TypeScript, a link that is not followed;
  - state(), fix() and why() for the five states, and the plugin id and scope that count;
  - read_plugins() on every way `claude plugin list --json` can fail (that means unknown, not empty);
  - the cmux reader: on, off, unreadable, an empty file, no file, comments and trailing commas;
  - Doctor.check_lsp / check_cmux: the lines and fixes they add, nothing when Tares lists no repo;
  - offer(), which `crew up` will call (crew up is not ported yet; its bash cases in lsp_test.sh need it):
    --yes installs nothing, a closed input is a no, a no installs nothing, a yes installs each binary and
    each plugin once, an unknown plugin list offers nothing.

Standard library and tares.crew. Run: python tests/test_crew_doctor_lsp.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

from tares.crew import claude, doctor, doctor_lsp as L  # noqa: E402
from tares.crew.errors import ClaudeNotFoundError, CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0
WIN = sys.platform == "win32"
T = Path(os.path.realpath(tempfile.mkdtemp(prefix="doctor-lsp-test-")))


def ck(label, cond, detail: object = ""):
    global P, F
    if cond:
        P += 1
        print("  ok   " + label)
    else:
        F += 1
        print("  FAIL " + label + (" :: " + str(detail) if detail else ""))


def fresh(name: str) -> Path:
    d = T / name
    d.mkdir(parents=True)
    return d


def touch(base: Path, *rel: str) -> None:
    for r in rel:
        p = base.joinpath(*r.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")


def langs(name: str, *files: str) -> list:
    d = fresh(name)
    touch(d, *files)
    return L.detect(str(d))


def bins(name: str, *names: str) -> dict:
    """An env whose FACTORY_LSP_PATH holds stand-ins for the named server binaries."""
    d = fresh(name)
    for n in names:
        p = d / (n + (".exe" if WIN else ""))
        p.write_text("", encoding="utf-8")
        if not WIN:
            os.chmod(p, 0o755)
    return {"FACTORY_LSP_PATH": str(d), "PATH": ""}


def plug(name: str, path: str | None = None) -> tuple:
    return (f"{name}@claude-plugins-official", path or "")


# ---------- the languages of a repo ----------

print("detect: which languages a repo's manifests show")
ck("a pyproject.toml at the root is Python", langs("d-py", "pyproject.toml") == ["Python"])
ck("each Python manifest counts", all(langs(f"d-py-{n}", n) == ["Python"] for n in
                                      ("setup.py", "setup.cfg", "Pipfile", "requirements.txt")))
ck("a go.mod is Go and a Cargo.toml is Rust", langs("d-go", "go.mod") == ["Go"] and langs("d-rs", "Cargo.toml") == ["Rust"])
ck("a manifest one folder down counts", langs("d-down", "native/Cargo.toml") == ["Rust"])
ck("a manifest two folders down does not", langs("d-deep", "a/b/go.mod", "README.md") == [])
ck("a package.json alone is JavaScript", langs("d-js", "package.json") == ["JavaScript"])
ck("a tsconfig.json is TypeScript, and the package.json next to it is not also JavaScript",
   langs("d-ts", "ui/package.json", "ui/tsconfig.json") == ["TypeScript"])
ck("a TypeScript folder and a JavaScript folder in one repo list the language once (one plugin serves both)",
   langs("d-mix", "a/tsconfig.json", "b/package.json") == ["TypeScript"])
ck("languages come in table order, not file order", langs("d-order", "Cargo.toml", "go.mod", "pyproject.toml") == ["Python", "Go", "Rust"])
ck("node_modules, .git, .factory, .venv and venv are skipped", langs(
    "d-prune", "node_modules/package.json", ".git/go.mod", ".factory/Cargo.toml", ".venv/requirements.txt", "venv/setup.py") == [])
ck("a folder name that only starts like a pruned one is not skipped", langs("d-near", "node_modules_old/go.mod") == ["Go"])
d = fresh("d-dir")
touch(d, "go.mod/x")   # go.mod is a folder here, so it is not a manifest file
ck("a folder where a manifest is expected does not count", L.detect(str(d)) == [])
ck("a repo path that does not exist has no languages", L.detect(str(T / "nope")) == [])
if not WIN:
    d = fresh("d-link")
    real = fresh("d-link-target")
    touch(real, "go.mod")
    os.symlink(real / "go.mod", d / "go.mod")
    os.symlink(real, d / "sub")
    ck("a linked manifest and a linked folder are not followed", L.detect(str(d)) == [])

# ---------- what Claude Code has ----------

print("state, fix and why")
env = bins("b-both", "pyright-langserver")
py = ("pyright-lsp", "pyright-langserver")
ck("plugin and binary: ready", L.state([plug("pyright-lsp")], *py, env) == "ready")
ck("plugin without the binary: no-binary", L.state([plug("pyright-lsp")], *py, bins("b-none")) == "no-binary")
ck("binary without the plugin: no-plugin", L.state([], *py, env) == "no-plugin")
ck("neither: neither", L.state([], *py, bins("b-none2")) == "neither")
ck("plugins that could not be listed: unknown, whatever the binary", L.state(None, *py, env) == "unknown")
ck("a plugin of another marketplace does not count", L.state([("pyright-lsp@somewhere-else", "")], *py, env) == "no-plugin")
ck("a plugin enabled for one project path does not count (stations run elsewhere)",
   L.state([plug("pyright-lsp", "/somewhere/else")], *py, env) == "no-plugin")
ck("another plugin does not count", L.state([plug("gopls-lsp")], *py, env) == "no-plugin")
ck("FACTORY_LSP_PATH, when set, is the only place to look", L.have_bin("pyright-langserver", {**env, "PATH": os.environ.get("PATH", "")})
   and not L.have_bin("pyright-langserver", {**bins("b-other"), "PATH": str(Path(env["FACTORY_LSP_PATH"]))}))
ck("an empty FACTORY_LSP_PATH falls back to PATH, as bash does", L.have_bin("pyright-langserver", {"FACTORY_LSP_PATH": "", "PATH": env["FACTORY_LSP_PATH"]}))
ck("without FACTORY_LSP_PATH, PATH is searched", L.have_bin("pyright-langserver", {"PATH": env["FACTORY_LSP_PATH"]}))
inst = "npm install -g pyright"
pl = "claude plugin install pyright-lsp@claude-plugins-official"
ck("fix: no-binary installs the binary only", L.fix("no-binary", "pyright-lsp", inst) == [inst])
ck("fix: no-plugin installs the plugin only", L.fix("no-plugin", "pyright-lsp", inst) == [pl])
ck("fix: neither installs the binary, then the plugin", L.fix("neither", "pyright-lsp", inst) == [inst, pl])
ck("fix: ready and unknown have nothing to run", L.fix("ready", "pyright-lsp", inst) == [] and L.fix("unknown", "pyright-lsp", inst) == [])
ck("why: each state says which part is there", "plugin is on, pyright-langserver is not on PATH" in L.why("no-binary", *py)
   and "pyright-langserver is installed, the pyright-lsp plugin is not on" in L.why("no-plugin", *py)
   and "neither the pyright-lsp plugin nor pyright-langserver is there" in L.why("neither", *py))

print("read_plugins")


def with_claude(result):
    """Run L.read_plugins with claude.run_claude answering `result` (an object, or an exception to raise)."""
    real = claude.run_claude

    def fake(args, claude_path=None, timeout=0, platform=None):
        fake.args = list(args)
        if isinstance(result, BaseException):
            raise result
        return result
    try:
        claude.run_claude = fake
        return L.read_plugins("/x/claude"), fake.args
    finally:
        claude.run_claude = real


def run_res(code, out):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


rows = json.dumps([{"id": "a@m", "enabled": True, "scope": "user"},
                   {"id": "b@m", "enabled": False, "scope": "user"},
                   {"id": "c@m", "enabled": True, "scope": "local", "projectPath": "/repo"}])
got, args = with_claude(run_res(0, rows))
ck("asks claude for plugin list --json", args == ["plugin", "list", "--json"], args)
ck("keeps the enabled plugins with their project path, user scope as an empty path", got == [("a@m", ""), ("c@m", "/repo")], got)
ck("an empty list is an empty list, not unknown", with_claude(run_res(0, "[]"))[0] == [])
ck("a non-zero exit is unknown", with_claude(run_res(1, rows))[0] is None)
ck("output that is not JSON is unknown", with_claude(run_res(0, "not json"))[0] is None)
ck("JSON that is not a list is unknown", with_claude(run_res(0, '{"id": "a"}'))[0] is None)
ck("a row that is not an object is unknown", with_claude(run_res(0, '["a@m"]'))[0] is None)
ck("claude that cannot start is unknown", with_claude(FileNotFoundError("claude"))[0] is None)
ck("no claude on PATH (a CrewError) is unknown, not a crash", with_claude(ClaudeNotFoundError())[0] is None)
ck("enabled must be true, not just truthy", with_claude(run_res(0, json.dumps([{"id": "a@m", "enabled": "yes"}, {"id": "b@m", "enabled": 1}])))[0] == [])
ck("a timeout is unknown", with_claude(__import__("subprocess").TimeoutExpired("claude", 1))[0] is None)

# ---------- the rows doctor and crew up use ----------

print("doctor_rows and missing")
r1 = fresh("r-api")
touch(r1, "pyproject.toml", "ui/package.json", "ui/tsconfig.json")
r2 = fresh("r-gw")
touch(r2, "go.mod")
env = bins("r-bins", "pyright-langserver")
rows_in = [("Mix", str(r1)), ("Mix", str(r2)), ("Mix", str(T / "gone"))]
got = L.doctor_rows(rows_in, [plug("pyright-lsp")], env)
ck("one row per language of each existing repo, in order", [(g[0], g[2]) for g in got] == [("ok", "Python"), ("warn", "TypeScript"), ("warn", "Go")], got)
ck("a ready server says which plugin and binary", f"project Mix: Python in {r1} has a language server for Claude Code (pyright-lsp plugin, pyright-langserver on PATH)" == got[0][3], got[0][3])
ck("a missing server names the cause, the exact fix and the crew up offer",
   got[1][3].startswith(f"project Mix: TypeScript in {r1} has no language server for Claude Code, so the LSP tool cannot answer for it (neither the typescript-lsp plugin nor typescript-language-server is there). Run `npm install -g typescript-language-server typescript` then `claude plugin install typescript-lsp@claude-plugins-official`; ")
   and got[1][3].endswith("crew up offers it") and len(got[1][4]) == 2, got[1])
ck("a repo that is not on disk is left out", all(str(T / "gone") not in g[3] for g in got))
unk = L.doctor_rows(rows_in, None, env)
ck("unknown plugins: a warn that says it cannot tell, with no fix", {g[0] for g in unk} == {"warn"} and all("cannot tell whether Claude Code has a language server, because claude plugin list --json failed" in g[3] and g[4] == [] for g in unk), unk)
m = L.missing(rows_in, [plug("pyright-lsp")], env)
ck("missing lists what crew up offers: TypeScript and Go, not ready Python", [(x[0], x[4]) for x in m] == [("TypeScript", "neither"), ("Go", "neither")], m)
ck("nothing is missing when the plugins are unknown", L.missing(rows_in, None, env) == [])

# ---------- cmux ----------


def cmux_env(name: str, text: str | None) -> dict:
    home = fresh(name)
    if text is not None:
        f = home / ".config" / "cmux" / "cmux.json"
        f.parent.mkdir(parents=True)
        f.write_text(text, encoding="utf-8", newline="")
    return {"HOME": str(home)}


print("cmux")
ck("no cmux config: nothing to say", L.cmux_idle_reminder(cmux_env("c-none", None)) == "" and L.cmux_check(cmux_env("c-none2", None)) is None)
ck("an empty file: nothing to say", L.cmux_idle_reminder(cmux_env("c-empty", "")) == "")
ck("a file of only comments: nothing to say", L.cmux_idle_reminder(cmux_env("c-comments", "// all defaults\n// nothing here\n")) == "")
ck("a config that does not mention the reminder: on", L.cmux_idle_reminder(cmux_env("c-on", '{"theme": "x"}')) == "on")
ck("agentIdleReminder true: on", L.cmux_idle_reminder(cmux_env("c-on2", '{"notifications": {"agentIdleReminder": true}}')) == "on")
ck("notifications without the reminder key: on", L.cmux_idle_reminder(cmux_env("c-nokey", '{"notifications": {"sound": true}}')) == "on")
ck("a null or 0 reminder is not false: on", all(L.cmux_idle_reminder(cmux_env(f"c-val{i}", '{"notifications": {"agentIdleReminder": %s}}' % v)) == "on" for i, v in enumerate(("null", "0"))))
ck("agentIdleReminder false: off", L.cmux_idle_reminder(cmux_env("c-off", '{"notifications": {"agentIdleReminder": false}}')) == "off")
ck("comment lines and a trailing comma are fine (cmux writes JSONC)",
   L.cmux_idle_reminder(cmux_env("c-jsonc", '// cmux\n{\n  // keep alerts\n  "notifications": {\n    "agentIdleReminder": false,\n  },\n}\n')) == "off")
ck("the string \"false\" is not false", L.cmux_idle_reminder(cmux_env("c-str", '{"notifications": {"agentIdleReminder": "false"}}')) == "on")
ck("a file that is not JSON: unreadable", L.cmux_idle_reminder(cmux_env("c-bad", "{not json")) == "unreadable")
ck("a file whose top level is not an object: unreadable", L.cmux_idle_reminder(cmux_env("c-list", "[1, 2]")) == "unreadable")
ck("notifications that is not an object: unreadable", L.cmux_idle_reminder(cmux_env("c-notobj", '{"notifications": 3}')) == "unreadable")
e = cmux_env("c-msg", '{"theme": 1}')
lvl, msg = L.cmux_check(e)
ck("on is a warn that names the file and the setting, and says Tares never edits it",
   lvl == "warn" and "agentIdleReminder\": false" in msg and L.cmux_config(e) in msg and "never edits it" in msg, msg)
ck("off is ok", L.cmux_check(cmux_env("c-msg2", '{"notifications": {"agentIdleReminder": false}}'))[0] == "ok")
lvl, msg = L.cmux_check(cmux_env("c-msg3", "{"))
ck("unreadable is a warn that names the file", lvl == "warn" and "could not parse" in msg and "cmux.json" in msg, msg)
ck("the config path uses forward slashes", "\\" not in L.cmux_config(e))
e = cmux_env("c-keep", '{"theme": 1}')
before = Path(L.cmux_config(e)).read_bytes()
L.cmux_check(e)
ck("doctor never edits the cmux file", Path(L.cmux_config(e)).read_bytes() == before)
ck("no printed message holds an em dash", all("—" not in s for s in (L.CMUX_ON, L.CMUX_OFF, L.CMUX_UNREADABLE)))

# ---------- the Doctor ----------

print("the Doctor holds the checks")


def make(name: str, home_env: dict | None = None):
    base = fresh(name)
    home, th, crew, plug_dir = base / "home", base / "th", base / "crew", base / "plugin"
    for d in (home, th, crew, plug_dir):
        d.mkdir()
    e = {"HOME": str(home), "USERPROFILE": str(home), "TARES_HOME": str(th), "FACTORY_CREW_HOME": str(crew),
         "PATH": os.environ.get("PATH", ""), "TARES_PLUGIN_DIR": str(plug_dir), **(home_env or {})}
    return doctor.Doctor(e, CrewPaths(str(crew)), str(plug_dir), None), home


d, home = make("doc-cmux")
d.check_cmux()
ck("no cmux config: doctor adds no cmux line", not d.checks)
cfg = home / ".config" / "cmux" / "cmux.json"
cfg.parent.mkdir(parents=True)
cfg.write_text("{}", encoding="utf-8")
d.check_cmux()
ck("a cmux config that leaves the reminder on: a warn with id cmux", [(c.level, c.id) for c in d.checks] == [("warn", "cmux")], d.checks)

repo = fresh("doc-repo")
touch(repo, "go.mod")
d, _ = make("doc-lsp", bins("doc-bins"))


def fake_client(rows=None, error=False):
    def project_repo_rows():
        if error:
            raise CrewError("no")
        return rows or []
    return SimpleNamespace(project_repo_rows=project_repo_rows)


real_read = L.read_plugins
try:
    L.read_plugins = lambda path="": []
    d.check_lsp(fake_client([("Shop", str(repo))]))
    ck("a Go repo with no server: one warn with id lsp_<project>_<language> and the commands as its fix",
       len(d.checks) == 1 and d.checks[0].id == "lsp_Shop_go" and d.checks[0].level == "warn"
       and d.checks[0].fix == "go install golang.org/x/tools/gopls@latest && claude plugin install gopls-lsp@claude-plugins-official", d.checks)
    d.checks.clear()
    d.check_lsp(fake_client([]))
    ck("no repo handed over: nothing added (and the plugins are not even asked for)", not d.checks)
    d.check_lsp(fake_client(error=True))
    ck("Tares that cannot list the repos: nothing added, no failure", not d.checks)
    L.read_plugins = lambda path="": None
    d.check_lsp(fake_client([("Shop", str(repo))]))
    ck("plugins that cannot be listed: a warn that says it cannot tell", len(d.checks) == 1 and "cannot tell" in d.checks[0].message and not d.checks[0].fix, d.checks)
finally:
    L.read_plugins = real_read

# a doctor with no claude installed still reports: the real read_plugins, no claude path, one repo
d, _ = make("doc-noclaude", {"PATH": str(fresh("doc-emptypath")), "FACTORY_LSP_PATH": str(fresh("doc-nobins"))})
real_bin = claude.claude_bin
try:
    claude.claude_bin = lambda *a, **k: (_ for _ in ()).throw(ClaudeNotFoundError())   # whatever this machine has
    d.check_lsp(fake_client([("Shop", str(repo))]))
    ck("no claude on PATH: the language-server line says it cannot tell, and the report goes on",
       len(d.checks) == 1 and d.checks[0].level == "warn" and "cannot tell" in d.checks[0].message, d.checks)
except CrewError as e:
    ck("no claude on PATH: the language-server check must not raise", False, repr(e))
finally:
    claude.claude_bin = real_bin

# collect() runs the language-server check only when Tares is on
class FakeClient:
    def __init__(self, *a, **k):
        self.say = lambda _m: None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def tares_on(self):
        return None

    def keep_token(self):
        return None


def collect_with(tares_on: bool) -> list:
    d, _ = make(f"doc-collect-{tares_on}")
    seen = []
    for name in ("check_basics", "check_key_files", "check_sessions", "check_trust_and_plugin", "check_mcp",
                 "check_crew_offline", "check_git", "check_gh", "check_stations", "check_cmux"):
        setattr(d, name, lambda *a: None)
    d.check_tares = lambda c: tares_on
    d.check_crew = lambda c: None
    d.check_lsp = lambda c: seen.append("lsp")
    real_client, real_merge = doctor.TaresClient, doctor.state.merge_crew_defaults
    try:
        doctor.TaresClient = FakeClient
        doctor.state.merge_crew_defaults = lambda paths: None
        d.collect()
    finally:
        doctor.TaresClient, doctor.state.merge_crew_defaults = real_client, real_merge
    return seen


ck("Tares on: collect runs the language-server check", collect_with(True) == ["lsp"])
ck("Tares off: collect skips the language-server check (bash: tares_on && doctor_lsp)", collect_with(False) == [])

# ---------- the offer crew up makes ----------

print("offer")
MISS = [("Python", "pyright-lsp", "pyright-langserver", "npm install -g pyright", "neither", "/r/api"),
        ("TypeScript", "typescript-lsp", "typescript-language-server", "npm install -g typescript-language-server typescript", "neither", "/r/api"),
        ("JavaScript", "typescript-lsp", "typescript-language-server", "npm install -g typescript-language-server typescript", "neither", "/r/site"),
        ("Go", "gopls-lsp", "gopls", "go install golang.org/x/tools/gopls@latest", "no-binary", "/r/gw"),
        ("Rust", "rust-analyzer-lsp", "rust-analyzer", "rustup component add rust-analyzer", "no-plugin", "/r/engine")]


class Run:
    """Records what offer asked for, and answers as told."""

    def __init__(self, answer="__none__"):
        self.said, self.err, self.asked, self.installs, self.plugins = [], [], [], [], []
        self.answer = answer

    def go(self, rows, assume_yes=False, fail_install=(), fail_plugin=()):
        def ask(prompt):
            self.asked.append(prompt)
            return self.answer

        def run_install(cmd):
            self.installs.append(cmd)
            return cmd not in fail_install

        def run_claude_install(pid):
            self.plugins.append(pid)
            return pid not in fail_plugin
        return L.offer(rows, assume_yes, "", self.said.append, self.err.append, ask, run_install, run_claude_install)


r = Run()
ck("nothing missing: nothing printed, asked or installed", r.go([]) == [] and not (r.said or r.err or r.asked or r.installs or r.plugins))

r = Run()
ck("--yes installs nothing and says so", r.go(MISS, assume_yes=True) == [] and not r.installs and not r.plugins and not r.asked
   and any("not installing language servers without a yes" in s for s in r.said), r.said)
text = "\n".join(r.err)
ck("the block names the section, each repo, and each command to run",
   "Code intelligence" in text and "  Python in /r/api" in text and "      npm install -g pyright" in text
   and "      claude plugin install pyright-lsp@claude-plugins-official" in text and "  Go in /r/gw" in text
   and "      go install golang.org/x/tools/gopls@latest" in text
   and "claude plugin install gopls-lsp" not in text and "rustup component add rust-analyzer" not in text.split("Rust in")[1], text)

r = Run(None)
ck("a closed input is a no", r.go(MISS) == [] and not r.installs and not r.plugins and any("no language servers installed" in s for s in r.said), r.said)
ck("it asks with the recommended answer first", r.asked and "recommended: yes" in r.asked[0] and "press enter to take it, or type no" in r.asked[0], r.asked)
for word in ("no", "n", "No", "maybe", "nope"):
    r = Run(word)
    ck(f"answering {word!r} installs nothing", r.go(MISS) == [] and not r.installs and not r.plugins and any("no language servers installed" in s for s in r.said))

for word in ("", "y", "Y", "yes", "Yes"):
    r = Run(word)
    done = r.go(MISS)
    ck(f"answering {word!r} is a yes", bool(r.installs) and bool(r.plugins) and done)

r = Run("yes")
done = r.go(MISS)
ck("a yes installs each binary once, however many repos need it",
   r.installs == ["npm install -g pyright", "npm install -g typescript-language-server typescript",
                  "go install golang.org/x/tools/gopls@latest"], r.installs)
ck("a yes installs each plugin once, and only the ones missing", r.plugins == ["pyright-lsp@claude-plugins-official", "typescript-lsp@claude-plugins-official",
                                                                         "rust-analyzer-lsp@claude-plugins-official"], r.plugins)
ck("a binary that was installed but whose plugin was on is not followed by a plugin install", "gopls-lsp@claude-plugins-official" not in r.plugins)
ck("it returns the plugins it installed", done == ["pyright-lsp", "typescript-lsp", "rust-analyzer-lsp"], done)
ck("it says what it installs, in order", [s for s in r.said if s.startswith("installing")][:2] == ["installing pyright-langserver: npm install -g pyright", "installing the pyright-lsp plugin"], r.said)

r = Run("yes")
done = r.go(MISS, fail_install=("npm install -g pyright",))
ck("a failed binary install is said, and its plugin is not installed", any("could not install pyright-langserver" in s for s in r.said)
   and "pyright-lsp@claude-plugins-official" not in r.plugins and "pyright-lsp" not in done, (r.said, r.plugins))
ck("the rest still install after a failure", "typescript-lsp" in done)
r = Run("yes")
done = r.go(MISS, fail_plugin=("typescript-lsp@claude-plugins-official",))
ck("a failed plugin install is said with the command to run, and not returned", any("could not install the typescript-lsp plugin; run: claude plugin install typescript-lsp@claude-plugins-official" in s for s in r.said)
   and "typescript-lsp" not in done, r.said)
ck("no message holds an em dash", all("—" not in s for s in r.said + r.err))

if F:
    print(f"\n{P} passed, {F} failed")
    sys.exit(1)
print(f"\n{P} passed, {F} failed")

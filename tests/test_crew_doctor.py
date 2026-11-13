"""tares factory doctor, in Python (TR-969): the checks bash does not make, the JSON output, and the
helpers that read the machine.

The bash crew is gone (TR-975): the doctor is Python only, and there is no engine line. This file covers it:

  - `--json` is one contract document {schema_version, ok, checks: [{id, ok, message, fix, level}]}; an unknown argument prints the usage and exits 2;
  - the exit code is 1 when any check is FIX, 0 when none is;
  - the tares version against the plugin's min_tares (TR-1064) and `tares` on PATH with `tares hook` (TR-1068);
  - the login key: in the keychain, in a file only the owner can read, or readable by others (TR-949);
  - the crew key file's permissions; on Windows the access list, read from a fake icacls;
  - that there is no engine line (an engine setting is not read), Git for Windows, and that no cmux or language server line appears without a cmux file or a project repo (TR-970 has its own test);
  - the watch's pid file, and that the checks marked new start with the labels in NEW_LABELS;
  - the module stays light: no DuckDB, FastAPI or daemon import.

No keychain is touched: PYTHON_KEYRING_BACKEND is pinned to the failing backend. Standard library, psutil
and tares.crew. Run: python tests/test_crew_doctor.py
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import ntpath
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"

from tares import login  # noqa: E402
from tares.crew import doctor, perms, state  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0
WIN = sys.platform == "win32"
T = Path(os.path.realpath(tempfile.mkdtemp(prefix="doctor-test-")))


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


def make(name: str, env: dict | None = None, run=None, plugin: dict | None = None):
    """A Doctor over a new home: (doctor, home, tares_home, crew paths)."""
    base = fresh(name)
    home, th, crew, plug = base / "home", base / "th", base / "crew", base / "plugin"
    for d in (home, th, crew, plug):
        d.mkdir()
    for fname, text in (plugin or {}).items():
        Path(plug, fname).write_text(text, encoding="utf-8")
    e = {"HOME": str(home), "USERPROFILE": str(home), "TARES_HOME": str(th), "FACTORY_CREW_HOME": str(crew),
         "PATH": os.environ.get("PATH", ""), "TARES_PLUGIN_DIR": str(plug), **(env or {})}
    paths = CrewPaths(str(crew))
    return doctor.Doctor(e, paths, str(plug), run), home, th, paths


def by_id(d, cid):
    return [c for c in d.checks if c.id == cid]


def one(d, cid):
    got = by_id(d, cid)
    return got[0] if len(got) == 1 else None


def res(code=0, out=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


def fake_run(table):
    """A run(cmd, ...) that answers by the cmd's tail: table maps a tuple of the last words to a result."""
    def run(cmd, cwd=None, timeout=0):
        for tail, r in table.items():
            if tuple(cmd[-len(tail):]) == tail:
                return r
        return None
    return run


def write_login(th: Path, mode: int, **doc):
    p = th / "login.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    if not WIN:
        os.chmod(p, mode)
    return p


# ---------- the shape of the output ----------

print("the JSON shape and the exit code")
c = doctor.Check("x", "warn", "m", "f")
ck("a warn is ok in JSON and keeps its level", c.as_json() == {"id": "x", "ok": True, "message": "m", "fix": "f", "level": "warn"}, c.as_json())
ck("a fix is not ok", doctor.Check("x", "fix", "m", "f").as_json()["ok"] is False)
ck("the text line keeps bash's wording", doctor.Check("x", "ok", "hello").line() == "  ok    hello"
   and doctor.Check("x", "fix", "hello").line() == "  FIX   hello"
   and doctor.Check("x", "warn", "hello").line() == "  warn  hello")

out, err = io.StringIO(), io.StringIO()
with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
    code = doctor.cmd_doctor(["doctor", "--wat"], {})
ck("an unknown argument exits 2 with the usage", code == 2 and "usage: tares factory doctor [--json]" in err.getvalue() and not out.getvalue(), (code, err.getvalue()))

out, err = io.StringIO(), io.StringIO()
try:
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        doctor.cmd_doctor(["doctor", "--wat", "--json"], {})
    exit_code = None
except SystemExit as e:
    exit_code = e.code
ck("an unknown argument with --json is the usage error shape, exit 2", exit_code == 2 and not err.getvalue()
   and json.loads(out.getvalue()) == {"schema_version": 1, "error": {"code": "usage", "message": doctor.USAGE}},
   (exit_code, out.getvalue(), err.getvalue()))

# a CrewError while collecting is the error document under --json (TR-973 review F2), a text line without it
_real_collect = doctor.Doctor.collect
def _boom(self):
    raise doctor.CrewError("the crew home is unreadable")
doctor.Doctor.collect = _boom
try:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = doctor.cmd_doctor(["doctor", "--json"], {})
    jd = json.loads(out.getvalue()) if out.getvalue().strip().startswith("{") else {}
    ck("a CrewError with --json is the error document on stdout, exit 1, nothing on stderr",
       code == 1 and jd.get("error", {}).get("message") == "the crew home is unreadable" and not err.getvalue(),
       (code, out.getvalue(), err.getvalue()))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = doctor.cmd_doctor(["doctor"], {})
    ck("a CrewError without --json is a text line on stderr, exit 1, nothing on stdout",
       code == 1 and "the crew home is unreadable" in err.getvalue() and not out.getvalue(), (code, out.getvalue(), err.getvalue()))
finally:
    doctor.Doctor.collect = _real_collect

base = fresh("run")
env_run = {"HOME": str(base / "h"), "USERPROFILE": str(base / "h"), "TARES_HOME": str(base / "th"),
           "FACTORY_CREW_HOME": str(base / "crew"), "PATH": os.environ.get("PATH", ""),
           "TARESD_URL": "http://127.0.0.1:1", "TARES_FACTORY_MCP_URL": "http://127.0.0.1:1/mcp",
           "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
(base / "h").mkdir()
for argv in (["doctor", "--json"], ["doctor"]):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = doctor.cmd_doctor(argv, dict(env_run))
    if argv[-1] == "--json":
        try:
            jdoc = json.loads(out.getvalue())
            rows = jdoc["checks"]
        except (ValueError, KeyError, TypeError):
            jdoc, rows = None, None
        ck("--json prints one contract document with the checks", isinstance(rows, list) and rows
           and jdoc.get("schema_version") == 1 and next(iter(jdoc)) == "schema_version"
           and len(out.getvalue().strip().splitlines()) == 1, out.getvalue()[:200])
        ck("--json: ok is false when a check is FIX", jdoc and jdoc["ok"] == (not any(not r["ok"] for r in rows)), jdoc and jdoc["ok"])
        ck("each check has id, ok, message and fix", all(isinstance(r.get("id"), str) and isinstance(r.get("ok"), bool)
                                                         and isinstance(r.get("message"), str) and isinstance(r.get("fix"), str)
                                                         for r in rows or []))
        ids = [r["id"] for r in rows or []]
        ck("--json: no two checks share an id", len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1}))
        ck("--json holds the Python-only checks", {"min_tares", "tares_path", "git"} <= set(ids) and "engine" not in ids, ids)
        ck("no Tares answers, so the doctor says so and exits 1", code == 1 and any(r["id"] == "tares" and not r["ok"] for r in rows or []), code)
        ck("a failing check carries a fix", all(r["fix"] for r in rows or [] if not r["ok"]), [r for r in rows or [] if not r["ok"] and not r["fix"]])
        ck("--json prints nothing but the JSON", out.getvalue().startswith('{"schema_version":1,'), out.getvalue()[:60])
        ck("no cmux file and no project repo: no line for cmux or a language server", not any(re.search(r"cmux|language server", r["message"], re.I) for r in rows or []))
    else:
        text = out.getvalue()
        ck("the text form starts with the title and ends with the verdict", text.startswith("tares factory doctor\n")
           and text.rstrip().endswith("fix the items marked FIX"), text[-120:])
        ck("the text form holds no em dash", "—" not in text)

# ---------- the checks bash does not make ----------

print("tares version against the plugin's min_tares (TR-1064)")
have = doctor.version.installed_version()
d, *_ = make("minv-high", plugin={"min_tares.txt": "999.0.0\n"})
d.check_tares_version()
r = one(d, "min_tares")
ck("an installed tares older than min_tares is a FIX with the upgrade", r and r.level == "fix" and "999.0.0" in r.message and "upgrade" in r.fix, r)
d, *_ = make("minv-ok", plugin={"min_tares.txt": "0.0.1\n"})
d.check_tares_version()
r = one(d, "min_tares")
ck("a tares that meets it is ok and names both versions", r and r.level == "ok" and have in r.message and "0.0.1" in r.message, r)
d, *_ = make("minv-none")
d.check_tares_version()
r = one(d, "min_tares")
ck("a plugin with no minimum is ok", r and r.level == "ok" and "no minimum" in r.message, r)
d, *_ = make("minv-bad", plugin={"min_tares.txt": "soon\n"})
d.check_tares_version()
r = one(d, "min_tares")
ck("a minimum that is not a version is a warn, not a crash", r and r.level == "warn", r)
d, *_ = make("minv-json")
Path(d.root, ".claude-plugin").mkdir()
Path(d.root, ".claude-plugin", "plugin.json").write_text('{"min_tares": "999.1"}', encoding="utf-8")
d.check_tares_version()
r = one(d, "min_tares")
ck("min_tares is read from plugin.json too", r and r.level == "fix" and "999.1" in r.message, r)

print("tares on PATH, and tares hook (TR-1068)")
bindir = fresh("bin")
exe = bindir / ("tares.cmd" if WIN else "tares")
exe.write_text("@echo off\n" if WIN else "#!/bin/sh\n", encoding="utf-8")
if not WIN:
    os.chmod(exe, 0o755)
d, *_ = make("path-none", env={"PATH": str(fresh("empty"))})
d.check_tares_path()
r = one(d, "tares_path")
ck("no tares on PATH is a FIX that says to install it", r and r.level == "fix" and "no tares resolves on PATH" in r.message and "install" in r.fix, r)
table_hook = {("--version",): res(0, "tares 1.2.3\n"), ("hook", "--help"): res(0, "usage: tares hook\n")}
d, *_ = make("path-hook", env={"PATH": str(bindir)}, run=fake_run(table_hook))
d.check_tares_path()
r = one(d, "tares_path")
ck("a tares with hook is ok, with its path and version", r and r.level == "ok" and "tares 1.2.3" in r.message and "has tares hook" in r.message, r)
d, *_ = make("path-nohook", env={"PATH": str(bindir)}, run=fake_run({("--version",): res(0, "tares 1.2.3\n"), ("hook", "--help"): res(2)}))
d.check_tares_path()
r = one(d, "tares_path")
ck("a tares without hook is a warn with the version and the upgrade", r and r.level == "warn" and "tares 1.2.3" in r.message and "upgrade" in r.fix, r)
d, *_ = make("path-noversion", env={"PATH": str(bindir)}, run=fake_run({}))
d.check_tares_path()
r = one(d, "tares_path")
ck("a tares that will not run is still reported, not a crash", r and r.level == "warn" and str(exe).lower() in r.message.lower(), r)

print("no engine line (TR-975)")
ck("the doctor has no check_engine and no engine label", not hasattr(doctor.Doctor, "check_engine") and "engine" not in doctor.NEW_LABELS)
for label, env in (("TARES_FACTORY_ENGINE=bash", {"TARES_FACTORY_ENGINE": "bash"}), ("TARES_FACTORY_ENGINE=perl", {"TARES_FACTORY_ENGINE": "perl"})):
    d, *_ = make("eng-" + label[-4:], env=env, run=fake_run({("--version",): res(0, "tares 1\n"), ("hook", "--help"): res(0)}))
    d.check_basics()
    ck(f"{label}: no engine check, and no line mentions an engine",
       not by_id(d, "engine") and not any(re.search(r"engine", c.message, re.I) for c in d.checks), [c.line() for c in d.checks])
d, home, *_ = make("eng-json")
d.paths.home.mkdir(parents=True, exist_ok=True)
(d.paths.home / "crew.json").write_text('{"engine": "bash"}', encoding="utf-8")
d.check_basics()
ck('an "engine" in crew.json is not read: no engine check', not by_id(d, "engine") and not any("engine" in c.message for c in d.checks),
   [c.line() for c in d.checks])

print("the login key (TR-949)")
d, home, th, _p = make("lk-none")
d.check_login_key()
r = one(d, "login_key")
ck("no saved login and no token: ok, a Tares without auth needs none", r and r.level == "ok" and "no saved login" in r.message, r)
d, home, th, _p = make("lk-none-token", env={"TARES_AUTH_TOKEN": "tok"})
d.check_login_key()
ck("no saved login but a token in the environment: no line", not by_id(d, "login_key"))
d, home, th, _p = make("lk-0600")
p = write_login(th, 0o600, cell_url="http://x", key="K1")
if WIN:
    perms.owner_only(p)
d.check_login_key()
r = one(d, "login_key")
ck("a key in a file only the owner reads is ok", r and r.level == "ok" and "readable by you only" in r.message, r)
if not WIN:
    d, home, th, _p = make("lk-0644")
    write_login(th, 0o644, cell_url="http://x", key="K1")
    d.check_login_key()
    r = one(d, "login_key")
    ck("a key in a file others can read is a FIX that says tares login rewrites it", r and r.level == "fix" and "0644" in r.message and "tares login" in r.fix, r)
    d, home, th, _p = make("lk-0640")
    write_login(th, 0o640, cell_url="http://x", key="K1")
    d.check_login_key()
    ck("group-readable is also a FIX", (one(d, "login_key") or doctor.Check("", "", "")).level == "fix")
d, home, th, _p = make("lk-nokey")
write_login(th, 0o600, cell_url="http://x")
d.check_login_key()
r = one(d, "login_key")
ck("a login with no key is a warn", r and r.level == "warn" and "holds no key" in r.message, r)

real_kc = login._kc_get
try:
    for problem, expect in (("", "no entry"), ("off", "TARES_KEYRING is not on"), ("unusable", "no keychain works"),
                            ("error", "refused")):
        login._kc_get = lambda account, p=problem: ("", p)
        d, home, th, _p = make("lk-kc-" + (problem or "empty"))
        write_login(th, 0o600, cell_url="http://x/", key_store="keychain")
        d.check_login_key()
        r = one(d, "login_key")
        ck(f"a key kept in the keychain that cannot be had ({problem or 'no entry'}) is a FIX that says why",
           r and r.level == "fix" and expect in r.message, r)
    asked = []
    login._kc_get = lambda account: (asked.append(account) or ("secret", ""))
    d, home, th, _p = make("lk-kc-ok")
    write_login(th, 0o600, cell_url="http://x/", key_store="keychain")
    d.check_login_key()
    r = one(d, "login_key")
    ck("a key found in the keychain is ok, and the message holds no key", r and r.level == "ok" and "keychain" in r.message and "secret" not in r.message, r)
    ck("the keychain is asked for the cell url without its trailing slash", asked == ["http://x"], asked)
    login._kc_get = lambda account: ("", "off")
    d, home, th, _p = make("lk-kc-off2")
    write_login(th, 0o600, cell_url="http://x", key_store="keychain")
    d.check_login_key()
    ck("the off case offers TARES_KEYRING=on in its fix", "TARES_KEYRING=on" in (one(d, "login_key") or doctor.Check("", "", "")).fix)
finally:
    login._kc_get = real_kc

print("the crew key file")
d, home, th, paths = make("kf-ok")
paths.token_file.parent.mkdir(parents=True, exist_ok=True)
paths.token_file.write_text("tok", encoding="utf-8")
if WIN:
    perms.owner_only(paths.token_file)
else:
    os.chmod(paths.token_file, 0o600)
d.check_key_files()
r = one(d, "crew_key_file")
ck("a crew key file only the owner reads is ok", r and r.level == "ok", r)
if not WIN:
    os.chmod(paths.token_file, 0o644)
    d, home, th, paths2 = make("kf-open")
    paths2.token_file.parent.mkdir(parents=True, exist_ok=True)
    paths2.token_file.write_text("tok", encoding="utf-8")
    os.chmod(paths2.token_file, 0o644)
    d.check_key_files()
    r = one(d, "crew_key_file")
    ck("a crew key file others can read is a FIX that says mode 0600", r and r.level == "fix" and "0644" in r.message and "0600" in r.fix, r)
d, home, th, paths = make("kf-none")
d.check_key_files()
ck("no crew key file: no line about it", not by_id(d, "crew_key_file"))
d, home, th, paths = make("kf-env", env={"TARES_FACTORY_TOKEN_FILE": str(T / "elsewhere")})
(T / "elsewhere").write_text("tok", encoding="utf-8")
if not WIN:
    os.chmod(T / "elsewhere", 0o666)
d.check_key_files()
r = one(d, "crew_key_file")
ck("TARES_FACTORY_TOKEN_FILE moves the file that is checked", r is not None and str(T / "elsewhere") in r.message, r)

print("who can read a file, from icacls (Windows rules, run against a fake icacls on any OS)")
real_win = doctor._windows
real_user = None
real_user = perms.windows_user
perms.windows_user = lambda: "HOST\\kiran"
doctor._windows = lambda: True
try:
    target = str(T / "key.txt")

    def icacls(*lines, code=0):
        return lambda cmd, cwd=None, timeout=0: res(code, "\n".join(lines) + "\n\nSuccessfully processed 1 files; Failed processing 0 files\n")

    only_me = icacls(f"{target} HOST\\kiran:(F)", "        NT AUTHORITY\\SYSTEM:(F)", "        BUILTIN\\Administrators:(F)")
    ck("only you, SYSTEM and Administrators: owner only", doctor.acl_extras(target, "HOST\\kiran", only_me) == [])
    ck("the user without a domain still counts as you", doctor.acl_extras(target, "kiran", only_me) == [])
    ck("names compare without regard to case", doctor.acl_extras(target, "host\\KIRAN", only_me) == [])
    everyone = icacls(f"{target} HOST\\kiran:(F)", "        Everyone:(R)", "        BUILTIN\\Users:(RX)")
    ck("Everyone and Users on the list are named", doctor.acl_extras(target, "HOST\\kiran", everyone) == ["Everyone", "BUILTIN\\Users"],
       doctor.acl_extras(target, "HOST\\kiran", everyone))
    ck("OWNER RIGHTS is the owner, not another reader", doctor.acl_extras(
        target, "HOST\\kiran", icacls(f"{target} NT AUTHORITY\\SYSTEM:(F)", "        OWNER RIGHTS:(F)", "        HOST\\kiran:(F)")) == [])
    odd = str(target)[:-1] + "\ufffd"  # the path as icacls printed it, one letter unreadable
    ck("a path read back changed still leaves the names after it", doctor.acl_extras(
        target, "HOST\\kiran", icacls(f"{odd} NT AUTHORITY\\SYSTEM:(F)", "        Everyone:(R)")) == ["Everyone"])
    deny = icacls(f"{target} HOST\\kiran:(F)", "        Everyone:(DENY)(R)")
    ck("a deny entry is not a grant", doctor.acl_extras(target, "HOST\\kiran", deny) == [])
    ck("icacls failing gives None, not an empty list", doctor.acl_extras(target, "HOST\\kiran", icacls("Access is denied.", code=5)) is None)
    ck("icacls that cannot start gives None", doctor.acl_extras(target, "HOST\\kiran", lambda *a, **k: None) is None)
    ck("owner_only_state: True when there are no extras", doctor.owner_only_state(target, only_me) == (True, ""))
    ck("owner_only_state: False and who when there are", doctor.owner_only_state(target, everyone) == (False, "Everyone, BUILTIN\\Users"))
    st = doctor.owner_only_state(target, icacls("x", code=1))
    ck("owner_only_state: None and why when icacls fails", st[0] is None and "icacls" in st[1], st)

    d, home, th, paths = make("kf-win", run=everyone)
    paths.token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.token_file.write_text("tok", encoding="utf-8")
    d.check_key_files()
    r = one(d, "crew_key_file")
    ck("on Windows the crew key file check names the extra principals and says access list", r and r.level == "fix" and "Everyone" in r.message and "access list" in r.fix, r)
    d, home, th, paths = make("kf-win-unknown", run=lambda *a, **k: None)
    paths.token_file.parent.mkdir(parents=True, exist_ok=True)
    paths.token_file.write_text("tok", encoding="utf-8")
    d.check_key_files()
    r = one(d, "crew_key_file")
    ck("on Windows an access list that cannot be read is a warn", r and r.level == "warn" and "could not tell" in r.message, r)

    print("Git for Windows")
    gitbin = fresh("gfw") / "Git" / "bin"
    gitbin.mkdir(parents=True)
    (gitbin / "bash.exe").write_text("", encoding="utf-8")
    d, *_ = make("gfw-ok", env={"ProgramFiles": str(gitbin.parent.parent), "PATH": str(fresh("nopath"))}, run=fake_run({("--version",): res(0, "git version 2.50\n")}))
    d.check_git()
    r = one(d, "git_windows")
    ck("Git Bash found under Program Files is ok, with forward slashes", r and r.level == "ok" and "\\" not in r.message, r)
    d, *_ = make("gfw-env", env={"CLAUDE_CODE_GIT_BASH_PATH": str(gitbin / "bash.exe"), "PATH": str(fresh("nopath2"))}, run=fake_run({("--version",): res(0, "git version 2.50\n")}))
    d.check_git()
    ck("CLAUDE_CODE_GIT_BASH_PATH is honoured", (one(d, "git_windows") or doctor.Check("", "", "")).level == "ok")
    real_git = os.path.isfile(r"C:\Program Files\Git\bin\bash.exe")  # the default install is always looked at
    d, *_ = make("gfw-none", env={"ProgramFiles": str(T / "nowhere"), "ProgramFiles(x86)": str(T / "nowhere"), "PATH": str(fresh("nopath3"))}, run=fake_run({("--version",): res(0, "git version 2.50\n")}))
    d.check_git()
    r = one(d, "git_windows")
    if not real_git:
        ck("no Git Bash is a FIX that names winget", r and r.level == "fix" and "winget" in r.fix, r)
finally:
    doctor._windows = real_win
    perms.windows_user = real_user

print("git")
ck("a Windows path is shown with forward slashes", doctor.slashes("C:\\Program Files\\Git\\bin\\bash.exe") == "C:/Program Files/Git/bin/bash.exe")
d, *_ = make("git-ok", run=fake_run({("--version",): res(0, "git version 2.50.1\n")}))
d.check_git()
r = one(d, "git")
ck("git that answers is ok with its version", r and r.level == "ok" and "git version 2.50.1" in r.message, r)
ck("no Git for Windows line off Windows", WIN or not by_id(d, "git_windows"))
d, *_ = make("git-none", run=fake_run({}))
d.check_git()
r = one(d, "git")
ck("git that will not run is a FIX", r and r.level == "fix" and r.fix, r)
ck("git that will not run: no identity line", not by_id(d, "git_identity"))

print("git identity")
ver = ("--version",)
d, *_ = make("gid-ok", run=fake_run({ver: res(0, "git version 2.50\n"), ("user.name",): res(0, "Kiran\n"),
                                     ("user.email",): res(0, "k@example.com\n")}))
d.check_git()
r = one(d, "git_identity")
ck("a global name and email are ok", r and r.level == "ok" and r.new and r.message.startswith("git identity"), r)
d, *_ = make("gid-none", run=fake_run({ver: res(0, "git version 2.50\n"), ("user.name",): res(1),
                                       ("user.email",): res(1)}))
d.check_git()
r = one(d, "git_identity")
ck("no global identity is a warn that names both keys and the fix",
   r and r.level == "warn" and "user.name or user.email" in r.message and "Author identity unknown" in r.message
   and "git config --global user.name" in r.fix and "git config --global user.email" in r.fix
   and r.new and "&&" not in r.fix, r)   # new: shown as a new check; no &&: PowerShell 5.1 refuses it
d, *_ = make("gid-email", run=fake_run({ver: res(0, "git version 2.50\n"), ("user.name",): res(0, "Kiran\n"),
                                        ("user.email",): res(0, "  \n")}))
d.check_git()
r = one(d, "git_identity")
ck("a blank email alone is named", r and r.level == "warn" and "no user.email" in r.message
   and "user.name" not in r.fix, r)
d, *_ = make("gid-cannot", run=fake_run({ver: res(0, "git version 2.50\n")}))
d.check_git()
ck("git config that cannot be asked: no identity line", not by_id(d, "git_identity"))
ck("the identity line's label is one of NEW_LABELS", any("git identity: x".startswith(l) for l in doctor.NEW_LABELS))

print("the watch's pid file, read with psutil")
d, home, th, paths = make("watch")
paths.home.mkdir(parents=True, exist_ok=True)
ck("no pid file: not running", doctor.watch_running(paths) is False)
(paths.home / "watch.pid").write_text("not a number\n", encoding="utf-8")
ck("a pid file that holds no number: not running", doctor.watch_running(paths) is False)
(paths.home / "watch.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
ck("a live process that is not a crew watch: not running", doctor.watch_running(paths) is False)
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "crew", "watch"])
try:
    (paths.home / "watch.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    ck("a live process whose command line holds crew watch: running", doctor.watch_running(paths) is True)
finally:
    proc.kill()
    proc.wait()
ck("the same pid after the process ended: not running", doctor.watch_running(paths) is False)

print("small readers")
ck("cc_retry_in: a few minutes", doctor.cc_retry_in("1000600", now=1000000) == "10 min")
ck("cc_retry_in: past ends at 1 min", doctor.cc_retry_in("100", now=1000000) == "1 min")
ck("cc_retry_in: hours", doctor.cc_retry_in("1010800", now=1000000) == "3 h", doctor.cc_retry_in("1010800", now=1000000))
ck("cc_retry_in: not a number is 1 min", doctor.cc_retry_in("", now=5) == "1 min")
ck("frozen_text with a time", doctor.frozen_text({"at": "2026-01-01", "by": "kiran"}) == "since 2026-01-01 by kiran")
ck("frozen_text without a time", doctor.frozen_text({}) == "(Tares does not say since when)")

print("the checks marked new start with the labels in NEW_LABELS")
pat = re.compile(r"^  (ok|warn|FIX)\s+(" + "|".join(re.escape(x) for x in doctor.NEW_LABELS) + r")\b")
d, *_ = make("sync", env={"PATH": str(bindir)}, run=fake_run({("--version",): res(0, "tares 1\n"), ("hook", "--help"): res(0)}))
th_login = Path(d.env["TARES_HOME"])
write_login(th_login, 0o600, cell_url="http://x", key="K")
Path(d.paths.token_file).parent.mkdir(parents=True, exist_ok=True)
Path(d.paths.token_file).write_text("t", encoding="utf-8")
if not WIN:
    os.chmod(d.paths.token_file, 0o600)
d.check_tares_version()
d.check_tares_path()
d.check_key_files()
d.check_git()
newer = [c for c in d.checks if c.new]
ck("every check marked new starts with one of the labels", newer and all(pat.match(c.line()) for c in newer), [c.line() for c in newer if not pat.match(c.line())])
ck("...and every label is used by some check", {m.group(2) for c in newer for m in [pat.match(c.line())] if m} >= {"tares version", "tares on PATH", "login key", "crew key file", "git"},
   [c.line() for c in newer])

print("check ids are unique (TR-1130)")
# A project with several repos, two of them in folders of the same name, one listed twice and one missing:
# every check the project and LSP sections add must still have an id of its own.
ids_home = fresh("ids")
for sub in ("a/api", "b/api", "web"):
    Path(ids_home, sub).mkdir(parents=True)
    Path(ids_home, sub, "go.mod").write_text("module x\n", encoding="utf-8")
rows = [("Shop", str(ids_home / "a" / "api")), ("Shop", str(ids_home / "b" / "api")), ("Shop", str(ids_home / "web")),
        ("Shop", str(ids_home / "web")), ("Shop", str(ids_home / "gone")), ("Other", str(ids_home / "web"))]
ck("repo_ids: the folder name, then -2 for a repeat inside the project, restarting for another project",
   doctor.repo_ids(rows) == ["api", "api-2", "web", "web-2", "gone", "web"], doctor.repo_ids(rows))
ck("repo_ids: a folder that is already named like a suffixed repeat still gets its own part (api, api, api-2)",
   doctor.repo_ids([("P", "/a/api"), ("P", "/b/api"), ("P", "/c/api-2")]) == ["api", "api-2", "api-2-2"],
   doctor.repo_ids([("P", "/a/api"), ("P", "/b/api"), ("P", "/c/api-2")]))
ck("repo_ids: a trailing slash or backslash does not make an empty part",
   doctor.repo_ids([("P", "/x/y/"), ("P", "C:\\x\\z\\")]) == ["y", "z"], doctor.repo_ids([("P", "/x/y/"), ("P", "C:\\x\\z\\")]))
d, *_ = make("ids-doc", {"FACTORY_LSP_PATH": str(fresh("ids-nobins"))},
             run=fake_run({("--json", "nameWithOwner,viewerPermission", "-q", '.nameWithOwner + " " + .viewerPermission'): res(0, "acme/shop WRITE\n")}))
d.trusted = lambda repo: True
real_lsp = doctor.doctor_lsp.read_plugins
doctor.doctor_lsp.read_plugins = lambda path="": []
try:
    d.check_projects(SimpleNamespace(project_repo_rows=lambda: rows))
    d.check_lsp(SimpleNamespace(project_repo_rows=lambda: rows))
finally:
    doctor.doctor_lsp.read_plugins = real_lsp
got = [c.id for c in d.checks]
dupes = sorted({i for i in got if got.count(i) > 1})
ck("a multi-repo project: no two checks share an id", got and not dupes, dupes)
ck("the per-repo ids carry the repo", "project_Shop_api_trust" in got and "project_Shop_api-2_trust" in got and "project_Shop_gone" in got, got)
ck("a repo of another project with the same folder name keeps its own ids", "project_Other_web_trust" in got, got)

print("trust under the key Claude Code writes on Windows (TR-1236)")
d, home, th, paths = make("trust-slash")
slash = paths.raw.replace("\\", "/")
Path(home, ".claude.json").write_text(json.dumps({"projects": {slash: {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
ck("a home recorded with forward slashes (C:/Users/me/factory on Windows) is accepted", d.accepted(paths.raw), (paths.raw, slash))
d, home, th, paths = make("trust-none")
Path(home, ".claude.json").write_text(json.dumps({"projects": {paths.raw + "-other": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
ck("a different folder's record does not count", not d.accepted(paths.raw))
with mock.patch.object(state, "os", SimpleNamespace(name="nt", path=ntpath)):
    keys = state.trust_keys("C:\\Users\\Jane\\factory")
    keys_dots = state.trust_keys("C:\\Users\\Jane\\.\\factory")
ck("trust_keys on Windows (any OS runs this one): the path as given, then with forward slashes",
   keys == ["C:\\Users\\Jane\\factory", "C:/Users/Jane/factory"], keys)
ck("...and the normalised path too", "C:/Users/Jane/factory" in keys_dots and keys_dots[0] == "C:\\Users\\Jane\\.\\factory", keys_dots)
ck("trust_keys off Windows is the path alone", os.name == "nt" or state.trust_keys("/a/b") == ["/a/b"])

print("the doctor stays light")
r = subprocess.run([sys.executable, "-c",
                    "import sys; import tares.crew.doctor, tares.crew.dispatch; "
                    "print([m for m in ('duckdb', 'fastapi', 'tares.daemon', 'tares.store') if m in sys.modules])"],
                   capture_output=True, text=True, cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)})
ck("importing it pulls in no DuckDB, FastAPI or daemon", r.stdout.strip() == "[]", (r.stdout, r.stderr[-200:]))
src = Path(doctor.__file__).read_text(encoding="utf-8")
ck("no em dash in the module's text", "—" not in src)
ck("the module is LF with no tab characters", "\r" not in src and "\t" not in src)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

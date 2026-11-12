"""Building and starting a station in Python (tares/crew/launch.py, TR-953).

The bash crew is gone (TR-975), so this test no longer compares with it. Part 1: the small rules (model check, levels, the parent's role, grants into allow rules), the
quoting of the status line command on Mac and on Windows (the Windows form is tested without Windows), the
cksum that fingerprints the kit, the kit folder, the two notes that are not ported yet, the idle wake, and what
a started session is given (its environment holds no key, its command line holds no token).

Part 2 runs the Python engine in a throwaway crew of its own (tests/factory/launch_support.py: a fake Tares, a
copy of the kit, the fake Claude Code) and checks what it leaves: the settings file of every role, also for a crew
home with a space in its path, and the `claude` command line of a start. The scenarios of the parity harness
(`parity.py launch_*`) compare the same output with what bash printed and wrote, one assertion at a time.

The test runs in a fresh HOME with fake keys and no keyring: nothing of the person's is read or written.

Run: .venv/bin/python tests/test_crew_launch.py
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# A fresh HOME, fake keys and no keyring, before anything is imported or run.
SCRATCH = Path(tempfile.mkdtemp(prefix="tares-crew-launch-"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "CLAUDE_PLUGIN_OPTION_"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
os.environ.update(
    HOME=str(SCRATCH / "home"), USERPROFILE=str(SCRATCH / "home"),
    PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
    ANTHROPIC_API_KEY="sk-fake-launch-test", TARES_HOME=str(SCRATCH / "tares-home"),
    FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
)
(SCRATCH / "home").mkdir()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient, cksum  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402
import parity  # noqa: E402

P = F = 0
FAKE_TOKEN = "tok-fake-launch-test-0123456789"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def raises(fn, *args, **kw):
    try:
        fn(*args, **kw)
    except CrewError as e:
        return str(e)
    return None


# ---------------------------------------------------------------------------------------------
# Part 1: the small rules

print("small rules")
msg = raises(L.check_model, "claude-haiku-4-5", "crew-reviewer")
ck("a Haiku model is refused, naming the station and the way out",
   msg is not None and "crew-reviewer" in msg and "Auto mode does not run on Haiku" in msg and "sonnet or opus" in msg, str(msg))
ck("...also written with a capital", raises(L.check_model, "Haiku", "s") is not None)
ck("sonnet and opus pass", raises(L.check_model, "sonnet", "s") is None and raises(L.check_model, "opus", "s") is None)
ck("level_rank: 3, 4, and nothing for anything else",
   (L.level_rank("L3-review"), L.level_rank("L4-ship"), L.level_rank("L5"), L.level_rank("")) == (3, 4, None, None))
ck("level_text says Tares merges after the human's yes on the desk at L3, and after the review gate and green CI at L4",
   L.level_text("L3-review") == "builders open PRs and Tares merges each one after the human's yes on the desk; nothing is deployed to production"
   and L.level_text("L4-ship") == "builders open PRs and Tares merges them after the review gate passes and CI is green; nothing is deployed to production")
ck("neither level text says a builder merges", not any("builders merge" in L.level_text(x) or "builder merges" in L.level_text(x) for x in ("L3-review", "L4-ship", "")))
ck("an empty level gets the L4 wording, as before", L.level_text("") == L.level_text("L4-ship"))
ck("neither level text allows production", all("nothing is deployed to production" in L.level_text(x) for x in ("L3-review", "L4-ship")))

crew_home = SCRATCH / "crew"
LS.make_crew(SCRATCH, {})
paths = CrewPaths.at(crew_home)
ck("parent_role: the crew's three roles, a builder, a stranger",
   [L.parent_role(p, paths) for p in ("crew-orchestrator", "crew-reviewer", "crew-releaser", "links-build-m1", "other")]
   == ["orchestrator", "reviewer", "releaser", "builder", ""])

print("the autocompact window")
said = []
for value, want, speaks in ((None, "200000", False), (100000, "150000", True), (150000, "150000", False), (400000, "400000", False),
                            ("auto", "auto", False), ("oops", "200000", False), (250000.7, "250000", False)):
    cfg = json.loads((crew_home / "crew.json").read_text(encoding="utf-8"))
    cfg.setdefault("limits", {}).pop("autocompact_tokens", None)
    if value is not None:
        cfg["limits"]["autocompact_tokens"] = value
    (crew_home / "crew.json").write_text(json.dumps(cfg), encoding="utf-8")
    said.clear()
    got = L.compact_window(paths, said.append)
    ck(f"compact_window: {value!r} gives {want}{' and says so' if speaks else ''}", got == want and bool(said) == speaks, f"{got!r} {said}")

print("grants into allow rules")
text = ('- "restart staging" (2026-01-02)\n@project - "push to main on Fridays" (2026-02-03)\n'
        '* 2026-03-04: deploy the docs\n2026-03-05 - tidy the cache\n- no date at all\nnot a grant\n- ""\n'
        '- "dropped, the last line has no newline" (2026-04-05)')
rules = L.grant_rules(text, "Links App", "Pat")
ck("six grants are read, the unterminated last line is dropped (as bash's read loop drops it)",
   len(rules) == 6 and not any("dropped" in r for r in rules), str(len(rules)))
ck("a quoted grant keeps the words and the date",
   'granted this on 2026-01-02 for all projects, in their own words: "restart staging". Acting' in rules[0], rules[0])
ck("an @project grant says which project", "for project Links App" in rules[1], rules[1])
ck("a grant without a date says so", any("(undated)" in r for r in rules))
ck("no grants, no rules", L.grant_rules("", "p", "Pat") == [])

print("the status line command")
HEAD = ["tares", "hook", "statusline", "--plugin-root"]
ck("POSIX: a plain kit path is left alone, a path with a space is one word",
   shlex.split(L.status_line_command("/a b/kit", "linux")) == HEAD + ["/a b/kit"]
   and L.status_line_command("/k", "darwin") == "tares hook statusline --plugin-root /k")
ck("POSIX: a quote, a dollar and a semicolon in the path stay inside the word",
   shlex.split(L.status_line_command("/a'b $x;y/kit", "linux"))[4:] == ["/a'b $x;y/kit"])
ck("Windows: plain path as it is", L.status_line_command("C:/Users/p/kit", "win32") == "tares hook statusline --plugin-root C:/Users/p/kit")
ck("Windows: a space puts the path in double quotes, backslashes become forward slashes",
   L.status_line_command("C:\\Users\\Pat Doe\\kit", "win32") == 'tares hook statusline --plugin-root "C:/Users/Pat Doe/kit"')
refused = [c for c in ('"', "%", "^", "!", "$", "`", "\n") if raises(L.quote_windows, f"C:/a{c}b/kit") is not None]
ck("Windows: a quote, %, ^, !, $, a backtick and a line break are refused", len(refused) == 7, f"refused only {refused!r}")
ck("...with a message that says what to do", "move the crew home or the kit" in (raises(L.quote_windows, "C:/a%b") or ""))

print("the kit")
ck("cksum of 123456789 is the POSIX check value 930766865", cksum(b"123456789") == 930766865)
ck("cksum of nothing is POSIX's 4294967295", cksum(b"") == 4294967295)
kd = SCRATCH / "kitdir"
(kd / "hooks").mkdir(parents=True)
(kd / "scripts").mkdir()
(kd / "__pycache__").mkdir()
(kd / "hooks" / "hooks.json").write_bytes(b"echo hi\n")
(kd / "scripts" / "b.py").write_bytes(b"print(2)\n")
(kd / "scripts" / "a.py").write_bytes(b"print(1)\n")
for name in (".hidden", "x.pyc", "y.swp", "z~", "4913", "q.orig", "q.rej"):
    (kd / name).write_text("noise")
(kd / "__pycache__" / "m.pyc").write_bytes(b"\0")
want = cksum(b"echo hi\nprint(1)\nprint(2)\n")
ck("kit_version: files in byte order of ./path, dot files, editor droppings and caches left out",
   L.kit_version(kd) == str(want), f"{L.kit_version(kd)} vs {want}")
if shutil.which("cksum") and os.name != "nt":
    real = subprocess.run("cat ./hooks/hooks.json ./scripts/a.py ./scripts/b.py | cksum", shell=True, cwd=str(kd),
                          capture_output=True, text=True).stdout.split()[0]
    ck("...equal to the real `cksum` of the same bytes", L.kit_version(kd) == real, f"{L.kit_version(kd)} vs {real}")
    big = os.urandom(70000)
    real = subprocess.run(["cksum"], input=big, capture_output=True).stdout.split()[0].decode()
    ck("...and cksum of 70000 random bytes equals the real one", str(cksum(big)) == real)
else:
    print("  skip the real `cksum` comparison: no cksum on this machine")
ck("kit_version of a folder that is not there is the fingerprint of nothing", L.kit_version(SCRATCH / "nope") == str(cksum(b"")))

os.environ["TARES_PLUGIN_DIR"] = str(kd)
ck("kit_dir: TARES_PLUGIN_DIR is the kit, symlinks resolved", L.kit_dir() == os.path.realpath(kd))
ck("plugin_flags: --plugin-dir and that kit", L.plugin_flags() == ["--plugin-dir", os.path.realpath(kd)])
os.environ["TARES_PLUGIN_DIR"] = str(SCRATCH / "empty")
(SCRATCH / "empty").mkdir()
msg = raises(L.kit_dir)
ck("kit_dir: a TARES_PLUGIN_DIR without hooks/hooks.json is an error, not a guess", msg is not None and "has no hooks/hooks.json" in msg, str(msg))
(SCRATCH / "empty" / "bin").mkdir()
(SCRATCH / "empty" / "bin" / "factory").write_text("#!/bin/sh\n")
ck("kit_dir: a folder with only the old bin/factory is not a kit either", raises(L.kit_dir) is not None)
os.environ.pop("TARES_PLUGIN_DIR")
ck("kit_dir: with no override it is the claude-plugin of this tree", L.kit_dir() == os.path.realpath(ROOT / "claude-plugin"), L.kit_dir())

print("the idle wake note")
note = L.idle_wake_note()
ck("idle_wake_note says the tick is released and how to delete one", "idle_wake is on" in note and "CronDelete" in note)


def client_for(world, tmp=SCRATCH):
    LS.make_crew(tmp, {})
    tares = LS.FakeTares(world)
    tares.__enter__()
    os.environ["TARESD_URL"] = tares.url
    c = TaresClient(CrewPaths.at(tmp / "crew"), env=dict(os.environ))
    return tares, c


for label, settings, want in (("on", {"idle_wake": True}, True), ("off", {"idle_wake": False}, False), ("missing", {}, False)):
    tares, c = client_for({"settings": {**LS.DEFAULT_WORLD["settings"], **settings}})
    try:
        c.prime()
        ck(f"idle_wake_on: setting {label} gives {want}", L.idle_wake_on(c) is want)
    finally:
        c.close()
        tares.__exit__(None, None, None)
tares, c = client_for({"settings_status": 500})
try:
    ck("idle_wake_on: settings Tares cannot give mean no", L.idle_wake_on(c) is False)
finally:
    c.close()
    tares.__exit__(None, None, None)


# ---------------------------------------------------------------------------------------------
# Part 1b: what launch hands to claude, and when it refuses (no bash; claude is a stand-in)

print("launch: the seams (what it hands to claude, and its refusals)")


def launch_seen(name, station, role, parent, project, repo_name, out, world_=None):
    """Run launch with a claude that prints `out`; returns (CrewError text or None, the args and keywords it
    gave run_claude, the settings file as a dict)."""
    tmp = SCRATCH / name
    tmp.mkdir()
    LS.make_crew(tmp, {"token": FAKE_TOKEN})
    kit = LS.make_kit(tmp)
    seen = {}

    def fake(args, claude=None, timeout=None, platform=None, **kw):
        seen["args"], seen["kw"] = [str(a) for a in args], kw
        return subprocess.CompletedProcess(args, 0, out, None)

    old = L.cl.run_claude
    L.cl.run_claude = fake
    os.environ["TARES_PLUGIN_DIR"] = str(kit)
    tares = LS.FakeTares(world_ or {"projects": []})
    tares.__enter__()
    os.environ["TARESD_URL"] = tares.url
    c = TaresClient(CrewPaths.at(tmp / "crew"), env=dict(os.environ))
    try:
        c.prime()
        err = raises(L.launch, c, station, role, parent, project, str(tmp / repo_name), str(tmp / "crew"), "sonnet", "high",
                     "go", claude="claude-stand-in", sleep=lambda _s: None)
    finally:
        L.cl.run_claude = old
        os.environ.pop("TARES_PLUGIN_DIR", None)
        c.close()
        tares.__exit__(None, None, None)
    doc = {}
    if "args" in seen and "--settings" in seen["args"]:
        doc = json.loads(Path(seen["args"][seen["args"].index("--settings") + 1]).read_text(encoding="utf-8"))
    return err, seen, doc


started = "backgrounded \u00b7 abcd1234 \u00b7 links-app-build-m1"
err, seen, doc = launch_seen("seam-ok", "links-app-build-m1", "builder", "crew-orchestrator", "Links App", "links", started)
ck("launch with a stand-in claude that names a session starts it", err is None, str(err))
kw = seen.get("kw", {})
ck("claude is run with stdout and stderr as one text (the session id may be on either)", kw.get("merge_stderr") is True, str(kw))
ck("claude is run in the station's folder", kw.get("cwd") == str(SCRATCH / "seam-ok" / "crew"), str(kw.get("cwd")))
ck("claude is run without the person's key and key sources", tuple(kw.get("drop_env", ())) == tuple(L.KEPT_FROM_CHILDREN)
   and "TARES_AUTH_TOKEN" in L.KEPT_FROM_CHILDREN, str(kw.get("drop_env")))
ck("claude is run with FORCE_COLOR=0", kw.get("env_extra", {}).get("FORCE_COLOR") == "0", str(kw.get("env_extra")))
env = doc.get("env", {})
ck("the settings file names the key file twice: TARES_FACTORY_TOKEN_FILE and TARES_FACTORY_CRED_FILE, the same file",
   env.get("TARES_FACTORY_CRED_FILE") and env.get("TARES_FACTORY_CRED_FILE") == env.get("TARES_FACTORY_TOKEN_FILE"), str(env))
ck("a project Tares does not list, with a plain name: the name is the default project", env.get("TARES_PROJECT") == "Links App",
   str(env.get("TARES_PROJECT")))

err, seen, doc = launch_seen("seam-utf", "caf-build-m1", "builder", "crew-orchestrator", "Caf\u00e9", "cafe", started.replace("links-app", "caf"))
ck("a project Tares does not list, with a non-ASCII name: no default project (the header takes no non-ASCII)",
   err is None and doc.get("env", {}).get("TARES_PROJECT") == "" and doc["env"].get("TARES_PROJECT_NAME") == "Caf\u00e9", str((err, doc.get("env"))))

err, seen, doc = launch_seen("seam-helper", "links-app-build-m1-h1", "helper", "links-app-build-m1", "Links App", "links", started)
a = seen.get("args", [])
ident = a[a.index("--append-system-prompt") + 1] if "--append-system-prompt" in a else ""
ck("a helper is told to load its parent's skill (station-builder), not station-helper",
   "station-builder from the tares plugin" in ident and "station-helper" not in ident, ident[:200])

err, seen, doc = launch_seen("seam-trust", "crew-reviewer", "reviewer", "", "", "x", "Error: this folder is not trusted")
ck("a folder claude does not trust: the error says to run claude there once, not 'could not start'",
   err is not None and "has not trusted" in err and "could not start" not in err, str(err))
err, seen, doc = launch_seen("seam-none", "crew-reviewer", "reviewer", "", "", "x", "some other failure")
ck("a claude that names no session: 'could not start <station>'", err == "could not start crew-reviewer", str(err))
# Windows paths in the settings file: backslashes become forward slashes, in the env and in the prose rules
tmpw = SCRATCH / "win-paths"
tmpw.mkdir()
LS.make_crew(tmpw, {"token": FAKE_TOKEN})
kitw = LS.make_kit(tmpw)
os.environ["TARES_PLUGIN_DIR"] = str(kitw)
winrepo = "C:\\Users\\Jane\\links"
tw = LS.FakeTares({"projects": [{"id": "uc_links", "name": "Links App", "repos": [{"path": winrepo}, {"path": "D:\\other\\api"}]}]})
tw.__enter__()
os.environ["TARESD_URL"] = tw.url
cw = TaresClient(CrewPaths.at(tmpw / "crew"), env=dict(os.environ))
real_platform = sys.platform
try:
    cw.prime()
    sys.platform = "win32"
    try:
        wfile = L.write_settings(cw, "links-app-build-m1", "builder", "crew-orchestrator", "Links App", winrepo)
    finally:
        sys.platform = real_platform
    wdoc = json.loads(Path(wfile).read_text(encoding="utf-8"))
finally:
    cw.close()
    tw.__exit__(None, None, None)
    os.environ.pop("TARES_PLUGIN_DIR", None)
wallow = " ".join(wdoc["autoMode"]["allow"])
ck("Windows: FACTORY_REPO and FACTORY_WORKTREES use forward slashes",
   wdoc["env"]["FACTORY_REPO"] == "C:/Users/Jane/links" and wdoc["env"]["FACTORY_WORKTREES"] == "C:/Users/Jane/links/.factory/worktrees",
   str(wdoc["env"]))
ck("Windows: the worktree rule lists the project's repos with forward slashes",
   "C:/Users/Jane/links, D:/other/api" in wallow and "\\" not in wallow, wallow[:300])
ck("a builder denies gh pr merge, and no allow sentence merges a pull request",
   wdoc["permissions"].get("deny") == ["Bash(gh pr merge:*)"] and "Merging a pull request" not in wallow and "pre-authorized" not in wallow,
   json.dumps(wdoc["permissions"]))
ck("it keeps its push and open-PR allow", sum(1 for a in wdoc["autoMode"]["allow"] if a.startswith("Pushing")) == 1, wallow[:300])


# ---------------------------------------------------------------------------------------------
# Part 2: the Python engine alone (the bash comparison went with the bash engine, TR-1172; the
# parity scenarios `launch_*` hold what bash printed and wrote)

def drive(tmp, steps, env=None):
    """Run the driver once per step in a throwaway crew under tmp; returns [(code, stdout, stderr)]."""
    tmp.mkdir(parents=True, exist_ok=True)
    base = parity.crew_env(tmp)
    out = []
    for op, args, world, setup in steps:
        e = {**base, **(env or {}), "LAUNCH_WORLD": json.dumps(world), "LAUNCH_SETUP": json.dumps(setup or {})}
        p = subprocess.run([sys.executable, str(HERE / "factory" / "launch_support.py"), op, *args], cwd=str(ROOT),
                           env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out.append((p.returncode, p.stdout, p.stderr))
    return out


def settings_of(tmp, station):
    """The station's settings file, parsed; the status line command is returned apart, as the words a shell makes of it."""
    doc = json.loads((tmp / "crew" / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))
    words = shlex.split(doc["statusLine"].pop("command"))
    return doc, words


def world(autonomy="L3-review", projects=None, **settings):
    return {"settings": {"autonomy": autonomy, "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3, **settings},
            "projects": projects or [], "grants": {"all": '- "the releaser may restart the staging database" (2026-01-02)\n',
                                                   "projects": {}}}


for label, base in (("a plain path", "tares-launch-plain-"), ("a path with a space", "tares launch spaced ")):
    print(f"the Python engine writes the settings, {label}")
    links = {"id": "uc_links", "name": "Links App", "repos": [{"path": "{TMP}/links"}]}
    cases = [("crew-orchestrator", "orchestrator", "", "", ""), ("crew-reviewer", "reviewer", "", "", ""),
             ("crew-releaser", "releaser", "", "", ""),
             ("links-app-build-m1", "builder", "crew-orchestrator", "Links App", "{TMP}/links"),
             ("links-app-build-m1-h1", "helper", "links-app-build-m1", "Links App", "{TMP}/links")]
    tmp = Path(tempfile.mkdtemp(prefix=f"{base}python-", dir=SCRATCH)).resolve()
    steps = [("write_settings", list(c), world(projects=[links]), {"stations": {"links-app-build-m1": {"autonomy": "L3-review", "project_id": "uc_links"}}})
             for c in cases]
    res = drive(tmp, steps)
    ck("every write_settings exits 0 and prints the file's path", all(r[0] == 0 for r in res) and all(r[1].strip().endswith(".settings.json") for r in res),
       str([(r[0], r[2][-120:]) for r in res]))
    for station, role, *_ in cases:
        doc, words = settings_of(tmp, station)
        words = [parity.norm_text(w, tmp) for w in words]
        ck(f"{role} {station}: permissions deny gh pr merge, an allow list is kept, no allow sentence merges",
           "Bash(gh pr merge:*)" in (doc["permissions"].get("deny") or []) and bool(doc["permissions"].get("allow"))
           and not any("Merging a pull request" in a or "pre-authorized" in a for a in doc["autoMode"]["allow"]),
           json.dumps(doc["permissions"]))
        ck(f"{role} {station}: the status line is `tares hook statusline --plugin-root <kit>`, the kit as one word",
           words[:4] == HEAD and len(words) == 5, str(words))
    if " " in tmp.name:
        _, words = settings_of(tmp, "crew-reviewer")
        ck("the crew home with a space is one word in the status line and in the file's own paths",
           " " in tmp.name and len(words) == 5)
        doc = json.loads((tmp / "crew" / "stations" / "crew-reviewer.settings.json").read_text(encoding="utf-8"))
        # ensure_ascii off: a letter outside ASCII in the profile folder would print as \u00e9, a backslash that is not a path separator
        env_text = json.dumps(doc["env"], ensure_ascii=False)
        ck("JSON paths use forward slashes", "\\" not in env_text.replace("\\n", ""), str({k: v for k, v in doc["env"].items() if "\\" in str(v)})[:300])
    ck("no token in a settings file", all(FAKE_TOKEN not in p.read_text(encoding="utf-8", errors="replace")
                                          for p in (tmp / "crew" / "stations").glob("*.settings.json")))

print("the Python engine starts a station with the right command line and environment")
tmp = Path(tempfile.mkdtemp(prefix="tares crew launch argv python ", dir=SCRATCH)).resolve()
secret = {"TARES_AUTH_TOKEN": FAKE_TOKEN, "TARES_FACTORY_SOURCE": "/x/source", "TARES_FACTORY_KEY_SOURCE": "/x/keysource"}
steps = [("launch", [f"crew-{role}", role, "", "", "", "{TMP}/crew", model, "high", f"Boot as the {role} station"], world(), None)
         for role, model in (("orchestrator", "opus"), ("reviewer", "sonnet"))]
steps.append(("launch", ["crew-releaser", "releaser", "", "", "", "{TMP}/crew", "sonnet", "high", "go", "sess-9"], world(), None))
res = drive(tmp, steps, env=secret)
all_calls = [json.loads(x) for x in (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
calls = [c for c in all_calls if c["args"] and c["args"][0] == "--bg"]
ck("it starts three stations", all(r[0] == 0 for r in res) and len(calls) == 3, str(([r[0] for r in res], len(calls))))
norm_calls = [{"cwd": parity.norm_text(c["cwd"], tmp), "args": [parity.norm_text(a, tmp) for a in c["args"]]} for c in calls]
a = norm_calls[0]["args"]
ck("the line is --bg -n <station> --model --effort --permission-mode auto --settings --autocompact --append-system-prompt --plugin-dir, prompt last",
   a[:3] == ["--bg", "-n", "crew-orchestrator"] and a[3:11:2] == ["--model", "--effort", "--permission-mode", "--settings"]
   and "--autocompact" in a and "--append-system-prompt" in a and "--plugin-dir" in a and a[-1] == "Boot as the orchestrator station", str(a)[:300])
ck("a resumed station: --resume with the session id, the prompt still last",
   norm_calls[2]["args"][-3:-1] == ["--resume", "sess-9"] and norm_calls[2]["args"][-1] == "go")
env_text = (tmp / "claude" / "last.env").read_text(encoding="utf-8")
env_map = dict(x.split("=", 1) for x in env_text.splitlines() if "=" in x)
ck("the session is started without the person's key or the key source (TARES_AUTH_TOKEN, TARES_FACTORY_*SOURCE)",
   not any(k in env_map for k in secret) and FAKE_TOKEN not in env_text, str([k for k in secret if k in env_map]))
ck("the session gets NO_COLOR=1 and FORCE_COLOR=0", env_map.get("NO_COLOR") == "1" and env_map.get("FORCE_COLOR") == "0",
   f"{env_map.get('NO_COLOR')!r} {env_map.get('FORCE_COLOR')!r}")
ck("no token in the command lines or in the output", all(FAKE_TOKEN not in json.dumps(c) for c in calls)
   and all(FAKE_TOKEN not in r[1] + r[2] for r in res))
st = json.loads((tmp / "crew" / "stations.json").read_text(encoding="utf-8"))
ck("stations.json records a kit fingerprint for a started station", bool(st["crew-orchestrator"].get("kit")), str(sorted(st["crew-orchestrator"])))
kit_py = L.kit_version(tmp / "kit")
ck("kit_version of a folder: the function and the driver agree",
   kit_py == drive(tmp, [("kit_version", [str(tmp / "kit")], world(), None)])[0][1].strip() == st["crew-orchestrator"]["kit"],
   f"{kit_py} vs {st['crew-orchestrator']['kit']}")

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

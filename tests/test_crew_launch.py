"""Building and starting a station in Python (tares/crew/launch.py, TR-953) does what the bash engine does.

Part 1 needs no bash: the small rules (model check, levels, the parent's role, grants into allow rules), the
quoting of the status line command on Mac and on Windows (the Windows form is tested without Windows), the
cksum that fingerprints the kit, the kit folder, the two notes that are not ported yet, the idle wake, and what
a started session is given (its environment holds no key, its command line holds no token).

Part 2 runs the same call on both engines in a throwaway crew of its own (tests/factory/launch_support.py:
a fake Tares, a copy of the kit, the fake Claude Code) and compares what they leave: the settings file of every
role, also for a crew home with a space in its path, and the `claude` command line of a start. It is skipped,
with the reason printed, when bash or jq is missing or on Windows. The scenarios of the parity harness
(`parity.py launch_*`) go further, one assertion at a time.

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
# Part 1: no bash

print("small rules")
msg = raises(L.check_model, "claude-haiku-4-5", "crew-reviewer")
ck("a Haiku model is refused, naming the station and the way out",
   msg is not None and "crew-reviewer" in msg and "Auto mode does not run on Haiku" in msg and "sonnet or opus" in msg, str(msg))
ck("...also written with a capital", raises(L.check_model, "Haiku", "s") is not None)
ck("sonnet and opus pass", raises(L.check_model, "sonnet", "s") is None and raises(L.check_model, "opus", "s") is None)
ck("level_rank: 3, 4, and nothing for anything else",
   (L.level_rank("L3-review"), L.level_rank("L4-ship"), L.level_rank("L5"), L.level_rank("")) == (3, 4, None, None))
ck("level_text names the human's yes at L3 and the reviewer's verdict at L4",
   "waits for the human's yes" in L.level_text("L3-review") and "reviewer's verdict and green CI" in L.level_text("L4-ship"))
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
ck("POSIX: a plain path is left alone, a path with a space is one word",
   shlex.split(L.status_line_command("/a b/kit/scripts/statusline.py", "linux")) == ["python3", "/a b/kit/scripts/statusline.py"]
   and L.status_line_command("/k/s.py", "darwin") == "python3 /k/s.py")
ck("POSIX: a quote, a dollar and a semicolon in the path stay inside the word",
   shlex.split(L.status_line_command("/a'b $x;y/s.py", "linux"))[1:] == ["/a'b $x;y/s.py"])
ck("Windows: plain path as it is, `python` not `python3`", L.status_line_command("C:/Users/p/kit/s.py", "win32") == "python C:/Users/p/kit/s.py")
ck("Windows: a space puts the path in double quotes, backslashes become forward slashes",
   L.status_line_command("C:\\Users\\Pat Doe\\kit\\s.py", "win32") == 'python "C:/Users/Pat Doe/kit/s.py"')
refused = [c for c in ('"', "%", "^", "!", "$", "`", "\n") if raises(L.quote_windows, f"C:/a{c}b/s.py") is not None]
ck("Windows: a quote, %, ^, !, $, a backtick and a line break are refused", len(refused) == 7, f"refused only {refused!r}")
ck("...with a message that says what to do", "move the crew home or the kit" in (raises(L.quote_windows, "C:/a%b") or ""))

print("the kit")
ck("cksum of 123456789 is the POSIX check value 930766865", cksum(b"123456789") == 930766865)
ck("cksum of nothing is POSIX's 4294967295", cksum(b"") == 4294967295)
kd = SCRATCH / "kitdir"
(kd / "bin").mkdir(parents=True)
(kd / "scripts").mkdir()
(kd / "__pycache__").mkdir()
(kd / "bin" / "factory").write_bytes(b"echo hi\n")
(kd / "scripts" / "b.py").write_bytes(b"print(2)\n")
(kd / "scripts" / "a.py").write_bytes(b"print(1)\n")
for name in (".hidden", "x.pyc", "y.swp", "z~", "4913", "q.orig", "q.rej"):
    (kd / name).write_text("noise")
(kd / "__pycache__" / "m.pyc").write_bytes(b"\0")
want = cksum(b"echo hi\nprint(1)\nprint(2)\n")
ck("kit_version: files in byte order of ./path, dot files, editor droppings and caches left out",
   L.kit_version(kd) == str(want), f"{L.kit_version(kd)} vs {want}")
if shutil.which("cksum") and os.name != "nt":
    real = subprocess.run("cat ./bin/factory ./scripts/a.py ./scripts/b.py | cksum", shell=True, cwd=str(kd),
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
ck("kit_dir: a TARES_PLUGIN_DIR without bin/factory is an error, not a guess", msg is not None, str(msg))
os.environ.pop("TARES_PLUGIN_DIR")
ck("kit_dir: with no override it is the claude-plugin of this tree", L.kit_dir() == os.path.realpath(ROOT / "claude-plugin"), L.kit_dir())

print("the notes not ported yet, and the idle wake")
ck("pace_launch_note and iw_launch_note are stubs (TR-962, TR-961) that add nothing",
   L.pace_launch_note("crew-reviewer") == "" and L.iw_launch_note("crew-reviewer") == "")
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

# ---------------------------------------------------------------------------------------------
# Part 2: bash and Python side by side

def drive(engine, tmp, steps, env=None):
    """Run the driver once per step in a throwaway crew under tmp; returns [(code, stdout, stderr)]."""
    tmp.mkdir(parents=True, exist_ok=True)
    base = parity.crew_env(tmp, engine)
    out = []
    for op, args, world, setup in steps:
        e = {**base, **(env or {}), "LAUNCH_WORLD": json.dumps(world), "LAUNCH_SETUP": json.dumps(setup or {})}
        p = subprocess.run([sys.executable, str(HERE / "factory" / "launch_support.py"), engine, op, *args], cwd=str(ROOT),
                           env=e, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out.append((p.returncode, p.stdout, p.stderr))
    return out


def settings_of(tmp, station):
    """The station's settings file, parsed; the status line command is returned apart (its quoting differs
    by design: shlex.quote against printf %q), as the words a shell makes of it."""
    doc = json.loads((tmp / "crew" / "stations" / f"{station}.settings.json").read_text(encoding="utf-8"))
    words = shlex.split(doc["statusLine"].pop("command"))
    return doc, words


def normal(doc, tmp):
    return json.loads(parity.render_json(parity.norm_json(doc, tmp)))


def world(autonomy="L3-review", projects=None, **settings):
    return {"settings": {"autonomy": autonomy, "releaser": True, "release_profile": "kind", "challenger": False,
                         "builders_max": 3, **settings},
            "projects": projects or [], "grants": {"all": '- "the releaser may restart the staging database" (2026-01-02)\n',
                                                   "projects": {}}}


why = parity.bash_available()
if why:
    print(f"skip the bash comparison: {why}")
else:
    for label, base in (("a plain path", "tares-launch-plain-"), ("a path with a space", "tares launch spaced ")):
        print(f"bash and Python write the same settings, {label}")
        links = {"id": "uc_links", "name": "Links App", "repos": [{"path": "{TMP}/links"}]}
        cases = [("crew-orchestrator", "orchestrator", "", "", ""), ("crew-reviewer", "reviewer", "", "", ""),
                 ("crew-releaser", "releaser", "", "", ""),
                 ("links-app-build-m1", "builder", "crew-orchestrator", "Links App", "{TMP}/links"),
                 ("links-app-build-m1-h1", "helper", "links-app-build-m1", "Links App", "{TMP}/links")]
        tmps = {e: Path(tempfile.mkdtemp(prefix=f"{base}{e}-", dir=SCRATCH)).resolve() for e in ("bash", "python")}
        for e, tmp in tmps.items():
            steps = [("write_settings", list(c), world(projects=[links]), {"stations": {"links-app-build-m1": {"autonomy": "L3-review", "project_id": "uc_links"}}})
                     for c in cases]
            res = drive(e, tmp, steps)
            ck(f"{e}: every write_settings exits 0 and prints the file's path", all(r[0] == 0 for r in res) and all(r[1].strip().endswith(".settings.json") for r in res),
               str([(r[0], r[2][-120:]) for r in res]))
        for station, role, *_ in cases:
            docs = {}
            for e, tmp in tmps.items():
                docs[e], words = settings_of(tmp, station)
                docs[e] = normal(docs[e], tmp)
                docs[e + "_words"] = [parity.norm_text(w, tmp) for w in words]
            ck(f"{role} {station}: same settings (key order aside)", docs["bash"] == docs["python"],
               "\n".join(k for k in docs["bash"] if docs["bash"].get(k) != docs["python"].get(k)))
            ck(f"{role} {station}: the status line runs the kit's statusline.py in both, as one word each",
               docs["bash_words"] == docs["python_words"] and docs["python_words"][0] == "python3"
               and docs["python_words"][1].endswith("/scripts/statusline.py") and len(docs["python_words"]) == 2, str(docs["python_words"]))
        if " " in tmps["python"].name:
            _, words = settings_of(tmps["python"], "crew-reviewer")
            ck("the crew home with a space is one word in the status line and in the file's own paths",
               " " in tmps["python"].name and len(words) == 2)
            doc = json.loads((tmps["python"] / "crew" / "stations" / "crew-reviewer.settings.json").read_text(encoding="utf-8"))
            ck("JSON paths use forward slashes", "\\" not in json.dumps(doc["env"]).replace("\\n", ""), json.dumps(doc["env"])[:200])
        ck("no token in a settings file", all(FAKE_TOKEN not in p.read_text(encoding="utf-8", errors="replace")
                                              for t in tmps.values() for p in (t / "crew" / "stations").glob("*.settings.json")))

    print("bash and Python start a station with the same command line and the same environment")
    tmps = {e: Path(tempfile.mkdtemp(prefix=f"tares crew launch argv {e} ", dir=SCRATCH)).resolve() for e in ("bash", "python")}
    secret = {"TARES_AUTH_TOKEN": FAKE_TOKEN, "TARES_FACTORY_SOURCE": "/x/source", "TARES_FACTORY_KEY_SOURCE": "/x/keysource"}
    runs = {}
    for e, tmp in tmps.items():
        steps = [("launch", [f"crew-{role}", role, "", "", "", "{TMP}/crew", model, "high", f"Boot as the {role} station"], world(), None)
                 for role, model in (("orchestrator", "opus"), ("reviewer", "sonnet"))]
        steps.append(("launch", ["crew-releaser", "releaser", "", "", "", "{TMP}/crew", "sonnet", "high", "go", "sess-9"], world(), None))
        res = drive(e, tmp, steps, env=secret)
        calls = [json.loads(x) for x in (tmp / "claude" / "calls.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        runs[e] = (res, [c for c in calls if c["args"] and c["args"][0] == "--bg"], tmp)
    ck("both start three stations", all(r[0] == 0 for e in runs for r in runs[e][0]) and all(len(runs[e][1]) == 3 for e in runs),
       str([(e, [r[0] for r in runs[e][0]], len(runs[e][1])) for e in runs]))
    norm_calls = {e: [{"cwd": parity.norm_text(c["cwd"], runs[e][2]), "args": [parity.norm_text(a, runs[e][2]) for a in c["args"]]}
                      for c in runs[e][1]] for e in runs}
    ck("the claude command lines are the same, argument for argument", norm_calls["bash"] == norm_calls["python"],
       json.dumps(norm_calls)[:300])
    a = norm_calls["python"][0]["args"]
    ck("the line is --bg -n <station> --model --effort --permission-mode auto --settings --autocompact --append-system-prompt --plugin-dir, prompt last",
       a[:3] == ["--bg", "-n", "crew-orchestrator"] and a[3:11:2] == ["--model", "--effort", "--permission-mode", "--settings"]
       and "--autocompact" in a and "--append-system-prompt" in a and "--plugin-dir" in a and a[-1] == "Boot as the orchestrator station", str(a)[:300])
    ck("a resumed station: --resume with the session id, the prompt still last",
       norm_calls["python"][2]["args"][-3:-1] == ["--resume", "sess-9"] and norm_calls["python"][2]["args"][-1] == "go")
    for e, (res, calls, tmp) in runs.items():
        env_text = (tmp / "claude" / "last.env").read_text(encoding="utf-8")
        env_map = dict(x.split("=", 1) for x in env_text.splitlines() if "=" in x)
        ck(f"{e}: the session is started without the person's key or the key source (TARES_AUTH_TOKEN, TARES_FACTORY_*SOURCE)",
           not any(k in env_map for k in secret) and FAKE_TOKEN not in env_text, str([k for k in secret if k in env_map]))
        ck(f"{e}: the session gets NO_COLOR=1 and FORCE_COLOR=0", env_map.get("NO_COLOR") == "1" and env_map.get("FORCE_COLOR") == "0",
           f"{env_map.get('NO_COLOR')!r} {env_map.get('FORCE_COLOR')!r}")
        ck(f"{e}: no token in the command lines or in the output", all(FAKE_TOKEN not in json.dumps(c) for c in calls)
           and all(FAKE_TOKEN not in r[1] + r[2] for r in res))
    st = {e: json.loads((runs[e][2] / "crew" / "stations.json").read_text(encoding="utf-8")) for e in runs}
    keys = {e: sorted(st[e]["crew-orchestrator"]) for e in st}
    ck("stations.json records the same fields for a started station", keys["bash"] == keys["python"], str(keys))
    ck("...and the same kit fingerprint (cksum)", st["bash"]["crew-orchestrator"]["kit"] == st["python"]["crew-orchestrator"]["kit"],
       f"{st['bash']['crew-orchestrator']['kit']} vs {st['python']['crew-orchestrator']['kit']}")
    kit_py = L.kit_version(runs["python"][2] / "kit")
    kit_sh = drive("bash", tmps["bash"], [("kit_version", [str(runs["bash"][2] / "kit")], world(), None)])[0][1].strip()
    ck("kit_version of the same folder: Python equals bash", kit_py == drive("python", tmps["python"], [("kit_version", [str(runs["python"][2] / "kit")], world(), None)])[0][1].strip()
       and kit_sh == st["bash"]["crew-orchestrator"]["kit"], f"{kit_py} vs {kit_sh}")

    print("bash and Python agree on the small functions")
    probes = [("level_rank", ["L3-review"]), ("level_rank", ["L4-ship"]), ("level_rank", ["L9"]), ("level_text", ["L3-review"]),
              ("level_text", ["L4-ship"]), ("check_model", ["claude-haiku-4-5", "crew-x"]), ("check_model", ["opus", "crew-x"]),
              ("parent_role", ["crew-orchestrator"]), ("parent_role", ["p-build-m1"]), ("parent_role", ["zzz"]),
              ("idle_wake_note", []), ("plugin_flags", [])]
    t_b = Path(tempfile.mkdtemp(prefix="tares crew launch small bash ", dir=SCRATCH)).resolve()
    t_p = Path(tempfile.mkdtemp(prefix="tares crew launch small python ", dir=SCRATCH)).resolve()
    rb = drive("bash", t_b, [(op, a, world(), None) for op, a in probes])
    rp = drive("python", t_p, [(op, a, world(), None) for op, a in probes])
    same = [(op, a) for (op, a), x, y in zip(probes, rb, rp)
            if (x[0], parity.norm_text(x[1], t_b), parity.norm_text(x[2], t_b)) != (y[0], parity.norm_text(y[1], t_p), parity.norm_text(y[2], t_p))]
    ck(f"{len(probes)} calls give the same exit code, output and error text", not same, str(same))

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

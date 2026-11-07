"""The engine ships inside the `tares` package, and the plugin refuses a `tares` that is too old (TR-1064).

Checks: a wheel built from this tree holds tares/crew/ with schemas/schema_version.json, the crew's
version module and the plugin's min_tares.txt, and its metadata requires psutil and keyring; pyproject
keeps the dependency lines and the dev group; check_plugin_compat (older and newer min_tares, rc
versions, a missing file, malformed JSON, min_tares in plugin.json); `tares hook compat` end to end
(one line when the plugin wants a newer tares, nothing otherwise, exit 0 both ways, exit 2 for an
unknown hook); and the plugin's session-start.sh (the line appears when min_tares is above the
installed version, and the hook says nothing extra when it is not, when tares is not on PATH, or when
the check fails).

The wheel is built in a copy of the tree (git's file list, with a stub console), so the test leaves no
dist/ or build output in the checkout. With `uv` not installed, the wheel checks are skipped and say so.

Run: .venv/bin/python tests/test_crew_packaging.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tares.crew import version as V  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def short(x, n=300):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "..."


T = Path(tempfile.mkdtemp(prefix="tares crew packaging "))

# ---------- pyproject ----------
print("pyproject")
PYPROJECT = ROOT / "pyproject.toml"
pp = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
deps = pp["project"]["dependencies"]
ck("psutil and keyring are runtime dependencies with lower bounds",
   any(d.startswith("psutil>=") for d in deps) and any(d.startswith("keyring>=") for d in deps), short(deps))
# One keyring entry only (TR-949 added it first; a second line here would be a duplicate dependency).
ck("keyring is listed once, as `\"keyring>=24\"`", sum(d.replace(" ", "") == "keyring>=24" for d in deps) == 1 and sum(d.startswith("keyring") for d in deps) == 1, short(deps))
ck("jsonschema is in the dev group only, not a runtime dependency",
   any(d.startswith("jsonschema") for d in pp.get("dependency-groups", {}).get("dev", []))
   and not any(d.startswith("jsonschema") for d in deps), short(pp.get("dependency-groups")))
schema = ROOT / "tares" / "crew" / "schemas" / "schema_version.json"
ck("tares/crew/schemas/schema_version.json is {\"schema_version\": 1}",
   schema.is_file() and json.loads(schema.read_text(encoding="utf-8")) == {"schema_version": 1})
MIN_FILE = ROOT / "claude-plugin" / "min_tares.txt"
ck("the plugin names a min_tares that is a version", MIN_FILE.is_file() and V.parse_version(MIN_FILE.read_text().strip()) is not None)

# ---------- the wheel ----------
print("wheel")
if not shutil.which("uv"):
    print("  skip  uv is not installed: the wheel is not built, so its contents are not checked")
else:
    src = T / "tree"
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=str(ROOT), capture_output=True,
                           check=True).stdout.decode("utf-8").split("\0")
    for rel in filter(None, files):
        if rel.startswith("ui/") or not (ROOT / rel).is_file():
            continue
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, src / rel)
    (src / "ui" / "dist").mkdir(parents=True)
    (src / "ui" / "dist" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    out = T / "wheel"
    r = subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out), str(src)], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(T))
    wheels = sorted(out.glob("tares-*.whl"))
    ck("uv build --wheel builds a wheel", r.returncode == 0 and len(wheels) == 1, short((r.returncode, r.stderr[-500:])))
    if wheels:
        with zipfile.ZipFile(wheels[0]) as z:
            names = set(z.namelist())
            meta = next((z.read(n).decode("utf-8") for n in names if n.endswith(".dist-info/METADATA")), "")
        ck("the wheel holds tares/crew/schemas/schema_version.json", "tares/crew/schemas/schema_version.json" in names)
        ck("...and the crew modules, including version.py", {"tares/crew/__init__.py", "tares/crew/version.py", "tares/crew/dispatch.py"} <= names)
        ck("...and the plugin with its min_tares.txt", "tares/claude-plugin/min_tares.txt" in names)
        req = [ln.split(":", 1)[1].strip().lower() for ln in meta.splitlines() if ln.startswith("Requires-Dist:")]
        ck("the wheel metadata lists psutil as Requires-Dist", any(x.startswith("psutil") for x in req), short(req))
        ck("...and keyring", any(x.startswith("keyring") for x in req), short(req))
        ck("...and not jsonschema (dev only)", not any(x.startswith("jsonschema") for x in req), short(req))

# ---------- check_plugin_compat ----------
print("check_plugin_compat")
have = V.installed_version()
ck("the installed version is read and parses", V.parse_version(have) is not None, have)


def plugin(name, min_txt=None, manifest=None):
    root = T / name
    (root / ".claude-plugin").mkdir(parents=True)
    if min_txt is not None:
        (root / "min_tares.txt").write_text(min_txt, encoding="utf-8")
    if manifest is not None:
        (root / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    return root


msg = V.check_plugin_compat(plugin("far", "999.0.0\n"))
ck("a plugin that wants a far newer tares gets one line", msg != "" and "\n" not in msg, short(msg))
ck("...saying the installed version, the needed version and how to upgrade",
   have in msg and "999.0.0" in msg and "uv tool upgrade tares" in msg and "pip install -U tares" in msg, short(msg))
ck("...with no em dash", chr(0x2014) not in msg and chr(0x2013) not in msg)
ck("a plugin whose min_tares is older gives nothing", V.check_plugin_compat(plugin("low", "0.0.1\n")) == "")
ck("a plugin whose min_tares is the installed version gives nothing", V.check_plugin_compat(plugin("same", have + "\n")) == "")
ck("a missing plugin folder gives nothing", V.check_plugin_compat(T / "nowhere") == "")
ck("a plugin with no min_tares gives nothing", V.check_plugin_compat(plugin("none", None, "{}")) == "")
ck("malformed plugin.json gives nothing", V.check_plugin_compat(plugin("bad", None, "{ not json")) == "")
ck("plugin.json that is not an object gives nothing", V.check_plugin_compat(plugin("list", None, "[1, 2]")) == "")
ck("a min_tares that is not a version gives nothing", V.check_plugin_compat(plugin("junk", "soon\n")) == "")
ck("an empty min_tares.txt gives nothing", V.check_plugin_compat(plugin("empty", "")) == "")
ck("a min_tares key in plugin.json works too", V.check_plugin_compat(plugin("key", None, '{"min_tares": "999.0.0"}')) != "")
ck("a non-string min_tares key gives nothing", V.check_plugin_compat(plugin("keynum", None, '{"min_tares": 5}')) == "")
ck("min_tares.txt wins over plugin.json", V.check_plugin_compat(plugin("both", "0.0.1\n", '{"min_tares": "999.0.0"}')) == "")
ck("a root that is None or a number does not raise", V.check_plugin_compat(None) == "" and V.check_plugin_compat(5) == "")

print("versions")
pv = V.parse_version
ck("1.2.3 < 1.10.0 (numbers, not text)", pv("1.2.3") < pv("1.10.0"))
ck("1.2 equals 1.2.0", pv("1.2") == pv("1.2.0"))
ck("an rc of 1.2.3 sorts below 1.2.3: -rc.1, rc1 and .rc1", pv("1.2.3-rc.1") < pv("1.2.3") and pv("1.2.3rc1") < pv("1.2.3") and pv("1.2.3.rc1") < pv("1.2.3"))
ck("rc1 < rc2 < the release; rc of the next version is above this one", pv("1.2.3rc1") < pv("1.2.3rc2") < pv("1.2.3") < pv("1.2.4rc1"))
ck("-rc.1 and rc1 are the same version", pv("1.2.3-rc.1") == pv("1.2.3rc1"))
ck("a v prefix and a local suffix are ignored", pv("v1.2.3") == pv("1.2.3") == pv("1.2.3+local.1"))
ck("a post release is not below its release", pv("1.2.3.post1") >= pv("1.2.3"))
ck("text that is not a version is None", pv("") is None and pv("abc") is None and pv(None) is None)
# the check itself with a stubbed installed version
real_installed = V.installed_version
try:
    V.installed_version = lambda: "1.2.3rc1"
    ck("installed 1.2.3rc1 is too old for min 1.2.3", V.check_plugin_compat(plugin("rc1", "1.2.3\n")) != "")
    ck("installed 1.2.3rc1 is enough for min 1.2.2", V.check_plugin_compat(plugin("rc2", "1.2.2\n")) == "")
    V.installed_version = lambda: "1.2.3"
    ck("installed 1.2.3 is enough for min 1.2.3rc1", V.check_plugin_compat(plugin("rc3", "1.2.3-rc.1\n")) == "")
    V.installed_version = lambda: "1.2.3"
    ck("installed 1.2.3 is enough for min 1.2.3", V.check_plugin_compat(plugin("eq", "1.2.3\n")) == "")
    V.installed_version = lambda: ""
    ck("an installed version that cannot be read gives nothing", V.check_plugin_compat(plugin("nover", "9.0.0\n")) == "")
finally:
    V.installed_version = real_installed

# ---------- tares hook compat ----------
print("tares hook compat")
SHIM_PY = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_"))}
CLEAN.update(HOME=str(T / "home"), PYTHONPATH=str(ROOT), TARES_HOME=str(T / "tares-home"))
(T / "home").mkdir(exist_ok=True)


def tares(args, extra=None):
    return subprocess.run([sys.executable, "-c", SHIM_PY, *args], capture_output=True, text=True, encoding="utf-8",
                          env=dict(CLEAN, **(extra or {})), cwd=str(T))


far, low = plugin("h_far", "999.0.0\n"), plugin("h_low", "0.0.1\n")
r = tares(["hook", "compat", "--plugin-root", str(far)])
ck("a plugin that wants a newer tares: one line on stdout, exit 0",
   r.returncode == 0 and len(r.stdout.splitlines()) == 1 and "999.0.0" in r.stdout, short((r.returncode, r.stdout, r.stderr[-200:])))
r = tares(["hook", "compat", "--plugin-root", str(low)])
ck("a low min_tares: nothing printed, exit 0", r.returncode == 0 and r.stdout == "" and r.stderr == "", short((r.returncode, r.stdout, r.stderr[-200:])))
r = tares(["hook", "compat"], {"CLAUDE_PLUGIN_ROOT": str(far)})
ck("the plugin root comes from CLAUDE_PLUGIN_ROOT", r.returncode == 0 and "999.0.0" in r.stdout, short((r.returncode, r.stdout)))
r = tares(["hook", "compat", "--plugin-root", str(low)], {"CLAUDE_PLUGIN_ROOT": str(far)})
ck("...and --plugin-root wins over it", r.returncode == 0 and r.stdout == "", short(r.stdout))
r = tares(["hook", "compat"])
ck("no plugin root at all: nothing, exit 0", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout)))
r = tares(["hook", "compat", "--plugin-root", str(T / "nowhere")])
ck("a plugin root that does not exist: nothing, exit 0", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout)))
r = tares(["hook", "compat", "--plugin-root", str(plugin("h_bad", None, "{ not json"))])
ck("a malformed plugin.json: nothing, exit 0", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout)))
r = tares(["hook", "nope"])
ck("an unknown hook name exits 2 with a message", r.returncode == 2 and "unknown hook" in r.stderr and "compat" in r.stderr and r.stdout == "",
   short((r.returncode, r.stdout, r.stderr)))
r = tares(["--help"])
ck("`tares --help` lists hook", r.returncode == 0 and "hook" in r.stdout, short(r.stdout[:200]))

# ---------- session-start.sh ----------
print("session-start.sh")
HOOK = ROOT / "claude-plugin" / "hooks" / "session-start.sh"
if os.name == "nt" or not shutil.which("bash") or not shutil.which("jq"):
    print("  skip  no bash or jq here: the session-start checks")
else:
    bindir = T / "hookbin"
    bindir.mkdir()
    shim = bindir / "tares"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" -c "{SHIM_PY}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    nobin = T / "nobin"  # a PATH with bash, jq and the system tools, and no tares
    nobin.mkdir()
    for tool in ("bash", "jq"):
        (nobin / tool).symlink_to(shutil.which(tool))
    broken = T / "brokenbin"  # a tares that fails and prints junk
    broken.mkdir()
    (broken / "tares").write_text("#!/bin/sh\necho junk\necho oops >&2\nexit 1\n", encoding="utf-8")
    (broken / "tares").chmod(0o755)
    syspath = os.pathsep.join(["/usr/bin", "/bin"])

    def hook(bins, root, stdin='{"source": "startup"}', extra=None):
        env = dict(CLEAN, PATH=os.pathsep.join([str(bins), syspath]), CLAUDE_PLUGIN_ROOT=str(root))
        env.update(extra or {})
        return subprocess.run(["bash", str(HOOK)], input=stdin, capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(T))

    def ctx(r):
        try:
            return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        except Exception:
            return None

    r = hook(bindir, far)
    c = ctx(r)
    ck("an ordinary session with a plugin that wants a newer tares: the line, as session-start context",
       r.returncode == 0 and c is not None and c.startswith("This Tares plugin needs tares 999.0.0") and "uv tool upgrade tares" in c,
       short((r.returncode, r.stdout, r.stderr[-200:])))
    r = hook(bindir, low)
    ck("...with a low min_tares: the hook says nothing, as before", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout, r.stderr[-200:])))
    r = hook(nobin, far)
    ck("...with no tares on PATH: nothing, and the hook does not fail", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout, r.stderr[-200:])))
    r = hook(broken, far)
    ck("...with a tares that fails: nothing, and the hook does not fail", r.returncode == 0 and r.stdout == "", short((r.returncode, r.stdout, r.stderr[-200:])))
    # the plugin root defaults to the hook's own plugin when CLAUDE_PLUGIN_ROOT is not set
    own = T / "own"
    (own / "hooks").mkdir(parents=True)
    shutil.copy(HOOK, own / "hooks" / "session-start.sh")
    (own / "min_tares.txt").write_text("999.0.0\n", encoding="utf-8")
    env = dict(CLEAN, PATH=os.pathsep.join([str(bindir), syspath]))
    r = subprocess.run(["bash", str(own / "hooks" / "session-start.sh")], input='{"source": "startup"}', capture_output=True, text=True,
                       encoding="utf-8", env=env, cwd=str(T))
    ck("...with CLAUDE_PLUGIN_ROOT unset it reads the plugin the hook sits in", r.returncode == 0 and (ctx(r) or "").startswith("This Tares plugin needs"),
       short((r.returncode, r.stdout, r.stderr[-200:])))

    bound = {"TARES_PROJECT": "Acme web", "TARES_FACTORY_URL": "http://tares.invalid"}
    r_low = hook(bindir, low, extra=bound)
    r_far = hook(bindir, far, extra=bound)
    base, withline = ctx(r_low), ctx(r_far)
    ck("a bound session: its text is unchanged when there is nothing to say", base is not None and "Follow the `session` skill" in base and "needs tares" not in base,
       short(base))
    ck("...and the compat line comes after that same text when there is something", withline is not None and base is not None
       and withline.startswith(base) and withline.endswith(V.check_plugin_compat(far)), short(withline))
    crew = {"FACTORY_ROLE": "builder", "FACTORY_STATION": "s1", "FACTORY_HOME": str(T / "no-factory-home")}
    base, withline = ctx(hook(bindir, low, extra=crew)), ctx(hook(bindir, far, extra=crew))
    ck("a crew station: its text is unchanged, with the line after it when there is one", base is not None and "builder station" in base and "needs tares" not in base
       and withline == base + " " + V.check_plugin_compat(far), short((base, withline)))
    r = hook(bindir, far, stdin="", extra=bound)
    ck("an empty stdin does not stop the line", r.returncode == 0 and "This Tares plugin needs" in (ctx(r) or ""), short((r.returncode, r.stdout, r.stderr[-200:])))

shutil.rmtree(T, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

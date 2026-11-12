"""scripts/check_ledger.py: the crew ledger is a finished record. Every row is dropped with a reason, or ported to Python that exists."""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check_ledger.py"
LEDGER = ROOT / "scripts" / "crew_ledger.json"
SECTIONS = ["functions", "helpers", "dispatcher_cases", "env_vars", "state_files", "test_suites",
            "cmux_touchpoints", "hook_entries"]

P = F = 0


def ck(l, c, d=""):
    global P, F
    P += 1 if c else 0
    F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def run(*args):
    r = subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode, r.stdout + r.stderr


def row(name, **kw):
    r = {"name": name, "kind": "function", "ticket": "TR-948", "dropped": False,
         "python": "tares.crew.state:slugify", "ported": True, "note": ""}
    r.update(kw)
    return r


def clean():
    """A small ledger that passes: one ported row of each way to point at Python, and a dropped one."""
    data = {"version": 1, "source": "claude-plugin/bin/factory (removed in TR-975)"}
    for s in SECTIONS:
        data[s] = []
    data["functions"] = [
        row("slugify"),
        row("method", python="tares.crew.doctor:Doctor.ok"),
        row("constant", python="tares.crew.usage:USAGE"),
        row("die", ticket=None, dropped="Python raises CrewError instead.", python="", ported=False),
    ]
    data["state_files"] = [row("a file", kind="file", python="scripts/check_ledger.py")]
    return data


tmp = Path(tempfile.mkdtemp(prefix="check_ledger_"))
led = tmp / "ledger.json"


def save(data):
    led.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check(data, *extra):
    save(data)
    return run("--ledger", str(led), *extra)


def load():
    return json.loads(LEDGER.read_text(encoding="utf-8"))


# the real ledger
code, out = run()
ck("the real ledger passes: every row ported or dropped", code == 0, out)
real = load()
ck("the real ledger names the removed bash as its source", real["source"] == "claude-plugin/bin/factory (removed in TR-975)", real["source"])
ck("the real ledger has no row that is neither ported nor dropped", all(
    r["ported"] is True or (isinstance(r["dropped"], str) and r["dropped"].strip())
    for s in SECTIONS for r in real[s]))
ck("the ledger lists no hook entries (every hooks.json command is a `tares hook` one)", real["hook_entries"] == [])
ck("the ledger has no em dash", "—" not in LEDGER.read_text(encoding="utf-8"))
code, out = run("--help")
ck("--help exits 0", code == 0 and "--ledger" in out, out)
for gone in ("--bash", "--list-only", "--refresh-lines"):
    code, out = run(gone)
    ck("%s is gone: exit 2" % gone, code == 2, "exit %d: %s" % (code, out))

# a clean fixture
code, out = check(clean())
ck("a clean ledger exits 0", code == 0, out)
ck("the clean ledger is counted", "ok, 5 rows" in out, out)

# ported=false and not dropped
data = clean()
data["functions"][0]["ported"] = False
data["functions"][0]["python"] = ""
code, out = check(data)
ck("a row with ported=false and not dropped exits 1", code == 1, "exit %d: %s" % (code, out))
ck("the unfinished row is named", "slugify" in out and "neither ported nor dropped" in out, out)

# a ported row that points at nothing
for label, python, want in (
        ("a symbol that is missing", "tares.crew.state:zz_no_such_symbol", "zz_no_such_symbol"),
        ("a module that is missing", "tares.crew.zz_no_module:main", "zz_no_module"),
        ("a method that is missing", "tares.crew.doctor:Doctor.zz_no_method", "zz_no_method"),
        ("a method of something that is not a class", "tares.crew.state:slugify.inner", "not a class"),
        ("a path that is missing", "tests/zz_no_such_file.py", "zz_no_such_file"),
        ("a path with ..", "scripts/../README.md", "relative to the repo"),
        ("an absolute path", str(ROOT / "README.md"), "relative to the repo"),
        ("a folder, not a file", "tares/crew", "does not exist as a file"),
        ("two colons", "tares.crew.state:slugify:x", "module.path:symbol"),
        ("no python at all", "", "python is empty")):
    data = clean()
    data["functions"][0]["python"] = python
    code, out = check(data)
    ck("a ported row with %s exits 1 and says why" % label, code == 1 and want in out, "exit %d: %s" % (code, out))
data = clean()
data["functions"][0]["python"] = "tares.crew.state"  # no symbol is not a module:symbol and not a file
code, out = check(data)
ck("a python value that is neither module:symbol nor a file exits 1", code == 1, "exit %d: %s" % (code, out))

# dropped
data = clean()
data["functions"][3]["dropped"] = ""
code, out = check(data)
ck("a dropped row with an empty reason exits 1", code == 1 and "without a reason" in out, "exit %d: %s" % (code, out))
data["functions"][3]["dropped"] = "   "
code, out = check(data)
ck("a dropped row with a blank reason exits 1", code == 1 and "without a reason" in out, "exit %d: %s" % (code, out))
data = clean()
data["functions"][3]["dropped"] = True
code, out = check(data)
ck("dropped=true (not a reason) exits 1", code == 1, "exit %d: %s" % (code, out))
data = clean()
data["functions"][3]["ported"] = True
code, out = check(data)
ck("a row both dropped and ported exits 1", code == 1 and "both dropped and ported" in out, "exit %d: %s" % (code, out))
data = clean()
data["functions"][0]["ticket"] = None
code, out = check(data)
ck("a row that is not dropped and has no ticket exits 1", code == 1 and "no owning ticket" in out, "exit %d: %s" % (code, out))
data["functions"][0]["ticket"] = "955"
code, out = check(data)
ck("a ticket that is not TR-<number> exits 1", code == 1 and "TR-123" in out, "exit %d: %s" % (code, out))

# a missing field, each in turn
for field in ("name", "kind", "ticket", "dropped", "python", "ported", "note"):
    data = clean()
    del data["functions"][0][field]
    code, out = check(data)
    ck("a row without %s exits 1" % field, code == 1, "exit %d: %s" % (code, out))
    if field != "name":
        ck("the missing field %s is named" % field, "lacks" in out and field in out, out)
data = clean()
data["functions"][0]["ported"] = "yes"
code, out = check(data)
ck("ported that is not a boolean exits 1", code == 1 and "ported must be" in out, "exit %d: %s" % (code, out))
data = clean()
data["functions"][0]["note"] = None
code, out = check(data)
ck("a note that is not a string exits 1", code == 1, "exit %d: %s" % (code, out))
data = clean()
data["functions"] = {"slugify": 1}
code, out = check(data)
ck("a section that is not a list exits 1", code == 1 and "must be a list" in out, "exit %d: %s" % (code, out))
data = clean()
data["functions"].append("slugify")
code, out = check(data)
ck("a row that is not an object exits 1", code == 1 and "not an object" in out, "exit %d: %s" % (code, out))

# a row listed twice, in every section
for section in ("functions", "env_vars", "state_files", "dispatcher_cases"):
    data = clean()
    data[section] = [row("zz dup", kind="x"), row("zz dup", kind="x")]
    code, out = check(data)
    ck("a duplicate %s row exits 1 and is named" % section, code == 1 and "twice" in out and "zz dup" in out, "exit %d: %s" % (code, out))
data = clean()
data["helpers"] = [row("same", file="a"), row("same", file="b")]
code, out = check(data)
ck("the same helper name in two files is not a duplicate", code == 0, out)

# the file itself
code, out = run("--ledger", str(tmp / "no_such_ledger.json"))
ck("an unreadable ledger exits 2", code == 2 and "cannot read" in out, "exit %d: %s" % (code, out))
code, out = run("--ledger", str(tmp), "--json")
ck("a ledger that is a folder exits 2", code == 2, "exit %d: %s" % (code, out))
code, out = run("--ledger", str(tmp / "no_such_ledger.json"), "--export-md")
ck("--export-md of an unreadable ledger exits 2", code == 2, "exit %d: %s" % (code, out))
led.write_text("{not json", encoding="utf-8")
code, out = run("--ledger", str(led))
ck("a ledger that is not JSON exits 1", code == 1 and "not valid JSON" in out, "exit %d: %s" % (code, out))
data = clean()
data["version"] = 2
code, out = check(data)
ck("a ledger of another version exits 1", code == 1 and "version 1" in out, "exit %d: %s" % (code, out))
code, out = run("--no-such-option")
ck("a bad command line exits 2", code == 2, "exit %d: %s" % (code, out))

# --json
code, out = check(clean(), "--json")
parsed = json.loads(out)
ck("--json on a clean ledger: ok true, no problems, exit 0", code == 0 and parsed["ok"] is True and parsed["problems"] == [], out)
data = clean()
data["functions"][0]["python"] = "tares.crew.state:zz_no_such_symbol"
code, out = check(data, "--json")
parsed = json.loads(out)
p0 = parsed["problems"][0] if parsed["problems"] else {}
ck("--json on a broken ledger: ok false, one problem, exit 1", code == 1 and parsed["ok"] is False and len(parsed["problems"]) == 1, out)
ck("--json names the section, the row and the problem", p0.get("section") == "functions" and p0.get("name") == "slugify"
   and p0.get("problem") == "python-unresolved" and "zz_no_such_symbol" in p0.get("message", ""), out)
code, out = check(clean(), "--json")
ck("--json does not print the text summary", out.lstrip().startswith("{"), out)

# --export-md
data = clean()
data["functions"][3]["dropped"] = "Python raises CrewError | instead."
save(data)
md1, md2 = tmp / "one.md", tmp / "two.md"
c1, o1 = run("--ledger", str(led), "--export-md", str(md1))
c2, o2 = run("--ledger", str(led), "--export-md", str(md2))
ck("--export-md PATH exits 0 and prints nothing", c1 == 0 and c2 == 0 and o1 == "" and o2 == "", o1 + o2)
ck("--export-md is the same twice", md1.read_bytes() == md2.read_bytes())
md = md1.read_text(encoding="utf-8")
ck("--export-md has the counts", "5 rows, 4 ported, 1 dropped." in md, md[:300])
ck("--export-md has every section title", all(("## %s (" % t) in md for t in (
    "Functions of claude-plugin/bin/factory (removed in TR-975)", "Helper scripts and glue", "Dispatcher cases", "Environment variables",
    "State files", "Test files (tests/factory)", "cmux touchpoints", "Hook entries (hooks.json)")), md)
ck("--export-md has every function name", all(n in md for n in ("slugify", "method", "constant", "die")), md)
ck("--export-md shows the python and the dropped reason, with a | escaped", "tares.crew.doctor:Doctor.ok" in md and "dropped: Python raises CrewError \\| instead." in md, md)
ck("--export-md writes LF line endings", b"\r" not in md1.read_bytes())
code, out = run("--ledger", str(led), "--export-md")
ck("--export-md without a path writes to stdout", code == 0 and out == md, out[:200])
code, out = run("--export-md")
ck("--export-md of the real ledger lists all its rows", code == 0 and "## Functions of" in out and
   all(r["name"] in out for r in real["functions"]), out[:200])
ck("--export-md of the real ledger has no em dash", "—" not in out)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)

"""scripts/check_ledger.py: the crew ledger and the bash crew list the same things, every row has an owner or a reason, and a ported row points at code that exists."""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "scripts" / "check_ledger.py"
LEDGER = ROOT / "scripts" / "crew_ledger.json"
FACTORY = ROOT / "claude-plugin" / "bin" / "factory"

P = F = 0


def ck(l, c, d=""):
    global P, F
    P += 1 if c else 0
    F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def run(*args):
    r = subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True, cwd=str(ROOT))
    return r.returncode, r.stdout + r.stderr


def load():
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="check_ledger_"))

# the real tree
code, out = run("--list-only")
ck("--list-only exits 0 on this tree", code == 0, out)
code, out = run()
ck("the full check exits 0 on this tree", code == 0, out)

# (a) a function row deleted from the ledger
data = load()
gone = data["functions"][-1]["name"]
data["functions"] = [r for r in data["functions"] if r["name"] != gone]
led = tmp / "a.json"
save(led, data)
code, out = run("--list-only", "--ledger", str(led))
ck("a deleted function row exits 1", code == 1, f"exit {code}: {out}")
ck("the deleted function row is named", gone in out and "missing" in out, out)

# a middle one too, so a check that only looks at one end cannot pass
data = load()
mid = data["functions"][len(data["functions"]) // 2]["name"]
data["functions"] = [r for r in data["functions"] if r["name"] != mid]
save(led, data)
code, out = run("--list-only", "--ledger", str(led))
ck("a deleted middle function row exits 1 and is named", code == 1 and mid in out, f"exit {code}: {out}")

# (b) a new bash function the ledger does not know
fake = tmp / "factory_extra"
fake.write_text(FACTORY.read_text(encoding="utf-8") + "\nzz_fake_function() {\n  echo fake\n}\n", encoding="utf-8")
code, out = run("--list-only", "--bash", str(fake))
ck("a new bash function exits 1", code == 1, f"exit {code}: {out}")
ck("the new bash function is named", "zz_fake_function" in out, out)

# a function at the very top of the file as well
text = FACTORY.read_text(encoding="utf-8")
first = text.index("\n", text.index("\n") + 1)
fake2 = tmp / "factory_top"
fake2.write_text(text[:first] + "\naa_first_fake() {\n  echo fake\n}\n" + text[first:], encoding="utf-8")
code, out = run("--list-only", "--bash", str(fake2))
ck("a new bash function near the top exits 1 and is named", code == 1 and "aa_first_fake" in out, f"exit {code}: {out}")

# (c) a stale ledger row
data = load()
row = dict(data["functions"][0])
row["name"] = "zz_not_in_bash"
data["functions"].append(row)
save(led, data)
code, out = run("--list-only", "--ledger", str(led))
ck("a stale ledger row exits 1", code == 1, f"exit {code}: {out}")
ck("the stale row is named", "zz_not_in_bash" in out and "stale" in out, out)

# a stale row in another section
data = load()
row = dict(data["env_vars"][0])
row["name"] = "ZZ_NOT_AN_ENV"
data["env_vars"].append(row)
save(led, data)
code, out = run("--list-only", "--ledger", str(led))
ck("a stale env row exits 1 and is named", code == 1 and "ZZ_NOT_AN_ENV" in out, f"exit {code}: {out}")

# (d) ported rows
data = load()
data["functions"][0]["ported"] = True
data["functions"][0]["python"] = "tares.cli:main"
save(led, data)
code, out = run("--ledger", str(led))
ck("a ported row whose symbol exists passes", code == 0, f"exit {code}: {out}")
data["functions"][0]["python"] = "tares.cli:zz_no_such_symbol"
save(led, data)
code, out = run("--ledger", str(led))
ck("a ported row whose symbol is missing fails", code == 1 and "zz_no_such_symbol" in out, f"exit {code}: {out}")
data["functions"][0]["python"] = "tares.zz_no_module:main"
save(led, data)
code, out = run("--ledger", str(led))
ck("a ported row whose module is missing fails", code == 1, f"exit {code}: {out}")
data["functions"][0]["python"] = ""
save(led, data)
code, out = run("--ledger", str(led))
ck("a ported row with no python field fails", code == 1, f"exit {code}: {out}")

# (e) dropped
data = load()
data["functions"][0]["dropped"] = ""
data["functions"][0]["ticket"] = None
save(led, data)
code, out = run("--ledger", str(led))
ck("a dropped row with no reason fails", code == 1 and "dropped" in out, f"exit {code}: {out}")
data["functions"][0]["dropped"] = "Windows has no such thing, nothing to port"
save(led, data)
code, out = run("--ledger", str(led))
ck("a dropped row with a reason and no ticket passes", code == 0, f"exit {code}: {out}")
data["functions"][0]["dropped"] = False
save(led, data)
code, out = run("--ledger", str(led))
ck("a row with no owner and not dropped fails", code == 1, f"exit {code}: {out}")

# (e2) things the bash can gain that the ledger must hear about: an env var, a state file, a cmux line
base = FACTORY.read_text(encoding="utf-8")


def bash_copy(name, extra):
    p = tmp / name
    p.write_text(base + extra, encoding="utf-8")
    return p


code, out = run("--list-only", "--bash", str(bash_copy("env_new", "\n# reads $FACTORY_ZZ_NEW_KNOB\n")))
ck("a new env var in the bash exits 1 and is named", code == 1 and "FACTORY_ZZ_NEW_KNOB" in out and "environment variable" in out, f"exit {code}: {out}")
code, out = run("--list-only", "--bash", str(bash_copy("state_new", '\n# writes "$CREW_HOME/zz-new-state"\n')))
ck("a new state file in the bash exits 1 and is named", code == 1 and "zz-new-state" in out and "state file" in out, f"exit {code}: {out}")
code, out = run("--list-only", "--bash", str(bash_copy("home_new", '\n# reads "$HOME/.zz-new-dotfile"\n')))
ck("a new $HOME dotfile in the bash exits 1 and is named", code == 1 and ".zz-new-dotfile" in out, f"exit {code}: {out}")
code, out = run("--list-only", "--bash", str(bash_copy("repo_new", '\n# writes "$repo/.factory/zz-new-dir"\n')))
ck("a new per-repo .factory path in the bash exits 1 and is named", code == 1 and ".factory/zz-new-dir" in out, f"exit {code}: {out}")
code, out = run("--list-only", "--bash", str(bash_copy("cmux_new", "\n# zz_new_cmux_line talks to cmux\n")))
ck("a new cmux line in the bash exits 1 and is named", code == 1 and "zz_new_cmux_line" in out and "cmux" in out, f"exit {code}: {out}")

# and the ledger rows that cover them, taken away one by one
for section, label in (("env_vars", "env var"), ("state_files", "state file"), ("cmux_touchpoints", "cmux touchpoint"),
                       ("hook_entries", "hook entry")):
    data = load()
    # an external state file is only seen through its row, so take a crew_home one; the rest: the last row
    pick = [r for r in data[section] if r.get("scope") != "external"] or data[section]
    gone = pick[-1]["name"]
    data[section] = [r for r in data[section] if r is not pick[-1]]
    save(led, data)
    code, out = run("--list-only", "--ledger", str(led))
    ck("a deleted %s row exits 1 and is named" % label, code == 1 and ("cmux mention" if section == "cmux_touchpoints" else gone) in out and "missing" in out, f"exit {code}: {out}")
for name in ("<repo>/.factory/worktrees", "<repo>/.git/info/exclude"):
    data = load()
    ck("the ledger has a row for %s" % name, any(r["name"] == name for r in data["state_files"]))
    data["state_files"] = [r for r in data["state_files"] if r["name"] != name]
    save(led, data)
    code, out = run("--list-only", "--ledger", str(led))
    ck("a deleted row for %s exits 1 and is named" % name, code == 1 and name in out, f"exit {code}: {out}")

# a row listed twice
for section in ("functions", "env_vars", "state_files", "hook_entries", "dispatcher_cases"):
    data = load()
    dup = data[section][0]["name"]
    data[section].append(dict(data[section][0]))
    save(led, data)
    code, out = run("--list-only", "--ledger", str(led))
    ck("a duplicate %s row exits 1 and is named" % section, code == 1 and "twice" in out and dup in out, f"exit {code}: {out}")

# --list-only leaves the ported and owner rules out; the full check applies them
data = load()
data["functions"][0]["ticket"] = None
data["functions"][1]["ported"] = True
data["functions"][1]["python"] = ""
save(led, data)
code, out = run("--list-only", "--ledger", str(led))
ck("--list-only skips the owner and ported rules", code == 0, f"exit {code}: {out}")
code, out = run("--ledger", str(led))
ck("the full check applies the owner and ported rules to the same ledger", code == 1 and "no owning ticket" in out and "ported but its python field is empty" in out, f"exit {code}: {out}")

# (f) the Markdown copy
md1, md2 = tmp / "one.md", tmp / "two.md"
c1, o1 = run("--export-md", str(md1))
c2, o2 = run("--export-md", str(md2))
ck("--export-md exits 0", c1 == 0 and c2 == 0, o1 + o2)
ck("--export-md is the same twice", md1.read_bytes() == md2.read_bytes())
md = md1.read_text(encoding="utf-8")
names = [r["name"] for r in load()["functions"]]
lost = [n for n in names if n not in md]
ck("--export-md has every function name (%d)" % len(names), not lost, ", ".join(lost[:5]))
ck("--export-md has no em dash", "\u2014" not in md)

# the ledger itself
raw = LEDGER.read_text(encoding="utf-8")
ck("the ledger has no em dash", "\u2014" not in raw)
ck("every row has an owner or a reason", all(
    (isinstance(r.get("ticket"), str) and re.match(r"^TR-\d+$", r["ticket"])) or (isinstance(r.get("dropped"), str) and r["dropped"].strip())
    for k, v in load().items() if isinstance(v, list) for r in v))

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)

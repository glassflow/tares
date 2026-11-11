"""scripts/crew_smoke.py: the isolation rules, the scratch environment, the trust-file edit, and one dry run
(fake Claude Code, scratch HOME, real scratch Tares) that must leave nothing behind and never touch the live
crew home or the real ~/.claude.json. The real-Claude run is by hand (TR-977)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "crew_smoke.py"
spec = importlib.util.spec_from_file_location("crew_smoke", SCRIPT)
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)

fails = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def refused(home, name, live):
    try:
        smoke.check_isolation(home, name, live)
    except smoke.Refused as e:
        return str(e)
    return None


tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="smoke-test-")))
try:
    live = [str(tmp / "me" / "factory")]
    scratch = tmp / "scratch" / "crew"
    check("a scratch home and smoke- name pass", refused(scratch, "smoke-1", live) is None)
    check("the live crew home is refused", refused(live[0], "smoke-1", live) is not None)
    check("a folder inside the live home is refused", refused(Path(live[0]) / "sub", "smoke-1", live) is not None)
    check("a folder holding the live home is refused", refused(tmp / "me", "smoke-1", live) is not None)
    os.makedirs(live[0])
    os.symlink(live[0], tmp / "link")
    check("a link to the live home is refused", refused(tmp / "link", "smoke-1", live) is not None)
    check('the crew name "crew" is refused', refused(scratch, "crew", live) is not None)
    check("a name without smoke- is refused", refused(scratch, "other", live) is not None)
    check("the real ~/factory is among the live homes", os.path.realpath(os.path.expanduser("~/factory")) in smoke.live_crew_homes())

    # the refusal reaches the exit code, before anything starts
    r = subprocess.run([sys.executable, str(SCRIPT), "--crew-home", os.path.expanduser("~/factory")], capture_output=True, text=True, timeout=60)
    check("main refuses ~/factory with exit 2", r.returncode == 2 and "refusing to start" in r.stderr, f"{r.returncode} {r.stderr}")
    r = subprocess.run([sys.executable, str(SCRIPT), "--crew-name", "crew"], capture_output=True, text=True, timeout=60)
    check('main refuses the crew name "crew" with exit 2', r.returncode == 2 and "refusing to start" in r.stderr, f"{r.returncode} {r.stderr}")

    # the scratch environment drops what a station carries and spells out the scratch values
    base = {"PATH": "/bin", "FACTORY_HOME": "/Users/x/factory", "FACTORY_CREW_NAME": "crew", "TARES_AUTH_TOKEN": "secret",
            "TARES_FACTORY_MCP_URL": "http://live/mcp", "TARESD_URL": "http://live", "TARES_KEYRING": "on",
            "TARES_FACTORY_TOKEN_FILE": "/Users/x/factory/.tares-token", "HOME": "/Users/x"}
    env = smoke.scratch_env(base, crew_home=scratch, crew_name="smoke-9", tares_home=tmp / "th", port=4001, mcp_port=4002)
    check("the live crew home is not inherited", env["FACTORY_HOME"] == str(scratch) == env["FACTORY_CREW_HOME"])
    check("the crew name is the smoke one", env["FACTORY_CREW_NAME"] == "smoke-9")
    check("no token, keyring switch or token file is inherited",
          not any(k in env for k in ("TARES_AUTH_TOKEN", "TARES_KEYRING", "TARES_FACTORY_TOKEN_FILE")))
    check("Tares and MCP point at the scratch ports", env["TARESD_URL"] == env["TARES_FACTORY_URL"] == "http://127.0.0.1:4001"
          and env["TARES_FACTORY_MCP_URL"] == "http://127.0.0.1:4002/mcp")
    check("the keyring is pinned to the fail backend", env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring")
    check("the watch is off", env["FACTORY_NO_WATCH"] == "1")

    # the trust entry: added, then removed, the rest of the file kept
    cj = tmp / "claude.json"
    cj.write_text(json.dumps({"theme": "dark", "projects": {"/a": {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    smoke.trust_folder(cj, "/scratch/crew")
    doc = json.loads(cj.read_text(encoding="utf-8"))
    check("trust_folder adds the entry", doc["projects"]["/scratch/crew"]["hasTrustDialogAccepted"] is True)
    smoke.untrust_folder(cj, "/scratch/crew")
    check("untrust_folder restores the file", json.loads(cj.read_text(encoding="utf-8")) ==
          {"theme": "dark", "projects": {"/a": {"hasTrustDialogAccepted": True}}})
    # the folders a real run trusts: crew home, the clone's parent (the builder runs there) and the worktrees
    import types
    sm = smoke.Smoke(types.SimpleNamespace(dry_run=False, repo="o/r", crew_name="smoke-9", workdir=str(tmp / "w"),
                                           crew_home=None))
    sm.hdir = tmp / "h"
    sm.hdir.mkdir()
    sm.trust_folders()
    got = set(json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8"))["projects"])
    check("trust covers the crew home, the repos folder and the worktrees",
          got == {str(sm.crew_home), str(sm.work / "repos"), str(sm.crew_home / "worktrees")}, str(got))
    for entry in sm.trusted:
        smoke.untrust_folder(*entry)
    check("untrusting every entry empties the file", json.loads((sm.hdir / ".claude.json").read_text(encoding="utf-8"))["projects"] == {})
    smoke.trust_folder(tmp / "missing.json", "/x")
    check("trust_folder makes a missing file", (tmp / "missing.json").is_file())

    # one dry run, end to end
    if not (shutil.which("jq") and shutil.which("git")):
        print("skip the dry run: it needs jq and git")
    else:
        real_cj = Path(os.path.expanduser("~/.claude.json"))
        work = tmp / "dry"
        r = subprocess.run([sys.executable, str(SCRIPT), "--dry-run", "--workdir", str(work), "--timeout", "120"],
                           capture_output=True, text=True, timeout=300)
        out = r.stdout + r.stderr
        check("the dry run exits 10, never 0 (no PR was checked)", r.returncode == 10, f"{r.returncode}\n{out[-1500:]}")
        check("the dry run started both crew stations under the smoke name",
              "started smoke-" in out and "-orchestrator" in out and "-reviewer" in out, out[-800:])
        check("the dry run never names the live crew's stations", "crew-orchestrator" not in out and "crew-reviewer" not in out)
        check("crew down ran", "all stations stopped" in out, out[-800:])
        # a live Claude Code rewrites the real file all the time, so look for our folder, not for a byte-equal file
        leaked = real_cj.is_file() and str(work) in real_cj.read_text(encoding="utf-8", errors="replace")
        check("the dry run leaves no entry in the real ~/.claude.json", not leaked)
        left = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
        check("no scratch daemon is left running", str(work) not in left)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"\n{len(fails)} failed: {', '.join(fails)}")
    sys.exit(1)
print("\nall passed")

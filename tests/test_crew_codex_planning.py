"""Planning on Codex (tares/crew/person.py `person_codex`, tares/crew/second_look.py, TR-1599).

`tares factory spec` and `session` start the person's own Claude Code on a Claude Code crew. On a crew that runs on
Codex they start the person's own `codex` instead, with the spec or session manual (role_text) as its developer
instructions and the crew's Tares MCP endpoint, all on the command line. A fake `codex` records what it was given.
There is no `claude` anywhere.

 a. spec on a Codex crew, with no terminal: one `codex exec` in the repo, its sandbox the workspace, the spec
    manual as developer instructions, Tares as a required MCP server reached with the crew's key through the
    plugin's headers helper and the crew's name as a header, the first prompt in words (no slash command) and
    the note that nobody answers; no claude is looked for;
 b. the first prompts: Claude Code's slash commands become words; a contributor is sent to its page;
 c. on a Claude Code crew spec still looks for claude, as before;
 d. second-look: one `codex exec` that is fresh (--ephemeral), read-only, with none of the person's Codex setup,
    in the folder given, the file as its prompt after the factory's head, the crew's second-look model when it
    names one; the answer printed with what it was; never inside another second look; an empty file refused.

Unix only: on Windows the test says so and passes.
Run: python tests/test_crew_codex_planning.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

if sys.platform == "win32":
    print("skipped on Windows: the fake codex is a shell script\n\n0 passed, 0 failed")
    sys.exit(0)

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402

SCRATCH = Path(tempfile.mkdtemp(prefix="tcpl-", dir="/tmp"))
for _k in [k for k in os.environ if k.startswith(("TARES", "FACTORY", "FAKE_CLAUDE", "FAKE_CODEX", "CLAUDE", "CODEX"))]:
    del os.environ[_k]
os.environ.pop("TARESD_URL", None)
BIN = SCRATCH / "bin"
BIN.mkdir()
HOME = SCRATCH / "home"
HOME.mkdir()
RECORD = SCRATCH / "codex-calls.jsonl"
(BIN / "codex").write_text(f"""#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
out = argv[argv.index("--output-last-message") + 1] if "--output-last-message" in argv else ""
stdin = sys.stdin.read() if "-" in argv[-1:] else ""
if out:
    open(out, "w").write("FINDING: the test of subtract misses negative numbers")
with open({str(RECORD)!r}, "a") as f:
    f.write(json.dumps({{"argv": argv, "cwd": os.getcwd(), "stdin": stdin,
                         "env": {{k: v for k, v in os.environ.items() if k.startswith(("TARES", "FACTORY"))}}}}) + "\\n")
""")
(BIN / "codex").chmod(0o755)
(BIN / "git").symlink_to(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  PATH=str(BIN))   # the fake codex and git: no claude
_crew_guard.install()

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))
from tares.crew import person, role_text, second_look  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402
import launch_support as LS  # noqa: E402

P = F = 0
TOKEN = "tok-fake-codex-planning-0123456789"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def calls():
    return [json.loads(x) for x in RECORD.read_text().splitlines()] if RECORD.exists() else []


def config(argv):
    """The -c key=value overrides of a codex command line."""
    out = {}
    for i, a in enumerate(argv):
        if a == "-c" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            out[k] = v
    return out


def crew(runtime):
    tmp = SCRATCH / runtime
    tmp.mkdir()
    LS.make_crew(tmp, {"token": TOKEN, "stations": {"crew-orchestrator": {"role": "orchestrator", "runtime": runtime,
                                                                          "status": "active"}}})
    tares = LS.FakeTares({"runtime": runtime})
    tares.__enter__()
    paths = CrewPaths.at(tmp / "crew")
    env = {**os.environ, "FACTORY_CREW_HOME": str(paths.home), "TARESD_URL": tares.url,
           "TARES_FACTORY_MCP_URL": tares.url + "/mcp", "TARES_PLANNING_EXEC": "1"}
    return tares, paths, env


def repo():
    r = SCRATCH / "repo"
    r.mkdir(exist_ok=True)
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv], cwd=r, check=True)
    return r


def run(fn, argv, env, cwd):
    out, err = io.StringIO(), io.StringIO()
    here = os.getcwd()
    os.chdir(cwd)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argv, env)
    finally:
        os.chdir(here)
    return code, out.getvalue(), err.getvalue()


print("a. spec on a Codex crew, with no terminal")
tares, paths, env = crew("codex")
r = repo()
code, out, err = run(person.cmd_spec, ["spec", "--project", "calc", "add subtract to calc.py"], env, r)
got = calls()
c = got[-1] if got else {}
argv = c.get("argv") or []
cfg = config(argv)
manual = role_text.manual(str(ROOT / "claude-plugin"), role_text.CODEX, "spec")
ck("it runs one codex exec in the repo, its sandbox the workspace, with no claude looked for",
   code == 0 and len(got) == 1 and argv[:1] == ["exec"] and "--sandbox" in argv
   and argv[argv.index("--sandbox") + 1] == "workspace-write" and os.path.realpath(c["cwd"]) == os.path.realpath(r)
   and "claude" not in err.lower().replace("claude code", ""), (code, err[-400:], argv[:6]))
ck("its developer instructions are the spec manual for Codex, whole",
   json.loads(cfg.get("developer_instructions", '""')) == manual and manual.startswith("You run on Codex"),
   cfg.get("developer_instructions", "")[:120])
ck("Tares is a required MCP server at the crew's endpoint, reached with the crew's key through the plugin's helper",
   json.loads(cfg["mcp_servers.tares.url"]) == tares.url + "/mcp" and cfg["mcp_servers.tares.required"] == "true"
   and "tares hook mcp-headers" in json.loads(cfg["mcp_servers.tares.http_headers_helper"])
   and str(paths.token_file) in json.loads(cfg["mcp_servers.tares.http_headers_helper"])
   and "X-Tares-Crew" in cfg["mcp_servers.tares.env_http_headers"] and TOKEN not in json.dumps(argv), cfg)
ck("its environment names the crew and the cell, and carries no key",
   c["env"].get("FACTORY_CREW") and c["env"].get("TARES_FACTORY_MCP_URL") == tares.url + "/mcp"
   and "TARES_AUTH_TOKEN" not in c["env"], c.get("env"))
ck("the first prompt is in words, with the project's name and the idea, and says nobody answers",
   argv[-1].startswith("Plan a project with me, as your spec manual says. (project name: calc) add subtract to calc.py")
   and "Nobody answers questions in this run" in argv[-1], argv[-1])
ck("it says it opens Codex", "opening Codex as calc-spec" in err, err[-300:])
tares.__exit__(None, None, None)

print("b. the first prompts")
ck("a session's slash command becomes its words", person.codex_prompt("/tares:session new: a todo app") == "new: a todo app"
   and person.codex_prompt("/tares:session calc (taken over from the crew)") == "calc (taken over from the crew)")
ck("a contributor is sent to its page", "contributor.md" in person.codex_prompt(
   "You are contributing to calc led by Pat. Load the spec-project skill, section Contributor."))

print("c. on a Claude Code crew spec still looks for claude")
tares, paths, env = crew("claude_code")
n = len(calls())
code, out, err = run(person.cmd_spec, ["spec", "an idea"], env, r)
ck("it stops for a missing claude, as before, and starts no codex", code != 0 and "claude" in err.lower() and len(calls()) == n,
   (code, err[-300:]))
tares.__exit__(None, None, None)

print("d. second-look")
look = SCRATCH / "look.md"
look.write_text("The diff and my findings: F1 the test misses zero.")
wt = SCRATCH / "wt"
wt.mkdir()
home = SCRATCH / "codex" / "crew"
env2 = {**os.environ, "FACTORY_ROLE": "reviewer"}
n = len(calls())
code, out, err = run(second_look.cmd_second_look, ["second-look", "--file", str(look), "--dir", str(wt)], env2, SCRATCH)
c = calls()[-1]
argv = c["argv"]
ck("one codex exec, fresh, read-only, with none of the person's Codex setup, in the folder given",
   code == 0 and len(calls()) == n + 1 and argv[:2] == ["exec", "--ephemeral"] and argv[argv.index("--sandbox") + 1] == "read-only"
   and "--ignore-user-config" in argv and argv[argv.index("-C") + 1] == str(wt) and argv[-1] == "-"
   and "--disable" in argv, argv)
ck("its prompt is the factory's head, then the file", c["stdin"].startswith(second_look.HEAD)
   and c["stdin"].endswith("F1 the test misses zero."), c["stdin"][:120])
ck("it runs marked as a second look, and prints the answer with what it was",
   c["env"].get("TARES_SECOND_LOOK") == "1" and "FINDING: the test of subtract misses negative numbers" in out
   and "a separate Codex session, read-only, on the same model as yours" in out and "-m" not in argv, out)
LS.make_crew(SCRATCH / "codex", {"crew": {"stations": {"reviewer": {"second_look_model": "gpt-5.6-other"}}}})
code, out, err = run(second_look.cmd_second_look, ["second-look", "--file", str(look)],
                     {**env2, "FACTORY_CREW_HOME": str(home)}, SCRATCH)
argv = calls()[-1]["argv"]
ck("the crew's second-look model, when it names one, and the answer says which",
   code == 0 and argv[argv.index("-m") + 1] == "gpt-5.6-other" and "on gpt-5.6-other" in out, (argv, out))
n = len(calls())
code, out, err = run(second_look.cmd_second_look, ["second-look", "--file", str(look)], {**env2, "TARES_SECOND_LOOK": "1"}, SCRATCH)
ck("never inside another second look", code == 1 and "does not start another" in err and len(calls()) == n, err)
(SCRATCH / "empty.md").write_text(" \n")
code, out, err = run(second_look.cmd_second_look, ["second-look", "--file", str(SCRATCH / "empty.md")], env2, SCRATCH)
ck("an empty file is refused", code == 1 and "is empty" in err and len(calls()) == n, err)

print(f"{P} passed, {F} failed")
sys.exit(1 if F else 0)

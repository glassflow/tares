"""A station's GitHub access with the cell's token (tares/crew/github_access.py): `tares factory git-credential`,
`tares factory gh`, and what a station's launch carries so that git and gh use them.

The commands run as `tares factory ...` against a fake Tares (tests/factory/launch_support.py) in a throwaway
crew home, with a fake `gh` first on PATH; the real one is never run. Launches are composed and written in this
process against the same fake Tares. No git or gh setup of the person's is read.

 a. git's credential helper: the protocol it prints, the one request it makes, and its silent exit 1;
 b. gh: where the repository is read from, which calls never get a token, a gh with a login of its own, a gh
    without one, what happens when there is no token, pass-through, and the shim files;
 c. the launch: what a start adds on a cell that gives tokens, when it adds nothing, the files it writes, the
    Codex station's config, the fingerprint, a station that starts a station, and real git with that environment;
 d. no token is left in any file, plan or output.

Checks that need a Unix shell are left out on Windows, and so is everything about a Codex station, which listens
on a Unix socket.

Run: python tests/test_crew_git_credential.py
"""
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

POSIX = sys.platform != "win32"
# a short folder: a Codex station's socket path holds about a hundred bytes
SCRATCH = Path(tempfile.mkdtemp(prefix="tgc-", dir="/tmp" if POSIX else None))
for _k in [k for k in os.environ if k.upper().startswith(
        ("TARES", "FACTORY", "CLAUDE", "CODEX", "FAKE_", "GH_", "GITHUB_", "GIT_", "SSH_ASKPASS"))]:
    del os.environ[_k]
HOME, BIN, FAKE = SCRATCH / "home", SCRATCH / "bin", SCRATCH / "fakegh"
for _d in (HOME / ".codex", BIN, FAKE):
    _d.mkdir(parents=True)
GITCONFIG = SCRATCH / "gitconfig"
GITCONFIG.write_text("")
SYSTEM_PATH = os.environ.get("PATH", "")
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", FACTORY_PAUSE="0", FACTORY_NO_WATCH="1",
                  GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(GITCONFIG), GH_CONFIG_DIR=str(SCRATCH / "gh-config"),
                  PATH=str(BIN) + os.pathsep + SYSTEM_PATH)
(HOME / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": "fake"}\n')


def script(folder: Path, name: str, python_file: Path) -> Path:
    """A command `name` in `folder` that runs a Python file with its arguments."""
    if POSIX:
        file = folder / name
        file.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{python_file}" "$@"\n')
        file.chmod(0o755)
    else:
        file = folder / f"{name}.cmd"
        file.write_text(f'@echo off\n"{sys.executable}" "{python_file}" %*\nexit /b %errorlevel%\n')
    return file


CLI = "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"
(SCRATCH / "tares_cli.py").write_text(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n{CLI.replace('; ', chr(10))}\n")
TARES_SCRIPT = script(BIN, "tares", SCRATCH / "tares_cli.py")
if POSIX:
    script(BIN, "codex", HERE / "factory" / "fake_codex.py")

# The fake gh writes down each call (its token as a digest only). `auth token` answers whether there is a login,
# and prints a token when there is one, as the real one does.
(SCRATCH / "fake_gh.py").write_text('''import hashlib, json, os, signal, sys
args = sys.argv[1:]
token = os.environ.get("GH_TOKEN")
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "token": hashlib.sha256(token.encode()).hexdigest() if token else None,
                        "tares": sorted(k for k in os.environ if k.startswith("TARES"))}) + "\\n")
if args == ["auth", "token", "--hostname", "github.com"]:
    how = os.environ.get("FAKE_GH_LOGIN", "no")
    if how == "kill":
        os.kill(os.getpid(), signal.SIGKILL)
    if how == "yes" or (how == "env" and (token or os.environ.get("GITHUB_TOKEN"))):
        print("gh" + "u_" + "fakelogin000000000000")
        print("gh" + "u_" + "fakelogin000000000000", file=sys.stderr)
        sys.exit(0)
    print("no oauth token found", file=sys.stderr)
    sys.exit(1)
print("fake gh ran")
if "--input" in args:
    print("stdin: " + sys.stdin.read())
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
''')
script(FAKE, "gh", SCRATCH / "fake_gh.py")
(SCRATCH / "slow.py").write_text("import time\ntime.sleep(5)\n")
(SCRATCH / "slowgh").mkdir()
SLOW_GH = script(SCRATCH / "slowgh", "gh", SCRATCH / "slow.py")
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import launch_support as LS  # noqa: E402

from tares.crew import codex as cx  # noqa: E402
from tares.crew import doctor_codex, state  # noqa: E402
from tares.crew import github_access as ga  # noqa: E402
from tares.crew import launch as L  # noqa: E402
from tares.crew.client import TaresClient, TaresHTTPError, cksum  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0
KEY = "tok-fake-git-credential-0123456789"
TOKEN = "ghs_" + "fakecelltoken00000000000000000000001"
OWN_TOKEN = "own-token-of-the-person"
NAME, EMAIL = "factory-bot[bot]", "1+factory-bot[bot]@users.noreply.github.com"
ANSWER = {"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z", "kind": "app", "login": NAME, "name": NAME,
          "email": EMAIL}
ROUTE = "/api/factory/github/token"
CAPS = ["integrations", "merge_queue", "guardrail_rules", "launch_spec"]
WITH_TOKENS = CAPS + [ga.CAPABILITY]
SEEN: list = []   # (what, bytes): every output and plan that must hold no token


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail!s}"[:900]))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def repo(name: str, origin: str = "") -> str:
    folder = SCRATCH / "repos" / name
    folder.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(folder)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(folder), "remote", "add", "origin", origin], check=True)
    return str(folder)


GITLAB = repo("a-gitlab", "https://gitlab.com/acme/tool.git")
WIDGETS = repo("b-widgets", "https://github.com/acme/widgets.git")
ORIGIN = repo("origin", "https://github.com/acme/origin.git")
PLAIN = SCRATCH / "plain"
PLAIN.mkdir()

# ---------------------------------------------------------------------------------------------
helper_tmp = SCRATCH / "h"
LS.make_crew(helper_tmp, {"token": KEY})
CREW = helper_tmp / "crew"
cell = LS.FakeTares({"token": KEY, "capabilities": WITH_TOKENS})
cell.__enter__()
GH_LOG = SCRATCH / "gh-calls.jsonl"
ENV = {**os.environ, "FACTORY_CREW_HOME": str(CREW), "TARESD_URL": cell.url, "TARES_AUTH_TOKEN": KEY,
       "TARES_URL_LOG": str(SCRATCH / "urls.log"), "FAKE_GH_LOG": str(GH_LOG), "PYTHONPATH": str(ROOT),
       "PATH": os.pathsep.join([str(FAKE), SYSTEM_PATH])}


def answers(status: int = 200, body=None) -> None:
    cell.world["routes"][ROUTE] = [status, ANSWER if body is None else body]


def asked() -> list:
    return [x["path"] for x in cell.log if x["path"].startswith(ROUTE)]


def factory(args, env=None, stdin=b"", cwd=None):
    return subprocess.run([sys.executable, "-c", CLI, "factory", *args], input=stdin, capture_output=True,
                          env=env or ENV, cwd=cwd or str(PLAIN), timeout=120)


def credential(question: str, word: str = "get", **env):
    cell.log.clear()
    r = factory(["git-credential", word], env={**ENV, **env}, stdin=question.encode())
    SEEN.append(("helper stderr", r.stderr))
    if not (r.returncode == 0 and r.stdout.startswith(b"username=")):
        SEEN.append(("helper stdout", r.stdout))
    return r


def question(protocol="https", host="github.com", path="acme/widgets.git") -> str:
    lines = [f"protocol={protocol}", f"host={host}"] + ([f"path={path}"] if path is not None else [])
    return "\n".join(lines) + "\n\n"


print("a. git's credential helper")
answers()
r = credential(question())
want = f"username=x-access-token\npassword={TOKEN}\n\n".encode()
ck("get prints the user name, the token and a blank line, and nothing else",
   r.returncode == 0 and r.stdout == want and r.stderr == b"", (r.returncode, len(r.stdout), r.stderr))
ck("it makes one request, for the repository asked about, with no /health read, under the crew's key",
   [(x["path"], x["auth"]) for x in cell.log] == [(f"{ROUTE}?repo=acme/widgets", f"Bearer {KEY}")], cell.log)
r = credential("capability[]=authtype\nprotocol=https\nhost=GitHub.COM\nwwwauth[]=Basic realm=x\n"
               "path=/acme/widgets.git/\n\n")
ck("the host's case, the outer slashes, .git and keys it does not know make no difference",
   r.returncode == 0 and r.stdout == want and asked() == [f"{ROUTE}?repo=acme/widgets"], (r.returncode, asked()))
r = credential(question(), FACTORY_STATION="p-build", TARES_AUTH_TOKEN="another-key",
               TARES_FACTORY_TOKEN_FILE=str(CREW / ".tares-token"))
ck("in a station the request carries the crew's key file's key",
   r.returncode == 0 and [x["auth"] for x in cell.log] == [f"Bearer {KEY}"], (r.returncode, cell.log))

for label, status, body in (("403", 403, {"code": "repo_not_allowed", "detail": "not this one"}),
                            ("404", 404, {"code": "no_credential", "detail": "none"}),
                            ("409", 409, {"code": "app_permissions_missing", "detail": "permissions"}),
                            ("502", 502, {"code": "github_error", "detail": "no answer"}),
                            ("a token with a line break", 200, {**ANSWER, "token": TOKEN + "\nhost=x"}),
                            ("an empty token", 200, {**ANSWER, "token": ""}),
                            ("an answer that is not an object", 200, ["x"])):
    answers(status, body)
    r = credential(question())
    ck(f"{label}: exit 1 and not a word", (r.returncode, r.stdout, r.stderr) == (1, b"", b""),
       (r.returncode, r.stdout, r.stderr))
answers()
for label, q in (("http", question(protocol="http")), ("another host", question(host="example.com")),
                 ("no path", question(path=None)), ("a path of one part", question(path="acme")),
                 ("a path of three parts", question(path="acme/widgets/more")), ("nothing asked", "")):
    r = credential(q)
    ck(f"{label}: exit 1, not a word and no request",
       (r.returncode, r.stdout, r.stderr) == (1, b"", b"") and cell.log == [], (r.returncode, r.stdout, r.stderr, cell.log))
r = credential(question(), TARESD_URL="http://127.0.0.1:9")
ck("Tares down: exit 1 and not a word", (r.returncode, r.stdout, r.stderr) == (1, b"", b""),
   (r.returncode, r.stdout, r.stderr))
r = credential(question(), TARES_PLUGIN_DIR=str(SCRATCH / "no-plugin"))
ck("what `tares factory` checks before a command is silent here too",
   (r.returncode, r.stdout, r.stderr) == (1, b"", b""), (r.returncode, r.stdout, r.stderr))
for word in ("store", "erase", "capability"):
    r = credential(question() + "password=another-password\n", word=word)
    ck(f"{word}: exit 0, nothing printed, no request",
       (r.returncode, r.stdout, r.stderr) == (0, b"", b"") and cell.log == [], (r.returncode, r.stdout, r.stderr, cell.log))

# ---------------------------------------------------------------------------------------------
print("b. gh")


def gh(*args, cwd=ORIGIN, stdin=b"", **env):
    GH_LOG.write_text("")
    cell.log.clear()
    r = factory(["gh", "--", *args], env={**ENV, **env}, stdin=stdin, cwd=cwd)
    SEEN.append(("gh output", r.stdout + r.stderr))
    calls = [json.loads(x) for x in GH_LOG.read_text(encoding="utf-8").splitlines()]
    return r, calls


def target(args, cwd=ORIGIN, **env) -> str:
    before = os.getcwd()
    os.chdir(cwd)
    try:
        return ga.target_repo(args, {**os.environ, **env})
    finally:
        os.chdir(before)


for label, args, env, want_repo in (
        ("-R", ["pr", "list", "-R", "acme/flag"], {}, "acme/flag"),
        ("--repo", ["pr", "list", "--repo", "acme/flag"], {}, "acme/flag"),
        ("-R with no space", ["pr", "list", "-Racme/flag"], {}, "acme/flag"),
        ("--repo=", ["pr", "list", "--repo=acme/flag"], {}, "acme/flag"),
        ("-R with the host", ["pr", "list", "-R", "github.com/acme/flag"], {}, "acme/flag"),
        ("-R with an address", ["pr", "list", "-R", "https://github.com/acme/flag.git"], {}, "acme/flag"),
        ("GH_REPO", ["pr", "list"], {"GH_REPO": "acme/env"}, "acme/env"),
        ("a gh api endpoint", ["api", "repos/acme/api/pulls?state=open"], {}, "acme/api"),
        ("a gh api endpoint with a first slash", ["api", "-X", "POST", "/repos/acme/api/issues"], {}, "acme/api"),
        ("gh api placeholders, which leave origin", ["api", "repos/{owner}/{repo}/pulls"], {}, "acme/origin"),
        ("an address among the arguments", ["pr", "view", "https://github.com/acme/web/pull/3"], {}, "acme/web"),
        ("--repo before GH_REPO", ["pr", "list", "-R", "acme/flag"], {"GH_REPO": "acme/env"}, "acme/flag"),
        ("GH_REPO before the endpoint", ["api", "repos/acme/api"], {"GH_REPO": "acme/env"}, "acme/env"),
        ("the endpoint before an address", ["api", "repos/acme/api", "https://github.com/acme/web"], {}, "acme/api"),
        ("an address in a text value, which names nothing",
         ["pr", "create", "--body", "https://gitlab.com/x/y"], {}, "acme/origin"),
        ("a link after a short body flag, which names nothing",
         ["pr", "comment", "5", "-b", "https://example.com/x/y"], {}, "acme/origin"),
        ("a text that starts with a link, which names nothing",
         ["release", "create", "v1", "-n", "https://example.com/x/y has the notes"], {}, "acme/origin"),
        ("what follows --, which is not read", ["pr", "list", "--", "-R", "acme/flag"], {}, "acme/origin")):
    got = target(args, **env)
    ck(f"repository from {label}", got == want_repo, got)
for form, url, name in (("https", "https://github.com/acme/web-https.git", "web-https"),
                        ("git@", "git@github.com:acme/web-scp.git", "web-scp"),
                        ("ssh://", "ssh://git@github.com/acme/web-ssh.git", "web-ssh")):
    got = target(["pr", "status"], cwd=repo(name, url))
    ck(f"repository from an origin remote written as {form}", got == f"acme/{name}", got)

answers()
r, calls = gh("pr", "view")
ck("no login: gh runs with the cell's token for origin's repository, after one request",
   r.returncode == 0 and r.stdout.strip() == b"fake gh ran" and r.stderr == b""
   and asked() == [f"{ROUTE}?repo=acme/origin"] and len(calls) == 2 and calls[1]["args"] == ["pr", "view"]
   and calls[1]["token"] == digest(TOKEN), (r.returncode, r.stdout, r.stderr, asked(), calls))
ck("the login is looked for first, with no token in that call",
   calls[0]["args"] == ["auth", "token", "--hostname", "github.com"] and calls[0]["token"] is None, calls)
ck("gh gets the environment the command was started with, without what `tares factory` sets for itself",
   calls[-1]["tares"] == sorted(k for k in ENV if k.startswith("TARES")), calls[-1]["tares"])
r, calls = gh("pr", "list", "-R", "acme/flag", cwd=str(PLAIN))
ck("the request names the repository the call is about", asked() == [f"{ROUTE}?repo=acme/flag"]
   and calls[-1]["token"] == digest(TOKEN), (asked(), calls))
r, calls = gh("auth", "status")
ck("gh auth status, which shows no token, gets one", asked() == [f"{ROUTE}?repo=acme/origin"]
   and calls[-1]["args"] == ["auth", "status"] and calls[-1]["token"] == digest(TOKEN), (asked(), calls))

for label, args, env, cwd in (
        ("gh auth token", ["auth", "token"], {}, ORIGIN),
        ("gh auth login", ["auth", "login"], {}, ORIGIN),
        ("gh auth logout", ["auth", "logout"], {}, ORIGIN),
        ("gh auth status -t", ["auth", "status", "-t"], {}, ORIGIN),
        ("gh auth status --show-token", ["auth", "status", "--show-token"], {}, ORIGIN),
        ("gh auth status -at", ["auth", "status", "-at"], {}, ORIGIN),
        ("--repo on another host", ["pr", "list", "-R", "gitlab.com/acme/tool"], {}, ORIGIN),
        ("GH_REPO on another host", ["pr", "list"], {"GH_REPO": "gitlab.com/acme/tool"}, ORIGIN),
        ("GH_HOST", ["pr", "list"], {"GH_HOST": "git.example.com"}, ORIGIN),
        ("--hostname", ["api", "--hostname", "git.example.com", "repos/acme/tool"], {}, ORIGIN),
        ("an address on a ghe.com host", ["pr", "view", "https://acme.ghe.com/acme/tool/pull/1"], {}, ORIGIN),
        ("a ghe.com name without https", ["repo", "view", "acme.ghe.com/acme/tool"], {}, ORIGIN),
        ("an origin on another host", ["pr", "list"], {}, GITLAB),
        ("no repository", ["pr", "list"], {}, str(PLAIN))):
    r, calls = gh(*args, cwd=cwd, **env)
    ck(f"{label}: gh runs as called, with no token and no request",
       r.returncode == 0 and asked() == [] and [(c["args"], c["token"]) for c in calls] == [(args, None)],
       (r.returncode, r.stderr, asked(), calls))

COMMANDS = ((["pr", "view"], "acme/origin"), (["run", "list"], "acme/origin"), (["issue", "list"], "acme/origin"),
            (["api", "repos/acme/api/statuses/0123abc"], "acme/api"))
for args, name in COMMANDS:
    r, calls = gh(*args, FAKE_GH_LOGIN="yes")
    ck(f"with a login, gh {args[0]} runs with no request and no GH_TOKEN",
       r.returncode == 0 and r.stdout.strip() == b"fake gh ran" and r.stderr == b"" and asked() == []
       and len(calls) == 2 and calls[1]["args"] == args and calls[1]["token"] is None,
       (r.returncode, r.stdout, r.stderr, asked(), calls))
    r, calls = gh(*args, FAKE_GH_LOGIN="env", GH_TOKEN=OWN_TOKEN)
    ck(f"with a GH_TOKEN already set, gh {args[0]} gets that one unchanged, with no request",
       r.returncode == 0 and asked() == [] and calls[-1]["token"] == digest(OWN_TOKEN), (asked(), calls))
    r, calls = gh(*args)
    ck(f"with no login, gh {args[0]} gets the cell's token",
       r.returncode == 0 and asked() == [f"{ROUTE}?repo={name}"] and calls[-1]["token"] == digest(TOKEN),
       (asked(), calls))
r, calls = gh("pr", "view", FAKE_GH_LOGIN="env", GITHUB_TOKEN=OWN_TOKEN)
ck("GITHUB_TOKEN counts as a login", asked() == [] and calls[-1]["token"] is None, (asked(), calls))

for label, status, body, words in (
        ("403", 403, {"code": "repo_not_allowed", "detail": "the credential does not cover acme/origin"},
         "the credential does not cover acme/origin"),
        ("404", 404, {"code": "no_credential", "detail": "no GitHub credential for acme/origin"},
         "no GitHub credential for acme/origin"),
        ("409", 409, {"code": "app_permissions_missing", "detail": "the app lacks a permission on acme/origin"},
         "the app lacks a permission"),
        ("502", 502, {"code": "github_error", "detail": "GitHub did not answer"}, "GitHub did not answer"),
        ("a token with a line break", 200, {**ANSWER, "token": TOKEN + "\n"}, "with no token"),
        ("an answer with no token", 200, {}, "with no token")):
    answers(status, body)
    r, calls = gh("pr", "view")
    said = r.stderr.decode()
    ck(f"{label}: gh runs without a token after one line that names the repository and the reason",
       r.returncode == 0 and r.stdout.strip() == b"fake gh ran" and len(said.splitlines()) == 1
       and "Tares gave no token for acme/origin" in said and words in said
       and calls[-1]["args"] == ["pr", "view"] and calls[-1]["token"] is None,
       (r.returncode, r.stdout, said, calls))
answers()
r, calls = gh("pr", "view", TARESD_URL="http://127.0.0.1:9")
said = r.stderr.decode()
ck("Tares down: gh runs without a token after one line", r.returncode == 0 and len(said.splitlines()) == 1
   and "Tares gave no token for acme/origin" in said and calls[-1]["token"] is None, (r.returncode, said, calls))

if POSIX:
    r, calls = gh("pr", "view", FAKE_GH_LOGIN="kill")
    ck("a login check that ends on a signal cannot tell: gh runs as called, with no request",
       r.returncode == 0 and asked() == [] and calls[-1]["args"] == ["pr", "view"] and calls[-1]["token"] is None,
       (r.returncode, r.stderr, asked(), calls))
limit, ga.LOGIN_CHECK = ga.LOGIN_CHECK, 0.3
try:
    ck("a login check that takes too long cannot tell", ga.has_login(str(SLOW_GH), dict(os.environ)) is None)
finally:
    ga.LOGIN_CHECK = limit
ck("a login check that cannot run cannot tell",
   ga.has_login(str(SCRATCH / "no-such-gh"), dict(os.environ)) is None)

r, calls = gh("pr", "view", FAKE_GH_EXIT="7")
ck("gh's exit code is the command's", r.returncode == 7, r.returncode)
r, calls = gh("api", "repos/acme/api/issues", "--input", "-", stdin=b"from the caller")
ck("stdin reaches gh, untouched by the login check", r.returncode == 0 and b"stdin: from the caller" in r.stdout
   and calls[-1]["token"] == digest(TOKEN), (r.returncode, r.stdout, r.stderr))

r, calls = gh("pr", "view", PATH=str(CREW / "bin"))
ck("no gh besides the crew's own: a line that says so and exit 127", r.returncode == 127
   and len(r.stderr.decode().splitlines()) == 1 and "no gh found" in r.stderr.decode() and cell.log == [],
   (r.returncode, r.stderr))

ga.write_station_files(type("C", (), {"paths": CrewPaths.at(CREW), "env": {}})(), "p-build",
                       {"GIT_CONFIG_KEY_0": ga.HELPER_KEY, "PATH": "x"})
SHIM = CREW / "bin" / ("gh" if POSIX else "gh.cmd")
(SCRATCH / "shim-copy").mkdir()
shutil.copy(SHIM, SCRATCH / "shim-copy" / SHIM.name)
r, calls = gh("pr", "view", PATH=os.pathsep.join([str(SCRATCH / "shim-copy"), str(CREW / "bin"), str(FAKE),
                                                  SYSTEM_PATH]))
ck("the crew's bin folder and a copy of the shim elsewhere on PATH are passed over",
   r.returncode == 0 and [c["args"][:2] for c in calls] == [["auth", "token"], ["pr", "view"]], (r.returncode, r.stderr, calls))
GH_LOG.write_text("")
cell.log.clear()
r = subprocess.run([str(SHIM), "pr", "view", "--repo", "acme/shim"], capture_output=True, cwd=str(PLAIN), timeout=120,
                   env={**ENV, "FACTORY_TARES": str(TARES_SCRIPT).replace("\\", "/"),
                        "PATH": os.pathsep.join([str(CREW / "bin"), str(FAKE), SYSTEM_PATH])})
SEEN.append(("shim output", r.stdout + r.stderr))
calls = [json.loads(x) for x in GH_LOG.read_text(encoding="utf-8").splitlines()]
ck("the shim file runs gh through the command once", r.returncode == 0 and r.stdout.strip() == b"fake gh ran"
   and [c["args"] for c in calls][1:] == [["pr", "view", "--repo", "acme/shim"]] and calls[-1]["token"] == digest(TOKEN)
   and asked() == [f"{ROUTE}?repo=acme/shim"], (r.returncode, r.stdout, r.stderr, calls, asked()))

# ---------------------------------------------------------------------------------------------
print("c. the launch")
GH_KEYS = ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1",
           "GIT_CONFIG_KEY_2", "GIT_CONFIG_VALUE_2", *ga.IDENTITY_VARS, "GIT_TERMINAL_PROMPT", "GCM_INTERACTIVE",
           "PATH")
TARES_BIN = L._forward(shutil.which("tares"))
_n = [0]


class Crew:
    """A throwaway crew and the fake Tares its launches are composed against."""

    def __init__(self, runtime="claude_code", caps=None, repos=(GITLAB, WIDGETS), body=None, status=200):
        _n[0] += 1
        tmp = SCRATCH / f"l{_n[0]}"
        tmp.mkdir()
        LS.make_crew(tmp, {"token": KEY})
        self.paths = CrewPaths.at(tmp / "crew")
        self.tares = LS.FakeTares({
            "token": KEY, "runtime": runtime, "capabilities": list(WITH_TOKENS if caps is None else caps),
            "projects": [{"id": "uc_1", "name": "alpha", "repos": [{"path": p} for p in repos]}],
            "routes": {ROUTE: [status, ANSWER if body is None else body]}})
        self.tares.__enter__()
        self.runtime = runtime
        self.said: list = []

    def client(self, **extra):
        env = {**os.environ, "TARESD_URL": self.tares.url, "TARES_FACTORY_MCP_URL": self.tares.url + "/mcp",
               "TARES_AUTH_TOKEN": KEY, "FACTORY_CREW_HOME": str(self.paths.home), **extra}
        c = TaresClient(self.paths, env=env, say=self.said.append)
        c.prime()
        return c

    def plan(self, client=None, station="crew-reviewer", role="reviewer", project="", folder="", without=False):
        """The launch plan and the requests it made; `without`: as it is with no GitHub access composed at all."""
        client = client or self.client()
        self.tares.log.clear()
        real = ga.launch_env
        if without:
            ga.launch_env = lambda *a, **k: {}
        model, effort = ("gpt-5.6-luna", "low") if self.runtime == "codex" else (None, None)
        try:
            plan = L.launch_plan(client, station, role, "", project, folder, model, effort)
        finally:
            ga.launch_env = real
        SEEN.append(("plan", json.dumps(plan).encode()))
        return plan, [x["path"] for x in self.tares.log]

    def token_asks(self, paths) -> list:
        return [p for p in paths if p.startswith(ROUTE)]

    def write(self, plan, client=None, station="crew-reviewer", role="reviewer"):
        return L.write_settings(client or self.client(), station, role, "", "", "", plan=plan)


crew = Crew()
client = crew.client()
plan, requests = crew.plan(client)
before, _ = crew.plan(without=True)
env = plan["env"]
BIN_DIR = str(crew.paths.home / "bin")
expected = {
    "GIT_CONFIG_COUNT": "3",
    "GIT_CONFIG_KEY_0": "credential.https://github.com.helper", "GIT_CONFIG_VALUE_0": "",
    "GIT_CONFIG_KEY_1": "credential.https://github.com.helper",
    "GIT_CONFIG_VALUE_1": f"!{shlex.quote(TARES_BIN)} factory git-credential",
    "GIT_CONFIG_KEY_2": "credential.https://github.com.useHttpPath", "GIT_CONFIG_VALUE_2": "true",
    "GIT_AUTHOR_NAME": NAME, "GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_NAME": NAME, "GIT_COMMITTER_EMAIL": EMAIL,
    "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
    "PATH": os.pathsep.join([BIN_DIR, *client.env["PATH"].split(os.pathsep)]),
    "CLAUDE_ENV_FILE": L._forward(str(crew.paths.station_file("crew-reviewer", "env.sh"))),
}
ck("on a cell that gives tokens, a Claude station gets the helper, the identity, the PATH and its env file",
   {k: env.get(k) for k in expected} == expected, {k: env.get(k) for k in expected if env.get(k) != expected[k]})
ck("and nothing else of its launch changes", {k: v for k, v in env.items() if k not in expected} == before["env"]
   and {k: v for k, v in plan.items() if k != "env"} == {k: v for k, v in before.items() if k != "env"})
ck("a station without a repository folder asks about the first github.com origin among the crew's",
   crew.token_asks(requests) == [f"{ROUTE}?repo=acme/widgets"], requests)
ck("composing the plan writes no file", not (crew.paths.home / "bin").exists()
   and not crew.paths.station_file("crew-reviewer", "env.sh").exists())
ck("the same inputs give the same plan", crew.plan()[0] == plan)
_, requests = crew.plan(station="alpha-build", role="builder", project="alpha", folder=ORIGIN)
ck("a station with a repository folder asks about that folder's origin",
   crew.token_asks(requests) == [f"{ROUTE}?repo=acme/origin"], requests)
other, requests = crew.plan(station="alpha-build", role="builder", project="alpha", folder=GITLAB)
ck("a station whose folder's origin is not on github.com gets nothing, and nothing is asked",
   crew.token_asks(requests) == [] and "GIT_CONFIG_COUNT" not in other["env"] and "CLAUDE_ENV_FILE" not in other["env"],
   (requests, other["env"]))

crew.write(plan)
shim, cmd_shim = crew.paths.home / "bin" / "gh", crew.paths.home / "bin" / "gh.cmd"
env_file = crew.paths.station_file("crew-reviewer", "env.sh")
ck("a start writes the gh shim, for sh and for cmd", shim.read_bytes()
   == b'#!/bin/sh\n# tares-factory gh shim\nexec "$FACTORY_TARES" factory gh -- "$@"\n'
   and cmd_shim.read_bytes()
   == b'@echo off\r\nrem tares-factory gh shim\r\n"%FACTORY_TARES%" factory gh -- %*\r\n'
   and plan["env"]["FACTORY_TARES"] == TARES_BIN, (shim.read_bytes(), cmd_shim.read_bytes()))
ck("and the station's env file, which puts the crew's bin folder first on PATH",
   env_file.read_text(encoding="utf-8").splitlines()[-1]
   == f'export PATH={shlex.quote(ga._sh_path(BIN_DIR))}:"$PATH"', env_file.read_text(encoding="utf-8"))
if POSIX:
    ck("the shim can be run and the env file is the owner's alone",
       stat.S_IMODE(shim.stat().st_mode) == 0o755 and stat.S_IMODE(env_file.stat().st_mode) == 0o600,
       (oct(shim.stat().st_mode), oct(env_file.stat().st_mode)))

# a Codex station listens on a Unix socket, so there is none to compose on Windows
RUNTIMES = ("claude_code", "codex") if POSIX else ("claude_code",)
for label, opts in (("a cell that does not list the capability", {"caps": CAPS}),
                    ("a cell with no credential", {"status": 404, "body": {"code": "no_credential", "detail": "none"}}),
                    ("no github.com origin in any folder", {"repos": (GITLAB, str(PLAIN))})):
    for runtime in RUNTIMES:
        c = Crew(runtime=runtime, **opts)
        got, requests = c.plan()
        was, was_requests = c.plan(without=True)
        extra = c.token_asks(requests)
        ck(f"{label} ({runtime}): the plan is as without this", got == was, (got["env"], was["env"]))
        ck(f"{label} ({runtime}): " + ("one request more, the token's" if "status" in opts else "no request more"),
           [p for p in requests if p not in extra] == was_requests and len(extra) == (1 if "status" in opts else 0),
           (requests, was_requests))
        c.write(got)
        ck(f"{label} ({runtime}): no bin folder and no env file are written",
           not (c.paths.home / "bin").exists() and not c.paths.station_file("crew-reviewer", "env.sh").exists())

for label, status, body in (("403", 403, {"code": "repo_not_allowed", "detail": "no"}),
                            ("409", 409, {"code": "app_permissions_missing", "detail": "no"}),
                            ("502", 502, {"code": "github_error", "detail": "no"}),
                            ("404 with another code", 404, {"detail": "Not Found"}),
                            ("an answer with no name", 200, {**ANSWER, "name": None}),
                            ("an answer with no email", 200, {**ANSWER, "email": ""}),
                            ("an answer with no identity", 200, {**ANSWER, "name": None, "email": None})):
    got, _ = Crew(status=status, body=body).plan()
    ck(f"{label}: git and gh are set up, with no commit identity",
       got["env"].get("GIT_CONFIG_COUNT") == "3" and "CLAUDE_ENV_FILE" in got["env"]
       and not [k for k in ga.IDENTITY_VARS if k in got["env"]], got["env"])
silent = crew.client()


def no_answer(*_a, **_k):
    raise TaresHTTPError("no answer")


silent.github_token = no_answer
got, _ = crew.plan(silent)
ck("no answer from the token endpoint: the same, and the start goes on",
   got["env"].get("GIT_CONFIG_COUNT") == "3" and not [k for k in ga.IDENTITY_VARS if k in got["env"]], got["env"])

station_env = {**plan["env"], "GIT_CONFIG_COUNT": "4", "GIT_CONFIG_KEY_3": "x.y", "GIT_CONFIG_VALUE_3": "z"}
from_station, _ = crew.plan(crew.client(**station_env))
ck("composed by a station, from its own environment, the values are the same and nothing inherited is copied",
   {k: from_station["env"].get(k) for k in (*GH_KEYS, "CLAUDE_ENV_FILE")}
   == {k: plan["env"].get(k) for k in (*GH_KEYS, "CLAUDE_ENV_FILE")}
   and "GIT_CONFIG_KEY_3" not in from_station["env"] and "GIT_CONFIG_VALUE_3" not in from_station["env"],
   {k: from_station["env"].get(k) for k in GH_KEYS if from_station["env"].get(k) != plan["env"].get(k)})

launcher = SCRATCH / "person env.sh"
launcher.write_text(f'export PERSON_MARK=kept\nexport PATH={shlex.quote(ga._sh_path(str(FAKE)))}:"$PATH"\n')
crew.write(plan, crew.client(CLAUDE_ENV_FILE=str(launcher)))
text = env_file.read_text(encoding="utf-8")
quoted = shlex.quote(ga._sh_path(str(launcher)))
ck("the launcher's own env file is sourced first, when it exists",
   text.splitlines()[-2] == f"if [ -f {quoted} ]; then . {quoted}; fi", text)
if POSIX:
    def sourced():
        return subprocess.run(["bash", "-c", '. "$1"; command -v gh; echo "$PERSON_MARK"', "bash", str(env_file)],
                              capture_output=True, text=True, timeout=30,
                              env={"HOME": str(HOME), "PATH": os.pathsep.join([str(FAKE), BIN_DIR, "/usr/bin", "/bin"])})
    r = sourced()
    ck("sourced, the shim is the gh that runs, also when PATH was ordered another way first",
       r.returncode == 0 and r.stdout.splitlines() == [str(shim), "kept"], (r.stdout, r.stderr))
    launcher.unlink()
    r = sourced()
    ck("a launcher env file that is gone is skipped without a word",
       r.returncode == 0 and r.stdout.splitlines() == [str(shim), ""] and r.stderr == "", (r.stdout, r.stderr))
for name in ("crew-reviewer", "crew-orchestrator"):
    crew.write(plan, crew.client(CLAUDE_ENV_FILE=str(crew.paths.station_file(name, "env.sh"))))
    text = env_file.read_text(encoding="utf-8")
    ck(f"the env file of a station of this crew ({name}) is not sourced", "if [ -f" not in text, text)
ck("a Windows path is written as sh takes it in PATH",
   ga._sh_path("C:/Users/pat/factory/bin", "win32") == "/c/Users/pat/factory/bin"
   and ga._sh_path("D:\\crew\\bin", "win32") == "/d/crew/bin" and ga._sh_path("E:", "win32") == "/e/"
   and ga._sh_path("/home/pat/factory/bin", "linux") == "/home/pat/factory/bin",
   (ga._sh_path("C:/Users/pat/factory/bin", "win32"), ga._sh_path("D:\\crew\\bin", "win32")))

if POSIX:
    GITCONFIG.write_text('[credential]\n\thelper = "!f() { echo username=other; echo password=other-password; }; f"\n')
    git_env = {**crew.client().env, **plan["env"], "PYTHONPATH": str(ROOT)}

    def fill(host: str, path: str):
        return subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\npath={path}\n\n",
                              capture_output=True, text=True, env=git_env, cwd=str(PLAIN), timeout=120)

    crew.tares.log.clear()
    r = fill("github.com", "acme/widgets.git")
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    ck("real git with a station's environment asks the crew's helper for github.com, with the path",
       r.returncode == 0 and got.get("username") == "x-access-token" and got.get("password") == TOKEN
       and crew.token_asks([x["path"] for x in crew.tares.log]) == [f"{ROUTE}?repo=acme/widgets"],
       (r.returncode, r.stderr, sorted(got), [x["path"] for x in crew.tares.log]))
    SEEN.append(("git stderr", r.stderr.encode()))
    r = fill("example.com", "acme/widgets.git")
    ck("for another host the person's helper still answers",
       r.returncode == 0 and "username=other" in r.stdout and "password=other-password" in r.stdout, (r.stdout, r.stderr))
    crew.tares.world["routes"][ROUTE] = [403, {"code": "repo_not_allowed", "detail": "no"}]
    r = fill("github.com", "acme/widgets.git")
    ck("refused by the cell, git fails without a prompt and without the person's helper",
       r.returncode != 0 and "other-password" not in r.stdout and TOKEN not in r.stdout, (r.returncode, r.stdout, r.stderr))
    SEEN.append(("git refusal", (r.stdout + r.stderr).encode()))
    GITCONFIG.write_text("")

if POSIX:
    codex = Crew(runtime="codex")
    plan, requests = codex.plan()
    before, _ = codex.plan(without=True)
    cx_bin = str(codex.paths.home / "bin")
    ck("a Codex station gets the same, without an env file",
       {k: plan["env"].get(k) for k in expected if k not in ("PATH", "CLAUDE_ENV_FILE")}
       == {k: v for k, v in expected.items() if k not in ("PATH", "CLAUDE_ENV_FILE")}
       and plan["env"]["PATH"].split(os.pathsep)[0] == cx_bin and "CLAUDE_ENV_FILE" not in plan["env"]
       and {k: v for k, v in plan["env"].items() if k not in GH_KEYS} == before["env"], plan["env"])
    codex.write(plan)
    ck("its start writes the shim and no env file", (codex.paths.home / "bin" / "gh").exists()
       and (codex.paths.home / "bin" / "gh.cmd").exists()
       and not codex.paths.station_file("crew-reviewer", "env.sh").exists())

    awkward = 'C:\\a b\\bin;/x"y/bin'
    doc = tomllib.loads(cx.home_config(str(PLAIN), awkward))
    ck("the station's Codex config gives its shell commands that PATH, written as a TOML string",
       doc["shell_environment_policy"] == {"set": {"PATH": awkward}}
       and all(v == {"trust_level": "untrusted"} for v in doc["projects"].values()), doc)
    ck("with no GitHub access the config is the trust marks alone",
       list(tomllib.loads(cx.home_config(str(PLAIN), ga.shell_path(before["env"])))) == ["projects"]
       and ga.shell_path({"PATH": "/x"}) == "")

    longer, _ = codex.plan(codex.client(PATH=str(PLAIN) + os.pathsep + os.environ["PATH"]))
    codex.tares.world["routes"][ROUTE] = [502, {"code": "github_error", "detail": "no"}]
    unnamed, _ = codex.plan()
    codex.tares.world["routes"][ROUTE] = [200, ANSWER]
    ck("the fingerprint does not move with the launcher's PATH or with the identity",
       longer["env"]["PATH"] != plan["env"]["PATH"] and "GIT_AUTHOR_NAME" not in unnamed["env"]
       and cx.fingerprint(plan, "kit") == cx.fingerprint(longer, "kit") == cx.fingerprint(unnamed, "kit"),
       [cx.fingerprint(x, "kit") for x in (plan, longer, unnamed)])
    ck("it does move when the station's GitHub setup does",
       cx.fingerprint(plan, "kit") != cx.fingerprint(before, "kit"))
    ck("a plan without the GitHub setup has the fingerprint it had", "PATH" not in before["env"]
       and cx.fingerprint(before, "kit") == str(cksum(state.dumps_compact(
           [before["args"], before["env"], before["settings"], "kit"]).encode("utf-8"))), sorted(before["env"]))

    import signal

    import psutil

    for label, c, held in (("with GitHub access", codex, True), ("without it", Crew(runtime="codex", caps=CAPS), False)):
        os.environ["FAKE_CODEX_DAEMON"] = str(SCRATCH / f"daemon{_n[0]}-{int(held)}")
        started = c.client(FAKE_CODEX_DAEMON=os.environ["FAKE_CODEX_DAEMON"])
        pid = None
        try:
            want_plan, _ = c.plan(started)
            L.launch(started, "crew-reviewer", "reviewer", "", "", "", str(c.paths.home), "gpt-5.6-luna", "low",
                     "Boot as the reviewer station.", "")
            pid = state.stations_load(c.paths)["crew-reviewer"]["codex"]["pid"]
            home = cx.station_home(c.paths, "crew-reviewer")
            doc = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
            found = doctor_codex.home_problems(c.paths, "crew-reviewer", started.env)
            if held:
                ck(f"a started Codex station {label}: its config.toml holds the plan's PATH",
                   doc.get("shell_environment_policy") == {"set": {"PATH": want_plan["env"]["PATH"]}}, doc)
                ck("and doctor finds its Codex folder in order", found == [], found)
                config = home / "config.toml"
                kept = config.read_text(encoding="utf-8")
                for extra in ('OTHER = "x"\n', '[shell_environment_policy.set]\nPATH = "/x"\n',
                              '[shell_environment_policy]\ninherit = "none"\n'):
                    config.write_text(kept + extra, encoding="utf-8")
                    wrong = doctor_codex.home_problems(c.paths, "crew-reviewer", started.env)
                    ck(f"doctor reports a config.toml that also holds {extra.splitlines()[0]}",
                       len(wrong) == 1 and "setup of its own" in wrong[0], wrong)
            else:
                ck(f"a started Codex station {label}: its config.toml is the trust marks alone",
                   list(doc) == ["projects"] and found == [], (doc, found))
        finally:
            if pid and psutil.pid_exists(pid):
                os.kill(pid, signal.SIGKILL)

# ---------------------------------------------------------------------------------------------
print("d. no token is left behind")
marks = (b"ghs_", b"ghu_", b"github_pat")
files = [p for p in SCRATCH.rglob("*") if p.is_file() and not p.is_symlink()]
holding = [str(p.relative_to(SCRATCH)) for p in files if any(m in p.read_bytes() for m in marks)]
names = {p.name for p in files}
ck("the files looked through hold the settings, the env file, the shims, the call logs and the URL log",
   {"crew-reviewer.settings.json", "crew-reviewer.env.sh", "gh", "gh.cmd", "gh-calls.jsonl", "urls.log"} <= names,
   sorted(names)[:60])
ck("no file under the scratch folder holds a token", not holding, holding)
told = [what for what, text in SEEN if any(m in text for m in marks)]
ck("no plan and no output holds a token, the credential protocol's answer aside", len(SEEN) > 50 and not told,
   (len(SEEN), told))

cell.__exit__()
shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

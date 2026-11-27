"""`tares factory doctor` and GitHub through the cell's token (TR-1718): the github_console check, and what the
gh, git identity and project checks say once it passes.

 a. the check: no line without the capability, which repositories it asks about, one line for every outcome;
 b. what a failing answer is called, and that no detail text, exception text or token is repeated;
 c. the warnings: nobody to commit as, a project folder that is not on github.com;
 d. the gh, git_identity and project lines;
 e. the one request to GitHub: address, headers, timeout, no redirect;
 f. `doctor --json` and the text output end to end against a fake cell (tests/factory/fake_tares.py);
 g. no token in any output, log record or file.

Hermetic: a scratch HOME, TARES_HOME and crew home, the failing keyring backend, and repositories made here with
real git. gh, claude and docker are never run, and the only network is the fake cell on 127.0.0.1: every httpx
request to another address is answered or refused by this file.

Run: python tests/test_crew_doctor_github.py
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _crew_guard  # noqa: E402  (read before HOME changes: it remembers the real home)

SCRATCH = Path(os.path.realpath(tempfile.mkdtemp(prefix="doctor-github-")))
for _k in [k for k in os.environ if k.upper().startswith(
        ("TARES", "FACTORY", "CLAUDE", "CODEX", "FAKE_", "GH_", "GITHUB_", "GIT_", "SSH_ASKPASS"))
        or k.upper().endswith("_PROXY")]:
    del os.environ[_k]
HOME, EMPTY = SCRATCH / "home", SCRATCH / "empty"
for _d in (HOME, EMPTY):
    _d.mkdir()
(SCRATCH / "gitconfig").write_text("")
os.environ.update(HOME=str(HOME), USERPROFILE=str(HOME), TARES_HOME=str(SCRATCH / "tares-home"),
                  FACTORY_CREW_HOME=str(SCRATCH / "crew"), FACTORY_NO_WATCH="1",
                  PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", GIT_CONFIG_NOSYSTEM="1",
                  GIT_CONFIG_GLOBAL=str(SCRATCH / "gitconfig"), GIT_CEILING_DIRECTORIES=str(SCRATCH),
                  GH_CONFIG_DIR=str(SCRATCH / "gh-config"), CLAUDE_BIN=str(EMPTY / "no-claude"))
_crew_guard.install()   # an exec or spawn that would reach the real ~/factory fails the run

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE / "factory"))

import httpx  # noqa: E402
from jsonschema import Draft202012Validator, FormatChecker  # noqa: E402

import fake_tares  # noqa: E402

# set while the doctor is imported and again around a request: neither moves where the token goes
OTHER_HOST = {k: "https://example.invalid" for k in
              ("GITHUB_API", "GITHUB_API_URL", "GITHUB_SERVER_URL", "GH_HOST", "TARES_GITHUB_API")}
with mock.patch.dict(os.environ, OTHER_HOST):
    from tares.crew import doctor, github_access, state  # noqa: E402
from tares.crew.client import TaresHTTPError  # noqa: E402
from tares.crew.errors import CrewError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0
PRODUCED: list = []   # every message, fix and printed text, searched for tokens at the end
# Put together here so that this file holds nothing that looks like a token.
TOKEN = "gh" + "s_" + uuid.uuid4().hex
OTHER_TOKEN = "gh" + "u_" + uuid.uuid4().hex
PAT = "github" + "_pat_" + uuid.uuid4().hex
GH = str(EMPTY / "gh-that-is-never-run")
TOKEN_ROUTE = "/api/factory/github/token"
GOOD = {"token": TOKEN, "name": "tares[bot]", "email": "bot@example.invalid", "login": "tares[bot]", "kind": "app",
        "expires_at": "2030-01-01T00:00:00Z"}
SIGN_IN = ("gh auth login", "git config --global")


class Records(logging.Handler):
    def emit(self, record):
        PRODUCED.append(record.getMessage())


logging.getLogger().addHandler(Records(level=logging.DEBUG))
logging.getLogger().setLevel(logging.DEBUG)


def ck(label, cond, detail: object = ""):
    global P, F
    if cond:
        P += 1
        print("  ok   " + label)
    else:
        F += 1
        print("  FAIL " + label + (" :: " + str(detail) if detail else ""))


def res(code=0, out=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr="")


# ---------- repositories ----------


def folder(name: str, origin: str = "") -> str:
    """A folder under the scratch folder; with an origin, a git repository that has it."""
    d = SCRATCH / "repos" / name
    d.mkdir(parents=True)
    if origin:
        subprocess.run(["git", "init", "-q", str(d)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(d), "remote", "add", "origin", origin], check=True, capture_output=True)
    return str(d)


CORE = folder("core", "https://github.com/Acme/core.git")
CORE_AGAIN = folder("core-again", "git@github.com:acme/CORE.git")
UI = folder("ui", "ssh://git@github.com/acme/ui")
ELSEWHERE = folder("elsewhere", "https://gitlab.example.invalid/acme/tools.git")
PLAIN = folder("plain")
MISSING = str(SCRATCH / "repos" / "missing")


# ---------- the fakes ----------


class Cell:
    """The three things the check asks of a TaresClient. `answers` maps a lowercased owner/name (or "*") to a
    (status, body) pair, or to an exception to raise."""

    def __init__(self, folders, answers=None, caps=("github_token",)):
        self.folders, self.answers, self.caps = folders, answers or {}, list(caps)
        self.asked: list = []
        self.rows_read = 0

    def capabilities(self):
        return self.caps

    def project_repo_rows(self):
        self.rows_read += 1
        if isinstance(self.folders, BaseException):
            raise self.folders
        return [("argus", f) for f in self.folders]

    def github_token(self, repo, seconds):
        self.asked.append((repo, seconds))
        answer = self.answers.get(repo.lower(), self.answers.get("*", (200, dict(GOOD))))
        if isinstance(answer, BaseException):
            raise answer
        return answer[0], dict(answer[1])


@contextlib.contextmanager
def machine(gh=GH, login=False, github=None, cell_port=None):
    """gh is where `which gh` finds it (None: not installed), login what has_login answers, github what
    api.github.com does with a request (default: 200). Docker is not installed. No other address is reached
    but the fake cell's port."""
    seen = SimpleNamespace(github=[], logins=[], refused=[])
    real_which, real_send = shutil.which, httpx.HTTPTransport.handle_request

    def which(name, *a, **k):
        if name == "gh":
            return gh
        return None if name == "docker" else real_which(name, *a, **k)

    def has_login(path, env):
        seen.logins.append(path)
        return login

    def send(transport, request):
        if request.url.host == "api.github.com":
            seen.github.append(request)
            return github(request) if github else httpx.Response(200, request=request)
        if request.url.host == "127.0.0.1" and request.url.port == cell_port:
            return real_send(transport, request)
        seen.refused.append(str(request.url))
        raise httpx.ConnectError("refused by the test", request=request)

    with mock.patch.object(shutil, "which", which), mock.patch.object(github_access, "has_login", has_login), \
            mock.patch.object(httpx.HTTPTransport, "handle_request", send):
        yield seen


def runner(status=1, host_status=1, view="", identity=False, calls=None):
    """Doctor's run: `gh auth status` exits with status, with --hostname github.com with host_status;
    `gh repo view` prints view (nothing: it fails); git's global identity is set or not."""
    def run(cmd, cwd=None, timeout=0):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[0] == GH:
            if cmd[1:] == ["auth", "status"]:
                return res(status)
            if cmd[1:] == ["auth", "status", "--hostname", "github.com"]:
                return res(host_status)
            if cmd[1:3] == ["repo", "view"]:
                return res(0 if view else 1, view)
        if cmd == ["git", "--version"]:
            return res(0, "git version 2.0.0\n")
        if cmd[:4] == ["git", "config", "--global", "--get"]:
            return res(0, "someone\n") if identity else res(1)
        return res(1)
    return run


def new_doctor(run=None, env=None):
    paths = CrewPaths(str(SCRATCH / "crew"))
    return doctor.Doctor({**os.environ, **(env or {})}, paths, str(EMPTY), run or runner())


def checked(cell, run=None, env=None):
    """A Doctor that has run check_github_console against this cell."""
    d = new_doctor(run, env)
    d.check_github_console(cell)
    keep(d)
    return d


def keep(d):
    for c in d.checks:
        PRODUCED.extend((c.message, c.fix, c.line()))


def one(d, cid):
    got = [c for c in d.checks if c.id == cid]
    return got[0] if len(got) == 1 else None


def line_of(d, cid="github_console"):
    c = one(d, cid)
    return (c.level, c.message, c.fix) if c else None


def flags(d):
    return d.github_console_ok, d.github_identity_ok


# ---------- a. when there is a line, and what it asks ----------

print("a. when there is a line")
with machine() as m:
    cell = Cell([CORE, UI], caps=["guardrail_rules", "launch_spec"])
    d = checked(cell)
ck("without the capability: no line", d.checks == [], d.checks)
ck("without the capability: no project read, no token request, no request to GitHub",
   cell.rows_read == 0 and cell.asked == [] and m.github == [], (cell.rows_read, cell.asked))
ck("without the capability: both flags are false", flags(d) == (False, False))

with machine() as m:
    cell = Cell(CrewError("no list"))
    d = checked(cell)
ck("a project list that cannot be read: no line, no token request",
   d.checks == [] and cell.asked == [] and flags(d) == (False, False), d.checks)

for label, folders in (("no project", []), ("only folders that are not on github.com", [ELSEWHERE, PLAIN]),
                       ("only a missing folder", [MISSING]),
                       ("a path that is not text, or empty", [7, "", None, [CORE], {"path": CORE}])):
    with machine() as m:
        cell = Cell(folders)
        d = checked(cell)
    ck(f"{label}: no line, no token request, flags false",
       d.checks == [] and cell.asked == [] and flags(d) == (False, False), (d.checks, cell.asked))

with machine() as m:
    cell = Cell([CORE, MISSING, CORE_AGAIN, UI, CORE])
    d = checked(cell)
ck("one token request per repository, whatever the case of its name, with a 10 second limit",
   cell.asked == [("Acme/core", 10), ("acme/ui", 10)], cell.asked)
ck("GitHub is asked once per repository",
   [str(r.url) for r in m.github] == ["https://api.github.com/repos/Acme/core",
                                      "https://api.github.com/repos/acme/ui"], [str(r.url) for r in m.github])
ck("a missing folder is left to the project checks and does not count against the identity",
   flags(d) == (True, True), flags(d))
ck("two repositories reached: one ok line",
   line_of(d) == ("ok", "GitHub through the console: 2 repos reachable", ""), line_of(d))
ck("the check is marked as one bash does not make, with a label of NEW_LABELS",
   one(d, "github_console").new and one(d, "github_console").message.startswith(doctor.NEW_LABELS))

with machine():
    d = checked(Cell([UI]))
ck("one repository reached: the singular", line_of(d) == ("ok", "GitHub through the console: 1 repo reachable", ""),
   line_of(d))

# GIT_DIR and friends point git at one repository whatever folder is asked about; a station drops them
with machine():
    cell = Cell([UI])
    d = checked(cell, env={"GIT_DIR": os.path.join(CORE, ".git"), "GIT_WORK_TREE": CORE,
                           "GIT_COMMON_DIR": os.path.join(CORE, ".git")})
ck("each folder's own origin is used when GIT_DIR points elsewhere", cell.asked == [("acme/ui", 10)], cell.asked)


# ---------- b. failures ----------

print("b. failures")
DETAIL = f"detail that quotes {OTHER_TOKEN} and {PAT}"


def failing(answer=None, github=None):
    """One failing repository (acme/ui) beside a good one (Acme/core)."""
    answers = {"acme/ui": answer} if answer is not None else {}

    def respond(request):
        if github is not None and request.url.path.endswith("/ui"):
            return github(request)
        return httpx.Response(200, request=request)

    with machine(github=respond):
        return checked(Cell([CORE, UI], answers))


def fails(label, d, why, fix):
    got = line_of(d)
    ck(f"{label}: one fix line that names the repository and says why",
       got is not None and got[0] == "fix" and got[1].startswith("GitHub through the console: acme/ui: ")
       and why in got[1] and "Acme/core" not in got[1], got)
    ck(f"{label}: the fix", got is not None and fix in got[2], got)
    ck(f"{label}: flags false", flags(d) == (False, False), flags(d))


d = failing((404, {"code": "no_credential", "detail": DETAIL}))
fails("404 no_credential", d, "404 no_credential", "Connect GitHub in the console: Setup, GitHub")
ck("404 no_credential: the fix is exactly that", line_of(d)[2] == "Connect GitHub in the console: Setup, GitHub",
   line_of(d))
fails("403 repo_not_allowed", failing((403, {"code": "repo_not_allowed", "detail": DETAIL})),
      "403 repo_not_allowed", "name acme/ui on a project and give the console's GitHub access to it")
fails("409 app_permissions_missing", failing((409, {"code": "app_permissions_missing", "detail": DETAIL})),
      "409 app_permissions_missing", "approve the App's permissions on GitHub")
for status, body in ((400, {"detail": DETAIL}), (502, {"detail": DETAIL}), (404, {"detail": DETAIL}),
                     (403, {"code": [PAT]}), (409, {"code": OTHER_TOKEN}), (500, {})):
    fails(f"another refusal ({status}, {sorted(body)})", failing((status, body)),
          f"Tares answered {status} to the token request", "check GitHub in the console")
for label, error in (("no answer from Tares", TaresHTTPError(f"could not reach it {TOKEN}")),
                     ("any other error of the request", ValueError(f"bad header {TOKEN}"))):
    d = failing(error)
    fails(label, d, f"no answer from Tares ({type(error).__name__})", "check that Tares answers")
    ck(f"{label}: the error's text is not repeated", "could not reach" not in line_of(d)[1]
       and "bad header" not in line_of(d)[1] and TOKEN not in line_of(d)[1], line_of(d))
for label, body in (("no token", {}), ("an empty token", {"token": ""}), ("a number for a token", {"token": 7}),
                    ("a null token", {"token": None}), ("a list for a token", {"token": [TOKEN]}),
                    ("a token with a line break", {"token": TOKEN + "\n"})):
    fails(f"200 with {label}", failing((200, body)), "Tares answered 200 with no usable token",
          "check GitHub in the console")
for status in (301, 401, 403, 404, 500):
    d = failing(github=lambda request, s=status: httpx.Response(s, text=DETAIL, request=request,
                                                                headers={"location": "https://example.invalid/"}))
    fails(f"GitHub answers {status}", d, f"GitHub answered {status}", "check that acme/ui is the repository's")


def unreachable(request):
    raise httpx.ConnectTimeout(f"timed out sending {request.headers['Authorization']}", request=request)


d = failing(github=unreachable)
fails("api.github.com unreachable", d, "no answer from api.github.com (ConnectTimeout)",
      "connection to api.github.com")
ck("api.github.com unreachable: the error's text is not repeated", "timed out" not in line_of(d)[1], line_of(d))
d = failing((200, {**GOOD, "token": TOKEN + "é"}))   # a header cannot carry it: the HTTP client refuses
fails("a token the request cannot carry", d, "(UnicodeEncodeError)", "api.github.com")

with machine():
    d = checked(Cell([CORE, UI, ELSEWHERE], {"acme/core": (404, {"code": "no_credential"}),
                                              "acme/ui": (403, {"code": "repo_not_allowed"})}))
got = line_of(d)
ck("two failing repositories: one line that lists both, and both fixes",
   got is not None and "Acme/core: " in got[1] and "acme/ui: " in got[1] and "Setup, GitHub; name acme/ui" in got[2],
   got)
with machine():
    d = checked(Cell([CORE, UI], {"*": (404, {"code": "no_credential"})}))
ck("the same fix for two repositories is written once",
   line_of(d)[2] == "Connect GitHub in the console: Setup, GitHub", line_of(d))


# ---------- c. warnings ----------

print("c. warnings")
NOBODY = (("no name", {"token": TOKEN, "email": "a@example.invalid"}),
          ("no email", {"token": TOKEN, "name": "a"}),
          ("null name and email", {"token": TOKEN, "name": None, "email": None}),
          ("an empty name", {**GOOD, "name": ""}), ("a blank email", {**GOOD, "email": "  "}),
          ("a number for a name", {**GOOD, "name": 7}), ("a list for an email", {**GOOD, "email": ["a"]}),
          ("a name with a NUL", {**GOOD, "name": "a\0b"}))
for label, body in NOBODY:
    with machine():
        d = checked(Cell([CORE, UI], {"acme/ui": (200, body)}))
    got = line_of(d)
    ck(f"{label}: a warn that names the repository and this machine's git identity",
       got is not None and got[0] == "warn" and got[1] == "GitHub through the console: 2 repos reachable; no name "
       "and email to commit as for acme/ui (stations there use this machine's git identity)"
       and got[2] == "set user.name and user.email in git on this machine", got)
    ck(f"{label}: the repositories count as reached, the identity does not", flags(d) == (True, False), flags(d))

with machine():
    d = checked(Cell([CORE, ELSEWHERE, PLAIN, ELSEWHERE]))
got = line_of(d)
ck("folders that are not on github.com: a warn that names each once, and this machine's git setup",
   got is not None and got[0] == "warn" and got[1] == "GitHub through the console: 1 repo reachable; no github.com "
   f"origin in {ELSEWHERE}, {PLAIN} (stations there use this machine's git setup)"
   and got[2] == "add a github.com origin to those folders, or set up git for them on this machine", got)
ck("folders that are not on github.com: reached, no console identity for every station", flags(d) == (True, False),
   flags(d))
with machine():
    d = checked(Cell([CORE, PLAIN], {"*": (200, {"token": TOKEN})}))
got = line_of(d)
ck("both reasons: one warn with both, and both fixes",
   got is not None and got[0] == "warn" and "Acme/core (stations" in got[1] and PLAIN in got[1]
   and got[2].startswith("set user.name") and "; add a github.com origin" in got[2], got)

# the doctor and a station's launch decide alike whether an answer names who to commit as
for label, body in (*NOBODY, ("a full answer", GOOD)):
    cell = SimpleNamespace(env=dict(os.environ), paths=CrewPaths(str(SCRATCH / "crew")),
                           capabilities=lambda: ["github_token"],
                           github_token=lambda repo, seconds, b=body: (200, dict(b)))
    launch = github_access.launch_env(cell, "builder-1", "/opt/tares", lambda: [UI], False)
    ck(f"{label}: the doctor agrees with a station's launch on the identity",
       bool(launch) and ("GIT_AUTHOR_NAME" in launch) == doctor.names_identity(body), sorted(launch))


# ---------- d. gh, git identity and projects ----------

print("d. gh, git identity and projects")
OK_CELL = lambda: Cell([CORE, UI])                                           # noqa: E731
WARN_CELL = lambda: Cell([CORE, UI], {"*": (200, {"token": TOKEN})})         # noqa: E731
BAD_CELL = lambda: Cell([CORE, UI], {"*": (404, {"code": "no_credential"})})  # noqa: E731
OFF_CELL = lambda: Cell([CORE, UI], caps=[])                                 # noqa: E731
SIGNED_OUT = ("fix", "gh not signed in: gh auth login", "gh auth login")


def gh_line(cell, login=False, gh=GH, status=1, host_status=1):
    calls: list = []
    with machine(gh=gh, login=login) as m:
        d = checked(cell, runner(status, host_status, calls=calls))
        d.check_gh()
    keep(d)
    return line_of(d, "gh"), m.logins, [c[1:] for c in calls if c[0] == GH]


for name, cell in (("a passing", OK_CELL), ("a warning", WARN_CELL)):
    got, logins, calls = gh_line(cell(), login=False)
    ck(f"gh with no login, {name} check: ok, no sign in needed",
       got == ("ok", "gh: stations use the console's GitHub token, no sign in needed", ""), got)
    ck(f"gh with no login, {name} check: asked with has_login, no second auth status",
       logins == [GH] and calls == [["auth", "status"]], (logins, calls))
for login in (True, None):
    got, logins, calls = gh_line(OK_CELL(), login=login, host_status=0)
    ck(f"auth status fails, github.com's works (has_login {login}): ok",
       got == ("ok", "gh signed in to github.com", ""), got)
    ck(f"has_login {login}: github.com is asked about by name",
       calls == [["auth", "status"], ["auth", "status", "--hostname", "github.com"]], calls)
    got, logins, calls = gh_line(OK_CELL(), login=login, host_status=1)
    ck(f"a github.com login that does not work (has_login {login}): fix is a logout",
       got == ("fix", "gh auth status fails for github.com, and stations use gh's own login when it has one",
               "gh auth logout --hostname github.com"), got)
# a token in the environment is a login that `gh auth logout` leaves in place; its value is never shown
VALUE = "value-" + uuid.uuid4().hex
for held, fix in ((("GH_TOKEN",), "remove GH_TOKEN from the environment"),
                  (("GITHUB_TOKEN",), "remove GITHUB_TOKEN from the environment"),
                  (("GH_TOKEN", "GITHUB_TOKEN"), "remove GH_TOKEN and GITHUB_TOKEN from the environment")):
    with mock.patch.dict(os.environ, dict.fromkeys(held, VALUE)):
        got, logins, calls = gh_line(OK_CELL(), login=True)
    ck(f"a login that does not work from {' and '.join(held)}: the fix removes it, and shows no value",
       got is not None and got[0] == "fix" and got[2] == fix and VALUE not in " ".join(got), got)
with mock.patch.dict(os.environ, {"GH_TOKEN": "", "GITHUB_TOKEN": ""}):
    got, logins, calls = gh_line(OK_CELL(), login=True)
ck("an empty token variable is no login: the fix is a logout", got[2] == "gh auth logout --hostname github.com", got)
got, logins, calls = gh_line(OK_CELL(), status=0)
ck("a working gh login keeps its line, and has_login is not asked",
   got == ("ok", "gh signed in", "") and logins == [] and calls == [["auth", "status"]], (got, logins, calls))
got, logins, calls = gh_line(OK_CELL(), gh=None)
ck("gh not installed keeps its fix line when the check passes",
   got == ("fix", "gh not installed (needed for PRs and reviews): brew install gh", "brew install gh")
   and logins == [], got)
for name, cell in (("a failing check", BAD_CELL), ("no capability", OFF_CELL)):
    got, logins, calls = gh_line(cell())
    ck(f"{name}: gh's line and calls are as before",
       got == SIGNED_OUT and logins == [] and calls == [["auth", "status"]], (got, logins, calls))


def identity_line(cell, identity=False):
    with machine():
        d = checked(cell, runner(identity=identity))
        d.check_git_identity()
    keep(d)
    return line_of(d, "git_identity")


ck("no global identity, a passing check: ok",
   identity_line(OK_CELL()) == ("ok", "git identity: stations commit as the console's GitHub identity", ""),
   identity_line(OK_CELL()))
ck("a global identity keeps its line", identity_line(OK_CELL(), True)
   == ("ok", "git identity: user.name and user.email are set in git's global config", ""))
MISSING_IDENTITY = identity_line(OFF_CELL())
ck("no global identity without the capability: the warn, with the git config fix",
   MISSING_IDENTITY[0] == "warn" and MISSING_IDENTITY[2].startswith('git config --global user.name "<your name>"'),
   MISSING_IDENTITY)
for name, cell in (("a warning", WARN_CELL()), ("a failing", BAD_CELL()),
                   ("a folder elsewhere", Cell([CORE, PLAIN]))):
    ck(f"no global identity, {name} check: the same warn as without the capability",
       identity_line(cell) == MISSING_IDENTITY)

UNREAD = "gh can't read the GitHub repo of {} (no GitHub remote, or no access); builders open PRs there"


def project_lines(cell, login=False, view=""):
    with machine(login=login) as m:
        d = checked(cell, runner(view=view))
        d.check_projects(cell)
    keep(d)
    return d, {c.id: (c.level, c.message, c.fix) for c in d.checks if c.id.endswith("_github")}, m.logins


d, got, logins = project_lines(OK_CELL())
ck("gh reads nothing, no login: an ok line per project folder, through the console",
   got == {"project_argus_core_github": ("ok", f"project argus: {CORE} is Acme/core on GitHub (through the console)",
                                         ""),
           "project_argus_ui_github": ("ok", f"project argus: {UI} is acme/ui on GitHub (through the console)", "")},
   got)
ck("has_login is asked once a run, however many folders", logins == [GH], logins)
d, got, _ = project_lines(Cell([CORE_AGAIN, CORE]))
ck("two folders of one repository: each gets its ok line, with its own spelling",
   [v[1].split(" is ")[1] for v in got.values()] == ["acme/CORE on GitHub (through the console)",
                                                    "Acme/core on GitHub (through the console)"], got)
for login in (True, None):
    d, got, _ = project_lines(Cell([UI]), login=login)
    ck(f"gh reads nothing with a login of its own (has_login {login}): fix, without a sign in command",
       got == {"project_argus_ui_github": (
           "fix", "project argus: " + UNREAD.format(UI),
           "give gh's own login access to acme/ui, or take that login away: "
           "gh auth logout --hostname github.com")}, got)
with mock.patch.dict(os.environ, {"GITHUB_TOKEN": VALUE}):
    d, got, _ = project_lines(Cell([UI]), login=True)
ck("the same with a token in the environment: the fix removes the variable, and shows no value",
   got["project_argus_ui_github"][2] == "give gh's own login access to acme/ui, or take that login away: remove "
   "GITHUB_TOKEN from the environment", got)
d, got, logins = project_lines(Cell([ELSEWHERE, UI]))
ck("a folder that is not on github.com keeps its fix, beside a reached one",
   got["project_argus_elsewhere_github"] == ("fix", "project argus: " + UNREAD.format(ELSEWHERE),
                                             "gh auth login, and add a GitHub remote")
   and got["project_argus_ui_github"][0] == "ok", got)
for name, cell in (("a failing check", BAD_CELL()), ("no capability", OFF_CELL())):
    d, got, logins = project_lines(cell)
    ck(f"{name}: the project lines are as before, and has_login is not asked",
       [v[2] for v in got.values()] == ["gh auth login, and add a GitHub remote"] * 2 and logins == [], got)
d, got, logins = project_lines(OK_CELL(), view="acme/x READ\n")
ck("a repository gh reads without write access keeps its fix",
   [v[2] for v in got.values()] == ["ask for write access to the repo"] * 2 and logins == [], got)


# ---------- e. the request to GitHub ----------

print("e. the request to GitHub")


def moved(request):
    return httpx.Response(301, headers={"location": "https://example.invalid/elsewhere"}, request=request)


with machine(github=moved) as m, mock.patch.dict(os.environ, OTHER_HOST):
    try:
        got = doctor.github_repo_status("acme/ui", TOKEN)
    except httpx.HTTPError as e:
        got = type(e).__name__
ck("a redirect is not followed: its status comes back, after one request",
   got == 301 and len(m.github) == 1 and m.refused == [], (got, m.refused))
request = m.github[0]
ck("a GET of the repository on api.github.com, whatever the environment names",
   request.method == "GET" and str(request.url) == "https://api.github.com/repos/acme/ui", str(request.url))
ck("the token is sent as a bearer, with GitHub's accept and version headers",
   request.headers.get("authorization") == f"Bearer {TOKEN}"
   and request.headers.get("accept") == "application/vnd.github+json"
   and request.headers.get("x-github-api-version") == "2022-11-28")
ck("20 seconds for every part of the request",
   request.extensions.get("timeout") == dict.fromkeys(("connect", "read", "write", "pool"), 20),
   request.extensions.get("timeout"))


# ---------- f. end to end ----------

print("f. end to end")
SCHEMA = json.loads((ROOT / "tares" / "crew" / "schemas" / "doctor.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
ck("the schema's id description names github_console",
   "github_console" in SCHEMA["properties"]["checks"]["items"]["properties"]["id"]["description"])
crew = SCRATCH / "crew"
crew.mkdir()
state.atomic_write_text(crew / "crew.json", "{}\n")
plugin = SCRATCH / "plugin"
(plugin / ".claude-plugin").mkdir(parents=True)
(plugin / ".claude-plugin" / "plugin.json").write_text('{"name": "tares", "version": "1.0.0"}\n', encoding="utf-8")


def run_doctor(world, json_mode=True, switched_off=False):
    """`doctor [--json]` through cmd_doctor against a fake cell: (exit code, stdout, what the machine saw)."""
    srv, url = fake_tares.serve(world)
    log = SCRATCH / "urls.log"
    log.unlink(missing_ok=True)
    env = {**os.environ, "TARESD_URL": url, "TARES_FACTORY_MCP_URL": f"{url}/mcp", "TARES_PLUGIN_DIR": str(plugin),
           "TARES_FAKE_PS_DIR": str(EMPTY), "TARES_URL_LOG": str(log)}
    out, err = io.StringIO(), io.StringIO()
    off = (mock.patch.object(doctor.Doctor, "check_github_console", lambda self, c: None) if switched_off
           else contextlib.nullcontext())
    try:
        with machine(cell_port=srv.server_address[1]) as m, off, mock.patch.dict(os.environ, env), \
                mock.patch.object(doctor, "_run", runner()), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = doctor.cmd_doctor(["doctor", *(["--json"] if json_mode else [])], env)
    finally:
        srv.shutdown()
        srv.server_close()
    PRODUCED.extend((out.getvalue(), err.getvalue()))
    m.urls = log.read_text(encoding="utf-8") if log.exists() else ""
    m.port = srv.server_address[1]
    return code, out.getvalue(), m


def world(answer=GOOD, caps=True):
    w = fake_tares.World(projects=[{"id": "uc_1", "name": "argus", "repos": [{"path": CORE}, {"path": UI}]}])
    if caps:
        w.capabilities.append("github_token")
    w.routes[TOKEN_ROUTE] = answer
    return w


def document(out):
    doc = json.loads(out)
    return doc, {c["id"]: c for c in doc["checks"]}


def invalid(doc):
    return [e.message for e in VALIDATOR.iter_errors(doc)]


w = world()
code, out, m = run_doctor(w)
doc, by = document(out)
ck("passing: the document validates against schemas/doctor.json", invalid(doc) == [], invalid(doc))
ck("passing: one github_console check, ok",
   [c["level"] for c in doc["checks"] if c["id"] == "github_console"] == ["ok"]
   and by["github_console"]["message"] == "GitHub through the console: 2 repos reachable", by.get("github_console"))
ids = [c["id"] for c in doc["checks"]]
ck("passing: it runs before the project checks",
   ids.index("github_console") < ids.index("project_argus_core_github"), ids)
ck("passing: gh, git identity and both projects are ok",
   all(by[i]["level"] == "ok" for i in ("gh", "git_identity", "project_argus_core_github",
                                         "project_argus_ui_github")), [by.get("gh"), by.get("git_identity")])
asked = [row for row in w.log if row[1] == TOKEN_ROUTE]
ck("passing: the cell is asked for a token for each repository",
   len(asked) == 2 and "token?repo=Acme/core" in m.urls and "token?repo=acme/ui" in m.urls, (asked, m.urls))
ck("passing: no check asks for a gh sign in or a global git identity",
   not [c["fix"] for c in doc["checks"] if any(s in c["fix"] + c["message"] for s in SIGN_IN)],
   [c["fix"] for c in doc["checks"] if c["fix"]])
ck("passing: nothing but the fake cell and api.github.com was asked", m.refused == [] and len(m.github) == 2, m.refused)
ck("the exit code is 1 exactly when a check is a fix", code == (0 if doc["ok"] else 1), (code, doc["ok"]))
code, out, m = run_doctor(world(), json_mode=False)
ck("passing, as text: the line, once", out.splitlines().count(
    "  ok    GitHub through the console: 2 repos reachable") == 1, out)
ck("passing, as text: no line asks for a gh sign in or a global git identity",
   not [s for s in SIGN_IN if s in out], out)

for label, answer in (("a body that is not an object", [TOKEN]), ("an empty answer", {})):
    code, out, m = run_doctor(world(answer))
    doc, by = document(out)
    ck(f"{label}: the document validates, and the check is a fix",
       invalid(doc) == [] and by["github_console"]["level"] == "fix" and by["github_console"]["ok"] is False
       and "no usable token" in by["github_console"]["message"] and doc["ok"] is False and code == 1 and not m.github,
       by.get("github_console"))
    ck(f"{label}: gh and the projects keep their fixes",
       by["gh"]["fix"] == "gh auth login" and by["project_argus_ui_github"]["fix"]
       == "gh auth login, and add a GitHub remote" and by["git_identity"]["level"] == "warn", by["gh"])

w = world(caps=False)
code, out, m = run_doctor(w)
doc, by = document(out)
ck("without the capability: no github_console check, no token request, nothing sent to GitHub",
   "github_console" not in by and not [r for r in w.log if r[1] == TOKEN_ROUTE] and m.github == []
   and m.logins == [] and invalid(doc) == [], (m.github, m.logins))
ck("without the capability: gh, the identity and the projects ask as before",
   by["gh"]["fix"] == "gh auth login" and by["git_identity"]["fix"].startswith("git config --global")
   and by["project_argus_core_github"]["fix"] == "gh auth login, and add a GitHub remote", by["gh"])
first_port = str(m.port)
code_off, out_off, m_off = run_doctor(world(caps=False), switched_off=True)
ck("without the capability: the output is that of a doctor that has no such check",
   code == code_off and out.replace(first_port, "PORT") == out_off.replace(str(m_off.port), "PORT"),
   (out, out_off))


# ---------- g. no token anywhere ----------

print("g. no token anywhere")
marks = ["gh" + "s_", "gh" + "u_", "github" + "_pat"]
everything = "\n".join(str(t) for t in PRODUCED)
ck("the outputs and log records were collected", "GitHub through the console" in everything and len(PRODUCED) > 100,
   len(PRODUCED))
ck("no token in any message, fix, printed text or log record",
   not [mark for mark in marks if mark in everything], [mark for mark in marks if mark in everything])
files = [p for p in SCRATCH.rglob("*") if p.is_file()]
leaks = [str(p) for p in files if any(mark.encode() in p.read_bytes() for mark in marks)]
ck("no token in any file under the scratch folder", len(files) > 0 and leaks == [], leaks)

shutil.rmtree(SCRATCH, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

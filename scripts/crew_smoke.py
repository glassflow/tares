#!/usr/bin/env python3
"""Smoke run of the Python crew engine against a real Claude Code, on a Mac or Linux box.

It builds a throwaway crew end to end and exits 0 only when the crew opened a pull request:

  1. a scratch Tares (its own port, data dir and MCP endpoint, auth off, never the hosted cell),
  2. `tares factory crew up --yes` with a scratch crew home and a crew called smoke-<pid>,
  3. a one-ticket project handed over on a throwaway GitHub repo (default glassflow/tares-crew-smoke),
  4. wait for the ticket's pull request (confirmed on GitHub, not only on the ticket),
  5. `tares factory crew down` for that scratch crew, then the scratch Tares is stopped.

Run it through the heavy queue, with a hard timeout, because every run spends Claude usage:

  tares factory heavy 2400 python scripts/crew_smoke.py

--dry-run swaps the fake Claude Code (tests/factory/fake-claude) in, inside a scratch HOME: it checks the
plumbing (daemon, MCP, crew up, hand-over, roster, crew down), opens no pull request and spends nothing.
It exits 10, never 0, because no pull request was checked.

Isolation, so a smoke run cannot touch the live crew (TR-1164):
  - every crew command gets an explicit scratch crew home (FACTORY_HOME and FACTORY_CREW_HOME) and the
    crew name smoke-<pid> (FACTORY_CREW_NAME); inherited FACTORY_*, TARES_* and TARESD_URL are dropped;
  - the script refuses to start when the crew home is, holds or sits inside ~/factory, or the crew is
    called "crew" or does not start with "smoke-";
  - the keyring is pinned to the fail backend, so nothing reaches the Keychain;
  - the teardown stops only this crew's recorded stations (crew down), then any session still named
    <crew>-orchestrator, -reviewer, -releaser or <crew>-hello-smoke-build-* (never a longer crew name that merely starts with this one), and only the scratch daemon and MCP processes this script started.

The one edit outside the scratch folder: Claude Code starts no session in a folder it has not trusted, so
the script adds the scratch crew home, the clone itself (its parent folder does not count) and the worktrees folder to ~/.claude.json
(hasTrustDialogAccepted) and removes those entries at the end. A real run therefore rewrites that file (a
symlink is followed, and a change made by a running Claude Code meanwhile is re-read and kept). Exit codes: 0 PR exists, 1 failed, 2 refused to start, 3 timed out, 10 dry run ok.
"""
from __future__ import annotations

import argparse
import contextlib
import re
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FACTORY_DIR = ROOT / "tests" / "factory"
DEFAULT_REPO = "glassflow/tares-crew-smoke"
EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_TIMEOUT, EXIT_DRY_OK = 0, 1, 2, 3, 10
MAX_POLL_ERRORS = 5
PROJECT_NAME = "hello-smoke"
TARES = [sys.executable, "-c", "import sys; sys.argv[0] = 'tares'; from tares.cli import main; main()"]


@contextlib.contextmanager
def ignoring_signals():
    """No SIGTERM or Ctrl-C while the body runs: a second one must not cut the teardown short and leave the
    trust entries and the scratch daemons behind. Only the main thread can change handlers."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    old = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


class Refused(Exception):
    """The run would reach the live crew or an unnamed crew: nothing was started."""


class Failed(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Isolation

def live_crew_homes(env_home: str | None = None) -> list:
    """Every place the live crew's home could be: ~/factory by the HOME in the environment and by the
    account's own home directory."""
    homes = {env_home or os.path.expanduser("~")}
    try:
        homes.add(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        pass
    return sorted({os.path.realpath(os.path.join(h, "factory")) for h in homes if h})


def check_isolation(crew_home, crew_name: str, live_homes: list | None = None) -> None:
    """Refuse (Refused) unless the crew home is a scratch folder and the crew name is a smoke- name."""
    live_homes = live_homes if live_homes is not None else live_crew_homes()
    home = os.path.realpath(str(crew_home))
    for live in live_homes:
        if home == live or home.startswith(live + os.sep) or live.startswith(home + os.sep):
            raise Refused(f"the crew home {home} is, holds or sits inside the live crew home {live}")
    if crew_name == "crew":
        raise Refused('the crew name is "crew", the live crew\'s name')
    if not crew_name.startswith("smoke-"):
        raise Refused(f'the crew name {crew_name!r} does not start with "smoke-"')


def scratch_env(base: dict, *, crew_home: Path, crew_name: str, tares_home: Path, port: int, mcp_port: int,
                fake_claude: dict | None = None) -> dict:
    """The environment of every command the script runs: nothing inherited from a station's FACTORY_*, TARES_*
    or TARESD_URL, then the scratch values spelled out."""
    env = {k: v for k, v in base.items()
           if not k.startswith(("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_")) and k not in ("TARESD_URL", "NAVFLOW_DB")}
    url = f"http://127.0.0.1:{port}"
    env.update(
        FACTORY_HOME=str(crew_home), FACTORY_CREW_HOME=str(crew_home), FACTORY_CREW_NAME=crew_name,
        TARES_HOME=str(tares_home), TARESD_URL=url, TARES_FACTORY_URL=url,
        TARES_FACTORY_MCP_URL=f"http://127.0.0.1:{mcp_port}/mcp",
        TARES_OTLP_GRPC_PORT="off", FACTORY_NO_WATCH="1",
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
        PYTHONPATH=str(ROOT) + (os.pathsep + base["PYTHONPATH"] if base.get("PYTHONPATH") else ""),
    )
    env.update(fake_claude or {})
    return env


# ---------------------------------------------------------------------------------------------
# Claude Code's trust file

def _stamp(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def edit_claude_json(path: Path, mutate) -> None:
    """Read Claude Code's config, let mutate(doc) change it (it returns True when it did), replace the file atomically.

    A running Claude Code rewrites this file too, so the file is read again when it changed between the read and
    the replace (up to 5 tries), which leaves only the gap between that last check and os.replace. A symlinked
    file is edited where it points, so the link stays a link."""
    for _ in range(5):
        real = Path(os.path.realpath(path))
        before = _stamp(real)
        doc = {}
        if before is not None:
            doc = json.loads(real.read_text(encoding="utf-8") or "{}")
        if not mutate(doc):
            return
        tmp = real.with_name(real.name + f".smoke{os.getpid()}")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        try:
            shutil.copymode(real, tmp)
        except OSError:
            pass
        if _stamp(real) == before:
            os.replace(tmp, real)
            return
        tmp.unlink()
    raise OSError(f"{path} kept changing under the edit")


def trust_folder(claude_json: Path, folder: str) -> None:
    """Mark folder trusted in Claude Code's config (hasTrustDialogAccepted)."""
    def add(doc):
        doc.setdefault("projects", {}).setdefault(folder, {})["hasTrustDialogAccepted"] = True
        return True
    edit_claude_json(claude_json, add)


def untrust_folder(claude_json: Path, folder: str) -> None:
    """Remove the entry trust_folder added (the folder is ours alone, so the whole entry goes)."""
    if not claude_json.exists():
        return
    edit_claude_json(claude_json, lambda doc: isinstance(doc.get("projects"), dict)
                     and doc["projects"].pop(folder, None) is not None)


# ---------------------------------------------------------------------------------------------
# Small helpers

T0 = time.time()


def say(msg: str) -> None:
    s = int(time.time() - T0)
    print(f"[{s // 60:02d}:{s % 60:02d}] {msg}", flush=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body=None, timeout: float = 20):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise Failed(f"{method} {url} answered {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Failed(f"{method} {url} failed: {e}") from None
    return json.loads(raw) if raw.strip() else {}


def wait_until(what: str, fn, seconds: float, every: float = 0.5):
    end = time.time() + seconds
    last = None
    while time.time() < end:
        try:
            got = fn()
            if got:
                return got
        except (Failed, OSError, ValueError) as e:
            last = e
        time.sleep(every)
    raise Failed(f"{what} not ready after {int(seconds)}s" + (f" ({last})" if last else ""))


def run(cmd: list, env: dict, timeout: float, cwd=None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise Failed(f"{' '.join(map(str, cmd[:6]))} timed out after {int(timeout)}s") from e


def find_claude(env: dict) -> str | None:
    """CLAUDE_BIN, else the first `claude` on PATH that is not a cmux shim (the crew engine skips those too)."""
    if env.get("CLAUDE_BIN"):
        return env["CLAUDE_BIN"]
    for d in (env.get("PATH") or "").split(os.pathsep):
        c = os.path.join(d, "claude")
        if "cmux" not in d and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def rows(value, key: str) -> list:
    if isinstance(value, dict):
        value = value.get(key, [])
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------------------------
# The run

class Smoke:
    def __init__(self, args):
        self.args = args
        self.dry = args.dry_run
        self.repo = args.repo
        self.pid = os.getpid()
        self.crew_name = args.crew_name or f"smoke-{self.pid}"
        self.real_home = os.path.expanduser("~")
        self.work = Path(os.path.realpath(args.workdir or tempfile.mkdtemp(prefix="crew-smoke-")))
        self.work.mkdir(parents=True, exist_ok=True)
        self.crew_home = Path(args.crew_home) if args.crew_home else self.work / "crew"
        self.hdir = self.work / "home" if self.dry else Path(self.real_home)
        self.port, self.mcp_port = free_port(), free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.procs: list = []
        self.trusted = []
        self.env: dict = {}
        self.started = time.time()
        self.project = ""
        self.lock = threading.Lock()
        self.torn_down = False
        self.watchdog_fired = False
        self.poll = 30

    # -- setup --

    def fake_env(self) -> dict:
        state = self.work / "claude-state"
        state.mkdir(parents=True, exist_ok=True)
        for f in ("fake-claude", "fake-ps"):
            (FACTORY_DIR / f).chmod(0o755)
        (self.work / "home").mkdir(parents=True, exist_ok=True)
        return dict(CLAUDE_BIN=str(FACTORY_DIR / "fake-claude"), FAKE_CLAUDE_STATE=str(state), FACTORY_PAUSE="0",
                    HOME=str(self.hdir), GIT_CONFIG_GLOBAL=str(self.work / "gitconfig"), GIT_CONFIG_NOSYSTEM="1")

    def prepare(self) -> None:
        check_isolation(self.crew_home, self.crew_name, live_crew_homes(self.real_home))
        self.crew_home.mkdir(parents=True, exist_ok=True)
        tares_home = self.work / "tares-home"
        tares_home.mkdir(parents=True, exist_ok=True)
        self.env = scratch_env(dict(os.environ), crew_home=self.crew_home, crew_name=self.crew_name, tares_home=tares_home,
                               port=self.port, mcp_port=self.mcp_port, fake_claude=self.fake_env() if self.dry else None)
        say(f"scratch run in {self.work}: crew {self.crew_name}, Tares {self.url}, MCP port {self.mcp_port}, "
            f"crew home {self.crew_home}" + (", FAKE Claude Code (dry run)" if self.dry else ""))

    def spawn(self, name: str, cmd: list) -> subprocess.Popen:
        log = open(self.work / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, env=self.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def start_tares(self) -> None:
        d = self.spawn("tares-up", TARES + ["up", "--host", "127.0.0.1", "--port", str(self.port), "--data-dir",
                                            str(self.work / "tares-home"), "--edition", "factory"])
        wait_until("scratch Tares", lambda: d.poll() is None and "integrations" in
                   (http("GET", self.url + "/health").get("capabilities") or []), 90)
        m = self.spawn("tares-mcp", TARES + ["mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port",
                                             str(self.mcp_port), "--taresd", self.url])
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "crew-smoke", "version": "1"}}}

        def mcp_up():
            if m.poll() is not None:
                raise Failed("the scratch MCP server exited")
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.mcp_port}/mcp", data=json.dumps(init).encode(), method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return "serverInfo" in r.read().decode("utf-8", "replace")

        wait_until("scratch MCP server", mcp_up, 60)
        say("scratch Tares and MCP are up")

    def tares_factory(self, *argv: str, timeout: float = 300) -> subprocess.CompletedProcess:
        return run(TARES + ["factory", *argv], self.env, timeout)

    def settings(self) -> None:
        http("PUT", self.url + "/api/crew/settings", {"autonomy": "L3-review", "releaser": False, "challenger": False,
                                                       "builders_max": 1})

    def clone(self) -> Path:
        dest = self.work / "repos" / self.repo.split("/")[-1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.dry:
            git = ["git", "-c", "user.name=smoke", "-c", "user.email=smoke@example.invalid"]
            for c in (["init", "-q", "-b", "main", str(dest)],):
                run(git + c, self.env, 60)
            (dest / "README.md").write_text("# throwaway\n", encoding="utf-8")
            run(git + ["-C", str(dest), "add", "."], self.env, 60)
            run(git + ["-C", str(dest), "commit", "-q", "-m", "init"], self.env, 60)
        else:
            r = run(["gh", "repo", "clone", self.repo, str(dest)], self.env, 180)
            if r.returncode:
                raise Failed(f"gh repo clone {self.repo} failed: {(r.stderr or r.stdout).strip()[:300]}")
        return dest

    def hand_over(self, repo_dir: Path) -> None:
        name = PROJECT_NAME
        p = http("POST", self.url + "/api/projects", {"template": "custom", "kind": "software_factory", "name": name,
                                                      "objects": [], "goal": "Smoke test of the crew engine"})
        self.project = p["id"]
        base = f"{self.url}/api/projects/{self.project}"
        spec = (f"This is a smoke test of the crew engine, run by a script. One ticket: add a file SMOKE.md at the "
                f"root of {self.repo} whose only line is `smoke run {self.pid}`. Open a pull request from the branch "
                f"factory/<ref> and stop there. Do not merge it. Do not put questions on the desk: nobody answers; "
                f"record an assumption and go on.")
        http("POST", base + "/docs", {"kind": "spec", "title": "Spec: smoke", "body": spec})
        http("POST", base + "/docs", {"kind": "agents", "title": "AGENTS.md: smoke",
                                      "body": "Change only SMOKE.md. The repo has no tests; the check is that the file exists."})
        wd = http("POST", base + "/docs", {
            "kind": "working", "title": "Working: add SMOKE.md",
            "body": f"## Goal\n\nAdd SMOKE.md with the single line `smoke run {self.pid}`.\n\n## Definition of done\n\n"
                    f"- A pull request on {self.repo} that adds only SMOKE.md.\n- Nothing merged.\n\n## Progress\n"})
        http("POST", base + "/milestones", {"name": "M1 smoke", "goal": "A pull request exists.",
                                            "checks": [{"check": "test -f SMOKE.md", "expect": "exits 0"}]})
        http("POST", base + "/tickets", {"title": "Add SMOKE.md", "working_doc": wd["id"], "milestone": "M1 smoke",
                                         "repo": self.repo})
        http("POST", base + "/handover", {"repos": [{"path": str(repo_dir), "name": self.repo}], "autonomy": "L3-review"})
        say(f"project {name} ({self.project}) handed over on {self.repo}, autonomy L3-review")

    # -- the crew --

    def trust_folders(self) -> None:
        """The orchestrator runs in the crew home, the builder in the clone and in its worktrees, and Claude Code
        refuses a session in a folder it has not trusted. Our own folders only; teardown removes every entry."""
        trust = self.hdir / ".claude.json"
        for folder in (self.crew_home, self.work / "repos" / self.repo.split("/")[-1], self.crew_home / "worktrees"):
            Path(folder).mkdir(parents=True, exist_ok=True)
            trust_folder(trust, str(folder))
            self.trusted.append((trust, str(folder)))

    def crew_up(self) -> None:
        self.trust_folders()
        r = self.tares_factory("crew", "up", "--yes", "--no-watch", timeout=600)
        sys.stdout.write(r.stdout)
        sys.stdout.write(r.stderr)
        if r.returncode:
            raise Failed(f"crew up exited {r.returncode}")

    def roster(self) -> list:
        r = self.tares_factory("roster", "--json", timeout=60)
        try:
            return rows(json.loads(r.stdout), "stations")
        except ValueError:
            return []

    def tickets(self) -> list:
        return rows(http("GET", f"{self.url}/api/projects/{self.project}/tickets"), "tickets")

    def desk(self) -> list:
        try:
            return rows(http("GET", self.url + "/api/desk"), "items")
        except Failed:
            return []

    def github_pr(self) -> dict | None:
        """This run's pull request: from a factory/ branch, opened since the run began, and adding the line
        `smoke run <pid>` the ticket asks for. A second smoke run, or another crew on the same test repo, opens
        pull requests that fail the last test. A gh error raises Failed (wait_for_pr tolerates a few in a row)."""
        r = run(["gh", "pr", "list", "-R", self.repo, "--state", "all", "--limit", "30", "--json",
                 "number,url,headRefName,createdAt,state"], self.env, 60)
        if r.returncode:
            raise Failed(f"gh pr list failed: {(r.stderr or r.stdout).strip()[:200]}")
        since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started - 5))
        mine = re.compile(rf"^\+[`\s]*smoke run {self.pid}[`\s]*$", re.M)
        try:
            prs = json.loads(r.stdout or "[]")
        except ValueError:
            raise Failed("gh pr list printed no JSON") from None
        for pr in prs:
            if not (pr["headRefName"].startswith("factory/") and pr["createdAt"] >= since):
                continue
            d = run(["gh", "pr", "diff", str(pr["number"]), "-R", self.repo], self.env, 60)
            if d.returncode:
                raise Failed(f"gh pr diff {pr['number']} failed: {(d.stderr or d.stdout).strip()[:200]}")
            if mine.search(d.stdout):
                return pr
        return None

    def wait_for_pr(self, seconds: float) -> dict:
        """Poll until this run's pull request exists. One failed poll (a scratch Tares hiccup, a gh or roster
        timeout) is logged and tried again; MAX_POLL_ERRORS in a row end the run with Failed, so a lasting
        error (gh logged out) fails in minutes instead of waiting out the timeout."""
        end = time.time() + seconds
        last = ""
        gone = 0
        idle = 0
        errors = 0
        while time.time() < end:
            try:
                pr = self.github_pr()
                if pr:
                    return pr
                stations = self.roster()
                stages = {t.get("label") or t.get("id"): t.get("stage") for t in self.tickets()}
                desk = len(self.desk())
            except Failed as e:
                errors += 1
                say(f"poll {errors} of {MAX_POLL_ERRORS} failed: {e}")
                if errors >= MAX_POLL_ERRORS:
                    raise Failed(f"{errors} polls in a row failed, the last: {e}") from None
                time.sleep(self.poll)
                continue
            errors = 0
            line = (f"stations {', '.join(s['station'].replace(self.crew_name + '-', '') + ':' + str(s.get('turn')) for s in stations) or 'none'}"
                    f" | tickets {stages or 'none'} | desk {desk}")
            if line != last:
                say(line)
                last = line
            gone = gone + 1 if stations and all(s.get("turn") == "gone" for s in stations) else 0
            # every station finished its turn and none is a builder: nothing will build the ticket (a refused
            # builder spawn looks like this), so waiting out the timeout only spends the hour
            idle = idle + 1 if stations and all(s.get("turn") in ("done", "gone") for s in stations) \
                and not any("-build-" in s["station"] for s in stations) else 0
            if idle >= 10:
                raise Failed("every station has finished its turn, no builder was started and no pull request was "
                             f"opened; read {self.crew_home}/reports/{self.crew_name}-orchestrator.md")
            if gone >= 3:
                raise Failed("every station of the scratch crew has ended and no pull request was opened "
                             "(the watch is off, so nothing revives them)")
            time.sleep(self.poll)
        raise TimeoutError(f"no pull request after {int(seconds)}s")

    # -- the end --

    def leftover_sessions(self) -> list:
        claude = find_claude(self.env)
        if not claude:
            return []
        r = run([claude, "agents", "--json"], self.env, 60)
        try:
            mine = re.compile(re.escape(self.crew_name) + r"-(orchestrator|reviewer|releaser|" + re.escape(PROJECT_NAME) + r"-build-.+)$")
            return [x for x in rows(json.loads(r.stdout), "agents") if mine.match(str(x.get("name", "")))]
        except ValueError:
            return []

    def teardown(self) -> None:
        with ignoring_signals():
            self._teardown()

    def _teardown(self) -> None:
        with self.lock:
            if self.torn_down:
                return
            self.torn_down = True
        say("teardown")
        if self.env and (self.crew_home / "crew.json").is_file():
            try:
                r = self.tares_factory("crew", "down", timeout=180)
                say(f"crew down: {(r.stdout + r.stderr).strip().splitlines()[-1] if (r.stdout + r.stderr).strip() else r.returncode}")
            except Failed as e:
                say(f"crew down failed: {e}")
        try:
            claude = find_claude(self.env)
            for s in self.leftover_sessions():
                say(f"stopping leftover session {s.get('name')}")
                for verb in ("stop", "rm"):
                    run([claude, verb, str(s.get("id"))], self.env, 60)
        except Failed as e:
            say(f"leftover check failed: {e}")
        for p in reversed(self.procs):
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        for p in self.procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
        for entry in self.trusted:
            try:
                untrust_folder(*entry)
            except (OSError, ValueError) as e:
                say(f"could not remove the trust entry for {entry[1]} from {entry[0]}: {e}")

    def close_pr(self, pr: dict) -> None:
        r = run(["gh", "pr", "close", str(pr["number"]), "-R", self.repo, "--delete-branch", "--comment",
                 "Closed by scripts/crew_smoke.py: a smoke run, never merged."], self.env, 60)
        say(f"closed PR {pr['url']}" if not r.returncode else f"could not close {pr['url']}: {r.stderr.strip()}")


def fire_watchdog(s: "Smoke") -> None:
    """The hard deadline: tell the main thread to stop (SIGTERM, which interrupts a sleep or a wait for a command),
    so the one teardown that runs is main's, in the main thread, and the process cannot exit under it."""
    say("watchdog: hard deadline, stopping")
    s.watchdog_fired = True
    os.kill(os.getpid(), signal.SIGTERM)


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"the throwaway GitHub repo (default {DEFAULT_REPO})")
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for the PR (default 1800)")
    ap.add_argument("--dry-run", action="store_true", help="fake Claude Code, scratch HOME, no PR; exits 10 when the plumbing works")
    ap.add_argument("--close-pr", action="store_true", help="close the smoke PR and delete its branch at the end (never merges)")
    ap.add_argument("--keep", action="store_true", help="keep the scratch folder (logs are in it)")
    ap.add_argument("--workdir", help="the scratch folder (default: a new temp folder)")
    ap.add_argument("--crew-home", help="the crew home (default <workdir>/crew); refused when it is the live one")
    ap.add_argument("--crew-name", help="the crew name (default smoke-<pid>); must start with smoke-")
    args = ap.parse_args(argv)
    s = Smoke(args)
    try:
        s.prepare()
    except Refused as e:
        print(f"crew_smoke: refusing to start: {e}", file=sys.stderr)
        if not args.workdir:
            shutil.rmtree(s.work, ignore_errors=True)
        return EXIT_REFUSED

    def stop(signum, _frame=None):
        raise KeyboardInterrupt(f"signal {signum}")

    signal.signal(signal.SIGTERM, stop)
    watchdog = threading.Timer(args.timeout + 600, fire_watchdog, [s])
    watchdog.daemon = True
    watchdog.start()
    code, pr = EXIT_FAIL, None
    try:
        s.start_tares()
        s.settings()
        repo_dir = s.clone()
        s.hand_over(repo_dir)
        s.crew_up()
        names = [x["station"] for x in s.roster()]
        say(f"stations: {', '.join(names) or 'none'}")
        if not any(n == f"{s.crew_name}-orchestrator" for n in names):
            raise Failed("the orchestrator is not on the scratch crew's roster")
        if s.dry:
            if not any(p.get("name") == PROJECT_NAME or p.get("id") == s.project for p in
                       rows(http("GET", s.url + "/api/crew/projects"), "projects")):
                raise Failed("the handed-over project is not on GET /api/crew/projects")
            say("dry run ok: plumbing works, no pull request checked")
            code = EXIT_DRY_OK
        else:
            pr = s.wait_for_pr(args.timeout)
            say(f"PR exists: {pr['url']}")
            code = EXIT_OK
    except TimeoutError as e:
        say(str(e))
        code = EXIT_TIMEOUT
    except (Failed, KeyboardInterrupt) as e:
        say(f"failed: {e}")
        code = EXIT_FAIL
    finally:
        with ignoring_signals():
            watchdog.cancel()
            s.teardown()
            if pr and args.close_pr:
                s.close_pr(pr)
            if args.keep or code not in (EXIT_OK, EXIT_DRY_OK):
                say(f"scratch folder kept: {s.work}")
            else:
                shutil.rmtree(s.work, ignore_errors=True)
    if s.watchdog_fired:
        code = 124
    if pr:
        print(f"PR: {pr['url']}")
    return code


if __name__ == "__main__":
    sys.exit(main())

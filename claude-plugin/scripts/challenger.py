#!/usr/bin/env python3
"""The challenger hooks: a second model (OpenAI Codex CLI, on this laptop) challenges Claude's plan
and every commit in a session the user marked as a challenger session.

Runs as three Claude Code hooks, all gated on the per-session marker the shipper writes when Claude
calls the `set_session_flow` MCP tool (see ship.py). Nothing here ever asks Tares whether to run.

- PostToolUse(ExitPlanMode): Codex critiques the plan. Findings come back to Claude as context so
  it can revise before the user sees the plan. Never blocks: the user approves the plan anyway.
- PostToolUse(Bash) after a successful `git commit`: `codex exec --sandbox read-only review --json --commit HEAD`.
  P1/P2 findings block Claude with the review (strict mode, the default) or come back as context
  (advise mode). Errors, timeouts and inconclusive reviews never block.
- Stop: while the last review of this session's commit is FAIL, keep Claude in the fix loop
  (amend while unpushed), capped at MAX_LOOPS turn ends and MAX_ROUNDS review rounds.

Every review is appended to .git/tares-challenger-reviews.jsonl and shipped to Tares as a
challenge_plan / challenge_commit line on the session timeline (best effort; a failed POST never
changes the verdict). State lives under .git/, never in the working tree.

The mechanism (post-commit review, priority-tagged findings, blocking fix loop, plan critique,
state in .git) follows andreidavid/codex-review (MIT); this is a rewrite in Python that adds the
session gate and the Tares record.

`challenger.py waive [n|all]` (the /tares:challenger-waive command) suppresses a disputed finding.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ship import CHALLENGER, _opt, config, read_flow, ship_lines, synthetic_line  # noqa: E402

MAX_LOOPS = int(os.environ.get("TARES_CHALLENGER_MAX_LOOPS", "5"))      # Stop-hook continuations
MAX_ROUNDS = int(os.environ.get("TARES_CHALLENGER_MAX_ROUNDS", "8"))    # consecutive FAIL reviews
TIMEOUT = min(int(os.environ.get("TARES_CHALLENGER_TIMEOUT", "600")), 880)
MAX_OUTPUT = 8000
FAIL_MAX_AGE = 3600
RUNNING_MAX_AGE = 16 * 60
STATE = "tares-challenger-state"
LOOPS = "tares-challenger-loop-count"
ROUNDS = "tares-challenger-round-count"
HISTORY = "tares-challenger-reviews.jsonl"
WAIVED = "tares-challenger-waived"
SKIP = "tares-challenger-skip"

# `git commit`, with any options in between (`git -c user.name="$(git config user.name || echo x)"
# commit`, `git -C dir commit`); the response check below drops the failed ones
_COMMIT_RE = re.compile(r"(^|[^\w])git\s.*?\bcommit\b|(^|[^\w])git\s+commit\b", re.S)
_FINDING_RE = re.compile(r"^\s*(?:[-*>]\s+)?\[(P[123])\]\s+(\S.*?)\s*$")

PLAN_BLOCKING = ("P1",)
PLAN_PROMPT = (
    "Review the implementation plan at {path}. Read it fully, then critique from these angles: "
    "missing steps or gaps; design flaws or wrong-architecture choices; scope creep or "
    "under-scoping; risks and unhandled edge cases; verification that is insufficient or missing. "
    "Format findings one per line using priorities: [P1] = critical (the plan will fail or produce "
    "wrong outcomes as written), [P2] = important (will cause rework), [P3] = advisory. If nothing "
    "material is wrong, say so explicitly."
)


def out(obj: dict) -> None:
    print(json.dumps(obj))


def context(event: str, text: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text[:MAX_OUTPUT]}}


def block(reason: str) -> dict:
    return {"decision": "block", "reason": reason[:MAX_OUTPUT]}


def codex_bin() -> str:
    return (_opt("codex_bin") or os.environ.get("CODEX_BIN") or shutil.which("codex") or "").strip()


def sandbox_args() -> list:
    mode = (_opt("codex_sandbox") or "").strip()
    return {"workspace-write": ["--sandbox", "workspace-write"],
            "danger-full-access": ["--sandbox", "danger-full-access"]}.get(mode, ["--sandbox", "read-only"])


# ── Codex ────────────────────────────────────────────────────────────────────

def run_codex(args: list, cwd: str) -> dict:
    """Run the Codex CLI; return {prose, findings, verdict, exit, duration_seconds, activity}.
    Parses the JSONL event stream (`--json`); falls back to the raw text of older CLIs."""
    started = time.time()
    try:
        p = subprocess.run([codex_bin(), *args], cwd=cwd, capture_output=True, text=True,
                           timeout=TIMEOUT)
        exit_code, stdout, stderr = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return {"prose": f"(review timed out after {TIMEOUT}s)", "findings": [], "verdict": "TIMEOUT",
                "exit": 124, "duration_seconds": round(time.time() - started, 1), "activity": False}
    except Exception as e:
        return {"prose": f"(could not run codex: {type(e).__name__}: {e})", "findings": [],
                "verdict": "ERROR", "exit": 1, "duration_seconds": round(time.time() - started, 1),
                "activity": False}
    duration = round(time.time() - started, 1)
    prose, activity, jsonl = "", False, False
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict) or "type" not in ev:
            continue
        jsonl = True
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        if ev.get("type") == "item.completed":
            if item.get("type") == "agent_message" and item.get("text"):
                prose = str(item["text"])
            if (item.get("type") == "command_execution" and item.get("exit_code", 1) == 0) or \
               (item.get("type") == "mcp_tool_call" and item.get("status") == "completed"):
                activity = True
    if not jsonl:
        text = re.sub(r"\x1b\[[0-9;]*m", "", stdout)
        m = re.search(r"^codex$", text, re.M)
        prose = text[m.end():].strip() if m else text.strip()
        activity = True   # the legacy text path cannot tell; do not call it inconclusive
    if exit_code != 0 and not prose:
        return {"prose": f"(codex exited {exit_code}; stderr: {stderr.strip()[:1500] or '<empty>'})",
                "findings": [], "verdict": "ERROR", "exit": exit_code,
                "duration_seconds": duration, "activity": activity}
    if not prose:
        prose = f"(Codex produced no message; stderr: {stderr.strip()[:1500] or '<empty>'})"
    findings = [{"priority": m.group(1), "title": m.group(2)}
                for m in (_FINDING_RE.match(l) for l in prose.splitlines()) if m]
    return {"prose": prose, "findings": findings, "verdict": "", "exit": exit_code,
            "duration_seconds": duration, "activity": activity}


def waive_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(:|line)\s*\d+", "", title)).strip().lower()


def apply_waivers(gitdir: str, findings: list) -> None:
    try:
        keys = {l.strip() for l in open(os.path.join(gitdir, WAIVED)) if l.strip() and not l.startswith("#")}
    except OSError:
        keys = set()
    for f in findings:
        f["waive_key"] = waive_key(f["title"])
        f["waived"] = f["waive_key"] in keys


def counts(findings: list, blocking_levels=("P1", "P2")) -> tuple:
    blocking = sum(1 for f in findings if f["priority"] in blocking_levels and not f.get("waived"))
    waived = sum(1 for f in findings if f.get("waived"))
    return len(findings), blocking, waived


# ── repo and state ───────────────────────────────────────────────────────────

def repo_root(cwd: str, command: str) -> str:
    """The repo the commit landed in: honours `git -C <dir>` and a leading `cd <dir>`."""
    cands = []
    m = re.search(r"git\s+(?:-C|--git-dir)\s+([\"']?)([^\s\"']+)\1", command)
    if m:
        cands.append(os.path.join(cwd, m.group(2)))
    for seg in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        m = re.match(r"\s*cd\s+([\"']?)([^\s\"']+)\1", seg)
        if m:
            cands.append(os.path.join(cwd, os.path.expanduser(m.group(2))))
    cands.append(cwd)
    for c in cands:
        try:
            p = subprocess.run(["git", "-C", c, "rev-parse", "--show-toplevel"], capture_output=True,
                               text=True, timeout=5)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except Exception:
            pass
    return ""


def git(root: str, *args: str) -> str:
    try:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def gitdir(root: str) -> str:
    d = git(root, "rev-parse", "--git-common-dir")
    return d if os.path.isabs(d) else os.path.join(root, d)


def sfx(session_id: str) -> str:
    return f".{session_id}" if session_id else ""


def read_state(gitdir_: str, session_id: str) -> dict:
    try:
        parts = open(os.path.join(gitdir_, STATE + sfx(session_id))).read().split()
        return {"verdict": parts[0], "sha": parts[1] if len(parts) > 1 else "",
                "ts": int(parts[2]) if len(parts) > 2 else 0}
    except Exception:
        return {}


def write_state(gitdir_: str, session_id: str, verdict: str, sha: str) -> None:
    with open(os.path.join(gitdir_, STATE + sfx(session_id)), "w") as f:
        f.write(f"{verdict} {sha} {int(time.time())} {session_id}\n")


def clear_state(gitdir_: str, session_id: str, rounds: bool = False) -> None:
    names = [STATE, LOOPS] + ([ROUNDS] if rounds else [])
    for n in names:
        try:
            os.remove(os.path.join(gitdir_, n + sfx(session_id)))
        except OSError:
            pass


def read_count(path: str) -> tuple:
    try:
        parts = open(path).read().split()
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except Exception:
        return 0, 0


def last_reviewed_sha(gitdir_: str, session_id: str) -> str:
    sha = ""
    try:
        for line in open(os.path.join(gitdir_, HISTORY)):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("session_id") == session_id:
                sha = e.get("sha", "")
    except OSError:
        pass
    return sha


def record(gitdir_: str, entry: dict) -> None:
    try:
        with open(os.path.join(gitdir_, HISTORY), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def ship_challenge(cfg: dict, hook: dict, typ: str, challenge: dict) -> None:
    """A challenge line, stamped like the session's own lines (its tares-factory station and
    project), so Tares ties it to the ticket whose branch it was on (TR-440)."""
    try:
        from ship import read_project, stamp_station
        line = synthetic_line(hook, typ, CHALLENGER, challenge=challenge)
        project = read_project(cfg["data_dir"], str(hook.get("session_id") or "")) \
            or os.environ.get("TARES_PROJECT", "").strip()
        if project:
            line["tares_project"] = project
        ship_lines(cfg, stamp_station([line]))
    except Exception:
        pass


# ── plan review ──────────────────────────────────────────────────────────────

def plan_text(hook: dict) -> tuple:
    """(plan markdown, its name). ExitPlanMode carries the plan in tool_input on current Claude
    Code; older builds only write ~/.claude/plans/<name>.md, so fall back to the newest file."""
    inp = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    plan = inp.get("plan")
    if isinstance(plan, str) and plan.strip():
        return plan, "plan from ExitPlanMode"
    d = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "plans")
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")),
                       key=os.path.getmtime, reverse=True)
    except OSError:
        files = []
    if not files:
        return "", ""
    try:
        return open(files[0]).read(), os.path.basename(files[0])
    except OSError:
        return "", ""


def review_plan(hook: dict, cfg: dict) -> dict:
    text, name = plan_text(hook)
    if not text.strip():
        return {}
    cwd = hook.get("cwd") or os.getcwd()
    with tempfile.NamedTemporaryFile("w", suffix=".md", prefix="tares-plan-", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        r = run_codex(["exec", *sandbox_args(), "--skip-git-repo-check", "--json",
                       PLAN_PROMPT.format(path=path)], cwd)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    # a plan is a sketch: only P1 counts as blocking here (P2 on a commit blocks, on a plan it
    # is advice), otherwise every plan comes back with a long list of "blocking" items
    n, blocking, _ = counts(r["findings"], PLAN_BLOCKING)
    verdict = r["verdict"] or ("FAIL" if blocking else "PASS")
    ship_challenge(cfg, hook, "challenge_plan", {
        "verdict": verdict, "plan": name, "finding_count": n, "blocking_count": blocking,
        "waived_count": 0, "duration_seconds": r["duration_seconds"], "prose": r["prose"][:4000],
        "findings": [{"priority": f["priority"], "title": f["title"], "waived": False} for f in r["findings"]]})
    if verdict in ("TIMEOUT", "ERROR"):
        return context("PostToolUse", f"Codex could not review the plan ({verdict.lower()}): {r['prose']} "
                       "You are not blocked; mention it to the user.")
    head = (f"Codex challenged the plan and found {blocking} blocking finding(s) ({n} total). Revise "
            "the plan for the P1 items before presenting it, or tell the user why you disagree. "
            "P2 and P3 items are advice; take what is worth it."
            if blocking else
            f"Codex challenged the plan: no blocking findings ({n} advisory). Mention that the plan "
            "was challenged when you present it.")
    return context("PostToolUse", head + "\n\n" + r["prose"])


# ── commit review ────────────────────────────────────────────────────────────

def is_commit(hook: dict) -> bool:
    inp = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    cmd = str(inp.get("command") or "")
    if not cmd or not _COMMIT_RE.search(cmd):
        return False
    resp = hook.get("tool_response")
    text = resp if isinstance(resp, str) else " ".join(
        str(resp.get(k) or "") for k in ("stdout", "stderr", "output")) if isinstance(resp, dict) else ""
    return not re.search(r"^(error|fatal):|nothing to commit", text, re.M)


def review_commit(hook: dict, cfg: dict, session_id: str) -> dict:
    inp = hook.get("tool_input") or {}
    root = repo_root(hook.get("cwd") or os.getcwd(), str(inp.get("command") or ""))
    if not root:
        return {}
    sha = git(root, "rev-parse", "HEAD")
    if not sha:
        return {}
    gd = gitdir(root)
    if os.path.exists(os.path.join(gd, SKIP)):
        return {}
    if last_reviewed_sha(gd, session_id) == sha:
        return {}   # this sha was already reviewed (a retried hook)
    strict = (_opt("challenger_mode") or "strict").strip().lower() != "advise"
    write_state(gd, session_id, "RUNNING", sha)

    r = run_codex(["exec", *sandbox_args(), "review", "--json", "--commit", sha], root)
    apply_waivers(gd, r["findings"])
    n, blocking, waived = counts(r["findings"])
    verdict = r["verdict"]
    if not verdict:
        if not r["findings"] and not r["activity"]:
            verdict = "INCONCLUSIVE"
        else:
            verdict = "FAIL" if blocking else "PASS"

    rounds_path = os.path.join(gd, ROUNDS + sfx(session_id))
    rnd, last = read_count(rounds_path)
    if last and time.time() - last > FAIL_MAX_AGE:
        rnd = 0
    capped = False
    if verdict == "FAIL":
        rnd += 1
        if rnd >= MAX_ROUNDS:
            capped = True
        with open(rounds_path, "w") as f:
            f.write(f"{rnd} {int(time.time())}\n")
    elif verdict == "PASS":
        try:
            os.remove(rounds_path)
        except OSError:
            pass
        rnd = 0

    short = sha[:8]
    entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha": sha,
             "short_sha": short, "branch": git(root, "rev-parse", "--abbrev-ref", "HEAD"),
             "session_id": session_id, "verdict": verdict, "capped": capped, "round": rnd,
             "findings": r["findings"], "finding_count": n, "blocking_count": blocking,
             "waived_count": waived, "review_prose": r["prose"][:2000], "codex_exit": r["exit"],
             "duration_seconds": r["duration_seconds"]}
    record(gd, entry)
    ship_challenge(cfg, hook, "challenge_commit", {
        "verdict": verdict, "sha": sha, "round": rnd, "finding_count": n, "blocking_count": blocking,
        "waived_count": waived, "duration_seconds": r["duration_seconds"], "capped": capped,
        "strict": strict, "prose": r["prose"][:4000],
        "findings": [{"priority": f["priority"], "title": f["title"], "waived": f.get("waived", False)}
                     for f in r["findings"]]})

    if verdict == "TIMEOUT":
        clear_state(gd, session_id)
        return context("PostToolUse", f"Codex review of commit {short} timed out after {TIMEOUT}s and was "
                       "skipped. You are not blocked. Mention the timeout to the user.")
    if verdict == "ERROR":
        clear_state(gd, session_id)
        return context("PostToolUse", f"Codex review of commit {short} errored and produced no verdict. "
                       f"You are not blocked.\n\n{r['prose']}\n\nMention the error to the user; likely a "
                       "Codex auth or rate-limit issue.")
    if verdict == "INCONCLUSIVE":
        clear_state(gd, session_id)
        return context("PostToolUse", f"Codex review of commit {short} was INCONCLUSIVE: it returned no "
                       "findings but never inspected the code (its sandbox probably failed to start). "
                       "This is NOT a clean pass. You are not blocked. Tell the user; they can set the "
                       "plugin's codex_sandbox option to danger-full-access and amend the commit to "
                       "re-run the review.\n\nReview output:\n" + r["prose"])
    if verdict == "FAIL" and capped:
        clear_state(gd, session_id, rounds=True)
        return context("PostToolUse", f"Codex review of commit {short} still has findings, but this is "
                       f"review round {rnd} of a max of {MAX_ROUNDS} since the last pass; the loop is "
                       "CAPPED and no longer blocking. Treat the findings as ADVISORY: fix what you judge "
                       "real, note the rest to the user, and proceed.\n\n" + r["prose"])
    waive_note = f" ({waived} finding(s) waived)" if waived else ""
    if verdict == "FAIL" and strict:
        write_state(gd, session_id, "FAIL", sha)
        pushed = git(root, "branch", "-r", "--contains", sha)
        fix = ("Fix the issues and fold them into the reviewed commit with `git commit --amend` (it is "
               "not on any remote)." if not pushed else
               "Fix the issues in a new commit (the reviewed commit is already on a remote; do not amend).")
        return block(f"Codex review of commit {short} found issues (fix round {rnd} of max {MAX_ROUNDS})"
                     f"{waive_note}:\n\n{r['prose']}\n\n{fix} Do NOT re-run the review yourself; it runs "
                     "on your next commit. Do NOT push until the review passes. If the user disagrees "
                     "with a finding, they can suppress it with /tares:challenger-waive.")
    if verdict == "FAIL":
        clear_state(gd, session_id)
        return context("PostToolUse", f"Codex review of commit {short} found {blocking} blocking "
                       f"finding(s){waive_note}. Advise mode: you are not blocked. Fix what you judge "
                       "real and tell the user the rest.\n\n" + r["prose"])
    clear_state(gd, session_id)
    return context("PostToolUse", f"Codex review of commit {short} passed{waive_note}.\n\n{r['prose']}\n\n"
                   "Ask the user if they want to push.")


# ── Stop: the fix loop ───────────────────────────────────────────────────────

def stop_loop(hook: dict, session_id: str) -> dict:
    root = repo_root(hook.get("cwd") or os.getcwd(), "")
    if not root:
        return {}
    gd = gitdir(root)
    state = read_state(gd, session_id)
    if not state:
        return {}
    now = int(time.time())
    if state["verdict"] == "RUNNING":
        if now - state["ts"] > RUNNING_MAX_AGE:
            clear_state(gd, session_id)
        return {}
    if state["verdict"] != "FAIL":
        clear_state(gd, session_id)
        return {}
    if now - state["ts"] > FAIL_MAX_AGE or state["sha"] != git(root, "rev-parse", "HEAD"):
        clear_state(gd, session_id)
        return {}
    loops_path = os.path.join(gd, LOOPS + sfx(session_id))
    if hook.get("stop_hook_active") and not os.path.exists(loops_path):
        clear_state(gd, session_id)
        return {}
    n, _ = read_count(loops_path)
    n += 1
    with open(loops_path, "w") as f:
        f.write(f"{n}\n")
    if n >= MAX_LOOPS:
        clear_state(gd, session_id)
        return context("Stop", f"The challenger fix loop reached its max of {MAX_LOOPS} iterations without "
                       "passing. Stopping. Tell the user what is still open.")
    return block("The Codex review of your last commit found issues that still need fixing. Fix them and "
                 "commit (amend the reviewed commit if it is not pushed, otherwise a new commit). Do NOT "
                 "re-run the review yourself; it runs on your commit. Do not stop until the review passes.")


# ── waive (the /tares:challenger-waive command) ──────────────────────────────

def waive(which: str, reason: str = "") -> int:
    """Waive a blocking finding. In a tares-factory station a waiver needs its reason (the
    reviewer judges it); a person's own session may waive without one, as before."""
    reason = " ".join(reason.split())
    if os.environ.get("FACTORY_STATION", "").strip() and not reason:
        print("a waiver in a tares-factory crew needs its reason: "
              "/tares:challenger-waive <n> <why this finding does not apply>")
        return 1
    root = repo_root(os.getcwd(), "")
    if not root:
        print("not in a git repository"); return 1
    gd = gitdir(root)
    last = None
    try:
        for line in open(os.path.join(gd, HISTORY)):
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("verdict") == "FAIL":
                last = e
    except OSError:
        pass
    if not last:
        print("no failed review to waive from"); return 1
    blocking = [f for f in last["findings"] if f["priority"] in ("P1", "P2") and not f.get("waived")]
    if not blocking:
        print("the last failed review has no unwaived blocking findings"); return 1
    if which == "all":
        chosen = blocking
    elif which.isdigit() and 1 <= int(which) <= len(blocking):
        chosen = [blocking[int(which) - 1]]
    elif not which and len(blocking) == 1:
        chosen = blocking
    else:
        print("which finding? one of:")
        for i, f in enumerate(blocking, 1):
            print(f"  {i}. [{f['priority']}] {f['title']}")
        return 1
    with open(os.path.join(gd, WAIVED), "a") as f:
        for c in chosen:
            f.write(waive_key(c["title"]) + "\n")
    clear_state(gd, last.get("session_id", ""))
    cfg = config()
    hook = {"session_id": last.get("session_id", ""), "cwd": root}
    ship_challenge(cfg, hook, "challenge_waived", {
        "sha": last.get("sha"), "finding_count": len(chosen),
        "findings": [{"priority": c["priority"], "title": c["title"], "waived": True,
                      **({"reason": reason} if reason else {})} for c in chosen]})
    for c in chosen:
        print(f"waived: [{c['priority']}] {c['title']}")
    print(f"waivers live in {os.path.join(gd, WAIVED)}; the commit is no longer blocked")
    return 0


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "waive":
        sys.exit(waive(sys.argv[2] if len(sys.argv) > 2 else "", " ".join(sys.argv[3:])))
    try:
        hook = json.load(sys.stdin)
    except Exception:
        out({}); return
    session_id = str(hook.get("session_id") or "")
    cfg = config()
    if read_flow(cfg["data_dir"], session_id) != CHALLENGER:
        out({}); return   # not a challenger session: nothing runs
    event = hook.get("hook_event_name")
    try:
        if event == "Stop":
            out(stop_loop(hook, session_id)); return
        if event != "PostToolUse":
            out({}); return
        tool = hook.get("tool_name")
        if tool == "ExitPlanMode":
            if not codex_bin():
                out(context("PostToolUse", "The challenger is on for this session but the Codex CLI is not "
                            "installed (npm install -g @openai/codex && codex login). The plan was not "
                            "challenged; tell the user."))
                return
            out(review_plan(hook, cfg)); return
        if tool == "Bash" and is_commit(hook):
            if not codex_bin():
                out({}); return
            out(review_commit(hook, cfg, session_id)); return
        out({})
    except Exception as e:   # a hook must never take the session down
        out(context("PostToolUse" if event == "PostToolUse" else "Stop",
                    f"The challenger hook failed ({type(e).__name__}: {e}); nothing was reviewed."))


if __name__ == "__main__":
    main()

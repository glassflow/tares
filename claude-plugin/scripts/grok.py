"""Grok as the challenger's second model: one xAI chat-completions call per review, stdlib only.

Codex reviews by running an agent over the repo. Grok gets the commit handed to it instead: the
patch plus the full text of each changed file at that commit, so it sees the code around a hunk.
It cannot run the project's commands, so it never claims a test result, only what the code says.

Returns the same dict as challenger.run_codex, so the verdict logic, the fix loop, waivers and the
Tares record stay as they are. A reply that names no finding and does not end on an explicit
`VERDICT: CLEAN` is inconclusive, never a pass.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

BASE_URL = (os.environ.get("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/")
MODEL = (os.environ.get("CLAUDE_PLUGIN_OPTION_GROK_MODEL") or os.environ.get("TARES_GROK_MODEL")
         or "grok-4.6")
MAX_PATCH = 60_000      # chars of `git show`; past this the file bodies are dropped first
MAX_FILE = 20_000       # chars per changed file at the commit
MAX_CONTEXT = 120_000   # chars of patch + files in one request

# `[P1] title`, also under a list, number or heading mark, bolded (`**[P1]**`) or with a colon
# after the tag: a finding the model formats its own way must never read as no finding
_FINDING_RE = re.compile(r"^\s*(?:(?:[-*+>]|\d+[.)]|#{1,6})\s+)*(?:\*\*|__|\*|_)?\[(P[123])\]"
                         r"(?:\*\*|__|\*|_)?\s*[:\-]?\s*(\S.*?)\s*$")
_VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(CLEAN|FINDINGS)\s*$", re.M)

SYSTEM = (
    "You are a second reviewer challenging another coding agent's work. Be adversarial about "
    "correctness: wrong behaviour, missed edge cases, error handling, security, data loss, tests that "
    "do not test what they claim. Ignore style and naming. You cannot run code; reason from the text "
    "and never claim a test passed or failed.\n\n"
    "Format, exactly:\n"
    "- one line per finding: `[P1] <title>` (breaks correctness or security), `[P2] <title>` (a real "
    "bug in a narrower case), `[P3] <title>` (worth a look). Under each, at most three lines: where "
    "(file:line) and why.\n"
    "- last line: `VERDICT: FINDINGS` if you listed any P1 or P2, else `VERDICT: CLEAN`.\n"
    "Say CLEAN only after reading every hunk. If the material is truncated or unreadable, say so and "
    "end with `VERDICT: FINDINGS` and a [P3] naming what you could not see."
)


def api_key() -> str:
    """The plugin's xai_api_key option (Claude Code injects it into the hook env), else XAI_API_KEY."""
    return (os.environ.get("CLAUDE_PLUGIN_OPTION_XAI_API_KEY") or os.environ.get("XAI_API_KEY")
            or "").strip()


def _git(cwd: str, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout if p.returncode == 0 else ""


def commit_material(cwd: str, sha: str) -> str:
    patch = _git(cwd, "show", "--stat", "--patch", "--format=commit %H%n%n%B", sha)
    if len(patch) > MAX_PATCH:
        patch = patch[:MAX_PATCH] + f"\n\n[patch truncated at {MAX_PATCH} chars]"
    parts = [patch]
    budget = MAX_CONTEXT - len(patch)
    # R and C too: a renamed or copied file that was also edited needs its body, not just the hunk
    files = [f for f in _git(cwd, "show", "--name-only", "--diff-filter=AMRC", "--format=", sha)
             .splitlines() if f.strip()]
    for f in files:
        body = _git(cwd, "show", f"{sha}:{f}")
        if not body or "\0" in body[:2000]:
            continue
        if len(body) > MAX_FILE:
            body = body[:MAX_FILE] + f"\n[file truncated at {MAX_FILE} chars]"
        block = f"\n\n===== {f} at {sha[:8]} =====\n{body}"
        if len(block) > budget:
            parts.append(f"\n\n[{f} and later files left out: context budget]")
            break
        parts.append(block)
        budget -= len(block)
    return "".join(parts)


def _chat(user: str, timeout: int) -> tuple[str, dict]:
    body = json.dumps({"model": MODEL, "temperature": 0,
                       "messages": [{"role": "system", "content": SYSTEM},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(f"{BASE_URL}/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {api_key()}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return str(msg), data.get("usage") or {}


def run_grok(user: str, timeout: int) -> dict:
    """One Grok call; the run_codex dict shape. `activity` is true only when the reply ends on an
    explicit verdict line, so a reply that never engaged reads as INCONCLUSIVE upstream."""
    started = time.time()
    if not api_key():
        return {"prose": "(XAI_API_KEY is not set; Grok could not review)", "findings": [],
                "verdict": "ERROR", "exit": 1, "duration_seconds": 0.0, "activity": False}
    try:
        prose, usage = _chat(user, timeout)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:1500]
        return {"prose": f"(xAI answered {e.code}: {detail})", "findings": [], "verdict": "ERROR",
                "exit": 1, "duration_seconds": round(time.time() - started, 1), "activity": False}
    except (TimeoutError, urllib.error.URLError) as e:
        # a read timeout raises TimeoutError; a connect timeout arrives wrapped in URLError
        if not isinstance(e, TimeoutError) and not isinstance(e.reason, TimeoutError):
            return {"prose": f"(could not reach xAI: {e.reason})", "findings": [], "verdict": "ERROR",
                    "exit": 1, "duration_seconds": round(time.time() - started, 1), "activity": False}
        return {"prose": f"(Grok review timed out after {timeout}s)", "findings": [],
                "verdict": "TIMEOUT", "exit": 124,
                "duration_seconds": round(time.time() - started, 1), "activity": False}
    except Exception as e:
        return {"prose": f"(could not reach xAI: {type(e).__name__}: {e})", "findings": [],
                "verdict": "ERROR", "exit": 1,
                "duration_seconds": round(time.time() - started, 1), "activity": False}
    findings = [{"priority": m.group(1), "title": m.group(2).rstrip("*_ ")}
                for m in (_FINDING_RE.match(l) for l in prose.splitlines()) if m]
    verdict = _VERDICT_RE.search(prose)
    # FINDINGS says a P1 or P2 is there; if none parsed, the format beat the regex. Say so, so
    # that the caller never reads the empty list as clean.
    unparsed = bool(verdict and verdict.group(1) == "FINDINGS"
                    and not any(f["priority"] in ("P1", "P2") for f in findings))
    prose = prose.strip() or "(Grok returned an empty message)"
    if unparsed:
        prose = ("(Grok's verdict is FINDINGS, but none of its findings could be read as a [P1] or "
                 "[P2] line. Read the review below for them.)\n\n" + prose)
    return {"prose": prose, "findings": findings, "verdict": "", "exit": 0,
            "duration_seconds": round(time.time() - started, 1), "activity": bool(verdict),
            "unparsed": unparsed, "model": MODEL, "usage": usage}


def review_commit(cwd: str, sha: str, timeout: int) -> dict:
    r = run_grok("Review this commit.\n\n" + commit_material(cwd, sha), timeout)
    if r.get("unparsed"):
        r["verdict"] = "FAIL"   # Grok found something blocking; an empty list must not pass it
    return r


def review_plan(plan: str, timeout: int) -> dict:
    if len(plan) > MAX_CONTEXT:
        plan = plan[:MAX_CONTEXT] + f"\n\n[plan truncated at {MAX_CONTEXT} chars]"
    return run_grok("Challenge this implementation plan before any code is written: what is "
                    "missing, wrong or risky? Only P1 means the plan must change.\n\n" + plan, timeout)

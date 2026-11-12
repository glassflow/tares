#!/usr/bin/env python3
"""The Tares side of the idle wake, for the `idle_wake_daemon` scenario of parity.py: it runs a real Tares and
checks, between watch rounds, what the daemon says (the review queue, the setting, the desk). The crew engine does
not take part; the daemon is the real one, run in this process on a throwaway database, with a small GitHub
stand-in for the pull request.

    idle_wake_daemon.py

Prints one JSON object per line, in the order of the checks. Standard library, httpx and the package itself.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

TMP = tempfile.mkdtemp(prefix="tares-idle-wake-daemon-")
os.environ["TARES_DB"] = os.path.join(TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ.pop("TARES_AUTH_TOKEN", None)
os.environ["HOME"] = TMP

STATE = {"head": "1111111aaaaaaaa", "ci": "success"}


class GitHub(http.server.BaseHTTPRequestHandler):
    """The pull request o/r#1: its head and its CI come from STATE."""

    def reply(self, doc) -> None:
        b = json.dumps(doc).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:
        p = self.path.split("?")[0]
        if p == "/health":
            self.reply({"ok": True})
        elif p == "/user":
            self.reply({"login": "stub"})
        elif p.startswith("/repos/o/r/pulls/"):
            self.reply({"head": {"sha": STATE["head"], "ref": "factory/T1"}, "state": "open", "merged": False,
                        "title": "Store", "html_url": "https://github.com" + p.replace("/repos", "").replace("/pulls/", "/pull/")})
        elif p.endswith("/status"):
            self.reply({"statuses": []})
        elif p.endswith("/check-runs"):
            self.reply({"check_runs": [{"status": "completed", "conclusion": STATE["ci"]}]})
        else:
            self.send_error(404)

    def log_message(self, *a) -> None:
        pass


def iso(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def main() -> int:
    import httpx
    sys.path.insert(0, ROOT)
    from tares.daemon import make_app
    app = make_app()
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def show(step: str, **doc) -> None:
        print(json.dumps({"step": step, **doc}, sort_keys=True))

    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    repo = os.path.join(TMP, "links")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True, env=env)
    repo = subprocess.run(["git", "-C", repo, "rev-parse", "--show-toplevel"], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            async def api(method: str, path: str, body=None):
                r = await cx.request(method, path, json=body)
                assert r.status_code < 300, f"{method} {path}: {r.status_code} {r.text}"
                return r.json() if r.content else None

            async def queue() -> list:
                return (await api("GET", "/api/review-queue"))["queue"]

            async def setting():
                return (await api("GET", "/api/crew/settings")).get("idle_wake")

            async def answered() -> list:
                return (await api("GET", "/api/desk?state=answered"))["desk"]

            async def pr_to(stage: str) -> None:
                await api("POST", f"/api/projects/{P}/tickets/T1/stage", {"stage": stage, "pr": "o/r#1"})

            async def verdict(head: str) -> None:
                await api("POST", f"/api/projects/{P}/tickets/T1/reviews",
                          {"head": head, "verdict": "changes", "verified": "read the diff",
                           "findings": [{"text": "name the flag", "blocking": True}]})

            async def user_says(words: str) -> None:
                r = await cx.post("/ingest/claude_code", content=json.dumps(
                    {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": iso(0),
                     "message": {"role": "user", "content": words}}) + "\n",
                    headers={"Content-Type": "application/x-ndjson"})
                assert r.status_code < 300, r.text

            await api("PUT", "/api/crew/settings", {"autonomy": "L3-review", "releaser": True, "release_profile": "kind",
                                                   "challenger": False, "builders_max": 2})
            P = (await api("POST", "/api/projects", {"template": "custom", "kind": "software_factory",
                                                     "name": "Links App", "objects": []}))["id"]
            await api("POST", f"/api/projects/{P}/handover", {"repos": [{"path": repo}]})
            await api("POST", f"/api/projects/{P}/tickets", {"title": "Store"})
            await api("POST", f"/api/projects/{P}/tickets/T1/assign", {"holder": "links-app-build-m1"})
            await api("POST", f"/api/projects/{P}/tickets/T1/stage", {"stage": "review"})
            show("waiting", queue=len(await queue()))

            show("unset", idle_wake=await setting())

            # the queue empties when the ticket is back in doing, and the setting reads back true
            await api("POST", f"/api/projects/{P}/tickets/T1/stage", {"stage": "doing"})
            show("emptied", queue=len(await queue()))
            await api("PUT", "/api/crew/settings", {"idle_wake": True})
            show("on", idle_wake=await setting())

            await api("POST", "/api/integrations/github", {"name": "stub", "kind": "token", "token": "t",
                                                          "api_url": f"http://127.0.0.1:{srv.server_address[1]}"})
            await pr_to("review")
            q = await queue()
            show("head", head=q[0]["head"] if q else None)

            STATE["head"] = "3333333cccccccc"
            await pr_to("review")
            await verdict("3333333cccccccc")
            q = await queue()
            show("verdict", current=(q[0].get("verdict") or {}).get("current") if q else None)

            # a desk question that is open is not an answer
            await api("POST", "/api/desk", {"question": "Keep the old endpoint?", "recommendation": "keep it",
                                            "why": "two callers", "options": ["keep it", "drop it"], "project": P})
            show("open_question", answered=len(await answered()),
                 open=len((await api("GET", "/api/desk?state=open"))["desk"]))

            await api("POST", "/api/sources", {"name": "claude_code", "connector": "claude_code", "poll": "10s",
                                               "config": {"push": True}})
            await user_says("Keep it, please")
            await api("POST", "/api/desk/D1/answer", {"words": "Keep it, please", "choice": "keep it"})
            show("answered", answered=len(await answered()))

            await api("POST", "/api/desk", {"question": "Ship the cache?", "recommendation": "yes", "why": "tested",
                                            "options": ["yes", "no"], "project": P})
            await user_says("Yes, ship it")
            await api("POST", "/api/desk/D2/answer", {"words": "Yes, ship it", "choice": "yes"})
            show("answered_two", answered=len(await answered()))

            await api("POST", "/api/desk", {"question": "Allow crew-reviewer to run gh?", "recommendation": "allow",
                                            "why": "needed", "station": "crew-reviewer", "command": "gh pr view 1",
                                            "project": P})
            await api("POST", "/api/desk/D3/allow", {"scope": "all", "words": "crew-reviewer may run gh pr view"})
            show("allowed", permissions=len([d for d in await answered() if d.get("command") is not None]))

            # a head nobody has seen yet, with a verdict on it
            STATE["head"] = "6666666ffffffff"
            await pr_to("review")
            await verdict("6666666ffffffff")
            q = await queue()
            show("unseen", head=q[0]["head"] if q else None, current=(q[0].get("verdict") or {}).get("current") if q else None)
    srv.shutdown()
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)

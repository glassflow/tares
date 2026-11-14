# Tares enterprise licence, see ee/LICENSE
"""A fake control plane for the brain index tests (TR-1198): the two index routes of tares-cloud's
`routers/internal.py`, in process, with every request recorded. Not a test itself.

    import fake_cp
    cp = fake_cp.FakeCp()                       # token "fake-cp-token", cell slug "cell-a"
    async with cp.client() as client:           # a CpClient wired to the fake, no socket
        await client.start_index("src-1", "https://github.com/acme/web.git", "ghs_x")
    cp.jobs["src-1"]                            # {"state": "started", ...}
    cp.set("src-1", "done")                     # what GET answers from now on
    cp.set("src-1", "queued", place=2)
    cp.set("src-1", "failed", error="fatal: Authentication failed")
    cp.start_as = ("queued", 3)                 # what a new job starts as (default "started")
    cp.fail("index_status", 502, "kube is down")        # the next call to that route fails (once)
    cp.raise_next("start_index", httpx.ConnectError("down"))
    cp.before["start_index"] = fn               # fn() runs inside the next call to that route, once, before it answers
    cp.calls("POST", "start_index")             # the recorded requests of one route

Routes are named `start_index` and `index_status`. Without the cell's Bearer token every route
answers 401, and a slug other than the cell's answers 404, as the control plane does. A start whose
body has no repo_source_id, an https remote_url without credentials, or a token answers 422 with a
detail that never holds the token. A repeat start for a job that still runs returns that job (no
second job); one whose job finished is replaced. A status of a repo with no job is 404.

Where the fake differs from the control plane (tares-cloud `control-plane/app/brain_index.py` is the
authority): source ids are not required to be UUIDs, jobs never end by themselves (a test says when),
and `queued` with a `place` is answered as TR-1218 will.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # ee/, for tares_ee
from tares_ee.brain.cp import CpClient  # noqa: E402

BASE_URL = "http://fake-cp.test"
TOKEN = "fake-cp-" + "token"
SLUG = "cell-a"
_ROUTES = [
    ("POST", r"/internal/cells/(?P<slug>[^/]+)/brain/index", "start_index"),
    ("GET", r"/internal/cells/(?P<slug>[^/]+)/brain/index/(?P<source>[^/]+)", "index_status"),
]


@dataclass
class Recorded:
    method: str
    path: str
    route: str
    headers: dict             # lower-case names
    json: object              # the parsed body, None when there was none
    timeout: dict = field(default_factory=dict)
    status: int = 0           # what the fake answered


class FakeCp:
    def __init__(self, token: str = TOKEN, slug: str = SLUG):
        self.token, self.slug = token, slug
        self.requests: list[Recorded] = []
        self.jobs: dict[str, dict] = {}   # source id -> {state, place, error, remote_url}
        self.start_as: tuple[str, int | None] = ("started", None)
        self._fail: dict[str, list[tuple[int, str]]] = {}
        self._raise: dict[str, list[Exception]] = {}
        self.before: dict[str, object] = {}

    # -- test controls ----------------------------------------------------------------------

    def set(self, source: str, state: str, place: int | None = None, error: str | None = None) -> None:
        job = self.jobs[source]
        job.update(state=state, place=place, error=error)

    def set_all(self, state: str, place: int | None = None, error: str | None = None) -> None:
        for source in self.jobs:
            self.set(source, state, place, error)

    def fail(self, route: str, status: int = 500, detail: str = "internal error") -> None:
        """The next call to `route` answers `status` with `{"detail": detail}`."""
        self._fail.setdefault(route, []).append((status, detail))

    def raise_next(self, route: str, exc: Exception) -> None:
        """The next call to `route` raises `exc` (a httpx timeout or connect error)."""
        self._raise.setdefault(route, []).append(exc)

    def calls(self, method: str | None = None, route: str | None = None) -> list[Recorded]:
        return [r for r in self.requests
                if (method is None or r.method == method) and (route is None or r.route == route)]

    def client(self, **kw) -> CpClient:
        return CpClient(BASE_URL, self.token, self.slug, transport=httpx.MockTransport(self.handle), **kw)

    # -- the server -------------------------------------------------------------------------

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)   # a real call yields; the callers of one repo can overlap
        path = request.url.raw_path.decode().split("?")[0]
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode(errors="replace")
        route, args = "", {}
        for method, pattern, name in _ROUTES:
            m = re.fullmatch(pattern, path)
            if m and method == request.method:
                route, args = name, {k: unquote(v) for k, v in m.groupdict().items()}
                break
        rec = Recorded(request.method, path, route, {k.lower(): v for k, v in request.headers.items()}, body,
                       dict(request.extensions.get("timeout") or {}))
        self.requests.append(rec)
        if self.before.get(route):
            self.before.pop(route)()
        if self._raise.get(route):
            raise self._raise[route].pop(0)
        res = self._answer(rec, route, args)
        rec.status = res.status_code
        return res

    def _answer(self, rec: Recorded, route: str, args: dict) -> httpx.Response:
        if rec.headers.get("authorization") != f"Bearer {self.token}":
            return _err(401, "invalid cell token")
        if not route:
            return _err(404, "Not Found")
        if args["slug"] != self.slug:
            return _err(404, "unknown cell")
        if self._fail.get(route):
            return _err(*self._fail[route].pop(0))
        return getattr(self, "_" + route)(rec, args)

    def _start_index(self, rec, args):
        b = rec.json if isinstance(rec.json, dict) else {}
        source, remote, token = b.get("repo_source_id"), b.get("remote_url"), b.get("token")
        if not isinstance(source, str) or not source.strip():
            return _err(422, "repo_source_id is required")
        if not isinstance(remote, str) or not re.fullmatch(r"https://[^/@\s]+(/\S*)?", remote):
            return _err(422, "remote_url must be an https:// URL without credentials")
        if not isinstance(token, str) or not token.strip():
            return _err(422, "token is required")
        job = self.jobs.get(source)
        if job is None or job["state"] in ("done", "failed"):
            state, place = self.start_as
            job = self.jobs[source] = {"state": state, "place": place, "error": None, "remote_url": remote}
        return httpx.Response(202, json={"state": job["state"], "job": f"brain-index-{self.slug}-{source}"})

    def _index_status(self, rec, args):
        job = self.jobs.get(args["source"])
        if job is None:
            return _err(404, "no index Job for that repo")
        out = {"state": job["state"]}
        if job["place"] is not None:
            out["place"] = job["place"]
        if job["error"] is not None:
            out["error"] = job["error"]
        return httpx.Response(200, json=out)


def _err(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail})

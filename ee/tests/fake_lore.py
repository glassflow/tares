# Tares enterprise licence, see ee/LICENSE
"""A fake lore for the brain tests (TR-1216): the same calls as lore's `src/http`, in process, with
every request recorded. Not a test itself; TR-1197 to TR-1201 import it.

    import fake_lore
    lore = fake_lore.FakeLore()                  # key "fake-lore-key"
    async with lore.client() as client:          # a LoreClient wired to the fake, no socket
        repo = await client.create_repo("tares")
    lore.requests[-1].json                       # what the client sent
    lore.calls("POST", "/v1/repos/{id}/sync")    # the recorded requests of one route
    lore.fail("ask", 502, "model is down")       # the next call to that route fails (once)
    lore.raise_next("ask", httpx.ReadTimeout("slow"))
    lore.progress[repo["id"]] = {...}            # what GET /progress answers for a repo
    lore.answer = "..."; lore.hits = [...]       # canned ask and search results

Routes are named `create_repo list_repos sync_repo repo_progress delete_repo search ask
put_documents get_document delete_document`. Without the right Bearer key every route answers 401.
Names and bodies are checked as lore checks them (repo names, blank question, blank q).

Where the fake still differs from lore (lore's source is the authority): ids are not required to be
UUIDs (lore's `Path<Uuid>` refuses others with a bare 400); `k` is clamped to at least 1 only (lore
has its own upper bound); search hits carry empty `chunk_id`, `path`, `start_line`, `end_line` and
`symbols`, and a document search is a substring match, not lore's ranking; ask answers one fixed
citation; `sync` answers `sync_report` at once instead of after an index run.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # ee/, for tares_ee
from tares_ee.brain.lore import LoreClient  # noqa: E402

BASE_URL = "http://fake-lore.test"
_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
_ROUTES = [
    ("POST", r"/v1/repos", "create_repo"),
    ("GET", r"/v1/repos", "list_repos"),
    ("POST", r"/v1/repos/(?P<id>[^/]+)/sync", "sync_repo"),
    ("GET", r"/v1/repos/(?P<id>[^/]+)/progress", "repo_progress"),
    ("DELETE", r"/v1/repos/(?P<id>[^/]+)", "delete_repo"),
    ("GET", r"/v1/search", "search"),
    ("POST", r"/v1/ask", "ask"),
    ("POST", r"/v1/documents", "put_documents"),
    ("GET", r"/v1/documents/(?P<id>[^/]+)", "get_document"),
    ("DELETE", r"/v1/documents/(?P<source>[^/]+)/(?P<external_id>.+)", "delete_document"),
]


@dataclass
class Recorded:
    method: str
    path: str                 # the path as sent, still escaped
    route: str                # the route name above, "" for an unknown path
    query: dict
    headers: dict             # lower-case names
    json: object              # the parsed body, None when there was none
    timeout: dict = field(default_factory=dict)   # httpx's connect/read/write/pool seconds
    status: int = 0           # what the fake answered


class FakeLore:
    def __init__(self, key: str = "fake-lore-" + "key"):
        self.key = key
        self.requests: list[Recorded] = []
        self.repos: dict[str, dict] = {}
        self.documents: dict[tuple[str, str], dict] = {}   # (source, external_id) -> document
        self.progress: dict[str, dict] = {}
        self.answer = "Fake answer."
        # what POST /sync answers, lore's SyncReport (IndexReport fields plus up_to_date and full):
        # lore replies only when the whole fetch and index is done
        self.sync_report: dict = {"indexed_commit": "", "files_indexed": 1, "files_unchanged": 0,
                                  "files_skipped": 0, "files_deleted": 0, "chunks": 1,
                                  "up_to_date": False, "full": True}
        self.hits: list[dict] | None = None   # None: search the stored documents by substring
        self._fail: dict[str, list[tuple[int, str]]] = {}
        self._raise: dict[str, list[Exception]] = {}

    # -- test controls ----------------------------------------------------------------------

    def fail(self, route: str, status: int = 500, error: str = "internal error") -> None:
        """The next call to `route` answers `status` with `{"error": error}`."""
        self._fail.setdefault(route, []).append((status, error))

    def raise_next(self, route: str, exc: Exception) -> None:
        """The next call to `route` raises `exc` (a httpx timeout or connect error)."""
        self._raise.setdefault(route, []).append(exc)

    def calls(self, method: str | None = None, route: str | None = None) -> list[Recorded]:
        return [r for r in self.requests
                if (method is None or r.method == method) and (route is None or r.route == route)]

    def client(self, **kw) -> LoreClient:
        return LoreClient(BASE_URL, self.key, transport=httpx.MockTransport(self.handle), **kw)

    # -- the server -------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
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
        rec = Recorded(request.method, path, route, dict(request.url.params),
                       {k.lower(): v for k, v in request.headers.items()}, body,
                       dict(request.extensions.get("timeout") or {}))
        self.requests.append(rec)
        if self._raise.get(route):
            raise self._raise[route].pop(0)
        res = self._answer(rec, route, args)
        rec.status = res.status_code
        return res

    def _answer(self, rec: Recorded, route: str, args: dict) -> httpx.Response:
        if rec.headers.get("authorization") != f"Bearer {self.key}":
            return _err(401, "unauthorized")
        if not route:
            return _err(404, "not found")
        if self._fail.get(route):
            return _err(*self._fail[route].pop(0))
        return getattr(self, "_" + route)(rec, args)

    def _create_repo(self, rec, args):
        b = rec.json if isinstance(rec.json, dict) else {}
        name = b.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return _err(400, "repo name must be 1 to 100 characters of A-Z, a-z, 0-9, . _ and -")
        repo = next((r for r in self.repos.values() if r["name"] == name), None)
        if repo is None:
            repo = {"id": str(uuid.uuid4()), "name": name, "indexed_commit": None,
                    "web_url_template": None, "remote_url": None}
            self.repos[repo["id"]] = repo
        if b.get("web_url_template") is not None:
            repo["web_url_template"] = b["web_url_template"]
        if b.get("remote_url"):
            if not re.fullmatch(r"https://[^/@\s]+(/\S*)?", b["remote_url"]):
                return _err(400, "remote_url must be an https:// URL without credentials")
        if b.get("remote_url") is not None:
            repo["remote_url"] = b["remote_url"] or None
        return _ok(repo)

    def _list_repos(self, rec, args):
        return _ok({"repos": list(self.repos.values())})

    def _sync_repo(self, rec, args):
        repo = self.repos.get(args["id"])
        if repo is None:
            return _err(404, "not found")
        repo["indexed_commit"] = "0" * 40
        self.progress.setdefault(repo["id"], {"stage": "queued"})
        return _ok(self.sync_report | {"indexed_commit": repo["indexed_commit"]})

    def _repo_progress(self, rec, args):
        if args["id"] not in self.repos:
            return _err(404, "not found")
        return _ok(self.progress.get(args["id"], {"stage": "queued"}))

    def _delete_repo(self, rec, args):
        if self.repos.pop(args["id"], None) is None:
            return _err(404, "not found")
        self.progress.pop(args["id"], None)
        return httpx.Response(204)

    def _search(self, rec, args):
        q = (rec.query.get("q") or "").strip()
        if not q:
            return _err(400, "q is required")
        if self.hits is not None:
            return _ok({"hits": self.hits})
        kinds = [k for k in rec.query.get("kind", "").split(",") if k]
        hits = []
        for (source, ext), d in self.documents.items():
            if q.lower() in (d["title"] + " " + d["text"]).lower() \
                    and (not kinds or d["kind"] in kinds) \
                    and (not rec.query.get("source") or rec.query["source"] == source):
                hits.append({"document_id": d["id"], "source": source, "kind": d["kind"],
                             "title": d["title"], "uri": d["uri"], "external_id": ext,
                             "snippet": d["text"][:200], "score": 1.0, "chunk_id": None, "path": None,
                             "start_line": None, "end_line": None, "symbols": []})
        return _ok({"hits": hits[: max(1, int(rec.query.get("k", 12)))]})

    def _ask(self, rec, args):
        b = rec.json if isinstance(rec.json, dict) else {}
        q = b.get("question")
        if not isinstance(q, str) or not q.strip():
            return _err(400, "question is required")
        mode = b.get("mode", "fast")
        if mode not in ("fast", "sources", "deep"):
            return _err(422, "unknown mode")
        return _ok({"answer": self.answer, "mode": mode, "model": "fake-model",
                    "citations": [{"label": "1", "document_id": None, "kind": "code",
                                   "title": "README.md", "path": "README.md", "start_line": 1,
                                   "end_line": 10, "url": "https://github.com/acme/app/blob/main/README.md#L1-L10"}],
                    "sources_considered": 1, "tool_calls": 0 if mode == "fast" else 3,
                    "usage": {"input_tokens": 1, "output_tokens": 1}, "latency_ms": 1})

    def _put_documents(self, rec, args):
        b = rec.json if isinstance(rec.json, dict) else {}
        source, docs = b.get("source"), b.get("documents")
        if not isinstance(source, str) or not source.strip() or not isinstance(docs, list):
            return _err(400, "source and documents are required")
        up = same = 0
        for d in docs:
            if not isinstance(d, dict) or not all(isinstance(d.get(f), str) for f in ("external_id", "title", "text")):
                return _err(400, "a document needs external_id, title and text")
            if d.get("kind") not in (None, "", "note", "doc"):
                return _err(400, "kind must be note or doc")
            doc = {"id": str(uuid.uuid4()), "kind": d.get("kind") or "note", "title": d["title"],
                   "uri": d.get("uri"), "metadata": d.get("metadata") or {}, "text": d["text"]}
            old = self.documents.get((source, d["external_id"]))
            if old and {k: old[k] for k in doc if k != "id"} == {k: doc[k] for k in doc if k != "id"}:
                same += 1
                continue
            if old:
                doc["id"] = old["id"]
            self.documents[(source, d["external_id"])] = doc
            up += 1
        return _ok({"upserted": up, "unchanged": same, "chunks": up})

    def _get_document(self, rec, args):
        for d in self.documents.values():
            if d["id"] == args["id"]:
                return _ok(d)
        return _err(404, "not found")

    def _delete_document(self, rec, args):
        if self.documents.pop((args["source"], args["external_id"]), None) is None:
            return _err(404, "not found")
        return httpx.Response(204)


def _ok(body) -> httpx.Response:
    return httpx.Response(200, json=body)


def _err(status: int, error: str) -> httpx.Response:
    return httpx.Response(status, json={"error": error})

# Tares enterprise licence, see ee/LICENSE
"""The client for lore, the central index behind the brain (TR-1216).

Every call a cell makes to lore goes through `LoreClient`. It reads `TARES_BRAIN_URL` and
`TARES_BRAIN_KEY` (sent as a Bearer key); with either missing, `LoreClient.from_env` raises
`BrainNotConfigured`. Calls time out after 10 seconds, a deep ask after 120 and a sync after 600
(lore answers a sync only when the fetch and the index are done).

Every failure is a `LoreError` whose message is plain words. The message never holds the key or a
GitHub token, and nothing here logs either: the text of a lore reply, a transport error and the
caller's own arguments all pass through `scrub` first. The response bodies and URLs of a failed
call are not copied into the message beyond lore's own `error` text.

Endpoints follow `src/http/mod.rs` of lore, with three the working doc names that lore's router did
not have yet when this was written (`GET /v1/repos/{id}/progress`, `DELETE /v1/repos/{id}` and the
`token` in the sync body); each returns lore's JSON unchanged.

    async with LoreClient.from_env() as lore:
        repo = await lore.create_repo("tares", remote_url="https://github.com/glassflow/tares.git")
        await lore.sync_repo(repo["id"], token=installation_token)
        answer = await lore.ask("how does a cell get its licence key?")
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Mapping, Sequence
from urllib.parse import quote

import httpx

__all__ = [
    "ASK_MODES", "BrainNotConfigured", "DEEP_TIMEOUT", "LoreClient", "LoreError", "NOT_CONFIGURED",
    "SYNC_TIMEOUT", "TIMEOUT", "scrub",
]

log = logging.getLogger("tares_ee.brain.lore")

ENV_URL = "TARES_BRAIN_URL"
ENV_KEY = "TARES_BRAIN_KEY"
TIMEOUT = 10.0        # seconds, every call but a deep ask and a sync
DEEP_TIMEOUT = 120.0  # seconds, `ask(mode="deep")`
SYNC_TIMEOUT = 600.0  # seconds, `sync_repo`: lore replies once the whole index run is done (A6)
ASK_MODES = ("fast", "deep")
NOT_CONFIGURED = "The brain is not configured on this cell."
HIDDEN = "[hidden]"

# GitHub tokens by shape (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_) and a token inside a URL.
_TOKEN_SHAPES = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9]{6,}|github_pat_[A-Za-z0-9_]{6,})"
    r"|(?<=://)[^/\s@]+:[^/\s@]+(?=@)"
)
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")


class LoreError(Exception):
    """A call to lore failed. `str(error)` is plain words and safe to show or log; `status` is the
    HTTP status lore answered with, or None when it did not answer (timeout, no connection)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BrainNotConfigured(LoreError):
    """`TARES_BRAIN_URL` or `TARES_BRAIN_KEY` is missing or blank."""

    def __init__(self):
        super().__init__(NOT_CONFIGURED)


def scrub(text: Any, secrets: Sequence[str] = ()) -> str:
    """`text` with each of `secrets` and anything shaped like a GitHub token replaced by [hidden]."""
    out = _TOKEN_SHAPES.sub(HIDDEN, "" if text is None else str(text))   # shapes first: a short secret must not cut a token in two
    for s in sorted({x for x in secrets if x}, key=len, reverse=True):
        out = out.replace(s, HIDDEN)
    return out


def _need_text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LoreError(f"{what} is required.")
    return value.strip()


def _opt_text(value: Any, what: str) -> str | None:
    """A stripped optional string; None or blank gives None, anything but a string is refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise LoreError(f"{what} must be text.")
    return value.strip() or None


def _kinds(kind: Any) -> list[str]:
    """The non-blank kinds of a str or a list of str."""
    items = [kind] if isinstance(kind, str) else list(kind or [])
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


def _count(k: Any) -> int:
    if isinstance(k, bool) or not isinstance(k, int):
        raise LoreError("The number of results must be a whole number.")
    return k


def _seg(value: Any, what: str, *, keep_slash: bool = False) -> str:
    """One path segment, checked and escaped. An external id may hold slashes (lore's route takes
    the rest of the path); a dot segment is refused as lore refuses it."""
    text = _need_text(value, what)
    if any(part in (".", "..") for part in text.split("/")):
        raise LoreError(f"{what} cannot hold a . or .. part.")
    return quote(text, safe="/" if keep_slash else "")


class LoreClient:
    """One lore, one key. Use it as `async with`, or call `aclose()`. `transport` is for tests
    (`FakeLore.client()` in ee/tests/fake_lore.py builds one)."""

    def __init__(self, base_url: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = TIMEOUT, deep_timeout: float = DEEP_TIMEOUT,
                 sync_timeout: float = SYNC_TIMEOUT):
        base = (base_url or "").strip().rstrip("/")
        key = (key or "").strip()
        if not base or not key:
            raise BrainNotConfigured()
        if not (key.isascii() and key.isprintable()):   # never echoed: httpx would put it in a UnicodeEncodeError
            raise LoreError("TARES_BRAIN_KEY must be plain printable ASCII.")
        if not base.startswith(("http://", "https://")):
            raise LoreError("TARES_BRAIN_URL must start with http:// or https://.")
        self.base_url = base
        self._key = key
        self.timeout = timeout
        self.deep_timeout = deep_timeout
        self.sync_timeout = sync_timeout
        self._http = httpx.AsyncClient(base_url=base, transport=transport, follow_redirects=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw) -> "LoreClient":
        env = os.environ if env is None else env
        return cls(env.get(ENV_URL, ""), env.get(ENV_KEY, ""), **kw)

    @staticmethod
    def configured(env: Mapping[str, str] | None = None) -> bool:
        env = os.environ if env is None else env
        return bool((env.get(ENV_URL) or "").strip() and (env.get(ENV_KEY) or "").strip())

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "LoreClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- repos ------------------------------------------------------------------------------

    async def create_repo(self, name: str, *, remote_url: str | None = None,
                          web_url_template: str | None = None) -> dict:
        """`POST /v1/repos`: the repo lore indexes (made if new). Returns `id`, `name`,
        `indexed_commit`, `web_url_template`, `remote_url`."""
        name = _need_text(name, "The repo name")
        if not _REPO_NAME.fullmatch(name):
            raise LoreError("The repo name must be 1 to 100 characters of A-Z, a-z, 0-9, . _ and -.")
        body: dict[str, Any] = {"name": name}
        if remote_url is not None:
            if not isinstance(remote_url, str):
                raise LoreError("The remote URL must be text.")
            body["remote_url"] = remote_url.strip()
        if web_url_template is not None:
            if not isinstance(web_url_template, str):
                raise LoreError("The web URL template must be text.")
            if web_url_template:
                body["web_url_template"] = web_url_template
        return await self._call("POST", "/v1/repos", json=body, secrets=_url_secrets(remote_url))

    async def list_repos(self) -> dict:
        """`GET /v1/repos`: `{"repos": [...]}`."""
        return await self._call("GET", "/v1/repos")

    async def sync_repo(self, repo_id: str, *, token: str | None = None) -> dict:
        """`POST /v1/repos/{id}/sync`: lore fetches the repo's remote and indexes what changed.
        `token` (a GitHub token for this one call) goes in the body, never in a URL or a log. Lore
        answers when the whole fetch and index is done, so this call waits up to `sync_timeout`
        (600 s) and returns lore's report (`indexed_commit`, `files_indexed`, `chunks`, ...)."""
        path = f"/v1/repos/{_seg(repo_id, 'The repo id')}/sync"
        if token is not None and not isinstance(token, str):
            raise LoreError("The token must be text.")
        token = (token or "").strip()
        body = {"token": token} if token else {}
        return await self._call("POST", path, json=body, secrets=[token], timeout=self.sync_timeout)

    async def repo_progress(self, repo_id: str) -> dict:
        """`GET /v1/repos/{id}/progress`: how far the index run is."""
        return await self._call("GET", f"/v1/repos/{_seg(repo_id, 'The repo id')}/progress")

    async def delete_repo(self, repo_id: str) -> None:
        """`DELETE /v1/repos/{id}`: lore forgets the repo and everything indexed from it."""
        await self._call("DELETE", f"/v1/repos/{_seg(repo_id, 'The repo id')}")

    # -- search and ask ---------------------------------------------------------------------

    async def search(self, q: str, *, k: int | None = None, kind: Sequence[str] | str | None = None,
                     source: str | None = None) -> dict:
        """`GET /v1/search`: `{"hits": [...]}`. Blank `kind` entries and a blank `source` are left out."""
        params: dict[str, Any] = {"q": _need_text(q, "The search text")}
        if k is not None:
            params["k"] = _count(k)
        kinds = _kinds(kind)
        if kinds:
            params["kind"] = ",".join(kinds)
        source = _opt_text(source, "The source")
        if source:
            params["source"] = source
        return await self._call("GET", "/v1/search", params=params)

    async def ask(self, question: str, *, mode: str = "fast", k: int | None = None,
                  kind: Sequence[str] | str | None = None, source: str | None = None) -> dict:
        """`POST /v1/ask`: an answer with citations. `mode` is `fast` or `deep`; deep waits up to
        120 seconds."""
        if mode not in ASK_MODES:
            raise LoreError("The ask mode must be fast or deep.")
        body: dict[str, Any] = {"question": _need_text(question, "The question"), "mode": mode}
        if k is not None:
            body["k"] = _count(k)
        kinds = _kinds(kind)
        if kinds:
            body["kind"] = kinds
        source = _opt_text(source, "The source")
        if source:
            body["source"] = source
        return await self._call("POST", "/v1/ask", json=body,
                                timeout=self.deep_timeout if mode == "deep" else self.timeout)

    # -- documents --------------------------------------------------------------------------

    async def put_documents(self, source: str, documents: Sequence[Mapping[str, Any]]) -> dict:
        """`POST /v1/documents`: upsert documents (`external_id`, `title`, `text`, optional `kind`,
        `uri`, `metadata`) under a source. Returns `upserted`, `unchanged`, `chunks`."""
        source = _need_text(source, "The source name")
        if not documents:
            raise LoreError("There are no documents to send.")
        docs = []
        for i, d in enumerate(documents, 1):
            if not isinstance(d, Mapping):
                raise LoreError(f"Document {i} must be an object.")
            for field in ("external_id", "title", "text"):
                if not isinstance(d.get(field), str) or (field != "text" and not d[field].strip()):
                    raise LoreError(f"Document {i} needs {field}.")
            docs.append(dict(d))
        return await self._call("POST", "/v1/documents", json={"source": source, "documents": docs})

    async def get_document(self, document_id: str) -> dict:
        """`GET /v1/documents/{id}`: one document's `kind`, `title`, `uri`, `metadata`, `text`."""
        return await self._call("GET", f"/v1/documents/{_seg(document_id, 'The document id')}")

    async def delete_document(self, source: str, external_id: str) -> None:
        """`DELETE /v1/documents/{source}/{external_id}`."""
        path = f"/v1/documents/{_seg(source, 'The source name')}/{_seg(external_id, 'The document id', keep_slash=True)}"
        await self._call("DELETE", path)

    # -- the one request --------------------------------------------------------------------

    async def _call(self, method: str, path: str, *, json: Any = None, params: Mapping | None = None,
                    timeout: float | None = None, secrets: Sequence[str] = ()) -> dict:
        secrets = [self._key, *secrets]
        timeout = self.timeout if timeout is None else timeout
        try:
            res = await self._http.request(
                method, path, json=json, params=params, timeout=timeout,
                headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        except httpx.TimeoutException:
            raise self._fail(f"The brain did not answer within {timeout:g} seconds.", None, method, path, secrets) from None
        except httpx.HTTPError:
            raise self._fail("Could not reach the brain.", None, method, path, secrets) from None
        except Exception:   # never let an unexpected error (and its text) out of the client
            raise self._fail("The call to the brain failed.", None, method, path, secrets) from None
        if res.status_code >= 300:
            raise self._fail(_status_words(res), res.status_code, method, path, secrets)
        if res.status_code == 204 or not res.content:
            return {}
        try:
            data = res.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise self._fail("The brain answered with something Tares cannot read.", res.status_code, method, path, secrets)
        return data

    @staticmethod
    def _fail(message: str, status: int | None, method: str, path: str, secrets: Sequence[str]) -> LoreError:
        message = scrub(message, secrets)
        log.warning("brain call %s %s failed: %s", method, scrub(path, secrets), message)
        return LoreError(message, status)


def _url_secrets(url: str | None) -> list[str]:
    """The `user:token` part of a remote URL, so an error that echoes the URL hides it."""
    m = re.match(r"\s*[a-z]+://([^/\s@]+)@", url or "", re.I)
    return [m.group(1)] if m else []


def _status_words(res: httpx.Response) -> str:
    code = res.status_code
    if code >= 300 and code < 400:
        return f"The brain answered with a redirect (status {code}). Check TARES_BRAIN_URL."
    if code == 401:
        return "The brain refused this cell's key."
    if code == 403:
        return "This cell's key may not do that."
    if code == 404:
        return "The brain has nothing there: no such repo or document, or the address is wrong."
    if code == 429:
        return "The brain is busy: too many requests, try again later."
    detail = ""
    try:
        body = res.json()
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            detail = body["error"].strip()[:300]
    except ValueError:
        pass
    tail = f": {detail}" if detail else ""
    if code >= 500:
        return f"The brain had a problem (status {code}{tail}). Try again later."
    return f"The brain refused the request (status {code}{tail})."

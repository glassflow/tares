# Tares enterprise licence, see ee/LICENSE
"""The client for the control plane's index Jobs (TR-1198, the routes are TR-1196 in tares-cloud).

A cell does not clone or index a repo itself. It asks the control plane to run an index Job for
one repo and then asks how that Job is doing:

    POST {CP}/internal/cells/{slug}/brain/index                 {repo_source_id, remote_url, token}
    GET  {CP}/internal/cells/{slug}/brain/index/{repo_source_id}  -> {state, place?, error?}

`CP` is `TARES_BRAIN_CP_URL`, the bearer token is `TARES_BRAIN_CP_TOKEN` (the cell's own, put in its
Secret by the control plane), and the slug is `TARES_CELL_SLUG`, or `TARES_INSTANCE_NAME` when that is
unset (the cell chart sets it to the slug). A cell missing any of the three raises `CpNotConfigured`.

The control plane answers `started`, `done` or `failed` today; `queued` with a `place` comes with its
queue (TR-1218). A repo with no Job answers 404, and `index_status` returns None for it.

Every failure is a `CpError` whose message is plain words. The message never holds the GitHub token
or the cell's token: whatever the control plane said and any transport error pass through `scrub`
first, and nothing here logs the request body.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from .lore import scrub

__all__ = ["CpClient", "CpError", "CpNotConfigured", "TIMEOUT", "missing_settings"]

log = logging.getLogger("tares_ee.brain.cp")

ENV_URL = "TARES_BRAIN_CP_URL"
ENV_TOKEN = "TARES_BRAIN_CP_TOKEN"
ENV_SLUG = "TARES_CELL_SLUG"
ENV_SLUG_FALLBACK = "TARES_INSTANCE_NAME"
TIMEOUT = 10.0   # seconds


class CpError(Exception):
    """A call to the control plane failed. `str(error)` is plain words and safe to show or log."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class CpNotConfigured(CpError):
    """The cell has no control plane address, token or slug: it cannot start an index."""


def _env(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def missing_settings(env: Mapping[str, str] | None = None) -> list[str]:
    """The settings a cell still needs before it can ask the control plane for an index."""
    env = os.environ if env is None else env
    out = []
    if not _env(env, ENV_URL):
        out.append(ENV_URL)
    if not _env(env, ENV_TOKEN):
        out.append(ENV_TOKEN)
    if not (_env(env, ENV_SLUG) or _env(env, ENV_SLUG_FALLBACK)):
        out.append(ENV_SLUG)
    return out


class CpClient:
    """One control plane, one cell. Use it as `async with`, or call `aclose()`. `transport` is for
    tests (`FakeCp.client()` in ee/tests/fake_cp.py builds one)."""

    def __init__(self, base_url: str, token: str, slug: str, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = TIMEOUT):
        base = (base_url or "").strip().rstrip("/")
        token, slug = (token or "").strip(), (slug or "").strip()
        if not base or not token or not slug:
            raise CpNotConfigured(not_configured_words(
                [n for n, v in ((ENV_URL, base), (ENV_TOKEN, token), (ENV_SLUG, slug)) if not v]))
        if not base.startswith(("http://", "https://")):
            raise CpError(f"{ENV_URL} must start with http:// or https://.")
        if not (token.isascii() and token.isprintable()):   # never echoed: httpx would put it in an error
            raise CpError(f"{ENV_TOKEN} must be plain printable ASCII.")
        self.base_url, self.slug, self.timeout = base, slug, timeout
        self._token = token
        self._http = httpx.AsyncClient(base_url=base, transport=transport, follow_redirects=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **kw) -> "CpClient":
        env = os.environ if env is None else env
        return cls(_env(env, ENV_URL), _env(env, ENV_TOKEN),
                   _env(env, ENV_SLUG) or _env(env, ENV_SLUG_FALLBACK), **kw)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "CpClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def start_index(self, repo_source_id: str, remote_url: str, token: str) -> dict:
        """Ask for an index Job of one repo. `token` is a fresh GitHub token for the clone; it goes
        in the body only. Returns the control plane's answer (`state`, `job`). A repo whose Job is
        still running gets that Job back, so asking twice is safe."""
        body = {"repo_source_id": repo_source_id, "remote_url": remote_url, "token": token}
        return await self._call("POST", f"/internal/cells/{self._slug()}/brain/index", json=body,
                                secrets=[token])

    async def index_status(self, repo_source_id: str) -> dict | None:
        """`{state, place?, error?}` of the repo's Job, or None when the control plane has no Job
        for it (never asked, or gone ten minutes after it finished)."""
        path = f"/internal/cells/{self._slug()}/brain/index/{quote(str(repo_source_id), safe='')}"
        try:
            return await self._call("GET", path)
        except CpError as e:
            if e.status == 404:
                return None
            raise

    def _slug(self) -> str:
        return quote(self.slug, safe="")

    async def _call(self, method: str, path: str, *, json: Any = None, secrets=()) -> dict:
        secrets = [self._token, *secrets]
        try:
            res = await self._http.request(method, path, json=json, timeout=self.timeout, headers={
                "Authorization": f"Bearer {self._token}", "Accept": "application/json"})
        except httpx.TimeoutException:
            raise self._fail(f"The control plane did not answer within {self.timeout:g} seconds.",
                             None, method, path, secrets) from None
        except httpx.HTTPError:
            raise self._fail("Could not reach the control plane.", None, method, path, secrets) from None
        except Exception:   # never let an unexpected error (and its text) out of the client
            raise self._fail("The call to the control plane failed.", None, method, path, secrets) from None
        if res.status_code >= 300:
            raise self._fail(_status_words(res), res.status_code, method, path, secrets)
        try:
            data = res.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise self._fail("The control plane answered with something Tares cannot read.",
                             res.status_code, method, path, secrets)
        return data

    @staticmethod
    def _fail(message: str, status: int | None, method: str, path: str, secrets) -> CpError:
        message = scrub(message, secrets)
        if status != 404:   # a missing Job is an answer, not a failure
            log.warning("control plane call %s %s failed: %s", method, scrub(path, secrets), message)
        return CpError(message, status)


def not_configured_words(missing: list[str]) -> str:
    return ("This cell cannot start an index: the control plane is not set up here "
            f"({', '.join(missing)} not set).")


def _status_words(res: httpx.Response) -> str:
    code = res.status_code
    if 300 <= code < 400:
        return f"The control plane answered with a redirect (status {code}). Check {ENV_URL}."
    if code == 401:
        return "The control plane refused this cell's brain token."
    if code == 404:
        return "The control plane has no index job for that repo."
    if code == 503:
        return "The control plane is not set up to run index jobs yet."
    detail = ""
    try:
        body = res.json()
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            detail = body["detail"].strip()[:300]
    except ValueError:
        pass
    tail = f": {detail}" if detail else ""
    if code >= 500:
        return f"The control plane could not run the index job (status {code}{tail}). Try again later."
    return f"The control plane refused the index request (status {code}{tail})."

"""A project's secrets (TR-775): values a builder needs (an API key, a read-only key of another cell),
kept on the cell, encrypted, and handed out only to the project's members, when a command needs them.

  GET    /api/projects/{uid}/secrets          a member: the names, who set each and when, when one was last used
  PUT    /api/projects/{uid}/secrets/{name}   a member, not a crew station: {value} sets or replaces it
  DELETE /api/projects/{uid}/secrets/{name}   a member, not a crew station
  POST   /api/projects/{uid}/secrets/use      a member: the values, for `tares factory with-secrets`

The list is all anyone sees: no read gives a value back, the person who set it included. Only the use
route answers with values, and each use is recorded (when and by whom). The MCP tools never return a value
either, so a model never reads one into a conversation. Claude Code sessions sent to the cell have every
known value replaced with `[secret NAME]` before they are stored (`scrub`, called by the claude_code
connector); a value that was encoded or split is not found.

A member is the project's lead or a person on its member list (store.is_project_member). With auth off,
or with the root credential, the one person using the cell counts as a member.

The values are encrypted with a key derived from TARES_SECRETS_KEY (any long random string), which the cell
gets from its environment, never from its database. Without it the feature is off: the list says so and a
write is refused. A cell whose key changed cannot read the values it had; they read as unavailable until
someone sets them again.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import threading

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Body, HTTPException, Request

from .factory_api import caller

KEY_ENV = "TARES_SECRETS_KEY"
NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_VALUE = 16384
MIN_SCRUB = 6          # a shorter value is not replaced in sessions: it would mangle ordinary text
OFF = (f"Secrets are off on this cell: it has no {KEY_ENV}. Set it to a long random string in the cell's "
       "environment and restart it.")


def _fernet(env=None) -> Fernet | None:
    key = (os.environ if env is None else env).get(KEY_ENV, "")
    if not key.strip():
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))


def enabled(env=None) -> bool:
    return _fernet(env) is not None


def seal(value: str, env=None) -> str:
    f = _fernet(env)
    if f is None:
        raise RuntimeError(OFF)
    return f.encrypt(value.encode()).decode()


def unseal(token: str, env=None) -> str | None:
    """The value, or None when it can't be read (no key, or a key other than the one that sealed it)."""
    f = _fernet(env)
    if f is None:
        return None
    try:
        return f.decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def name_ok(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("a secret's name is capital letters, digits and _, starting with a letter or _, "
                         "64 at most (like CELL_READ_KEY)")
    return name


def value_ok(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a secret needs a value")
    if len(value) > MAX_VALUE:
        raise ValueError(f"a secret's value is {MAX_VALUE} characters at most")
    return value.strip("\n")


# ---------- scrubbing sessions ----------

_cache_lock = threading.Lock()
_cache: dict = {"version": None, "pairs": []}


def scrub_pairs(store) -> list:
    """(value, replacement) for every readable secret of the cell, longest first; cached until one changes."""
    version = store.secrets_version()
    with _cache_lock:
        if _cache["version"] == version:
            return _cache["pairs"]
    pairs = []
    for row in store.all_secret_rows():
        v = unseal(row["value"])
        if v and len(v) >= MIN_SCRUB:
            pairs.append((v, f"[secret {row['name']}]"))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    with _cache_lock:
        _cache.update(version=version, pairs=pairs)
    return pairs


def scrub(obj, pairs: list):
    """`obj` (text, or lists and dicts of it) with every known value replaced."""
    if not pairs:
        return obj
    if isinstance(obj, str):
        for v, r in pairs:
            if v in obj:
                obj = obj.replace(v, r)
        return obj
    if isinstance(obj, list):
        return [scrub(x, pairs) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v, pairs) for k, v in obj.items()}
    return obj


# ---------- who, and what they see ----------


def is_member(store, h, p: dict, request) -> bool:
    """The project's lead or a person on its member list; the root credential and auth off count."""
    if h.is_system(request):
        return True
    me = h.current_person(request)
    return bool(me and (me.get("id") == "local" or store.is_project_member(p["id"], me["id"])))


def listing(store, uid: str) -> dict:
    """What a member sees: the names, who set each and when, the last use. Never a value."""
    rows = store.secret_rows(uid)
    return {"enabled": enabled(), "off": "" if enabled() else OFF,
            "secrets": [{"name": r["name"], "set_by": r["set_by"], "set_at": r["set_at"],
                         "used_at": r["used_at"], "used_by": r["used_by"],
                         "readable": unseal(r["value"]) is not None} for r in rows]}


def page_listing(store, h, p: dict, request) -> dict | None:
    """For the project page's one read: the listing for a member, None for anyone else."""
    return listing(store, p["id"]) if is_member(store, h, p, request) else None


# ---------- the routes ----------


def register(app, store, h):
    def refuse(code: int, message: str):
        raise HTTPException(status_code=code, detail=message)

    def member_of(uid: str, request: Request) -> dict:
        p = h.project_or_404(h.resolve_project(uid))
        if not is_member(store, h, p, request):
            refuse(403, "only the project's members may see or change its secrets")
        return p

    def not_station(request: Request):
        if caller(request)["station"]:
            refuse(403, "a crew station does not set secrets; the person sets them in the console")

    @app.get("/api/projects/{uid}/secrets")
    def list_secrets(uid: str, request: Request):
        p = member_of(uid, request)
        return listing(store, p["id"])

    @app.put("/api/projects/{uid}/secrets/{name}")
    def set_secret(uid: str, name: str, request: Request, body: dict = Body(...)):
        not_station(request)
        p = member_of(uid, request)
        if not enabled():
            refuse(409, OFF)
        try:
            n, v = name_ok(name), value_ok(body.get("value"))
        except ValueError as e:
            refuse(422, str(e))
        store.set_secret(p["id"], n, seal(v), h.by(request, body))
        return listing(store, p["id"])

    @app.delete("/api/projects/{uid}/secrets/{name}")
    def delete_secret(uid: str, name: str, request: Request):
        not_station(request)
        p = member_of(uid, request)
        if not store.delete_secret(p["id"], str(name or "").strip()):
            refuse(404, f"{p['name']} has no secret {name}")
        return listing(store, p["id"])

    @app.post("/api/projects/{uid}/secrets/use")
    def use_secrets(uid: str, request: Request, body: dict = Body(default={})):
        p = member_of(uid, request)
        if not enabled():
            refuse(409, OFF)
        want = body.get("names") if isinstance(body, dict) else None
        rows = [r for r in store.secret_rows(p["id"]) if not want or r["name"] in want]
        values, unreadable = {}, []
        for r in rows:
            v = unseal(r["value"])
            if v is None:
                unreadable.append(r["name"])
            else:
                values[r["name"]] = v
        station = caller(request)["station"]
        who = h.by(request, {})
        store.secrets_used(p["id"], list(values), f"{who} ({station})" if station and who else (station or who))
        return {"project": p["id"], "values": values, "unreadable": unreadable}

# Tares enterprise licence, see ee/LICENSE
"""A project's secrets (TR-775): sealed on the cell, a list for members, values only through the use route.

  - with TARES_SECRETS_KEY unset the feature is off: the list says so, a write is 409;
  - a member (the lead, or a person on the member list) sets, lists, replaces and removes; nobody else
    gets anything (403), and no list or write answer ever holds a value;
  - the value is stored sealed: the database row does not hold it;
  - a crew station (X-Tares-Station) may list and use, never set or remove;
  - the use route gives the values and records who used them and when;
  - a key other than the one that sealed a value leaves it unreadable, not wrong;
  - a Claude Code session sent to the cell has every known value replaced with [secret NAME];
  - the MCP tool gives names only.

The feature is core; this test is here because a second person on a cell needs a licence.

Run: .venv/bin/python ee/tests/test_project_secrets.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-secrets-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN = "root-token-secrets"
os.environ.pop("TARES_SECRETS_KEY", None)

import httpx

from tares import project_secrets as ps
from tares.daemon import make_app

P = F = 0
VALUE = "nvf_readonly_0123456789abcdef"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    app = make_app()
    store = app.state.store if hasattr(app.state, "store") else None
    root = {"Authorization": f"Bearer {ROOT_TOKEN}"}
    keys, pid = {}, {}

    async def call(who, method, path, body=None, headers=None):
        hdr = {**(root if who == "root" else keys[who]), **(headers or {})}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.request(method, path, json=body)

    for who in ("ann", "bob"):
        r = await call("root", "POST", "/api/keys", {"name": f"{who}-laptop", "scopes": ["admin"],
                                                     "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
        ck(f"{who} has a key", r.status_code in (200, 201), r.text[:200])
        keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
        pid[who] = r.json()["person"]["id"]
    r = await call("ann", "POST", "/api/projects", {"template": "custom", "name": "ui", "objects": []})
    uid = r.json()["id"]
    lead = (await call("ann", "GET", f"/api/projects/{uid}")).json().get("lead")
    ck("ann made the project and leads it", (lead.get("id") if isinstance(lead, dict) else lead) == pid["ann"], str(lead))
    base = f"/api/projects/{uid}/secrets"

    print("== off without TARES_SECRETS_KEY ==")
    r = await call("ann", "GET", base)
    ck("the list says secrets are off and how to turn them on",
       r.status_code == 200 and r.json()["enabled"] is False and "TARES_SECRETS_KEY" in r.json()["off"], r.text)
    r = await call("ann", "PUT", f"{base}/CELL_KEY", {"value": VALUE})
    ck("a write is refused while off", r.status_code == 409, r.text)

    os.environ["TARES_SECRETS_KEY"] = "a-long-random-string-for-this-cell-only"
    print("== a member sets and lists ==")
    r = await call("ann", "PUT", f"{base}/CELL_KEY", {"value": VALUE})
    ck("the lead sets a secret", r.status_code == 200, r.text)
    body = r.text
    ck("the answer holds the name and never the value", "CELL_KEY" in body and VALUE not in body, body)
    row = r.json()["secrets"][0]
    ck("it says who set it and when, and that it was not used yet",
       row["set_by"] == "Ann" and row["set_at"] and row["used_at"] is None and row["readable"] is True, row)
    raw = app.state.store.secret_rows(uid)[0]["value"] if hasattr(app.state, "store") else None
    if raw is not None:
        ck("the database holds it sealed, not as typed", VALUE not in raw and raw.startswith("gAAAA"), raw[:20])
    for bad, why in (("lower", "a lower-case name"), ("1ST", "a name starting with a digit")):
        r = await call("ann", "PUT", f"{base}/{bad}", {"value": "x"})
        ck(f"{why} is refused", r.status_code == 422, r.text)
    r = await call("ann", "PUT", f"{base}/EMPTY", {"value": "  "})
    ck("an empty value is refused", r.status_code == 422, r.text)

    print("== who may ==")
    for method, path, b in (("GET", base, None), ("PUT", f"{base}/X", {"value": "v"}),
                            ("DELETE", f"{base}/CELL_KEY", None), ("POST", f"{base}/use", {})):
        r = await call("bob", method, path, b)
        ck(f"bob, not on the project: {method} {path.rsplit('/', 1)[-1]} is refused", r.status_code in (403, 404), r.text)
        ck("and the answer holds no value", VALUE not in r.text)
    app.state.store.set_project_kind(uid, "software_factory") if hasattr(app.state.store, "set_project_kind") else \
        app.state.store.con.execute("UPDATE usecases SET kind = 'software_factory' WHERE id = ?", [uid])
    pg = await call("bob", "GET", f"/api/factory/projects/{uid}")
    ck("the project page's read has no secrets for someone not on it",
       pg.status_code == 200 and pg.json().get("secrets") is None and VALUE not in pg.text, pg.text[:200])
    pg = await call("ann", "GET", f"/api/factory/projects/{uid}")
    ck("and the names for a member, never a value",
       pg.json()["secrets"]["secrets"][0]["name"] == "CELL_KEY" and VALUE not in pg.text, pg.text[:200])
    app.state.store.add_project_member(uid, pid["bob"], added_by="ann") if hasattr(app.state, "store") else None
    r = await call("bob", "GET", base)
    ck("once bob is a member he sees the list", r.status_code == 200 and r.json()["secrets"][0]["name"] == "CELL_KEY", r.text)
    r = await call("bob", "PUT", f"{base}/OTHER_CELL_KEY", {"value": "second-value-123"})
    ck("and any member may set one", r.status_code == 200 and [s["name"] for s in r.json()["secrets"]] == ["CELL_KEY", "OTHER_CELL_KEY"], r.text)

    print("== a crew station ==")
    st = {"x-tares-station": "ui-build-m1", "x-tares-role": "builder"}
    r = await call("bob", "PUT", f"{base}/X", {"value": "v"}, st)
    ck("a station does not set secrets", r.status_code == 403 and "console" in r.text, r.text)
    r = await call("bob", "DELETE", f"{base}/CELL_KEY", None, st)
    ck("nor remove them", r.status_code == 403, r.text)
    r = await call("bob", "POST", f"{base}/use", {}, st)
    ck("a member's station gets the values to use", r.status_code == 200
       and r.json()["values"] == {"CELL_KEY": VALUE, "OTHER_CELL_KEY": "second-value-123"}, r.text)
    r = await call("bob", "POST", f"{base}/use", {"names": ["CELL_KEY"]}, st)
    ck("--only asks for some of them", list(r.json()["values"]) == ["CELL_KEY"], r.text)
    rows = {s["name"]: s for s in (await call("ann", "GET", base)).json()["secrets"]}
    ck("the list records who used it, through which station, and when",
       rows["CELL_KEY"]["used_by"] == "Bob (ui-build-m1)" and rows["CELL_KEY"]["used_at"], rows["CELL_KEY"])

    print("== replace and remove ==")
    r = await call("ann", "PUT", f"{base}/CELL_KEY", {"value": "replaced-value-456"})
    ck("replacing keeps one row", [s["name"] for s in r.json()["secrets"]] == ["CELL_KEY", "OTHER_CELL_KEY"], r.text)
    r = await call("ann", "POST", f"{base}/use", {})
    ck("the new value is the one used", r.json()["values"]["CELL_KEY"] == "replaced-value-456", r.text)
    r = await call("ann", "DELETE", f"{base}/OTHER_CELL_KEY")
    ck("a member removes one", r.status_code == 200 and [s["name"] for s in r.json()["secrets"]] == ["CELL_KEY"], r.text)
    r = await call("ann", "DELETE", f"{base}/OTHER_CELL_KEY")
    ck("removing one that is gone is 404", r.status_code == 404, r.text)

    print("== a changed key ==")
    os.environ["TARES_SECRETS_KEY"] = "some-other-key-entirely"
    r = await call("ann", "GET", base)
    ck("a value sealed with another key reads as unreadable", r.json()["secrets"][0]["readable"] is False, r.text)
    r = await call("ann", "POST", f"{base}/use", {})
    ck("and is not handed out", r.json()["values"] == {} and r.json()["unreadable"] == ["CELL_KEY"], r.text)
    os.environ["TARES_SECRETS_KEY"] = "a-long-random-string-for-this-cell-only"

    print("== sessions ==")
    pairs = ps.scrub_pairs(app.state.store)
    line = {"type": "assistant", "message": {"content": [{"type": "text", "text": "the key is replaced-value-456 ok"}]}}
    out = ps.scrub(line, pairs)
    ck("a known value in a session line becomes [secret NAME]",
       "replaced-value-456" not in json.dumps(out) and "[secret CELL_KEY]" in json.dumps(out), json.dumps(out))
    app.state.store.set_secret(uid, "SHORT", ps.seal("abc"), "Ann")
    ck("a value under 6 characters is not replaced (it would mangle ordinary text)",
       ps.scrub("abc def", ps.scrub_pairs(app.state.store)) == "abc def")
    app.state.store.set_secret(uid, "LATER", ps.seal("added-after-cache-789"), "Ann")
    ck("a secret added later is replaced too (the cache follows changes)",
       "added-after-cache-789" not in json.dumps(ps.scrub("x added-after-cache-789 y", ps.scrub_pairs(app.state.store))))
    from tares.connectors.claude_code import ClaudeCodeConnector
    conn = ClaudeCodeConnector.__new__(ClaudeCodeConnector)
    conn.store = app.state.store
    got = conn._scrub_secrets([{"type": "user", "message": {"content": "use replaced-value-456 now"}}])
    ck("the session connector scrubs before anything is stored", "replaced-value-456" not in json.dumps(got), json.dumps(got))

    print("== MCP ==")
    from tares import mcp_factory as mf
    mf.TARESD = "http://t"
    real = mf._cx

    def cx(seconds):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=keys["ann"])
    mf._cx = cx
    try:
        text = await mf.list_secrets(uid)
    finally:
        mf._cx = real
    ck("the tool lists names and how to use them, never a value",
       "CELL_KEY" in text and "with-secrets" in text and "replaced-value-456" not in text, text)

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)

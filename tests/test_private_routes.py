"""A private project is unknown to anyone who is not on it (TR-625). Its lead, its members, the
system credential and a project key for it see it; everyone else gets 404 on every route under
/api/projects/{uid}, MCP tools included, and sees it in the list only as a name with its lead.

The sweep reads the daemon's own route list (/openapi.json), so a route added later is checked
without anyone remembering to add it here. A route that must not 404 for a stranger goes in
ALLOWLIST with its reason; the test fails if the list grows.

Run: .venv/bin/python tests/test_private_routes.py
"""
import asyncio, json, logging, os, re, shutil, socket, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
import tares.mcp_server as m

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

AUTH = "root-token"
TMP = tempfile.mkdtemp(prefix="private-routes-")

# routes a stranger may reach on a private project, as "METHOD path": why. The PATCH is not here:
# it answers 404 unless it only hands the lead over, which is tested on its own below.
ALLOWLIST: dict[str, str] = {}


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


async def _wait(url):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def start(name):
    db, port = os.path.join(TMP, name + ".duckdb"), free_port()
    env = {**clean_env(), "TARES_DB": db, "TARES_PORT": port, "TARES_OTLP_GRPC_PORT": "off",
           "TARES_AUTH_TOKEN": AUTH}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, f"http://127.0.0.1:{port}"


def routes(spec):
    """(METHOD, path template) for every operation under /api/projects/{uid}, deleting the
    project itself last (a stranger who could would end the sweep)."""
    out = []
    for path, ops in spec["paths"].items():
        if re.match(r"^/api/projects/\{[^}]+\}", path):
            out += [(meth.upper(), path) for meth in ops if meth in ("get", "post", "put", "patch", "delete")]
    return sorted(out, key=lambda r: (r == ("DELETE", "/api/projects/{uid}"), r))


def sample(spec, schema, depth=0):
    """A value that passes validation against an OpenAPI schema: required fields only."""
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        return {k: sample(spec, props[k], depth + 1) for k in schema.get("required", []) if k in props}
    return {"integer": 1, "number": 1, "boolean": True, "array": []}.get(t, "x")


def fill(path, uid):
    """The path with the project's id in its first slot and a made-up value in the others."""
    first = re.match(r"^/api/projects/(\{[^}]+\})", path).group(1)
    return re.sub(r"\{[^}]+\}", "nope", path.replace(first, uid, 1))


async def hit(cx, B, hd, spec, meth, path, uid):
    """The route called with an empty body, then with the fields it requires if it asked for them."""
    url = B + fill(path, uid)
    try:
        if meth == "GET":
            return await cx.get(url, headers=hd)
        r = await cx.request(meth, url, headers=hd, json={})
        if r.status_code == 422:
            body = (spec["paths"][path][meth.lower()].get("requestBody", {}).get("content", {})
                    .get("application/json", {}).get("schema"))
            if body:
                r = await cx.request(meth, url, headers=hd, json=sample(spec, body))
        return r
    except httpx.HTTPError as e:   # a stream that never ends counts as an answer, not a 404
        return type("R", (), {"status_code": 0, "text": repr(e)})()


def unknown(r, uid):
    return r.status_code == 404 and "unknown project" in r.text and uid in r.text


async def main():
    proc, B = start("on")
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        ROOT = H(AUTH)
        async with httpx.AsyncClient(timeout=15) as cx:
            async def person(name, email):
                return (await cx.post(f"{B}/api/people", headers=ROOT,
                                      json={"name": name, "email": email})).json()["id"]

            async def key(pid, scope):
                r = await cx.post(f"{B}/api/keys", headers=ROOT,
                                  json={"name": f"k-{pid}-{scope}", "scopes": [scope], "person_id": pid})
                return H(r.json()["secret"])

            pa, pb, pm = await person("Ann", "ann@x.dev"), await person("Bob", "bob@x.dev"), await person("Mia", "mia@x.dev")
            A_, B_, BR, M_ = (await key(pa, "admin"), await key(pb, "admin"), await key(pb, "read"),
                              await key(pm, "admin"))

            uid = (await cx.post(f"{B}/api/projects", headers=A_, json={
                "template": "custom", "name": "Secret", "objects": []})).json()["id"]
            team = (await cx.post(f"{B}/api/projects", headers=A_, json={
                "template": "custom", "name": "Open", "objects": []})).json()["id"]
            await cx.post(f"{B}/api/projects/{uid}/members", headers=A_, json={"person_id": pm})
            await cx.post(f"{B}/api/projects/{uid}/docs", headers=A_,
                          json={"kind": "note", "title": "Plan", "body": "the plan"})
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=A_, json={"scope": "private"})
            ck("set up: Ann leads a private project with Mia on it", r.status_code == 200
               and r.json()["scope"] == "private", r.text[:200])
            pk = (await cx.post(f"{B}/api/projects/{uid}/keys", headers=A_, json={"name": "agent"})).json()
            PK = H(pk.get("secret") or pk.get("key") or "")
            ck("set up: a project key for it", len(PK["Authorization"]) > 20, str(pk)[:200])
            spec = (await cx.get(f"{B}/openapi.json", headers=ROOT)).json()
            rt = routes(spec)
            ck("the sweep has routes to sweep", len(rt) > 60, str(len(rt)))

            print("== a stranger gets 404 on every route ==")
            for who, hd, only_get in (("Bob, admin scope", B_, False), ("Bob, read scope", BR, True)):
                bad = []
                for meth, path in rt:
                    if only_get and meth != "GET":
                        continue
                    if f"{meth} {path}" in ALLOWLIST:
                        continue
                    r = await hit(cx, B, hd, spec, meth, path, uid)
                    # a read key is refused for lack of scope before the project is looked at,
                    # which says nothing about the project
                    if not (unknown(r, uid) or (only_get and r.status_code == 403 and "lacks" in r.text)):
                        bad.append(f"{meth} {path} -> {r.status_code} {r.text[:80]}")
                ck(f"{who}: unknown project on all {sum(1 for mm, _ in rt if not only_get or mm == 'GET')} routes",
                   not bad, "\n      ".join(bad))
            ck("the allowlist stays small", len(ALLOWLIST) <= 3, str(ALLOWLIST))

            print("== the people on it, the system and a project key see it ==")
            for who, hd in (("the lead", A_), ("a member", M_), ("the system credential", ROOT),
                            ("a project key", PK)):
                g = await cx.get(f"{B}/api/projects/{uid}", headers=hd)
                d = await cx.get(f"{B}/api/projects/{uid}/docs", headers=hd)
                ck(f"{who}: the project and its docs", g.status_code == 200 and d.status_code == 200
                   and g.json()["name"] == "Secret", f"{g.status_code} {g.text[:80]} / {d.status_code} {d.text[:80]}")
            ck("a team project is open to Bob", (await cx.get(f"{B}/api/projects/{team}", headers=B_)).status_code == 200)
            ck("... and its docs", (await cx.get(f"{B}/api/projects/{team}/docs", headers=BR)).status_code == 200)

            print("== the list ==")
            lst = (await cx.get(f"{B}/api/projects", headers=B_)).json()["projects"]
            row = next((p for p in lst if p["id"] == uid), None)
            ck("Bob sees it as a name and its lead, nothing else", row == {
                "id": uid, "name": "Secret", "scope": "private", "hidden": True,
                "lead": {"id": pa, "name": "Ann"}}, str(row))
            ck("... and the team project whole", any(p["id"] == team and "template" in p for p in lst))
            lst = (await cx.get(f"{B}/api/projects", headers=A_)).json()["projects"]
            row = next(p for p in lst if p["id"] == uid)
            ck("Ann sees it whole", "hidden" not in row and row["name"] == "Secret" and "members" in row, str(row)[:200])
            lst = (await cx.get(f"{B}/api/projects", headers=ROOT)).json()["projects"]
            ck("the system credential sees it whole", "hidden" not in next(p for p in lst if p["id"] == uid))

            print("== naming it ==")
            r = await cx.get(f"{B}/api/triggers", headers=B_, params={"project": "Secret"})
            ck("by name, to a stranger, it is as unknown as a name that does not exist",
               r.status_code == (await cx.get(f"{B}/api/triggers", headers=B_,
                                              params={"project": "No such"})).status_code, f"{r.status_code} {r.text[:100]}")

            for ref, what in (("Secret", "by name"), (uid, "by id")):
                r = await cx.post(f"{B}/api/keys", headers=B_, json={"name": "peek", "project": ref})
                nope = await cx.post(f"{B}/api/keys", headers=B_, json={"name": "peek", "project": "No such"})
                ck(f"a key for it, {what}, to a stranger: as unknown as a name that does not exist",
                   r.status_code == nope.status_code == 400 and "unknown project" in r.text and "secret" not in r.text,
                   f"{r.status_code} {r.text[:120]}")
            r = await cx.post(f"{B}/api/sources", headers=B_, json={
                "name": "peek-src", "connector": "docker_logs", "config": {"container": "x"}, "project": "Secret"})
            ck("a source created into it, to a stranger: unknown project, and no source made",
               r.status_code == 400 and "unknown project" in r.text
               and "peek-src" not in [x["name"] for x in (await cx.get(f"{B}/api/sources", headers=ROOT)).json()],
               f"{r.status_code} {r.text[:120]}")
            r = await cx.post(f"{B}/api/keys", headers=A_, json={"name": "mine", "project": "Secret"})
            ck("... while its lead gets the key", r.status_code == 201 and r.json().get("secret"), f"{r.status_code} {r.text[:120]}")

            print("== an admin who is not on it hands the lead over, and nothing else ==")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=B_, json={"scope": "team"})
            ck("scope: unknown", unknown(r, uid), f"{r.status_code} {r.text[:120]}")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=B_, json={"lead_person_id": pb, "scope": "team"})
            ck("lead with scope: unknown", unknown(r, uid), f"{r.status_code} {r.text[:120]}")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=BR, json={"lead_person_id": pb})
            ck("a read key may not", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
            r = await cx.patch(f"{B}/api/projects/{uid}", headers=B_, json={"lead_person_id": pm})
            ck("the lead goes to Mia, answered as the hidden row", r.status_code == 200 and r.json() == {
                "id": uid, "name": "Secret", "scope": "private", "hidden": True,
                "lead": {"id": pm, "name": "Mia"}}, f"{r.status_code} {r.text[:200]}")
            g = (await cx.get(f"{B}/api/projects/{uid}", headers=A_)).json()
            ck("Ann, who handed it over, stays on it", (pa, "manual") in [(x["id"], x["reason"]) for x in g["members"]],
               str(g["members"]))
            r = await cx.get(f"{B}/api/projects/{uid}", headers=B_)
            ck("... and Bob still does not see it", unknown(r, uid), f"{r.status_code} {r.text[:100]}")
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=B_, json={"person_id": pb})
            ck("a stranger cannot add themselves", unknown(r, uid), f"{r.status_code} {r.text[:100]}")
            r = await cx.post(f"{B}/api/projects/{uid}/members", headers=M_, json={"person_id": pb})
            ck("the lead (Mia) adds Bob", r.status_code == 200, r.text[:150])
            ck("... and Bob sees it", (await cx.get(f"{B}/api/projects/{uid}", headers=B_)).status_code == 200)

            print("== the MCP tools ==")
            await cx.delete(f"{B}/api/projects/{uid}/members/{pb}", headers=M_)
            doc = next(d["id"] for d in (await cx.get(f"{B}/api/projects/{uid}/docs", headers=M_)).json() if d["title"] == "Plan")
            m.TARESD = B
            m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=B_)
            out = await m.get_doc(doc, project="Secret")
            ck("get_doc on a private project, as a stranger: not found", "no project" in out and "the plan" not in out, out[:200])
            out = await m.get_doc(doc, project=uid)
            ck("... by id too", "no project" in out and "the plan" not in out, out[:200])
            out = json.loads(await m.list_projects())
            ck("list_projects leaves it out", uid not in [p["id"] for p in out] and team in [p["id"] for p in out], str(out)[:200])
            m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=M_)
            out = await m.get_doc(doc, project="Secret")
            ck("get_doc as the lead", "the plan" in out, out[:200])
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())

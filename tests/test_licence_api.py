"""The cell side of the licence (TR-912): where the key comes from, what /api/licence says, and how
the cell warns and answers when a licence is missing.

Three daemons that trust the test root (licence_testkit.daemon_argv), auth on:
  A  no TARES_LICENSE: nothing gives source "none"; PUT of a bad key is 400 and stores nothing; PUT of a
     good key is 200 and GET shows it at once; a read key may GET but not PUT or DELETE; a project key
     may not GET; DELETE removes it; /metrics has the four tares_licence_* lines; no body (and no log
     line) holds the key text or any 20-character slice of it.
  B  TARES_LICENSE not a valid key: it still wins over a valid stored key, with valid false and its reason.
  C  TARES_LICENSE a valid key: it wins over a stored key, and PUT says env_in_use.
In this process: the desk warnings with a frozen clock (30, 7, 0 days, over seats, a renewal), the order
of env and setting in resolve(), ee_allowed(), and Host.licence_required answering 402 with exactly
{code, message, url}.

Run: python tests/test_licence_api.py
"""
import asyncio, json, os, re, shutil, socket, subprocess, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import clean_env, scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-licence-api-")
os.environ["TARES_DB"] = os.path.join(TMP, "inproc.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
import licence_testkit as kit
import tares.licence as L

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


AUTH = "root-token-licence"
UTC = timezone.utc
TODAY = datetime.now(UTC).date()
GET_KEYS = {"valid", "source", "kid", "org", "seats", "used", "features", "expires", "days_left",
            "expired", "over_seats", "ee_allowed", "reason", "stored", "env_set"}

root_priv, root_pub = kit.keypair(kit.ROOT_PHRASE)
iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
CERT = kit.issuer_cert(root_priv, iss_pub, (TODAY + timedelta(days=3000)).isoformat())


def make(org, seats, kid, expires, features=("enterprise",), issued="2026-11-05T00:00:00Z"):
    return kit.sign(iss_priv, CERT, org, seats, features, issued, expires, kid)


LIC_A = make("Acme", 5, "lic_a", (TODAY + timedelta(days=100)).isoformat())
LIC_B = make("Beta Org", 3, "lic_b", (TODAY + timedelta(days=200)).isoformat())
# a good key with its signature spoiled: well formed, refused
BAD = LIC_A[:-4] + ("AAAA" if not LIC_A.endswith("AAAA") else "BBBB")
BROKEN = "TL1.broken.broken"


def leaks(text: str, key: str) -> bool:
    """True when `text` holds the key or any 20-character slice of it."""
    return any(key[i:i + 20] in text for i in range(0, len(key) - 19))


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def start(name, **env):
    port = free_port()
    log = open(os.path.join(TMP, name + ".log"), "w")
    e = {**clean_env(), "TARES_DB": os.path.join(TMP, name + ".duckdb"), "TARES_PORT": port,
         "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": AUTH,
         "TARES_CATALOG": os.path.join(TMP, "none.yaml")}
    e.update(env)
    proc = subprocess.Popen(kit.daemon_argv(), env=e, stdout=log, stderr=subprocess.STDOUT)
    return proc, f"http://127.0.0.1:{port}", os.path.join(TMP, name + ".log")


async def wait_up(url):
    for _ in range(240):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url + "/health", timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def metric(text, name):
    m = re.search(rf"^{name} (\S+)$", text, re.M)
    return float(m.group(1)) if m else None


async def daemon_a(B, bodies):
    """No environment key."""
    async with httpx.AsyncClient(base_url=B, timeout=30) as cx:
        async def get(tok=AUTH):
            r = await cx.get("/api/licence", headers=H(tok)); bodies.append(r.text); return r
        async def put(key, tok=AUTH):
            r = await cx.put("/api/licence", headers=H(tok), json={"key": key}); bodies.append(r.text); return r
        async def delete(tok=AUTH):
            r = await cx.delete("/api/licence", headers=H(tok)); bodies.append(r.text); return r

        print("== A: no environment key ==")
        r = await cx.get("/api/licence")
        ck("no credential: 401", r.status_code == 401, r.text)
        r = await get()
        g = r.json()
        ck("GET with nothing set: 200", r.status_code == 200, r.text)
        ck("... source none, not valid, not stored, no env", g["source"] == "none" and g["valid"] is False
           and g["stored"] is False and g["env_set"] is False and g["ee_allowed"] is False, g)
        ck("... exactly the contract's keys", set(g) == GET_KEYS, sorted(set(g) ^ GET_KEYS))
        ck("... used counts the people on the cell", g["used"] >= 1, g)

        r = await put("")
        ck("PUT with an empty key: 400 that names DELETE", r.status_code == 400 and "DELETE" in r.text, r.text)
        r = await put("   ")
        ck("PUT with a blank key: 400", r.status_code == 400, r.text)
        r = await cx.put("/api/licence", headers=H(AUTH), json={"nope": 1}); bodies.append(r.text)
        ck("PUT with no key field: 400", r.status_code == 400, r.text)
        r = await put(BAD)
        ck("PUT of an invalid key: 400 with the reason", r.status_code == 400 and r.json()["detail"]
           and "signature" in r.json()["detail"], r.text)
        r = await put(BROKEN)
        ck("PUT of text that is no key: 400", r.status_code == 400, r.text)
        g = (await get()).json()
        ck("... nothing was stored", g["stored"] is False and g["source"] == "none", g)

        r = await put(LIC_A)
        p = r.json()
        ck("PUT of a valid key: 200", r.status_code == 200, r.text)
        ck("... the GET body plus env_in_use false", set(p) == GET_KEYS | {"env_in_use"}
           and p["env_in_use"] is False, sorted(set(p) ^ (GET_KEYS | {"env_in_use"})))
        ck("... valid, from settings, stored", p["valid"] and p["source"] == "settings" and p["stored"]
           and p["env_set"] is False, p)
        ck("... org, seats, kid, features, ee_allowed", p["org"] == "Acme" and p["seats"] == 5
           and p["kid"] == "lic_a" and p["features"] == ["enterprise"] and p["ee_allowed"] is True, p)
        ck("... days_left counts to the expiry date", p["days_left"] in (99, 100) and p["expired"] is False
           and p["over_seats"] is False, p)
        g = (await get()).json()
        ck("GET shows it in the next request, no restart", g["valid"] and g["org"] == "Acme"
           and g["stored"] is True and "env_in_use" not in g, g)

        # keys: a read key with a person, a project key
        r = await cx.post("/api/keys", headers=H(AUTH), json={
            "name": "reader", "scopes": ["read"], "person": {"email": "ada@example.com", "name": "Ada"}})
        reader = r.json()["secret"]
        r = await cx.post("/api/projects", headers=H(AUTH), json={"template": "custom", "name": "P1", "objects": []})
        uid = r.json()["id"]
        r = await cx.post(f"/api/projects/{uid}/keys", headers=H(AUTH), json={"name": "pk", "scopes": ["read"]})
        pkey = r.json().get("secret", "")
        ck("both keys were made", reader.startswith("nvf_") and pkey.startswith("nvf_"))

        r = await get(reader)
        ck("a read key of a person: GET 200", r.status_code == 200 and r.json()["org"] == "Acme", r.text)
        ck("... and sees the new person in `used`", r.json()["used"] >= 2, r.text)
        r = await put(LIC_B, reader)
        ck("... PUT 403", r.status_code == 403, r.text)
        r = await delete(reader)
        ck("... DELETE 403", r.status_code == 403, r.text)
        g = (await get()).json()
        ck("... and the stored key is untouched", g["org"] == "Acme" and g["stored"], g)
        r = await get(pkey)
        ck("a project key: GET 403", r.status_code == 403, r.text)
        r = await put(LIC_B, pkey)
        ck("... PUT 403", r.status_code == 403, r.text)

        m = (await cx.get("/metrics")).text
        bodies.append(m)
        g = (await get()).json()
        ck("/metrics: tares_licence_valid 1", metric(m, "tares_licence_valid") == 1.0, m[-400:])
        ck("/metrics: tares_licence_seats 5", metric(m, "tares_licence_seats") == 5.0)
        ck("/metrics: tares_licence_used is the people on the cell",
           metric(m, "tares_licence_used") == float(g["used"]), (metric(m, "tares_licence_used"), g["used"]))
        ck("/metrics: tares_licence_days_left is the days left",
           metric(m, "tares_licence_days_left") in (99.0, 100.0), metric(m, "tares_licence_days_left"))

        r = await delete()
        d = r.json()
        ck("DELETE: 200 with the GET body", r.status_code == 200 and set(d) == GET_KEYS, r.text)
        ck("... stored false, source none, not valid", d["stored"] is False and d["source"] == "none"
           and d["valid"] is False and d["ee_allowed"] is False, d)
        g = (await get()).json()
        ck("... and GET agrees", g["source"] == "none" and g["stored"] is False, g)
        m = (await cx.get("/metrics")).text
        ck("/metrics after DELETE: valid 0, seats 0, days_left 0", metric(m, "tares_licence_valid") == 0.0
           and metric(m, "tares_licence_seats") == 0.0 and metric(m, "tares_licence_days_left") == 0.0)
        r = await delete()
        ck("DELETE again is fine", r.status_code == 200 and r.json()["stored"] is False, r.text)


async def daemon_b(B, bodies):
    """The environment holds a key that is not valid."""
    print("== B: TARES_LICENSE is not a valid key, a valid one is stored ==")
    async with httpx.AsyncClient(base_url=B, timeout=30) as cx:
        g = (await cx.get("/api/licence", headers=H(AUTH))).json()
        ck("env invalid, nothing stored: source env, not valid, with its reason", g["source"] == "env"
           and g["valid"] is False and g["reason"] and g["env_set"] is True and g["stored"] is False, g)
        r = await cx.put("/api/licence", headers=H(AUTH), json={"key": LIC_A}); bodies.append(r.text)
        p = r.json()
        ck("PUT valid while env is set: 200, env_in_use true", r.status_code == 200 and p["env_in_use"] is True, r.text)
        ck("... the source is still env, and not valid, with the env key's reason",
           p["source"] == "env" and p["valid"] is False and p["reason"] and p["stored"] is True
           and p["ee_allowed"] is False, p)
        r = await cx.get("/api/licence", headers=H(AUTH)); bodies.append(r.text)
        g = r.json()
        ck("GET: the invalid env key still wins over the valid stored one", g["source"] == "env"
           and g["valid"] is False and g["org"] is None and g["stored"] is True, g)
        m = (await cx.get("/metrics")).text
        ck("/metrics: valid 0", metric(m, "tares_licence_valid") == 0.0)
        g = (await cx.delete("/api/licence", headers=H(AUTH))).json()
        ck("DELETE removes the stored key, env stays", g["stored"] is False and g["source"] == "env", g)


async def daemon_c(B, bodies):
    """The environment holds a valid key."""
    print("== C: TARES_LICENSE is valid, another valid key is stored ==")
    async with httpx.AsyncClient(base_url=B, timeout=30) as cx:
        g = (await cx.get("/api/licence", headers=H(AUTH))).json()
        ck("env valid: source env, valid, its org", g["source"] == "env" and g["valid"] is True
           and g["org"] == "Beta Org" and g["env_set"] is True and g["stored"] is False, g)
        r = await cx.put("/api/licence", headers=H(AUTH), json={"key": LIC_A}); bodies.append(r.text)
        p = r.json()
        ck("PUT valid while env is set: 200, env_in_use true", r.status_code == 200 and p["env_in_use"] is True, r.text)
        ck("... still the env key in force (Beta Org, source env)", p["source"] == "env"
           and p["org"] == "Beta Org" and p["stored"] is True, p)
        g = (await cx.get("/api/licence", headers=H(AUTH))).json()
        ck("GET: the env key wins over the stored one", g["source"] == "env" and g["org"] == "Beta Org"
           and g["seats"] == 3, g)
        m = (await cx.get("/metrics")).text
        ck("/metrics: seats 3 from the env key", metric(m, "tares_licence_seats") == 3.0)


# ── in this process ──
def desk_items(store, needle):
    return [d for d in store.list_desk("open") if needle in d["question"]]


def in_process():
    from tares.daemon import make_app
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})
    app = make_app()
    store = app.state.store
    h = app.state.host

    print("== env and setting, in this process ==")
    L._cache = None
    ck("ee_allowed is False before any check", L.ee_allowed() is False)
    ck("resolve with nothing: (None, none)", L.resolve(store) == (None, "none"))
    store.set_setting(L.LICENCE_SETTING, LIC_A)
    ck("resolve: the stored key, from settings", L.resolve(store) == (LIC_A, "settings"))
    os.environ["TARES_LICENSE"] = LIC_B
    ck("resolve: env beats the stored key", L.resolve(store) == (LIC_B, "env"))
    os.environ["TARES_LICENSE"] = BROKEN
    ck("resolve: an invalid env key still beats a valid stored one", L.resolve(store) == (BROKEN, "env"))
    st = L.current(store, force=True)
    ck("current: the env key's verdict, not the stored key's", st["source"] == "env" and st["valid"] is False
       and st["reason"] and st["stored"] is True and st["env_set"] is True, st)
    ck("ee_allowed follows the last check (invalid: no)", L.ee_allowed() is False)
    os.environ["TARES_LICENSE"] = "   "
    ck("resolve: a blank env is not set", L.resolve(store) == (LIC_A, "settings"))
    del os.environ["TARES_LICENSE"]
    st = L.current(store, force=True)
    ck("current: settings key valid, ee_allowed true", st["source"] == "settings" and st["valid"] and L.ee_allowed()
       and L.ee_allowed("enterprise") and not L.ee_allowed("other-feature"), st)
    store.set_setting(L.LICENCE_SETTING, LIC_B)
    ck("current is remembered: the old key until forced or a minute", L.current(store)["org"] == "Acme")
    ck("... and fresh when forced", L.current(store, force=True)["org"] == "Beta Org")
    store.create_person("Zed", "zed@example.com")
    ck("... the seats in use are never remembered", L.current(store)["used"] == len(store.list_people()))
    ck("peek is the remembered answer, with the seats the last check counted",
       L.peek()["org"] == "Beta Org" and L.peek()["used"] == len(store.list_people()), L.peek())
    store.create_person("Xan", "xan@example.com")
    seen = []
    orig = store.list_people
    store.list_people = lambda *a, **k: (seen.append(1), orig(*a, **k))[1]
    try:
        peeked = L.peek()
    finally:
        store.list_people = orig
    ck("peek reads no state database", seen == [] and peeked["org"] == "Beta Org", seen)
    L.current(store)
    ck("... and follows `used` once a check has run", L.peek()["used"] == len(store.list_people()), L.peek())
    L._cache = None
    ck("peek is None before the first check", L.peek() is None)
    L.current(store, force=True)
    ck("the host's licence_state is the same check", h.licence_state()["org"] == "Beta Org"
       and set(h.licence_state()) == GET_KEYS)

    print("== the desk, with a frozen clock ==")
    exp = TODAY + timedelta(days=700)
    lic_d = make("Dee Corp", 5, "lic_d", exp.isoformat())
    store.set_setting(L.LICENCE_SETTING, lic_d)

    def at(days_before):
        return datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC) - timedelta(days=days_before)

    steps = [(31, 0), (30, 1), (29, 1), (30, 1), (7, 2), (7, 2), (6, 2), (0, 3), (0, 3), (-3, 3)]
    for days, want in steps:
        L.licence_desk(store, at(days))
        got = len(desk_items(store, "Dee Corp"))
        ck(f"{days} days before the end: {want} item(s) on the desk", got == want, got)
    items = desk_items(store, "Dee Corp")
    qs = sorted(d["question"] for d in items)
    ck("the items say 30 days, 7 days, and the last day, in plain words",
       any("ends in 30 days" in q for q in qs) and any("ends in 7 days" in q for q in qs)
       and any("ends today" in q for q in qs), qs)
    ck("... from tares, not blocking, no options", all(d["asked_by"] == "tares" and not d["blocking"]
       and d["options"] == [] for d in items), items[:1])
    ck("... no em dash in the words", all("—" not in d["question"] for d in items))
    warned = json.loads(store.get_setting(L.WARNED_SETTING))
    ck("the warned list holds <kid>:<threshold>", sorted(warned) == ["lic_d:0", "lic_d:30", "lic_d:7"], warned)

    lic_e = make("Dee Corp", 5, "lic_e", exp.isoformat())
    store.set_setting(L.LICENCE_SETTING, lic_e)
    L.licence_desk(store, at(30))
    ck("a renewed key (new kid) warns again at 30 days", len(desk_items(store, "Dee Corp")) == 4,
       len(desk_items(store, "Dee Corp")))
    L.licence_desk(store, at(29))
    ck("... once", len(desk_items(store, "Dee Corp")) == 4)
    L.licence_desk(store, at(7))
    ck("... and at 7 days", len(desk_items(store, "Dee Corp")) == 5)
    ck("... the list keeps the new kid only", sorted(json.loads(store.get_setting(L.WARNED_SETTING)))
       == ["lic_e:30", "lic_e:7"], store.get_setting(L.WARNED_SETTING))

    lic_f = make("Fay Ltd", 5, "lic_f", exp.isoformat())
    store.set_setting(L.LICENCE_SETTING, lic_f)
    L.licence_desk(store, at(5))
    ck("a key first seen 5 days before the end: one item, not one per threshold",
       len(desk_items(store, "Fay Ltd")) == 1, desk_items(store, "Fay Ltd"))
    L.licence_desk(store, at(3))
    ck("... and nothing more on later days", len(desk_items(store, "Fay Ltd")) == 1)
    L.licence_desk(store, at(-2))
    qs = [d["question"] for d in desk_items(store, "Fay Ltd")]
    ck("a key past its end says it ran out", len(qs) == 2 and any("ran out on" in q for q in qs), qs)

    store.set_setting(L.LICENCE_SETTING, BAD)
    before = len(store.list_desk("open"))
    L.licence_desk(store, at(1))
    ck("a key that is not valid adds nothing", len(store.list_desk("open")) == before)
    store.set_setting(L.LICENCE_SETTING, None)
    L.licence_desk(store, at(1))
    ck("no key adds nothing", len(store.list_desk("open")) == before)

    print("== over seats ==")
    lic_s = make("Seat Co", 1, "lic_s", (TODAY + timedelta(days=900)).isoformat())
    store.set_setting(L.LICENCE_SETTING, lic_s)
    n = len(store.list_people())
    ck("the cell has more people than the licence has seats", n > 1, n)
    L.licence_desk(store, datetime.now(UTC))
    items = desk_items(store, "Seat Co")
    ck("over seats: one item that says how many seats and people", len(items) == 1
       and "1 seat" in items[0]["question"] and f"{n} people" in items[0]["question"], items)
    L.licence_desk(store, datetime.now(UTC))
    store.create_person("Yan", "yan@example.com")
    L.licence_desk(store, datetime.now(UTC))
    ck("... and only once", len(desk_items(store, "Seat Co")) == 1)
    st = L.current(store, force=True)
    ck("the state says over_seats", st["over_seats"] is True and st["used"] == len(store.list_people()), st)

    print("== licence_required, answered 402 ==")
    @app.post("/needs-a-licence")
    def need():
        h.licence_required("Teams need an enterprise licence.")

    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            return await cx.post("/needs-a-licence", json={})
    r = asyncio.run(call())
    ck("the route answers 402", r.status_code == 402, r.status_code)
    body = r.json()
    ck("... with exactly code, message and url at the top level", set(body) == {"code", "message", "url"}, body)
    ck("... licence_required, the words given, the enterprise page", body.get("code") == "licence_required"
       and body.get("message") == "Teams need an enterprise licence."
       and body.get("url") == "https://docs.glassflow.ai/tares/enterprise", body)
    print("== PUT writes the setting only for a valid key ==")
    store.set_setting(L.LICENCE_SETTING, None)

    async def put_key(key):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cx:
            return await cx.put("/api/licence", json={"key": key})
    r = asyncio.run(put_key(BROKEN))
    ck("PUT of an invalid key: 400", r.status_code == 400, r.status_code)
    ck("... and the setting row is still empty", store.get_setting(L.LICENCE_SETTING) in (None, ""),
       "the setting was written")
    r = asyncio.run(put_key(LIC_A))
    ck("PUT of a valid key: 200", r.status_code == 200, r.status_code)
    ck("... and the setting row holds it", store.get_setting(L.LICENCE_SETTING) == LIC_A,
       "the setting was not written")
    store.set_setting(L.LICENCE_SETTING, None)
    try:
        L.licence_required("x")
        ck("licence_required raises", False)
    except L.LicenceRequired as e:
        ck("licence_required raises LicenceRequired with the message", e.message == "x")
    app.state.store.close() if hasattr(app.state.store, "close") else None


async def main():
    ps = {}
    for name, env in (("a", {}), ("b", {"TARES_LICENSE": BROKEN}), ("c", {"TARES_LICENSE": LIC_B})):
        ps[name] = start(name, **env)
    bodies = {"a": [], "b": [], "c": []}
    try:
        ups = await asyncio.gather(*(wait_up(ps[n][1]) for n in ps))
        ck("three daemons came up", all(ups), ups)
        if all(ups):
            await daemon_a(ps["a"][1], bodies["a"])
            await daemon_b(ps["b"][1], bodies["b"])
            await daemon_c(ps["c"][1], bodies["c"])
    finally:
        for proc, _, _ in ps.values():
            proc.terminate()
        for proc, _, _ in ps.values():
            try:
                proc.wait(timeout=20)
            except Exception:
                proc.kill()

    print("== the key text never leaves ==")
    ck("the leak check catches a leak", leaks("x" + LIC_A + "y", LIC_A) and leaks(LIC_A[30:70], LIC_A)
       and not leaks("nothing here", LIC_A))
    ck("A: no body holds the key or a 20-character slice of it (GET, PUT, DELETE, 400, 403, /metrics)",
       bodies["a"] and not any(leaks(b, LIC_A) or leaks(b, LIC_B) or leaks(b, BAD) for b in bodies["a"]),
       len(bodies["a"]))
    ck("B: no body holds the stored or the env key", bodies["b"] and not any(
        leaks(b, LIC_A) or leaks(b, BROKEN + BROKEN) for b in bodies["b"]))
    ck("C: no body holds the stored or the env key", bodies["c"] and not any(
        leaks(b, LIC_A) or leaks(b, LIC_B) for b in bodies["c"]))
    log = open(ps["a"][2]).read() + open(ps["b"][2]).read() + open(ps["c"][2]).read()
    ck("the logs say a key was stored (with its kid) and removed", "licence key stored (kid lic_a)" in log
       and "licence key removed" in log, log[-600:])
    ck("... and hold no key text", not any(leaks(log, k) for k in (LIC_A, LIC_B, BAD)))


try:
    asyncio.run(main())
    in_process()
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

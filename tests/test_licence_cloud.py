"""A Tares Cloud cell never shows licence wording (TR-1088).

The control plane sets TARES_WORKSPACE_API_URL on a cloud cell. On it the desk gets no licence
items, and a refused team action answers 402 with plain words that name no licence, key or
enterprise and carry no docs link. The check itself does not change: a cloud cell with no key still
cannot add a person (D165). A self-hosted cell keeps today's words and link.

In process, no ports. Run: python tests/test_licence_cloud.py
"""
import asyncio, os, re, sys, tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-licence-cloud-")
ROOT = "root-token-licence-cloud"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
import licence_testkit as kit
import tares.licence as L

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


UTC = timezone.utc
TODAY = datetime.now(UTC).date()
CLOUD_URL = "https://workspace-api.example.test"
WORDS = re.compile(r"licen[cs]e|enterprise|\bkey\b", re.I)
DOCS = "https://docs.glassflow.ai/tares/enterprise"

root_priv, root_pub = kit.keypair(kit.ROOT_PHRASE)
iss_priv, iss_pub = kit.keypair(kit.ISSUER_PHRASE)
CERT = kit.issuer_cert(root_priv, iss_pub, (TODAY + timedelta(days=3000)).isoformat())


def make(org, seats, kid, expires):
    return kit.sign(iss_priv, CERT, org, seats, ("enterprise",), "2026-11-05T00:00:00Z", expires, kid)


def cloud(on=True):
    if on:
        os.environ["TARES_WORKSPACE_API_URL"] = CLOUD_URL
    else:
        os.environ.pop("TARES_WORKSPACE_API_URL", None)


def desk_items(store, needle):
    return [d for d in store.list_desk("open") if needle in d["question"]]


def main():
    from tares.daemon import make_app
    L._set_root_keys_for_tests({kit.kid_of(root_pub): root_pub})
    app = make_app()
    store = app.state.store
    h = app.state.host
    H = {"Authorization": f"Bearer {ROOT}"}

    print("== the words ==")
    ck("the cloud words name no licence, key or enterprise", not WORDS.search(L.CLOUD_WORDS), L.CLOUD_WORDS)
    ck("... and no link", "http" not in L.CLOUD_WORDS and "docs." not in L.CLOUD_WORDS)
    ck("... and no em dash", "—" not in L.CLOUD_WORDS)

    print("== which cell is a cloud cell ==")
    cloud(False)
    ck("no TARES_WORKSPACE_API_URL: self-hosted", L.hosted() is False)
    os.environ["TARES_WORKSPACE_API_URL"] = "   "
    ck("a blank one: self-hosted", L.hosted() is False)
    cloud(True)
    ck("set: cloud", L.hosted() is True)

    print("== the desk on a cloud cell ==")
    exp = TODAY + timedelta(days=700)

    def at(days_before):
        return datetime(exp.year, exp.month, exp.day, 12, 0, tzinfo=UTC) - timedelta(days=days_before)

    store.set_setting(L.LICENCE_SETTING, make("Cloud Co", 5, "lic_cloud", exp.isoformat()))
    cloud(True)
    for days in (31, 30, 29, 7, 6, 0, -3):
        ret = L.licence_desk(store, at(days))
        ck(f"{days} days before the end: no item on the desk", len(desk_items(store, "Cloud Co")) == 0
           and not ret, str(ret))
    ck("... and nothing was marked as warned", store.get_setting(L.WARNED_SETTING) in (None, "", "[]"),
       store.get_setting(L.WARNED_SETTING))
    store.set_setting(L.LICENCE_SETTING, make("Seat Co", 1, "lic_seat", exp.isoformat()))
    store.create_person("Zed", "zed@example.com")
    ck("over seats: the cell has more people than seats", len(store.list_people()) > 1)
    ret = L.licence_desk(store, datetime.now(UTC))
    ck("over seats: no item on the desk", len(desk_items(store, "Seat Co")) == 0 and not ret, str(ret))
    ck("no desk item mentions a licence at all", not [d for d in store.list_desk("open")
                                                      if WORDS.search(d["question"])],
       [d["question"] for d in store.list_desk("open")])

    print("== the same keys on a self-hosted cell still warn ==")
    cloud(False)
    store.set_setting(L.LICENCE_SETTING, make("Cloud Co", 5, "lic_cloud", exp.isoformat()))
    for days, want in ((31, 0), (30, 1), (7, 2), (0, 3)):
        L.licence_desk(store, at(days))
        ck(f"{days} days before the end: {want} item(s)", len(desk_items(store, "Cloud Co")) == want,
           len(desk_items(store, "Cloud Co")))
    store.set_setting(L.LICENCE_SETTING, make("Seat Co", 1, "lic_seat", exp.isoformat()))
    L.licence_desk(store, datetime.now(UTC))
    ck("over seats: one item", len(desk_items(store, "Seat Co")) == 1, len(desk_items(store, "Seat Co")))

    print("== a refused team action, answered 402 ==")

    @app.post("/needs-a-licence")
    def need():
        h.licence_required("Teams need an enterprise licence.")

    async def post(path, body, auth=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=H if auth else {}) as cx:
            return await cx.post(path, json=body)

    cloud(True)
    r = asyncio.run(post("/needs-a-licence", {}))
    b = r.json()
    ck("cloud: 402", r.status_code == 402, r.status_code)
    ck("... with the same three keys", set(b) == {"code", "message", "url"}, b)
    ck("... code licence_required, the plain words, no link", b.get("code") == "licence_required"
       and b.get("message") == L.CLOUD_WORDS and b.get("url") == "", b)
    ck("... nothing in the body names a licence, key or enterprise page", "enterprise" not in r.text.lower()
       and DOCS not in r.text and not WORDS.search(b["message"]), r.text)
    cloud(False)
    r = asyncio.run(post("/needs-a-licence", {}))
    b = r.json()
    ck("self-hosted: 402 with the words given and the enterprise page", r.status_code == 402
       and b == {"code": "licence_required", "message": "Teams need an enterprise licence.", "url": DOCS}, b)

    print("== the check itself does not change on a cloud cell ==")
    store.set_setting(L.LICENCE_SETTING, None)
    L.current(store, force=True)
    people = len(store.list_people())
    for label, on in (("cloud", True), ("self-hosted", False)):
        cloud(on)
        ck(f"{label}, no key: ee_allowed is False", L.ee_allowed() is False)
        r = asyncio.run(post("/api/people", {"name": f"New {label}", "email": f"{label}@example.com"}))
        b = r.json()
        ck(f"{label}, no key: adding a person is refused (402)", r.status_code == 402, f"{r.status_code} {r.text}")
        ck(f"{label}, no key: nobody was added", len(store.list_people()) == people, len(store.list_people()))
        if on:
            ck("cloud: the refusal is the plain words, no link", b.get("message") == L.CLOUD_WORDS
               and b.get("url") == "" and not WORDS.search(b["message"]), r.text)
        else:
            ck("self-hosted: the refusal is today's words and link", "enterprise licence" in b.get("message", "")
               and b.get("url") == DOCS, r.text)

    print("== the watch still refreshes the check on a cloud cell ==")

    def age_cache():
        c = L._cache
        L._cache = (c[0], c[1] - L.CACHE_SECONDS - 1, c[2])

    for label, on in (("cloud", True), ("self-hosted", False)):
        cloud(on)
        store.set_setting(L.LICENCE_SETTING, make("Watch Co", 9, "lic_watch_" + label, exp.isoformat()))
        L.current(store, force=True)
        ck(f"{label}: a valid key allows ee", L.ee_allowed() is True)
        store.set_setting(L.LICENCE_SETTING, None)   # cleared behind the check
        ck(f"{label}: the check still remembers the old key", L.ee_allowed() is True)
        age_cache()
        L.licence_desk(store)   # one tick of the watch, the cache older than a minute
        ck(f"{label}: one tick of the watch noticed the cleared key", L.ee_allowed() is False)
        ck(f"{label}: ... and left nothing on the desk", len(desk_items(store, "Watch Co")) == 0)
    cloud(False)


main()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

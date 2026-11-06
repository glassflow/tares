"""Private projects stay hidden, and every key stays its own person, in every licence state (TR-915).

Who may see a project and who a key belongs to are decided in core (`_can_see`, `_key_person`,
`_current_person`, `store.key_person_id` and the event filter), never by the licence or the team.
This test keeps that true: one database written by a team cell (an owner, Ann and Bob, a key each,
Ann leading a private project P that Bob and the owner are not on, an event labelled for P) is
opened by a daemon in five states, and the same checks run in each:

  a. no TARES_LICENSE
  b. a valid test licence
  c. an expired test licence
  d. a licence check that raises (licence.state is replaced before the daemon starts; the boot
     check runs for real, because make_app does not guard it, every later one raises)
  e. tares_ee unimportable (sys.modules["tares_ee"] = None), with a valid licence; until TR-918
     adds the tares.ee entry point there is no ee to load, so this state equals state b

In each state:
  - GET /api/whoami names the key's own person (never `local` for Ann's or Bob's key);
  - Bob: GET /api/projects/P is 404, the list shows P only as a hidden card (name and lead),
    and a POST /read over P's source returns none of P's events but still the open ones;
  - Ann sees P in full and its event;
  - the owner, who is not on P, sees only the hidden card and none of P's events.

It uses no route that moves to ee (people, areas, project access), so it passes in the `mit`
suite where tares_ee is deleted. The database is written with the daemon stopped (DuckDB has one
writer), the way a team cell would have left it. It is DuckDB-only: Store(DB, state_dsn="") keeps
it out of the Postgres run's state, because the daemon it starts gets no TARES_* variables.

Run: .venv/bin/python tests/test_private_any_licence.py
"""
import hashlib, json, os, shutil, signal, socket, subprocess, sys, tempfile, time
from datetime import timedelta

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env
import licence_testkit as tk
from tares.envelope import Envelope, now_utc
from tares.store import Store

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


AUTH = "root-token"
_base = os.path.join(os.environ.get("CLAUDE_JOB_DIR", ""), "tmp")
TMP = tempfile.mkdtemp(prefix="private-any-licence-", dir=_base if os.path.isdir(_base) else None)
DB = os.path.join(TMP, "cell.duckdb")


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


# ── the licences ────────────────────────────────────────────────────────────────

root_priv, _root_pub = tk.keypair(tk.ROOT_PHRASE)
iss_priv, iss_pub = tk.keypair(tk.ISSUER_PHRASE)
CERT = tk.issuer_cert(root_priv, iss_pub, "2099-01-01")
VALID = tk.sign(iss_priv, CERT, "Test Org", 5, ["enterprise"], "2026-01-01T00:00:00Z", "2099-01-01", "lic_valid")
EXPIRED = tk.sign(iss_priv, CERT, "Test Org", 5, ["enterprise"], "2025-01-01T00:00:00Z", "2025-06-01", "lic_old")

# ── the daemon ──────────────────────────────────────────────────────────────────

# what runs before taresd starts, per state
PRELUDE = {
    "none": "",
    "valid": "",
    "expired": "",
    # make_app checks the licence once at boot and does not guard it, so the first check is the
    # real one and every later check raises; nothing is remembered between checks
    "crash": (
        "import tares.licence as _L\n"
        "_real, _n = _L.state, [0]\n"
        "def _boom(*a, **k):\n"
        "    _n[0] += 1\n"
        "    if _n[0] > 1:\n"
        "        raise RuntimeError('licence check crashed on purpose')\n"
        "    return _real(*a, **k)\n"
        "_L.state = _boom\n"
        "_L.CACHE_SECONDS = 0\n"
    ),
    "no_ee": "import sys\nsys.modules['tares_ee'] = None\n",
}


def argv(prelude: str) -> list:
    """What `taresd` runs, with the test root trusted and `prelude` run first."""
    base = tk.daemon_argv()
    return [base[0], "-c", prelude + base[2]]


def env_for(port: str, licence: str | None) -> dict:
    env = clean_env()
    for k in [k for k in env if k.startswith(("TARES_", "FACTORY_")) and k != "TARES_HOME"]:
        del env[k]
    env["TARES_HOME"] = os.path.join(TMP, "home")
    os.makedirs(env["TARES_HOME"], exist_ok=True)
    env.update(TARES_DB=DB, TARES_PORT=port, TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=AUTH)
    if os.environ.get("TARES_UI_DIST"):
        env["TARES_UI_DIST"] = os.environ["TARES_UI_DIST"]
    if licence:
        env["TARES_LICENSE"] = licence
    return env


def start(prelude: str = "", licence: str | None = None):
    """(process, base url) once /health answers, polled every 0.5s for up to 30s; (None, url) if it never does."""
    port = free_port()
    proc = subprocess.Popen(argv(prelude), env=env_for(port, licence),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None, base
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                return proc, base
        except Exception:
            pass
        time.sleep(0.5)
    stop(proc)
    return None, base


def stop(proc):
    """Stop the daemon this test started, by its own pid."""
    if proc is None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
        proc.wait()


# ── phase 1: the database a team cell leaves, written with no daemon running ────

def new_key(store, kid, name, person_id, scopes):
    secret = f"nvf_{kid}_{kid}-secret-for-the-test"
    store.insert_api_key(kid, name, f"nvf_{kid}", hashlib.sha256(secret.encode()).hexdigest(),
                         scopes, person_id=person_id)
    return secret


def write_team_cell() -> dict:
    store = Store(DB, state_dsn="")
    try:
        owner = store.claim_local("owner@x.dev", "Olive Owner")
        ann = store.create_person("Ann", "ann@x.dev")
        bob = store.create_person("Bob", "bob@x.dev")
        ids = {"owner": owner["id"], "ann": ann["id"], "bob": bob["id"]}
        keys = {"owner": new_key(store, "own00001", "owner key", owner["id"], ["read", "ingest", "admin"]),
                "ann": new_key(store, "ann00001", "ann key", ann["id"], ["read", "ingest"]),
                "bob": new_key(store, "bob00001", "bob key", bob["id"], ["read", "ingest"])}
        store.set_setting("areas", json.dumps([{"repo": "acme/api", "person_id": ann["id"], "note": ""}]))
        return {"ids": ids, "keys": keys}
    finally:
        store.con.close()


# ── phase 2: project P, made by the system credential, then made private under Ann ──

def make_project(cell: dict) -> bool:
    proc, base = start()
    if proc is None:
        return False
    try:
        with httpx.Client(timeout=20) as cx:
            r = cx.post(f"{base}/api/projects", headers=H(AUTH),
                        json={"template": "custom", "name": "Secret", "objects": []})
            ck("set up: P is created with the system token", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}")
            cell["p"] = r.json()["id"]
            r = cx.post(f"{base}/api/sources", headers=H(AUTH), json={
                "name": "evts", "connector": "webhook", "config": {"labels": [
                    {"name": "service", "field": "service", "primary": True},
                    {"name": "tares_project", "field": "tares_project"}]}})
            ck("set up: the source evts", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    finally:
        stop(proc)
    store = Store(DB, state_dsn="")
    try:
        now = now_utc() - timedelta(seconds=30)
        store.append([
            Envelope("evts", "event_stream", "checkout", "evt", "SECRET event of P", now,
                     {"text": "SECRET event of P"}, {"service": "checkout", "tares_project": cell["p"]}),
            Envelope("evts", "event_stream", "checkout", "evt", "OPEN event of nobody", now,
                     {"text": "OPEN event of nobody"}, {"service": "checkout"}),
        ])
        store.set_project_lead(cell["p"], cell["ids"]["ann"])
        store.set_project_scope(cell["p"], "private")
    finally:
        store.con.close()
    return True


# ── the checks, run in each state ────────────────────────────────────────────────

def check_state(label: str, base: str, cell: dict, crashing: bool = False) -> None:
    ids, keys, p = cell["ids"], cell["keys"], cell["p"]
    print(f"== {label} ==")
    with httpx.Client(timeout=20) as cx:
        if crashing:
            # a fresh connection: the server closes the one an unhandled error was answered on
            r = httpx.get(f"{base}/api/licence", headers=H(AUTH), timeout=20)
            ck(f"{label}: the licence check is crashing here", r.status_code >= 500, f"{r.status_code} {r.text[:200]}")
        for who in ("owner", "ann", "bob"):
            r = cx.get(f"{base}/api/whoami", headers=H(keys[who]))
            got = (r.json().get("person") or {}).get("id") if r.status_code == 200 else None
            ck(f"{label}: {who}'s key is {who} ({ids[who]})", got == ids[who], f"{r.status_code} {r.text[:200]}")
        for who in ("ann", "bob"):
            r = cx.get(f"{base}/api/whoami", headers=H(keys[who]))
            ck(f"{label}: {who}'s key is not `local`",
               r.status_code == 200 and (r.json().get("person") or {}).get("id") != "local", f"{r.status_code} {r.text[:200]}")

        def read(who):
            r = cx.post(f"{base}/read", headers=H(keys[who]),
                        json={"selector": {"service": "checkout"}, "window": "1h", "sources": ["evts"]})
            return r.status_code, r.text

        def listed(who):
            r = cx.get(f"{base}/api/projects", headers=H(keys[who]))
            return r.status_code, {x["id"]: x for x in (r.json().get("projects") or [])} if r.status_code == 200 else {}

        for who in ("bob", "owner"):
            r = cx.get(f"{base}/api/projects/{p}", headers=H(keys[who]))
            if who == "bob":
                ck(f"{label}: bob gets 404 on P", r.status_code == 404, f"{r.status_code} {r.text[:200]}")
            code, cards = listed(who)
            card = cards.get(p) or {}
            ck(f"{label}: {who} sees P in the list only as a hidden card with its name and lead",
               code == 200 and card.get("hidden") is True and card.get("name") == "Secret"
               and "lead" in card and card.get("lead") is not None
               and set(card) <= {"id", "name", "scope", "hidden", "lead"},
               f"{code} {json.dumps(card)[:300]}")
            code, text = read(who)
            ck(f"{label}: {who}'s read of P's source has none of P's events, and the open one",
               code == 200 and "SECRET" not in text and "OPEN event of nobody" in text, f"{code} {text[:300]}")

        r = cx.get(f"{base}/api/projects/{p}", headers=H(keys["ann"]))
        ck(f"{label}: ann gets P", r.status_code == 200 and r.json().get("id") == p, f"{r.status_code} {r.text[:200]}")
        code, cards = listed("ann")
        card = cards.get(p) or {}
        ck(f"{label}: ann sees P in full in the list", code == 200 and card and not card.get("hidden"), f"{code} {json.dumps(card)[:300]}")
        code, text = read("ann")
        ck(f"{label}: ann's read has P's event and the open one",
           code == 200 and "SECRET event of P" in text and "OPEN event of nobody" in text, f"{code} {text[:300]}")


STATES = [
    ("no licence", "none", None),
    ("valid licence", "valid", VALID),
    ("expired licence", "expired", EXPIRED),
    ("licence check crashes", "crash", None),
    ("tares_ee unimportable", "no_ee", VALID),
]


def main() -> int:
    cell = write_team_cell()
    ck("set up: the cell has three people", len(cell["ids"]) == 3 and len(set(cell["ids"].values())) == 3)
    if not make_project(cell):
        ck("set up: the daemon starts to make P", False)
        return 1
    for label, key, lic in STATES:
        proc, base = start(PRELUDE[key], lic)
        if proc is None:
            ck(f"{label}: the daemon starts and answers /health", False)
            continue
        try:
            check_state(label, base, cell, crashing=key == "crash")
        finally:
            stop(proc)
    return 0


try:
    main()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F or not P else 0)

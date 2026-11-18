# Tares enterprise licence, see ee/LICENSE
"""Folder suggestions for a repo that is not connected yet (TR-1441): GET
/api/brain/repos/available/{owner}/{name}/folders.

An admin picks decision folders while connecting a repo, so Tares scans a repo the cell's GitHub connection
can read before it is on the brain. Against the shared fake lore and control plane, a fake GitHub repo list
and a fake GitHub tree (never a real one). In process, no ports. With CI's Postgres run (TARES_TEST_PG and
tests/pg_harness on PYTHONPATH) the same script keeps its state in Postgres.

Run: .venv/bin/python ee/tests/test_brain_folders_available.py
"""
import asyncio, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings, puts the repo, ee/ and tests/ on sys.path
licensed.install_test_root()

TMP = tempfile.mkdtemp(prefix="tares-brain-folders-available-")
ROOT = "root-token-" + "brain-folders-available"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = ROOT

import httpx

import fake_cp
import fake_lore
from tares import github_credentials
from tares_ee.brain import index as index_mod
from tares_ee.brain import repos as repos_mod

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


BRAIN_KEY = "brain-" + "key-test-1"   # gitleaks:allow


def blob(path):
    return {"path": path, "type": "blob"}


def tree(path):
    return {"path": path, "type": "tree"}


def make_tree():
    """docs/adr (3 markdown files, one nested) and a numbered folder that is not a known name."""
    return [tree("docs"), tree("docs/adr"), tree("docs/adr/old"), tree("design"), tree("design/records"),
            blob("docs/adr/0001-use-postgres.md"), blob("docs/adr/0002-queue.md"), blob("docs/adr/old/0003-x.md"),
            blob("design/records/0001-api.md"), blob("design/records/0002-auth.md"), blob("README.md")]


async def main():
    from tares.daemon import make_app
    lore = fake_lore.FakeLore()
    cp = fake_cp.FakeCp()
    cp.start_as = ("queued", 2)
    repos_mod.lore_client = lore.client
    index_mod.cp_client = cp.client

    async def never(self):
        await asyncio.Event().wait()
    index_mod.Indexer.run = never

    listing = {"answer": [{"full_name": f"acme/{n}"} for n in ("web", "api", "cut", "down", "slow")]}
    list_calls = []

    async def readable_repos(cred, query=""):
        list_calls.append(1)
        got = listing["answer"]
        if isinstance(got, Exception):
            raise got
        return got
    github_credentials.list_repos_for = readable_repos

    trees = {}   # repo -> what the fake GitHub answers for its tree, or an exception to raise
    asked = []

    async def fake_tree(token, repo, ref="HEAD", api_url=None):
        asked.append(repo)
        got = trees.get(repo, {"entries": make_tree(), "truncated": False})
        if isinstance(got, Exception):
            raise got
        if got == "hang":
            await asyncio.Event().wait()
        return got
    github_credentials.git_tree = fake_tree

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(ROOT))
        store = app.state.store
        store.upsert_github_credential("setup", "s" * 10 + "tok")
        AV = "/api/brain/repos/available/{}/folders"

        print("== no licence, no brain ==")
        r = await cx.get(AV.format("acme/web"))
        ck("no licence: 404 as an unmounted route", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        r = await cx.put("/api/licence", json={"key": licensed.test_licence()})
        ck("the licence is stored", r.status_code == 200, r.text[:200])
        r = await cx.get(AV.format("acme/web"))
        ck("a licence but no brain settings: 404", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        os.environ["TARES_BRAIN_URL"] = fake_lore.BASE_URL
        os.environ["TARES_BRAIN_KEY"] = BRAIN_KEY

        print("== a repo that is not connected ==")
        r = await cx.get(AV.format("acme/web"))
        j = r.json()
        ck("answers repo, folders, suggested, partial, scanned, scan_error (the shape of the connected route)",
           r.status_code == 200 and set(j) == {"repo", "folders", "suggested", "partial", "scanned", "scan_error"}, f"{r.status_code} {r.text[:300]}")
        ck("docs/adr is suggested with 3 files, not chosen", {"path": "docs/adr", "files": 3, "why": "known", "chosen": False} in j["suggested"], str(j["suggested"]))
        ck("a numbered folder is suggested too", {"path": "design/records", "files": 2, "why": "numbered", "chosen": False} in j["suggested"], str(j["suggested"]))
        ck("nothing is chosen yet; scanned, not partial, no error", j["folders"] == [] and (j["partial"], j["scanned"], j["scan_error"]) == (False, True, None), r.text[:300])
        ck("the tree of this repo was read once", asked == ["acme/web"], str(asked))
        ck("nothing was connected, stored or sent to lore or the control plane",
           store.list_brain_repos() == [] and store.list_brain_folders("acme/web") == [] and not lore.requests and not cp.requests,
           f"{store.list_brain_repos()} {len(lore.requests)}")
        r = await cx.get(AV.format("Acme/WEB"))
        ck("the repo name is matched in any case", r.status_code == 200 and r.json()["repo"] == "acme/web", f"{r.status_code} {r.text[:100]}")

        print("== a connected repo answers too, with its chosen folders ==")
        r = await cx.post("/api/brain/repos", json={"repos": ["acme/api"], "folders": ["docs/adr"]})
        ck("acme/api is connected with docs/adr", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        r = await cx.get(AV.format("acme/api"))
        j = r.json()
        ck("the chosen folder is listed with its count and marked chosen in the suggestions", r.status_code == 200
           and [(f["path"], f["files"]) for f in j["folders"]] == [("docs/adr", 3)]
           and {s["path"]: s["chosen"] for s in j["suggested"]} == {"docs/adr": True, "design/records": False}, r.text[:400])
        r2 = await cx.get("/api/brain/repos/acme/api/folders")
        ck("... the same answer as the connected repo's own route", r2.status_code == 200 and r2.json() == j, r2.text[:300])

        print("== any other repo is 404 ==")
        before = list(asked)
        r = await cx.get(AV.format("acme/secret"))
        ck("a repo the GitHub connection cannot read is 404, the same words as the folder routes", r.status_code == 404 and "not a repo this cell's GitHub connection can read" in r.text, f"{r.status_code} {r.text[:200]}")
        ck("... and its tree was never read", asked == before, str(asked))
        r = await cx.get(AV.format("other/web"))
        ck("a repo with the same name under another owner is 404", r.status_code == 404, f"{r.status_code}")
        r = await cx.get("/api/brain/repos/available/a_b/c/folders")
        ck("a name that is not owner/name is 400", r.status_code == 400 and "is not a repo" in r.text, f"{r.status_code} {r.text[:200]}")
        r = await cx.get("/api/brain/repos/available/acme/../folders")
        ck("a dot dot name is refused or not found, never a scan", r.status_code in (400, 404) and asked == before, f"{r.status_code}")
        store.delete_github_credential("setup")
        r = await cx.get(AV.format("acme/web"))
        ck("with no GitHub connection every repo is 404", r.status_code == 404 and asked == before, f"{r.status_code} {r.text[:100]}")
        store.upsert_github_credential("setup", "s" * 10 + "tok")

        print("== GitHub cannot be asked ==")
        trees["acme/down"] = ValueError("GitHub returned 500 for the tree of acme/down")
        r = await cx.get(AV.format("acme/down"))
        j = r.json()
        ck("a failed scan is 200 with scanned false and the reason", r.status_code == 200 and j["scanned"] is False and "returned 500" in j["scan_error"]
           and j["suggested"] == [] and j["folders"] == [], f"{r.status_code} {r.text[:300]}")
        trees["acme/slow"] = "hang"
        repos_mod.SCAN_TIMEOUT = 0.2
        r = await cx.get(AV.format("acme/slow"))
        ck("a scan past the time limit is 200 with scanned false", r.status_code == 200 and r.json()["scanned"] is False
           and "took too long" in r.json()["scan_error"], f"{r.status_code} {r.text[:300]}")
        listing["answer"] = ValueError("GitHub returned 502 for the list of repos")
        n = len(asked)
        r = await cx.get(AV.format("acme/web"))
        ck("when the list of repos fails the answer is 200 with scanned false and the reason", r.status_code == 200 and r.json()["scanned"] is False
           and "returned 502" in r.json()["scan_error"] and len(asked) == n, f"{r.status_code} {r.text[:300]}")
        listing["answer"] = [{"full_name": f"acme/{n}"} for n in ("web", "api", "cut", "down", "slow")]

        print("== a huge repo: the tree is cut short ==")
        trees["acme/cut"] = {"entries": [tree("docs"), tree("docs/adr"), blob("docs/adr/0001-a.md"), blob("docs/adr/0002-b.md"),
                                         tree("design/records"), blob("design/records/0001-x.md"), blob("design/records/0002-y.md")],
                             "truncated": True}
        r = await cx.get(AV.format("acme/cut"))
        j = r.json()
        ck("partial is true and only the known names are suggested", j["partial"] is True and [s["path"] for s in j["suggested"]] == ["docs/adr"], r.text[:400])

        print("== who may ==")
        pid = (await cx.post("/api/people", json={"name": "Jo", "email": "jo@x.dev"})).json()["id"]

        async def key(scopes):
            r = await cx.post("/api/keys", json={"name": "k-" + "-".join(scopes), "scopes": scopes, "person_id": pid})
            return H(r.json()["secret"])
        reader, admin = await key(["read"]), await key(["admin"])
        n = len(asked)
        r = await cx.get(AV.format("acme/web"), headers=reader)
        ck("a read key is refused (403) and nothing is scanned", r.status_code == 403 and len(asked) == n, f"{r.status_code} {r.text[:100]}")
        r = await cx.get(AV.format("acme/web"), headers=admin)
        ck("an admin key is answered", r.status_code == 200 and r.json()["scanned"] is True, f"{r.status_code}")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as bare:
            r = await bare.get(AV.format("acme/web"))
        ck("no credential at all is refused by the daemon", r.status_code == 401, f"{r.status_code}")

        class Host:
            def __init__(self):
                self.store = store
            def require_admin(self, request):
                self.err(PermissionError("this needs an admin"), 403)
            def current_person(self, request): return None
            def err(self, e, code=400):
                from fastapi import HTTPException
                raise HTTPException(status_code=code, detail=str(e))
        from fastapi import FastAPI
        bare_app = FastAPI()
        repos_mod.register_routes(bare_app, Host())
        n = len(asked)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bare_app), base_url="http://b") as bx:
            r = await bx.get(AV.format("acme/web"))
        ck("the handler checks admin itself, not only the daemon's guard", r.status_code == 403 and len(asked) == n, f"{r.status_code}")

        print("== the licence goes away ==")
        await cx.delete("/api/licence")
        r = await cx.get(AV.format("acme/web"))
        ck("the route is 404 again", r.status_code == 404 and r.json() == {"detail": "Not Found"}, f"{r.status_code} {r.text[:100]}")
        await cx.aclose()


os.environ["TARES_LICENSE"] = ""
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

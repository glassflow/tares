"""A repo map builders start from (TR-681).

One map per GitHub repo, stored on Tares and shared by every project that names the repo. The first
builder on a repo with none writes it; the builder brief says so. A second project on the same repo
gets the first project's map in its builder brief, and the reviewer's brief carries it too. The map
is capped (about 2,000 tokens), every change is a dated line in its history, and only a builder
writes it. The builder manual tells builders when to write and update it.

Run: .venv/bin/python tests/test_factory_repomap.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-repomap-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-repomap"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

logging.getLogger("httpx").setLevel(logging.WARNING)

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory, factory_github

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def fake_github(request: httpx.Request) -> httpx.Response:
    parts = request.url.path.strip("/").split("/")
    if parts[3] == "pulls":
        return httpx.Response(200, json={
            "number": int(parts[4]), "title": "PR", "state": "open", "merged": False,
            "html_url": f"https://github.com/acme/shop/pull/{parts[4]}",
            "head": {"sha": "aaaaaaa1111", "ref": "factory/T1"}})
    if parts[5] == "status":
        return httpx.Response(200, json={"statuses": []})
    return httpx.Response(200, json={"check_runs": []})


factory_github._transport = httpx.MockTransport(fake_github)

MAP = ("Layout: api/ (FastAPI), web/ (React). Entry: api/main.py. Install: uv sync. Test: pytest -q. "
       "Lint: ruff check. Trap: the orders table is migrated by hand.")


def unit():
    print("== the size cap and the note (TR-681) ==")
    ok = factory.map_ok(MAP, "first map")
    ck("a map within the cap passes, trimmed", ok == (MAP, "first map"), ok)
    for label, body, note, word in (("empty", "  ", "n", "empty"),
                                    ("over the cap", "x" * (factory.MAP_MAX_CHARS + 1), "n", "most is"),
                                    ("no note", MAP, " ", "what changed")):
        try:
            factory.map_ok(body, note)
            ck(f"{label} is refused", False)
        except factory.DocError as e:
            ck(f"{label} is refused", word in str(e), str(e))
    try:
        factory.map_ok("x" * 8001, "n")
        ck("8,001 characters (about 2,000 tokens) is over the cap", False)
    except factory.DocError:
        ck("8,001 characters (about 2,000 tokens) is over the cap", True)
    ck("exactly the cap passes", factory.map_ok("x" * factory.MAP_MAX_CHARS, "n")[0] == "x" * factory.MAP_MAX_CHARS)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station, role, parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        web = json.loads(await m.create_project("Storefront", "A new front"))["id"]
        as_station("crew-orchestrator", "orchestrator")
        for name, repo in (("Shop", "acme/shop"), ("Storefront", "Acme/Shop")):
            await m.write_ticket("Cart", project=name, repo=repo)
            await mf.write_milestone("Store", "A shop", [{"check": "curl /", "expect": "ok"}], project=name)
        await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        await mf.assign_ticket("T1", "front-build-m1", project="Storefront")

        print("== the first builder on a repo with none is told to write the map (TR-681) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        brief = await mf.builder_brief("T1", project="Shop")
        ck("the builder brief says there is no map and how to write one",
           "## Repo map: acme/shop" in brief and "first builder here" in brief and "write_repo_map" in brief, brief)

        print("== writing it (TR-681) ==")
        out = json.loads(await mf.write_repo_map("acme/shop", MAP, "first map from TR-681 work", project="Shop"))
        ck("the map is saved with its size", out["changed"] and out["chars"] == len(MAP)
           and out["max_chars"] == factory.MAP_MAX_CHARS, out)
        v1 = factory.map_version(MAP)
        again = json.loads(await mf.write_repo_map("acme/shop", MAP, "same again", v1, project="Shop"))
        ck("the same map again changes nothing and adds no history line", not again["changed"]
           and len(again["history"]) == 1, again)
        over = await mf.write_repo_map("acme/shop", "x" * (factory.MAP_MAX_CHARS + 1), "too big", v1, project="Shop")
        ck("a map over the cap is refused and the saved one stays", "most is" in over
           and store.get_repo_map("acme/shop")["body"] == MAP, over[:200])
        nonote = await mf.write_repo_map("acme/shop", MAP + " More.", " ", v1, project="Shop")
        ck("a change with no note is refused", "what changed" in nonote and store.get_repo_map("acme/shop")["body"] == MAP, nonote)
        longnote = await mf.write_repo_map("acme/shop", MAP + " More.", "n" * (factory.MAP_NOTE_MAX + 1), v1, project="Shop")
        ck("a note over 200 characters is refused", "note" in longnote and store.get_repo_map("acme/shop")["body"] == MAP, longnote)
        bad = await mf.write_repo_map("not a repo", MAP, "n", project="Shop")
        ck("something that is not owner/name is refused", "owner/name" in bad, bad)

        print("== a second project on the same repo gets it (TR-681) ==")
        as_station("front-build-m1", "builder", "crew-orchestrator")
        brief = await mf.builder_brief("T1", project="Storefront")
        ck("the second project's brief carries the first project's map", MAP in brief
           and "shared by every project on it" in brief and "shop-build-m1" in brief, brief)
        ck("the repo name matches whatever the case", "## Repo map: Acme/Shop" in brief, brief[:300])
        ck("with the recent change line and the update rule", "first map from TR-681 work" in brief
           and "update the map with write_repo_map" in brief, brief)
        ck("and the version to write from", f'base="{v1}"' in brief and f"version {v1}" in brief, brief)
        ck("no em dashes", "—" not in brief)
        out = json.loads(await mf.write_repo_map("acme/shop", MAP + " Deploys: make ship.", "added how it deploys",
                                                 v1, project="Storefront"))
        hist = store.repo_map_history("acme/shop")
        ck("an update from the second project is a dated line, newest first",
           out["changed"] and [h["note"] for h in hist] == ["added how it deploys", "first map from TR-681 work"]
           and hist[0]["by"] == "front-build-m1" and hist[0]["project"] == web, hist)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        brief = await mf.builder_brief("T1", project="Shop")
        v2 = factory.map_version(MAP + " Deploys: make ship.")
        ck("and the first project sees the update", "Deploys: make ship." in brief and "added how it deploys" in brief, brief)

        print("== the reviewer's brief carries it too (TR-681) ==")
        store.upsert_github_credential("gh", "ghp_x")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "doing", project="Shop")
        await mf.set_stage("T1", "review", pr="https://github.com/acme/shop/pull/7", project="Shop")
        as_station("crew-reviewer", "reviewer")
        rb = await mf.review_brief("T1", project="Shop")
        ck("the review brief has the map, without the builder's instructions", "## Repo map: acme/shop" in rb
           and "Deploys: make ship." in rb and "write_repo_map" not in rb, rb)

        print("== who may write it (TR-681) ==")
        for station, role in (("crew-reviewer", "reviewer"), ("crew-orchestrator", "orchestrator"),
                              ("crew-releaser", "releaser")):
            as_station(station, role)
            out = await mf.write_repo_map("acme/shop", "hijack", "n", v2, project="Shop")
            ck(f"a {role} may not", "only a builder" in out and store.get_repo_map("acme/shop")["body"].startswith("Layout"), out)
        as_station("", "")
        out = json.loads(await mf.write_repo_map("acme/shop", MAP, "person's own session puts it back", v2, project="Shop"))
        ck("a person's own session may", out["changed"], out)

        print("== a write from a stale copy cannot overwrite a newer map (TR-684) ==")
        as_station("front-build-m1", "builder", "crew-orchestrator")
        old = await mf.write_repo_map("acme/shop", MAP + " Lint: ruff.", "stale write", v2, project="Storefront")
        ck("a base that is not the saved version is refused with the map now", old.startswith("error 409")
           and "changed since you read it" in old and f"version {v1}" in old and MAP in old, old[:300])
        ck("and nothing was written", store.get_repo_map("acme/shop")["body"] == MAP and len(store.repo_map_history("acme/shop")) == 3,
           store.repo_map_history("acme/shop"))
        nobase = await mf.write_repo_map("acme/shop", MAP + " Lint: ruff.", "no base", project="Storefront")
        ck("a write that names no base is refused the same way", nobase.startswith("error 409") and "gave no base" in nobase, nobase[:200])
        good = json.loads(await mf.write_repo_map("acme/shop", MAP + " Lint: ruff.", "folded in", v1, project="Storefront"))
        ck("a write from the version it read goes through", good["changed"] and good["version"] == factory.map_version(MAP + " Lint: ruff."), good)

        print("== only a repo some project names (TR-684) ==")
        stray = await mf.write_repo_map("acme/typo", MAP, "stray", project="Storefront")
        ck("a repo no project names is refused and no map is made", stray.startswith("error 404") and "no project names acme/typo" in stray
           and store.get_repo_map("acme/typo") is None, stray)
        await store_handover(app, shop, [{"path": "/w/shop", "name": "acme/shop"}, {"path": "/w/api", "name": "acme/api"}])
        first = await mf.write_repo_map("acme/api", MAP, "a base for a map that is not there", "abc", project="Shop")
        ck("a base for a repo with no map yet is refused", first.startswith("error 409") and "no map for this repo yet" in first, first)
        ok = json.loads(await mf.write_repo_map("acme/api", MAP, "first api map", "", project="Shop"))
        ck("a repo only a hand-over names is accepted", ok["changed"], ok)

        print("== a ticket with no repo falls back to the project's only repo (TR-681) ==")
        as_station("crew-orchestrator", "orchestrator")
        await m.write_ticket("Loose", project="Shop")
        await mf.assign_ticket("T2", "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        none = await mf.builder_brief("T2", project="Shop")
        ck("with no repo to name, no map section", "## Repo map" not in none, none)
        for label, repos in (("one named repo", [{"path": "/w/shop", "name": "acme/shop"}]),):
            await store_handover(app, shop, repos)
            brief = await mf.builder_brief("T2", project="Shop")
            ck(f"with {label} on the hand-over, that repo's map", "## Repo map: acme/shop" in brief, brief)
        for label, repos in (("two named repos", [{"path": "/w/shop", "name": "acme/shop"}, {"path": "/w/api", "name": "acme/api"}]),
                             ("one named and one unnamed repo", [{"path": "/w/shop", "name": "acme/shop"}, {"path": "/w/docs"}])):
            await store_handover(app, shop, repos)
            brief = await mf.builder_brief("T2", project="Shop")
            ck(f"with {label}, a ticket that names none gets no map (it could be either)", "## Repo map" not in brief, brief)
        await store_handover(app, shop, [{"path": "/w/docs"}])
        brief = await mf.builder_brief("T2", project="Shop")
        ck("with one repo and no GitHub name, no map", "## Repo map" not in brief, brief)

        print("== a ticket's PR repo is a named repo, and base is trimmed (TR-685) ==")
        as_station("crew-orchestrator", "orchestrator")
        await m.write_ticket("Lands in another repo", project="Shop")
        pr_t = store.get_ticket(shop, "T3")
        pr_link = {"repo": "Acme/Landed", "url": "https://github.com/acme/landed/pull/9", "number": 9}
        store.update_ticket(pr_t["id"], pr=pr_link)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        unread = await mf.write_repo_map("acme/landed", MAP, "map for a PR GitHub has not answered for", "", project="Shop")
        ck("a PR repo GitHub has not answered for is not yet named", unread.startswith("error 404") and store.get_repo_map("acme/landed") is None, unread)
        store.update_ticket(pr_t["id"], pr={**pr_link, "checked_at": "2026-10-25T01:00:00+00:00", "error": "no credential"})
        failed = await mf.write_repo_map("acme/landed", MAP, "map for a PR whose read failed", "", project="Shop")
        ck("nor is one whose read failed", failed.startswith("error 404"), failed)
        store.update_ticket(pr_t["id"], pr={**pr_link, "checked_at": "2026-10-25T01:00:00+00:00", "error": None})
        landed = await mf.write_repo_map("acme/landed", MAP, "map for the PR's repo", "", project="Shop")
        ck("a repo only a ticket's PR names is accepted once GitHub has answered", landed.startswith("{") and json.loads(landed)["changed"], landed)
        version = store.get_repo_map("acme/landed") and factory.map_version(store.get_repo_map("acme/landed")["body"])
        spaced = await mf.write_repo_map("acme/landed", MAP + " More.", "base with a stray space", f" {version}\n", project="Shop")
        ck("a base with a stray space or newline still matches", spaced.startswith("{") and json.loads(spaced)["changed"], spaced)
        store.update_ticket(pr_t["id"], pr={})

        print("== the map and its history are one write (TR-684) ==")
        before = store.get_repo_map("acme/api")["body"]
        store.sc.execute("ALTER TABLE repo_map_history RENAME TO repo_map_history_away")
        try:
            try:
                store.set_repo_map("acme/api", MAP + " Torn.", "torn", "x", None, datetime.now(timezone.utc),
                                   factory.map_version(before))
                failed = False
            except Exception:
                failed = True
        finally:
            store.sc.execute("ALTER TABLE repo_map_history_away RENAME TO repo_map_history")
        ck("a history write that fails leaves the map as it was", failed and store.get_repo_map("acme/api")["body"] == before,
           store.get_repo_map("acme/api"))
        retry = store.set_repo_map("acme/api", MAP + " Torn.", "torn", "x", None, datetime.now(timezone.utc),
                                   factory.map_version(before))
        ck("and a retry then writes both", retry and store.repo_map_history("acme/api")[0]["note"] == "torn")

    print("== the builder manual says when to write and update it (TR-681) ==")
    manual = open(os.path.join(ROOT, "claude-plugin/skills/station-builder/SKILL.md")).read()
    ck("write it when the brief says there is none", "write_repo_map" in manual and "no map" in manual, manual[:100])
    ck("update it after a merge that changes layout or commands", re.search(
        r"layout.{0,80}(build|test|lint)", manual, re.S) is not None and "update" in manual.lower())
    steps = {n: line for line in manual.splitlines() for n in ("7", "11") if line.startswith(n + ". ")}
    ck("step 7 writes a new map with an empty base", "write_repo_map" in steps["7"] and "empty `base`" in steps["7"], steps["7"][-300:])
    ck("step 11 passes the brief's version as base (TR-685)", "write_repo_map" in steps["11"] and "`version` as `base`" in steps["11"], steps["11"][-300:])
    rman = open(os.path.join(ROOT, "claude-plugin/skills/station-reviewer/SKILL.md")).read()
    rsteps = {n: line for line in rman.splitlines() for n in ("6", "11") if line.startswith(n + ". ")}
    ck("reviewer step 6 is the design check and reads the approval with list_decisions",
       "Design check" in rsteps["6"] and "list_decisions" in rman.split("Design check", 1)[1].split("\n7. ")[0], rsteps["6"][:200])
    ck("reviewer step 6 reads the choice and compares author dates as timestamps",
       all(w in rman.split("Design check", 1)[1].split("\n7. ")[0]
           for w in ("`choice`", "%at", "sort -n | head -1", "git fetch origin <base>", "origin/<base>..<head>", "the SHA pinned in step 2", "convert `at` to epoch", "A git error", "on its own and check its exit code", "set -o pipefail", "prints a `fatal:` or `error:` line", "hides a bad ref", "neither its `choice` nor its `words` asks for a change")), rsteps["6"][:300])
    ck("reviewer step 6 does not take the first UI commit with --reverse, and blocks when a screen ticket has none",
       "log --reverse" not in rman.split("Design check", 1)[1].split("\n7. ")[0]
       and "clean run with no output means no UI commit, which for a screen ticket is a `block`" in rman.split("Design check", 1)[1].split("\n7. ")[0], rsteps["6"][:300])
    ck("builder step 3 reads the choice and wants no requested change",
       all(w in manual.split("Mock first", 1)[1].split("\n4. ")[0] for w in ("choice", "neither the choice nor the words ask for a change")), manual[:100])
    ck("reviewer step 11 still tidies the worktree", "factory tidy" in rsteps["11"], rsteps["11"][-200:])


async def store_handover(app, uid, repos):
    app.state.store.set_handover(uid, repos, None, "t", datetime.now(timezone.utc))


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

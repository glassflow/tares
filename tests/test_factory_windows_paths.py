"""A repo folder on a Windows crew machine stays on that machine (TR-1184).

A project handed over with C:/x/repo (or a share, \\\\srv\\share\\repo) shows the repo's name wherever a
title, a goal, a check or a note names the folder, however the text spells it: backslashes, forward
slashes, any case. A finding's file on a Windows machine shows its last name. A repo at a drive root
(C:/) hides nothing. Checked on the three screens that clean text: the project page, the tickets list
and the ticket page.

Run: .venv/bin/python tests/test_factory_windows_paths.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-winpaths-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-winpaths"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import factory as factory_mod
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def rules():
    print("== the rules, on their own ==")
    ck("a drive root hides nothing", factory_mod.hidden_key("C:/") is None
       and factory_mod.hidden_key("C:") is None and factory_mod.hidden_key("C:/x") == "C:/x")
    ck("a folder's last name, for either slash",
       factory_mod.folder_name("C:/x/repo") == "repo" and factory_mod.folder_name("\\\\srv\\share\\r") == "r"
       and factory_mod.folder_name("/a/b/") == "b")
    h = {"C:/x/repo": "shop", "\\\\srv\\share\\web": "web"}
    ck("a drive folder hides in any case and either slash",
       factory_mod.hide_paths("a C:\\x\\repo\\s b c:\\X\\Repo c C:\\x/repo d", h) == "a shop\\s b shop c shop d")
    ck("a share hides in any case and either slash",
       factory_mod.hide_paths("\\\\SRV\\share\\web\\z and //srv/share/web", h) == "web\\z and web")
    ck("a Mac path that only looks like a share is not hidden by a share",
       factory_mod.hide_paths("/srv/share/web", h) == "/srv/share/web")
    ck("a Mac folder is hidden as written, case kept",
       factory_mod.hide_paths("/W/Repo and /w/repo", {"/w/repo": "r"}) == "/W/Repo and r")


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    cx = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})

    def project(name, folder, repo):
        uid = f"uc_{name}"
        store.create_project(uid, "custom", name, {}, goal=f"Work in {folder}")
        store.set_project_kind(uid, "software_factory")
        store.set_handover(uid, [{"path": folder, "name": repo}], None, None, now_utc())
        return uid

    # the same folder, as titles spell it
    SPELLINGS = ["C:\\x\\repo", "c:\\X\\Repo", "C:\\x/repo", "C:/x/repo"]
    SHARES = ["\\\\srv\\share\\web", "\\\\SRV\\Share\\Web", "//srv/share/web"]

    async with app.router.lifespan_context(app):
        for tag, folder, repo, spellings in (("drive", "C:/x/repo", "example-org/win-drive", SPELLINGS),
                                             ("share", "\\\\srv\\share\\web", "example-org/win-share", SHARES)):
            print(f"== a {tag} folder, on three screens ==")
            px = project(f"win{tag}", folder, repo)
            title = "Fix links under " + " and ".join(s + "\\docs" if "\\" in s else s + "/docs" for s in spellings)
            m = store.create_milestone(px, "M1: links", f"Links work under {spellings[1]}.",
                                       [{"check": f"open {spellings[0]}\\index.html", "expect": f"it is in {spellings[2]}"}])
            tid = store.create_ticket(px, "tares", title, milestone=m)
            store.add_review(px, tid, None, "aaaaaaa1111", "block", f"read {spellings[1]}", None, "crew-reviewer",
                             [{"severity": "high", "file": "C:\\Users\\sam\\proj\\win.ts", "line": 3,
                               "blocking": True, "text": f"Not in {spellings[0]}"}],
                             {}, factory_mod.finding_kind, now_utc())
            async with cx() as c:
                page = (await c.get(f"/api/factory/projects/{px}")).text
                lst = (await c.get("/api/factory/tickets?limit=200&state=")).text
                one = (await c.get(f"/api/factory/projects/{px}/tickets/T1")).text
            for screen, text in (("the project page", page), ("the tickets list", lst), ("the ticket page", one)):
                left = [s for s in spellings if s.replace("\\", "\\\\") in text or s in text]
                ck(f"{screen}: no spelling of the {tag} folder is left", left == [], left)
                ck(f"{screen}: the repo's name stands in its place", repo in text, text[:200])
            ck("the ticket page: a Windows finding file shows its last name",
               '"file":"win.ts"' in one and "Users" not in one, one[:300])

        print("== a repo at a drive root ==")
        pr = project("winroot", "C:/", "example-org/root-repo")
        store.create_ticket(pr, "tares", "Copy C:\\data and the C: drive notes")
        async with cx() as c:
            lst = (await c.get(f"/api/factory/tickets?limit=200&state=&project={pr}")).text
        ck("a title naming C: keeps its words", "Copy C:\\\\data and the C: drive notes" in lst, lst[:300])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


rules()
asyncio.run(main())

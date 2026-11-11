"""A project's icon (TR-617): one emoji, set when the project is made or by the project's own
update route, changed, removed, refused when it is not one emoji, kept from read-only keys, and
never part of the name. Every console read that names a project carries it.

Run: PYTHONPATH=.:ee .venv/bin/python tests/test_project_icon.py
"""
import asyncio
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-project-icon-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-icon"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares.projects.icon import ICON_MAX_CODEPOINTS, NOT_ONE_EMOJI, normalize_icon

P = F = 0

GOOD = ["🚀", "🔥", "⭐", "❤️", "☀️", "1️⃣", "#️⃣", "🇩🇪", "👍🏽", "👩‍💻", "👨‍👩‍👧‍👦", "🏳️‍🌈",
        "🏴\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f"]
BAD = ["a", "ab", "1", "#", "🚀a", "a🚀", " ", "\n", " 🚀", "🚀 ", "🚀🚀", "<b>x</b>", "&#128640;", "\U0001f1e9",
       "🇩🇪🇫🇷", "‍", "️", "‍🚀", "🚀‍", "©", "™", "🚀" + "‍🚀" * ICON_MAX_CODEPOINTS, "x" * 200]


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def units():
    print("== the rule ==")
    for v in GOOD:
        ck(f"one emoji is kept as sent: {v!r}", normalize_icon(v) == v)
    ck("None and the empty string mean no icon", normalize_icon(None) is None and normalize_icon("") is None)
    for v in BAD + [5, ["🚀"]]:
        try:
            normalize_icon(v)
            ok = False
        except ValueError as e:
            ok = str(e) == NOT_ONE_EMOJI
        ck(f"refused: {v!r}", ok)
    src = open(os.path.join(ROOT, "ui/src/editions/factory/emojiData.ts"), encoding="utf8").read()
    chars = re.findall(r'\be\("([^"]+)"', src)
    ck("the picker's list is a couple of hundred emoji", 200 <= len(chars) <= 600, len(chars))
    ck("every emoji in the picker's list is one the cell accepts",
       all(normalize_icon(c) == c for c in chars), [c for c in chars if not _ok(c)])
    ck("no emoji is listed twice", len(set(chars)) == len(chars),
       sorted({c for c in chars if chars.count(c) > 1}))


def _ok(c):
    try:
        return normalize_icon(c) == c
    except ValueError:
        return False


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    mk = lambda token=TOKEN: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t",
        headers={"Authorization": f"Bearer {token}"} if token else {})

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
            assert r.status_code < 300, r.text
            reader_key = r.json()["secret"]

            async def make(name, **kw):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": [],
                                                         "goal": "Move the blog", "kind": "software_factory", **kw})
                assert r.status_code < 300, r.text
                return r.json()["id"]

            print("== made with a project ==")
            uid = await make("project-y")
            ck("no icon asked for, no icon", store.get_project(uid)["icon"] is None)
            with_icon = await make("project-a", icon="🚀")
            ck("an icon given when the project is made is kept", store.get_project(with_icon)["icon"] == "🚀")
            r = await cx.post("/api/projects", json={"template": "custom", "name": "project-bad", "objects": [],
                                                     "kind": "software_factory", "icon": "hello"})
            ck("a project made with a wrong icon is refused, with a sentence",
               r.status_code == 400 and NOT_ONE_EMOJI in r.text, (r.status_code, r.text))
            ck("and nothing is made", "project-bad" not in {p["name"] for p in store.list_projects()})

            print("== set, change, remove ==")
            r = await cx.put(f"/api/projects/{uid}", json={"icon": "🚀"})
            ck("set: the update route answers with the icon", r.status_code == 200 and r.json()["icon"] == "🚀", r.text)
            ck("the store has it", store.get_project(uid)["icon"] == "🚀")
            ck("the name is unchanged", store.get_project(uid)["name"] == "project-y")
            before = store.get_project(uid)["updated_at"]
            r = await cx.put(f"/api/projects/{uid}", json={"icon": "👩‍💻"})
            ck("change: a sequence is kept whole", r.status_code == 200 and store.get_project(uid)["icon"] == "👩‍💻", r.text)
            ck("an icon does not count as activity on the project",
               store.get_project(uid)["updated_at"] == before)
            r = await cx.put(f"/api/projects/{uid}", json={"goal": "Move the blog now", "icon": "🇩🇪"})
            ck("goal and icon in one body: both saved", r.status_code == 200
               and store.get_project(uid)["goal"] == "Move the blog now" and store.get_project(uid)["icon"] == "🇩🇪", r.text)
            r = await cx.put(f"/api/projects/{uid}", json={"goal": "Other words"})
            ck("a body without the icon leaves it", r.status_code == 200 and store.get_project(uid)["icon"] == "🇩🇪", r.text)
            r = await cx.put(f"/api/projects/{uid}", json={"icon": ""})
            ck("remove: the empty string removes it", r.status_code == 200 and store.get_project(uid)["icon"] is None, r.text)
            await cx.put(f"/api/projects/{uid}", json={"icon": "🔥"})
            r = await cx.put(f"/api/projects/{uid}", json={"icon": None})
            ck("remove: null removes it", r.status_code == 200 and store.get_project(uid)["icon"] is None, r.text)

            print("== refused ==")
            await cx.put(f"/api/projects/{uid}", json={"icon": "🔥"})
            for v in BAD:
                r = await cx.put(f"/api/projects/{uid}", json={"icon": v})
                ck(f"refused over the wire: {v!r}", r.status_code == 400 and NOT_ONE_EMOJI in r.text, (r.status_code, r.text))
            ck("and the icon stayed", store.get_project(uid)["icon"] == "🔥")
            r = await cx.put(f"/api/projects/{uid}", json={"goal": "Changed", "icon": "<b>"})
            ck("a wrong icon beside a goal saves neither", r.status_code == 400
               and store.get_project(uid)["goal"] == "Other words", (r.status_code, r.text))
            r = await cx.put(f"/api/projects/{uid}", json={"icon": 5})
            ck("a value that is not text is refused", r.status_code == 422, r.status_code)
            ck("the name never changed", store.get_project(uid)["name"] == "project-y")

            print("== who may change it ==")
            async with mk(reader_key) as rd:
                r = await rd.put(f"/api/projects/{uid}", json={"icon": "🐛"})
                ck("a read-only key cannot change it", r.status_code == 403, (r.status_code, r.text))
                r = await rd.get(f"/api/factory/projects/{uid}")
                ck("but sees it, and the page says it cannot change it", r.status_code == 200
                   and r.json()["icon"] == "🔥" and r.json()["can_edit_icon"] is False, r.text)
            ck("the icon stayed", store.get_project(uid)["icon"] == "🔥")
            r = await cx.get(f"/api/factory/projects/{uid}")
            ck("an admin's page says it can", r.json()["can_edit_icon"] is True, r.json().get("can_edit_icon"))
            ck("the name is still just the name, on every read",
               r.json()["name"] == "project-y" and "🔥" not in r.json()["name"])

            print("== the reads the console uses ==")
            page = r.json()
            ck("the project page has it", page["icon"] == "🔥")
            rows = (await cx.get("/api/factory/projects")).json()["projects"]
            by = {x["name"]: x for x in rows}
            ck("the Projects list has it", by["project-y"]["icon"] == "🔥" and by["project-a"]["icon"] == "🚀", rows)
            home = (await cx.get("/api/home")).json()
            ck("Home's project rows have it, and the name is clean",
               {x["name"]: x["icon"] for x in home["projects"]} == {"project-y": "🔥", "project-a": "🚀"}, home["projects"])
            r = await cx.get(f"/api/factory/projects/{uid}/github")
            ck("the GitHub page has it", r.json().get("icon") == "🔥", r.text[:100])
            r = await cx.get(f"/api/factory/projects/{uid}/docs")
            ck("the docs page's project has it", r.json()["project"].get("icon") == "🔥", r.text[:100])


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)

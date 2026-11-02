"""The factory Docs and memory page (TR-737): GET /api/factory/docs and /api/factory/docs/{id}.

The page reads the crew's shared rules and memory, and each project's spec, plan, rules and memory,
as documents. Every string is shown as it is, so the author and the time are worded for people
("the Builder", "3 days ago"). The words of a document body are the person's and the crew's own and
are exempt from the word check; the rest of the answer is checked by tests/test_factory_docs_page.py.

Who and when per line exists only for a project's memory: its lines end with "(2026-10-25, name)".
Everywhere else the page says who changed the whole document and when. A line is added through the
routes that already exist (`add.path` in the answer), so there is no write route here.

Each route answers the whole page in one read (the list and one open document), so a page load is
one call whichever way it was reached: /docs gives the first shared document, /docs?doc=<id> that
one.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timezone

from fastapi import HTTPException

from .. import docs as docs_mod
from ..envelope import now_utc
from ..factory_home import _dt, plain_ago

# the documents the page shows, in reading order, and the word for each
SHOWN = ("spec", "plan", "agents", "memory")
LABEL = {"spec": "Spec", "plan": "Plan", "agents": "Rules", "memory": "Memory"}
# the title of a shared document; a project's own is "<word>: <project name>"
SHARED_TITLE = {"agents": "Rules for every project", "memory": "Memory for every project"}
# who a crew member is called, by its role
ROLE_WORD = {"orchestrator": "the Lead", "reviewer": "the Reviewer", "releaser": "the Releaser",
             "builder": "the Builder", "helper": "the Helper"}
# a line of project memory ends with who wrote it and the day
STAMP = re.compile(r"\s*\((\d{4}-\d{2}-\d{2}),\s*([^()]*?)\)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")


def ago(ts, now) -> str:
    """"just now", "5 minutes ago", "3 hours ago", "3 days ago" (days up to two months), then
    weeks. "" without a time."""
    t, n = _dt(ts), _dt(now)
    if t is None or n is None:
        return ""
    minutes = max(0, int((n - t).total_seconds() // 60))
    if 1440 <= minutes < 60 * 1440:
        d = minutes // 1440
        return f"{d} day{'' if d == 1 else 's'} ago"
    return plain_ago(t, n)


def blocks(body: str) -> list[dict]:
    """The body as the rows the page shows: {kind, text}, kind one of h1, h2, h3, li, p. A fenced
    block, a table and the lines of one paragraph stay together; a heading and a bullet are
    one row each. The text is markdown; the page renders it without raw HTML."""
    out: list[dict] = []
    lines = str(body or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                j += 1
            out.append({"kind": "p", "text": "\n".join(lines[i:j + 1])})
            i = j + 1
            continue
        m = _HEADING.match(line)
        if m:
            out.append({"kind": f"h{min(len(m.group(1)), 3)}", "text": m.group(2).strip()})
            i += 1
            continue
        m = _BULLET.match(line)
        if m:
            out.append({"kind": "li", "text": m.group(1).strip()})
            i += 1
            continue
        j = i
        while (j < len(lines) and lines[j].strip() and not _HEADING.match(lines[j])
               and not _BULLET.match(lines[j]) and not lines[j].lstrip().startswith("```")
               and not (lines[j].lstrip().startswith("|") != line.lstrip().startswith("|"))):
            j += 1
        out.append({"kind": "p", "text": "\n".join(lines[i:j])})
        i = j
    return out


def register(app, store, h, ctx):
    def roles() -> dict[str, str]:
        """{station name: role} of the crew's sessions."""
        try:
            rows = store.station_sessions(h.cc_source())
        except Exception:
            return {}
        return {r["station"]: r["role"] for r in rows if r.get("station") and r.get("role")}

    def who(name, station_roles: dict[str, str]) -> str:
        """The author in words: a crew member by its role, the console as "you"."""
        name = str(name or "").strip()
        if not name:
            return ""
        if name in ("console", "you"):
            return "you"
        if name == "tares":
            return "Tares"
        role = station_roles.get(name)
        if not role and name.startswith("crew-"):
            role = name[len("crew-"):].split("-")[0]
        return ROLE_WORD.get(role or "", name)

    def changed(d: dict, station_roles, now) -> str:
        by = who(d.get("updated_by"), station_roles)
        when = ago(d.get("updated_at"), now)
        return ("Changed " + " ".join(x for x in (f"by {by}," if by else "", when) if x)).rstrip(",")

    def project_docs(uid: str, gids: set[str]) -> list[dict]:
        found: dict[str, dict] = {}
        for d in sorted(store.list_docs(uid), key=lambda x: str(x["created_at"])):
            if d["kind"] in SHOWN and d["id"] not in gids and d["kind"] not in found:
                found[d["kind"]] = d
        return [{"id": found[k]["id"], "kind": k, "label": LABEL[k]} for k in SHOWN if k in found]

    def listing() -> dict:
        ids = h.global_ids()
        gids = set(ids.values())
        shared = [{"id": ids.get(k, k), "kind": k, "label": LABEL[k],
                   "title": SHARED_TITLE[k]} for k in ("agents", "memory")]
        open_, done = [], []
        for p in store.list_projects():
            if p.get("kind") != "software_factory":
                continue
            docs = project_docs(p["id"], gids)
            if docs:
                (done if p["status"] == "done" else open_).append(
                    {"id": p["id"], "name": p["name"], "docs": docs})
        return {"empty": not gids and not open_ and not done, "shared": shared,
                "projects": open_, "finished": done}

    def view(doc_id: str) -> dict:
        """One document as the rows the page shows, with who changed it and when, and where a
        line is added (`add`, null when the page does not add to it)."""
        now = now_utc()
        station_roles = roles()
        ids = h.global_ids()
        gkind = next((k for k, v in ids.items() if v == doc_id), None)
        if gkind is None and doc_id in docs_mod.GLOBAL_DOCS and doc_id not in ids:
            gkind = doc_id   # a shared document nobody has written yet
        if gkind:
            d = store.get_doc(None, ids[gkind]) if gkind in ids else None
            d = d or {"id": gkind, "kind": gkind, "body": "", "updated_at": None, "updated_by": None}
            title = SHARED_TITLE[gkind]
            project = None
            word = "the shared rules" if gkind == "agents" else "the shared memory"
            add = {"path": f"/api/docs/global/{gkind}/lines",
                   "hint": f"Add a line to {word}. The crew reads it from the next task."}
        else:
            d = store.get_doc(None, doc_id)
            if d is None or d["kind"] not in SHOWN or not d.get("projects"):
                raise HTTPException(404, f"no document {doc_id!r}")
            p = store.get_project(d["projects"][0])
            if p is None or p.get("kind") != "software_factory":
                raise HTTPException(404, f"no document {doc_id!r}")
            project = {"id": p["id"], "name": p["name"]}
            title = f"{LABEL[d['kind']]}: {p['name']}"
            add = ({"path": f"/api/projects/{p['id']}/memory/lines",
                    "hint": "Add a line to this memory. The crew reads it from the next task."}
                   if d["kind"] == "memory" else None)
        kind = d["kind"]
        rows = blocks(d.get("body"))
        if rows and rows[0]["kind"] == "h1":
            rows = rows[1:]   # the title is in the header
        if kind == "memory":
            # only the lines people and the crew added, not the note that opens the document
            first = next((i for i, r in enumerate(rows) if r["kind"] == "li"), len(rows))
            rows = rows[first:]
        last = max((i for i, r in enumerate(rows) if r["kind"] == "li"), default=-1)
        for i, r in enumerate(rows):
            r["by"], r["when"] = "", ""
            if project and kind == "memory" and r["kind"] == "li":
                m = STAMP.search(r["text"])
                if m:
                    r["text"] = r["text"][:m.start()].rstrip()
                    r["by"] = who(m.group(2), station_roles)
                    day = datetime.combine(datetime.fromisoformat(m.group(1)).date(), time(),
                                           tzinfo=timezone.utc)
                    fresh = (i == last and _dt(d.get("updated_at")) is not None
                             and _dt(d["updated_at"]).date() == now.date() == day.date())
                    r["when"] = ago(d["updated_at"] if fresh else day, now) if (
                        fresh or day.date() < now.date()) else "today"
        empty = not rows
        return {"id": doc_id, "kind": kind, "label": LABEL.get(kind, kind), "title": title,
                "project": project, "shared": project is None,
                "changed": "Nothing here yet." if empty else changed(d, station_roles, now),
                "empty": empty, "lines": rows, "add": add}

    @app.get("/api/factory/docs")
    def list_docs():
        """The page in one call: the documents, grouped (the two shared ones, each open project's
        own, then the finished projects'), and in `doc` the first shared one, open. `empty` is
        true when no document exists yet, and then `doc` is null."""
        out = listing()
        out["doc"] = None if out["empty"] else view(out["shared"][0]["id"])
        return out

    @app.get("/api/factory/docs/{doc_id}")
    def get_doc(doc_id: str):
        """The same page with `doc` set to this one (404 for a document the page does not show)."""
        out = listing()
        out["doc"] = view(doc_id)
        return out

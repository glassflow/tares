# Tares enterprise licence, see ee/LICENSE
"""Decisions added by hand, and one list of every decision the brain holds (TR-1212).

    POST   /api/brain/decisions         add a decision that lives nowhere else          201
    DELETE /api/brain/decisions/{id}    take back one added by hand                      200
    GET    /api/brain/decisions         every decision from every place, with counts

Anyone on the cell adds a decision by pasting text or uploading a file, and names who decided. It is
put into lore as a document of kind `decision` under the source `decisions` (`SOURCE`), with the
external id `hand:<uuid4>` and the metadata `where: by_hand`.

Add: the JSON body is

    {"title": "Use Postgres for the state store",        # required, at most 200 characters
     "text": "We chose ...",                              # the decision, OR a file, never both
     "file": {"name": "adr-7.pdf", "content_base64": "JVBERi0x..."},
     "decided_by": "Ana Lopez"}                           # optional

`file` is a Markdown (.md, .markdown), plain text (.txt) or PDF (.pdf) file, sent as base64 inside
the JSON, at most 5 MB once decoded (413 above that). The text of a decision, pasted or read from a
file, is at most 1 MB (lore's limit; 413 in plain words above that, nothing is sent to lore). A text
file must be UTF-8. A PDF becomes text on the cell with `pypdf` (the `ee` extra of the package; without it a PDF answers 501 and pasting
still works); a PDF with no text in it (a scan), a password-protected one and a corrupt one are each
a 400 in plain words. Everything is checked before anything is sent to lore: a refused request writes
nothing. A null, empty or blank `text` or `file` counts as not given.

`decided_by` is a person on the cell: their id, their email or their exact name (ignoring case). It
defaults to the person adding the decision. A name that matches nobody, or two people, is a 400. The
person's display name is what is stored (`decided_by: [name]`), with the date it was added
(`decided_at`, RFC 3339 UTC), who added it (`added_by`, a display name) and `added_by_id` (the
adder's person id, so a delete can tell who added it; lore keeps keys it does not know) and a
one-line `summary` (the first sentence, cut at 200 characters).

Delete: only a decision added by hand (an id that starts with `hand:`; any other id is a 400 and is
never deleted). The person who added it or an admin may delete it; another member gets 403, an id
lore does not hold 404. With auth off the one person is the adder and the admin.

List: `?where=&repo=&cursor=&limit=` (limit 1 to 200, default 50). Every `decision` lore holds under
the cell's brain key, newest first, from the repos' folders (`repo_folder`), shared factory projects
(`factory_project`) and by hand (`by_hand`). A row has `id` (the external id), `source`, `title`,
`summary` (null when lore's listing has none: it carries no text, and only a decision added by hand
keeps a summary), `where` (null when missing or unknown), `decided_by`, `changed_by`, `decided_at`,
`changed_at`, `link`, `repo`, `project`, `added_by`, `can_delete`, `project_name` and `doc_kind` (TR-1443).
`can_delete` is true only for a decision added by hand that the viewer added or that the viewer may
delete as an admin, the rule DELETE enforces (with auth off, true on every hand-added row); it is
false on every other row, and the 201 from add carries it too. `project_name` is the name the
project has on the cell now and `doc_kind` is `spec`, `plan` or `note`, both only on a factory
project's row and null on any other; a row put into lore before `doc_kind` existed says null until
the project pass puts it again (the next pass after the cell starts). `where` and `repo` (`owner/name`, any case)
filter on the cell; `cursor` is lore's own and opaque: pass back `next_cursor` for the next page
(null on the last). A page holds exactly the matching rows lore's page showed, so a filter never skips
a row or repeats one. `counts` is `{all, repo_folder, factory_project, by_hand}` over every visible
decision, whatever `where`, `repo`, `cursor` or `limit` say (a decision with a missing or unknown
`where` counts only in `all`); it is read by walking lore's pages, nothing is cached.

Privacy: a decision that names a project (`metadata.project`) is dropped, from the list and the
counts, when that project is private or unknown to the cell. Shared projects' decisions are not
narrowed per person: only private projects have members, and the brain never holds those.

Every route answers 404 as if never mounted without a licence or without the brain settings. Nothing
secret is returned or logged: lore's errors pass through the client's own scrubbing.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import Body, HTTPException, Request

from tares import licence

from . import repos as repos_mod
from .decisions import DOC_KINDS
from .lore import LoreClient, LoreError

__all__ = ["KIND", "MAX_BYTES", "PREFIX", "SOURCE", "TEXT_MAX", "WHERE", "register_routes"]

SOURCE = "decisions"                  # the lore source decisions added by hand live under
KIND = "decision"
PREFIX = "hand:"                      # every external id added by hand starts with this
WHERE = ("repo_folder", "factory_project", "by_hand")
MAX_BYTES = 5 * 1024 * 1024           # a file, decoded
TEXT_MAX = 1024 * 1024                # the text of a decision: lore refuses a document text over 1 MiB
TITLE_MAX = 200                       # characters
NAME_BYTES = 256                      # lore's limit for a name
SUMMARY_MAX = 200                     # characters
PAGE = 200                            # documents per lore page when walking
MAX_PAGES = 500                       # a walk that never ends is given up (100,000 decisions)
SCAN_PAGES = 50                       # lore pages one list request reads while its filters skip rows
TEXT_FILES = (".md", ".markdown", ".txt")
_ID = re.compile(r"hand:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _clip(text: str, limit: int = NAME_BYTES) -> str:
    """`text` cut to at most `limit` bytes, never in the middle of a character."""
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore").rstrip()


def summary_of(text: str) -> str:
    """The first sentence of `text` on one line, at most SUMMARY_MAX characters."""
    flat = " ".join(text.split())
    m = re.search(r"[.!?](?:\s|$)", flat)
    first = flat[: m.end()].strip() if m else flat
    return first if len(first) <= SUMMARY_MAX else first[: SUMMARY_MAX - 3].rstrip() + "..."


def pdf_text(data: bytes) -> str:
    """The text of a PDF. Raises ValueError in plain words when it cannot be read or holds none, and
    LookupError when `pypdf` is not installed."""
    try:
        import pypdf   # imported here: the module works without it, a PDF upload does not
    except ImportError:
        raise LookupError("pypdf") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("This PDF is password protected. Remove the password or paste the text.")
        parts, size = [], 0
        for page in reader.pages:
            t = (page.extract_text() or "").strip()
            if t:
                parts.append(t)
                size += len(t.encode("utf-8"))
                if size > MAX_BYTES:
                    raise ValueError("The text in this PDF is over 5 MB. Paste the part that records the decision.")
    except ValueError:
        raise
    except Exception:   # noqa: BLE001  (a corrupt file raises anything; its words may be odd, so say our own)
        raise ValueError("This file could not be read as a PDF. Check that it is a PDF and not damaged.") from None
    text = "\n\n".join(parts).replace("\x00", "").strip()
    if not text:
        raise ValueError("This PDF holds no text to read (it may be a scan). Paste the text instead.")
    return text


def register_routes(app, host) -> None:
    store = host.store

    def need_brain():
        if not licence.ee_allowed() or not LoreClient.configured():
            raise HTTPException(status_code=404, detail="Not Found")   # as if never mounted

    def lore_fail(e: LoreError):
        host.err(ValueError(str(e)), 502)

    # -- people -----------------------------------------------------------------------------

    def display_name(p: dict) -> str:
        return _clip(" ".join(str(p.get("name") or "").split()))

    def resolve_person(ref: str) -> dict:
        """The person `ref` names: an id, an email or an exact name. 400 in plain words otherwise."""
        ref = ref.strip()
        people = store.list_people()
        found = [p for p in people if p["id"] == ref]
        if not found:
            low = ref.lower()
            found = [p for p in people if low in ([(p.get("email") or "").lower()] + [e.lower() for e in p.get("emails") or []])]
        if not found:
            found = [p for p in people if " ".join(str(p.get("name") or "").split()) == ref] \
                or [p for p in people if " ".join(str(p.get("name") or "").split()).lower() == ref.lower()]
        if not found:
            host.err(ValueError(f"decided_by: nobody on this cell is {ref!r}. Name a person by their name, email or id."))
        if len(found) > 1:
            host.err(ValueError(f"decided_by: more than one person is called {ref!r}. Use their email or id."))
        if not display_name(found[0]):
            host.err(ValueError("decided_by: that person has no name on this cell yet."))
        return found[0]

    # -- walking lore -----------------------------------------------------------------------

    def visible(entry: dict, private: set[str], known: set[str]) -> bool:
        """False for a decision of a private or unknown project. Never shown, never counted."""
        project = (entry.get("metadata") or {}).get("project")
        if project is None:
            return True
        return isinstance(project, str) and project in known and project not in private

    def privacy():
        private = {p["id"] for p in store.private_projects()}
        known = {p["id"] for p in store.list_projects()}
        return private, known

    async def walk(lore, *, source: str | None = None):
        """Every decision lore holds (of `source` when given), page by page."""
        cursor, seen = None, set()
        for _ in range(MAX_PAGES):
            page = await lore.list_documents(kind=KIND, source=source, limit=PAGE, cursor=cursor)
            for d in page.get("documents") or []:
                if isinstance(d, dict):
                    yield d
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                return
            seen.add(cursor)

    def project_names() -> dict[str, str]:
        """The names the cell's projects have now, by project id."""
        return {p["id"]: (p.get("name") or "").strip() or p["id"] for p in store.list_projects()}

    def viewer_of(request: Request) -> tuple[str | None, bool, bool]:
        """Who is asking, for can_delete: their person id (None for a key with no person), whether
        they are an admin (with auth off, everyone is) and whether the credential may write at all
        (the guard refuses DELETE for a read-only key before this route runs; auth off sets no
        scopes and lets everyone through)."""
        person = host.current_person(request)
        scopes = getattr(request.state, "scopes", None)
        writes = scopes is None or "ingest" in scopes or "admin" in scopes
        return (person["id"] if person else None), bool(host.is_admin(request)), writes

    def row_of(d: dict, viewer: tuple[str | None, bool, bool] = (None, False, False), pnames: dict[str, str] | None = None) -> dict:
        m = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        where = m.get("where") if m.get("where") in WHERE else None

        def text(k):
            v = m.get(k)
            return v if isinstance(v, str) and v else None

        def names(k):
            v = m.get(k)
            return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
        ext = d.get("external_id")
        # the rule DELETE enforces: a decision added by hand, taken back by its adder or an admin
        by_hand = d.get("source") == SOURCE and isinstance(ext, str) and _ID.fullmatch(ext) is not None
        adder = m.get("added_by_id")
        can_delete = by_hand and viewer[2] and (viewer[1] or (viewer[0] is not None and adder == viewer[0]))
        project = text("project")
        in_project = where == "factory_project" and project is not None
        kind = m.get("doc_kind")
        return {"id": d.get("external_id"), "source": d.get("source"), "title": d.get("title"),
                "summary": text("summary"), "where": where, "decided_by": names("decided_by"),
                "changed_by": names("changed_by"), "decided_at": text("decided_at"),
                "changed_at": text("changed_at"), "link": d.get("uri") or None,
                "repo": text("repo"), "project": project, "added_by": text("added_by"),
                "can_delete": bool(can_delete),
                "project_name": (pnames or {}).get(project) if in_project else None,
                "doc_kind": kind if in_project and kind in DOC_KINDS else None}

    # -- add --------------------------------------------------------------------------------

    @app.post("/api/brain/decisions", status_code=201)
    async def add_decision(request: Request, body: dict = Body(...)):
        """Add a decision that lives nowhere else; the module's docstring has the body. 201 with the
        new decision: {id, source, title, summary, where, decided_by, changed_by, decided_at,
        changed_at, link, repo, project, added_by}. Every check runs before lore is called."""
        need_brain()
        extra = sorted(set(body) - {"title", "text", "file", "decided_by"})
        if extra:
            host.err(ValueError(f"unknown field {extra[0]!r}: send title, text or file, and decided_by"))
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            host.err(ValueError("title: give the decision a title."))
        title = title.replace("\x00", "").strip()
        if not title:
            host.err(ValueError("title: give the decision a title."))
        if len(title) > TITLE_MAX:
            host.err(ValueError(f"title: at most {TITLE_MAX} characters (this one has {len(title)})."))
        text, file = body.get("text"), body.get("file")
        if text is not None and not isinstance(text, str):
            host.err(ValueError("text: must be text."))
        if file is not None and not isinstance(file, (dict, str)):
            host.err(ValueError('file: send {"name": "adr.pdf", "content_base64": "..."}.'))
        has_text = bool(text and text.strip())
        has_file = bool(file) and not (isinstance(file, str) and not file.strip())
        if has_text and has_file:
            host.err(ValueError("Send the text or a file, not both."))
        if not has_text and not has_file:
            host.err(ValueError("Paste the text of the decision or upload a file."))
        said = body.get("decided_by")
        if said is not None and not isinstance(said, str):
            host.err(ValueError("decided_by: must be a person's name, email or id."))
        adder = host.current_person(request)
        if said and said.strip():
            decider = resolve_person(said)
        elif adder is not None and store.get_person(adder["id"]) is not None:
            decider = resolve_person(adder["id"])
        else:
            host.err(ValueError("decided_by: say who decided, by name, email or id."))
        if has_file:
            text = await file_text(file)
        else:
            text = text.replace("\x00", "").strip()
        if not text:
            host.err(ValueError("text: the text has nothing in it."))
        size = len(text.encode("utf-8"))
        if size > TEXT_MAX:
            host.err(ValueError(f"The text of a decision is at most 1 MB and this one is {size / 1048576:.1f} MB. "
                                "Paste or upload the part that records the decision."), 413)
        added_by = _clip(" ".join(str(adder.get("name") or "").split())) if adder else ""
        added_by = added_by or _clip(" ".join(host.by(request).split())) or "Cell admin"
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ext = PREFIX + str(uuid.uuid4())
        meta: dict[str, Any] = {"where": "by_hand", "decided_by": [display_name(decider)], "decided_at": now,
                                "added_by": added_by, "summary": summary_of(text)}
        if adder:
            meta["added_by_id"] = adder["id"]
        doc = {"external_id": ext, "title": title, "text": text, "kind": KIND, "metadata": meta}
        try:
            async with repos_mod.lore_client() as lore:
                await lore.put_documents(SOURCE, [doc])
        except LoreError as e:
            lore_fail(e)
        return row_of({"external_id": ext, "source": SOURCE, "title": title, "uri": None, "metadata": meta},
                      viewer_of(request))

    async def file_text(file) -> str:
        """The text of an uploaded file, or a 400 (413 when it is too big, 501 for a PDF without pypdf)."""
        if not isinstance(file, dict):
            host.err(ValueError('file: send {"name": "adr.pdf", "content_base64": "..."}.'))
        name, b64 = file.get("name"), file.get("content_base64")
        if not isinstance(name, str) or not name.strip():
            host.err(ValueError("file: name the file, like adr.md or adr.pdf."))
        if not isinstance(b64, str) or not b64.strip():
            host.err(ValueError("file: content_base64 is empty."))
        name = name.strip().lower()
        pdf = name.endswith(".pdf")
        if not pdf and not name.endswith(TEXT_FILES):
            host.err(ValueError("file: upload a Markdown (.md), text (.txt) or PDF (.pdf) file."))
        b64 = "".join(b64.split())
        if len(b64) > (MAX_BYTES // 3 + 1) * 4:   # too big even before decoding: do not decode it
            host.err(ValueError("file: at most 5 MB."), 413)
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            host.err(ValueError("file: content_base64 is not valid base64."))
        if len(data) > MAX_BYTES:
            host.err(ValueError("file: at most 5 MB."), 413)
        if not data:
            host.err(ValueError("file: the file is empty."))
        if pdf:
            try:
                text = await asyncio.to_thread(pdf_text, data)
            except LookupError:
                host.err(RuntimeError("Reading PDF files is not set up on this cell (the pypdf package is missing). Paste the text instead."), 501)
            except ValueError as e:
                host.err(e)
            return text
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            host.err(ValueError("file: a text file must be UTF-8."))
        text = text.replace("\x00", "").strip()
        if not text:
            host.err(ValueError("file: the file has no text in it."))
        return text

    # -- delete -----------------------------------------------------------------------------

    @app.delete("/api/brain/decisions/{decision_id}")
    async def delete_decision(decision_id: str, request: Request):
        """Take back a decision added by hand. Only the person who added it or an admin; only an id
        that starts with `hand:` (any other is a 400 and nothing is deleted)."""
        need_brain()
        if not decision_id.startswith(PREFIX):
            host.err(ValueError("Only a decision added by hand can be deleted here. Decisions from a repo or a project are changed where they live."))
        if not _ID.fullmatch(decision_id):
            host.err(KeyError("There is no such decision."), 404)
        try:
            async with repos_mod.lore_client() as lore:
                found = None
                async for d in walk(lore, source=SOURCE):
                    if d.get("external_id") == decision_id:
                        found = d
                        break
                if found is None:
                    host.err(KeyError("There is no such decision."), 404)
                person = host.current_person(request)
                added_by = (found.get("metadata") or {}).get("added_by_id")
                mine = bool(person and isinstance(added_by, str) and added_by == person["id"])
                if not (mine or host.is_admin(request)):
                    host.err(PermissionError("Only the person who added this decision, or an admin, can delete it."), 403)
                await lore.delete_document(SOURCE, decision_id)
        except LoreError as e:
            if e.status == 404:
                host.err(KeyError("There is no such decision."), 404)
            lore_fail(e)
        return {"ok": True, "removed": decision_id}

    # -- list -------------------------------------------------------------------------------

    @app.get("/api/brain/decisions")
    async def list_decisions(request: Request, where: str | None = None, repo: str | None = None,
                             cursor: str | None = None, limit: int = 50):
        """Every decision from every place, newest first, with `counts` (see the module's docstring):
        {decisions: [...], next_cursor, counts: {all, repo_folder, factory_project, by_hand}}."""
        need_brain()
        where = (where or "").strip()
        if where in ("all",):
            where = ""
        if where and where not in WHERE:
            host.err(ValueError(f"where: one of {', '.join(WHERE)}."))
        repo = (repo or "").strip().lower()
        if not 1 <= limit <= 200:
            host.err(ValueError("limit: a whole number from 1 to 200."))
        private, known = privacy()
        viewer, pnames = viewer_of(request), project_names()

        def wanted(d: dict) -> bool:
            if not visible(d, private, known):
                return False
            r = row_of(d)
            return (not where or r["where"] == where) and (not repo or (r["repo"] or "").lower() == repo)

        rows: list[dict] = []
        nxt = (cursor or "").strip() or None
        counts = {"all": 0, **{w: 0 for w in WHERE}}
        try:
            async with repos_mod.lore_client() as lore:
                for _ in range(SCAN_PAGES):
                    # ask for only as many as are still missing: a page never overshoots, so the cursor
                    # lore gives back is exactly where this page ended
                    page = await lore.list_documents(kind=KIND, limit=limit - len(rows), cursor=nxt)
                    rows += [row_of(d, viewer, pnames) for d in page.get("documents") or [] if isinstance(d, dict) and wanted(d)]
                    nxt = page.get("next_cursor") or None
                    if len(rows) >= limit or not nxt:
                        break
                async for d in walk(lore):
                    if visible(d, private, known):
                        counts["all"] += 1
                        w = row_of(d)["where"]
                        if w:
                            counts[w] += 1
        except LoreError as e:
            if e.status == 400 and cursor:
                host.err(ValueError("cursor: not a cursor the brain gave out."))
            lore_fail(e)
        return {"decisions": rows, "next_cursor": nxt, "counts": counts}

# Tares enterprise licence, see ee/LICENSE
"""The specs, plans and notes of shared factory projects, in the brain as decisions (TR-1211).

A shared (not private) project that is linked to at least one repo the brain holds has its docs of
kind `spec`, `plan` and `note` put into lore, each as one document of kind `decision` under the source
`factory_projects`, so an ask about that repo can cite why the work was done. A person asks nothing:
`Decisions` compares what the cell holds with what lore holds every `INTERVAL_SECONDS` and makes lore
match.

What goes in:

* only a project whose scope is `team`, linked (its hand-over names the repo) to a repo on the brain;
* only docs of kind spec, plan or note. Never a ticket, a working doc, an AGENTS.md, a start prompt,
  memory, grants, or a person's own doc;
* one lore document per (project, doc): external id `project:<project id>:doc:<doc id>`. A doc that
  several projects include goes in once for each shared, linked project that includes it, and each entry
  names that project. A doc that only a private project includes never goes in, and a private project
  is never named in anything sent.

What comes out: every lore entry of this source whose external id starts with `project:` and is not in
the wanted set. That covers a project made private, a project unlinked from every repo, a doc removed
from the project, a doc whose kind changed to one that is left out. A project made shared again has its
docs put back by the next pass. Entries of other sources, and of this source with another external id
shape, are never touched.

Privacy ordering: a pass deletes before it puts, and the delete step does not depend on lore's listing
working (it falls back to what this process put earlier). A project is checked for privacy again just
before each batch is sent and once more when the pass ends.

Nothing secret is sent or logged: only the docs' own title, text and author name. A lore error is logged
through the client's own scrubbed words; any other error by its type only.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from urllib.parse import quote

from tares import licence
from tares.factory import norm_repo
from tares.slack import public_base

from .lore import LoreClient, LoreError, scrub

__all__ = ["BATCH", "DOC_KINDS", "INTERVAL_SECONDS", "KIND", "Decisions", "SOURCE"]

log = logging.getLogger("tares_ee.brain.decisions")

SOURCE = "factory_projects"          # the lore source these documents live under
KIND = "decision"                    # the lore kind of each one
PREFIX = "project:"                  # every external id this loop owns starts with this
DOC_KINDS = ("spec", "plan", "note") # the kinds of docs that go in; nothing else ever does
INTERVAL_SECONDS = 60.0              # between two passes (a test makes it small)
BATCH = 50                           # documents in one request
PAGE = 200                           # documents in one listing page
MAX_PAGES = 1000                     # a listing that never ends is given up
NAME_BYTES = 256                     # lore's limit for a name and a project id
UNKNOWN = "Unknown"                  # the author of a doc that has none


def _say(e) -> str:
    """An error in words that are safe to log: lore's own text with the brain's key taken out."""
    return scrub(e, [os.environ.get("TARES_BRAIN_KEY", "")])


def external_id(project: str, doc_id: str) -> str:
    return f"{PREFIX}{project}:doc:{doc_id}"


def doc_uri(project: str, doc_id: str) -> str:
    """The doc in the console. A relative path while the cell has no public address."""
    return f"{public_base()}/projects/{quote(project, safe='')}/docs?doc={quote(doc_id, safe='')}"


def _clip(text: str, limit: int = NAME_BYTES) -> str:
    """`text` cut to at most `limit` bytes, never in the middle of a character."""
    raw = text.encode("utf-8")
    return text if len(raw) <= limit else raw[:limit].decode("utf-8", "ignore").rstrip()


def _when(*values) -> str:
    """The first usable time of `values` as an RFC 3339 string in UTC; today's date when none is."""
    for v in values:
        try:
            if isinstance(v, datetime):
                t = v
            elif isinstance(v, date):
                return v.isoformat()
            elif isinstance(v, str) and v.strip():
                t = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace("z", "+00:00"))
            else:
                continue
        except ValueError:
            continue
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return datetime.now(timezone.utc).date().isoformat()


def build_document(project: str, doc: dict) -> dict | None:
    """The lore document for `doc` (a store doc with its body) as it is in `project`, or None when it
    cannot be sent (a project id lore would refuse). `project` is the shared project the doc is sent
    for; nothing else about where the doc lives is read."""
    if not project or len(project.encode("utf-8")) > NAME_BYTES:
        return None
    title = (doc.get("title") or "").strip() or "Untitled"
    text = doc.get("body") or ""
    if not text.strip():
        text = title   # lore needs text; the title keeps an empty doc findable
    author = _clip(" ".join(str(doc.get("updated_by") or "").split())) or UNKNOWN
    return {
        "external_id": external_id(project, doc["id"]),
        "title": title,
        "text": text,
        "kind": KIND,
        "uri": doc_uri(project, doc["id"]),
        "metadata": {
            "where": "factory_project",
            "project": project,
            "doc_kind": doc.get("kind") if doc.get("kind") in DOC_KINDS else None,
            "decided_by": [author],
            "decided_at": _when(doc.get("updated_at"), doc.get("created_at")),
        },
    }


def _digest(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class Decisions:
    """One per app. `lore_client` is a callable that gives a `LoreClient` (looked up per call, so a test
    can swap it)."""

    def __init__(self, store, lore_client):
        self.store = store
        self._lore = lore_client
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._sent: dict[str, str] = {}   # external id -> digest of what this process last put there
        self._known: set[str] = set()     # external ids this process saw in lore or put there
        self.task: asyncio.Task | None = None
        self.passes = 0                   # passes that ran, for tests

    def active(self) -> bool:
        return licence.ee_allowed() and LoreClient.configured()

    def wake(self) -> None:
        self._wake.set()

    # -- what lore should hold --------------------------------------------------------------

    def _private_ids(self) -> set[str]:
        return {p["id"] for p in self.store.private_projects()}

    def wanted(self) -> dict[str, dict]:
        """`{external id: lore document}` for every doc that should be in lore now, read from the
        cell's own store. Blocking: the pass runs it in a thread."""
        private = self._private_ids()
        connected = {r["repo"] for r in self.store.list_brain_repos()}
        out: dict[str, dict] = {}
        if not connected:
            return out
        for p in self.store.list_projects():
            pid = p["id"]
            if pid in private or p.get("scope") != "team":
                continue
            if not self._linked(pid, connected):
                continue
            for listed in self.store.list_docs(pid):
                if listed["kind"] not in DOC_KINDS or listed.get("owner"):
                    continue
                full = self.store.get_doc(pid, listed["id"])
                if not full or full["kind"] not in DOC_KINDS or full.get("owner"):
                    continue   # gone or changed since the listing
                doc = build_document(pid, full)
                if doc is not None:
                    out[doc["external_id"]] = doc
        return out

    def _linked(self, project: str, connected: set[str]) -> bool:
        handover = self.store.get_handover(project) or {}
        for r in handover.get("repos") or []:
            try:
                name = norm_repo((r or {}).get("name"))
            except ValueError:
                continue
            if name in connected:
                return True
        return False

    # -- lore -------------------------------------------------------------------------------

    async def _listed(self, lore) -> dict[str, dict]:
        """Every decision of the source in lore, `{external id: entry}`."""
        out: dict[str, dict] = {}
        cursor, seen = None, set()
        for _ in range(MAX_PAGES):
            page = await lore.list_documents(kind=KIND, source=SOURCE, limit=PAGE, cursor=cursor)
            for d in page.get("documents") or []:
                if isinstance(d, dict) and isinstance(d.get("external_id"), str):
                    out[d["external_id"]] = d
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
        return out

    async def _delete(self, lore, ext: str) -> bool:
        """Delete one entry. A failure is logged and false; an entry already gone counts as done."""
        try:
            await lore.delete_document(SOURCE, ext)
        except LoreError as e:
            if e.status == 404:
                self._forget(ext)
                return True
            log.warning("brain decisions: could not remove %s: %s", ext, _say(e))
            return False
        except Exception as e:   # noqa: BLE001
            log.warning("brain decisions: could not remove %s: %s", ext, type(e).__name__)
            return False
        self._forget(ext)
        return True

    def _forget(self, ext: str) -> None:
        self._sent.pop(ext, None)
        self._known.discard(ext)

    async def reconcile(self) -> None:
        """One pass: delete what must not be in lore, then put what is missing or changed."""
        if not self.active():
            return
        async with self._lock:
            await self._pass()

    async def _pass(self) -> None:
        wanted = await asyncio.to_thread(self.wanted)
        async with self._lore() as lore:
            listed = None
            try:
                listed = await self._listed(lore)
            except LoreError as e:
                log.warning("brain decisions: could not list lore's documents: %s", _say(e))
            except Exception as e:   # noqa: BLE001
                log.warning("brain decisions: could not list lore's documents: %s", type(e).__name__)
            if listed is not None:
                self._known |= set(listed)
            # 1. deletes first. Without a listing, what this process put earlier is all it can name.
            current = set(listed) if listed is not None else set(self._known)
            for ext in sorted(e for e in current if e.startswith(PREFIX) and e not in wanted):
                await self._delete(lore, ext)
            # 2. puts, a project re-checked for privacy just before each batch
            todo = []
            for ext, doc in wanted.items():
                digest = _digest(doc)
                if self._sent.get(ext) == digest and (listed is None or ext in listed):
                    continue   # put before, unchanged since, and lore still has it
                todo.append((ext, doc, digest))
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                private = await asyncio.to_thread(self._private_ids)
                batch = [t for t in batch if t[1]["metadata"]["project"] not in private]
                if not batch:
                    continue
                try:
                    await lore.put_documents(SOURCE, [t[1] for t in batch])
                except LoreError as e:
                    log.warning("brain decisions: could not put %d documents: %s", len(batch), _say(e))
                    continue
                except Exception as e:   # noqa: BLE001
                    log.warning("brain decisions: could not put %d documents: %s", len(batch), type(e).__name__)
                    continue
                for ext, _doc, digest in batch:
                    self._sent[ext] = digest
                    self._known.add(ext)
            # 3. a project made private while the pass ran
            if self._known:
                private = await asyncio.to_thread(self._private_ids)
                if private:
                    for ext in sorted(e for e in self._known if e.startswith(PREFIX)
                                      and e[len(PREFIX):].split(":doc:", 1)[0] in private):
                        await self._delete(lore, ext)
        self.passes += 1

    # -- the task ---------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.reconcile()
            except Exception as e:   # noqa: BLE001  (one bad pass must not end the loop)
                log.warning("brain decisions: a pass failed: %s", type(e).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(0.05, INTERVAL_SECONDS))

    def attach(self, app) -> None:
        """Start the task with the app and stop it when the app stops."""
        if getattr(app.state, "brain_decisions", None) is not None:
            return
        app.state.brain_decisions = self
        outer = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a):
            async with outer(a) as state:
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self.run())
                try:
                    yield state
                finally:
                    task, self.task = self.task, None
                    if task is not None:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task

        app.router.lifespan_context = lifespan

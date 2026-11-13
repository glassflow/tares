"""Tares memory as the built-in memory integration (TR-865).

The crew's memory stays in the two docs it always lived in: a project's own memory doc (what the
builders learned about that project) and the cell's shared memory doc (what holds across
projects). This class is a front over those docs, so a cell with no other memory integration
behaves as before. It adds no storage.

`scope` is a project's id, or `factory` for the shared memory.

    add(scope, line)               appends the line to the doc of that scope (a line already
                                   there is not added twice)
    recall(scope, question, limit) the lines of that project's memory plus the shared memory
                                   that share words with the question, each prefixed [project] or
                                   [shared]; for scope `factory`, the shared memory alone

Built bare (the settings page tests a connection) only `check_connection` works.
"""
from __future__ import annotations

import asyncio
import json
import re

from ..memory import MemoryStore
from ... import docs as docs_mod

FACTORY = "factory"
GLOBAL_SETTING = "global_docs"
MAX_LIMIT = 50
# words that carry no meaning of their own, left out of a question when it has others
_STOP = frozenset("""a an and are as at be but by can do does for from had has have how i if in into is
it its me my no not of on or our so than that the their them then there these they this to us was we
were what when where which who why will with would you your""".split())
_WORD = re.compile(r"[a-z0-9_]+")


# ── the docs ──────────────────────────────────────────────────────────────────
def global_ids(store, create: bool = False) -> dict[str, str]:
    """{kind: doc id} of the cell's shared docs that exist; with `create`, make the missing ones."""
    try:
        ids = json.loads(store.get_setting(GLOBAL_SETTING) or "{}")
    except ValueError:
        ids = {}
    ids = {k: v for k, v in ids.items() if store.get_doc(None, v) is not None}
    if create and set(docs_mod.GLOBAL_DOCS) - set(ids):
        for kind, (title, body) in docs_mod.GLOBAL_DOCS.items():
            if kind not in ids:
                ids[kind] = store.create_doc(None, kind, title, body, by="tares")
        store.set_setting(GLOBAL_SETTING, json.dumps(ids))
    return ids


def project_memory_doc(store, uid: str, create: bool = False) -> str | None:
    """The id of a project's own memory doc (not the shared one that sits beside it); with
    `create`, made with the first line."""
    gid = global_ids(store).get("memory")
    d = next((x for x in store.list_docs(uid, "memory") if x["id"] != gid), None)
    if d or not create:
        return d["id"] if d else None
    p = store.get_project(uid)
    return store.create_doc(uid, "memory", f"Memory: {p['name']}",
                            f"# Memory for {p['name']}\n\nWhat the builders learned about "
                            "this project, one line each, newest last.\n\n", by="tares")


def write_line(store, scope: str, text, note: str = "", by: str = "tares") -> dict:
    """Append one line to the doc of `scope`: {doc_id, added, line}, `line` being what is kept
    (the words, then `note`). Raises docs.DocError for an empty or too long line, and KeyError
    for a project that does not exist."""
    scope = str(scope or "").strip()
    if not scope:
        raise ValueError("a scope is required: a project's id, or `factory`")
    if scope == FACTORY:
        doc_id = global_ids(store, create=True)["memory"]
        cur = store.get_doc(None, doc_id)
    else:
        if store.get_project(scope) is None:
            raise KeyError(f"no project {scope!r}")
        doc_id = project_memory_doc(store, scope, create=True)
        cur = store.get_doc(scope, doc_id)
    body, added = docs_mod.append_line(cur["body"], text, note)
    if added:
        store.update_doc(doc_id, body=body, by=by)
    return {"doc_id": doc_id, "added": added, "line": " ".join(str(text).split()) + note}


# ── recall ────────────────────────────────────────────────────────────────────
def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def words(text) -> set[str]:
    """The lower-case words of `text`, a trailing plural s dropped."""
    return {_stem(w) for w in _WORD.findall(str(text or "").lower())}


def question_words(question) -> set[str]:
    """The words a question asks with: its own words, less the empty ones (the, is, how) unless
    those are all it has."""
    every = words(question)
    keep = {w for w in every if w not in _STOP and _stem(w) not in _STOP}
    return keep or every


def doc_lines(body: str) -> list[str]:
    """The remembered lines of a memory doc, in the order written (oldest first): its bullets."""
    return [ln[2:].strip() for ln in str(body or "").splitlines()
            if ln.startswith("- ") and ln[2:].strip()]


def rank(sources: list[tuple[str, list[str]]], question, limit: int = 10) -> list[str]:
    """`sources` is [(label, lines oldest first)]. The lines that share a word with `question`,
    most shared words first, then newest first, then the earlier source; each as `[label] line`,
    at most `limit` (1 to MAX_LIMIT)."""
    asked = question_words(question)
    if not asked:
        return []
    scored = []
    for label, lines in sources:
        for age, line in enumerate(reversed(lines)):
            hit = len(asked & words(line))
            if hit:
                scored.append((-hit, age, f"[{label}] {line}"))
    scored.sort(key=lambda s: (s[0], s[1]))   # stable: the first source wins a tie
    return [s[2] for s in scored[:max(1, min(int(limit or 10), MAX_LIMIT))]]


class MemoryTares(MemoryStore):
    name = "tares"
    title = "Tares"
    settings_schema: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Memory lives in Tares: nothing to connect."

    def _store(self):
        if self.store is None:
            raise RuntimeError("the Tares memory integration needs the cell's store")
        return self.store

    async def add(self, scope: str, line: str) -> None:
        await asyncio.to_thread(write_line, self._store(), scope, line)

    async def recall(self, scope: str, question: str, limit: int = 10) -> list[str]:
        return await asyncio.to_thread(self._recall, str(scope or "").strip(), question, limit)

    def _recall(self, scope: str, question: str, limit: int) -> list[str]:
        store = self._store()
        sources = []
        if scope and scope != FACTORY:
            doc_id = project_memory_doc(store, scope)
            d = store.get_doc(scope, doc_id) if doc_id else None
            sources.append(("project", doc_lines(d["body"] if d else "")))
        gid = global_ids(store).get("memory")
        g = store.get_doc(None, gid) if gid else None
        sources.append(("shared", doc_lines(g["body"] if g else "")))
        return rank(sources, question, limit)

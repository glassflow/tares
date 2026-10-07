"""Docs and tickets (P-TR-176, TR-403): what a spec session leaves in a project for the session
that builds it.

A doc is markdown that belongs to the cell and that projects include, like a skill: the spec, the
plan, the AGENTS.md of the build, the prompt a session starts with, a note, or the working doc of
one ticket. A ticket is one piece of work in a project's list. When the project uses Linear,
Linear owns its tickets and Tares keeps a synced record of them (tares/linear.py); otherwise Tares
owns them. Working docs and every other doc live only in Tares.

Two docs belong to every project: the cell's AGENTS.md and its memory (GLOBAL_DOCS). They are made
when the first project gets docs, and a session adds to them a line at a time.

This module validates what the API and the MCP tools hand the store.
"""
from __future__ import annotations

DOC_KINDS = {
    "spec": "what is built and why",
    "plan": "milestones and the order of work",
    "working": "the working doc of one ticket: context, steps, files likely touched, how to verify",
    "agents": "the AGENTS.md of the build: conventions, commands, how to test",
    "start": "the prompt a session that builds the project starts with",
    "note": "anything else worth keeping with the project",
    "memory": "facts and preferences that hold across projects, one line each",
    "grants": "what the person allows a tares-factory crew to do, in their words, dated",
}
# the order the console and list_docs show them in
KIND_ORDER = ["start", "spec", "plan", "agents", "memory", "grants", "note", "working"]

# The cell's global docs: made once the first project has docs, included in every project that
# has docs, and grown a line at a time by sessions (append_line). {kind: (title, starting body)}
GLOBAL_DOCS = {
    "agents": ("AGENTS.md (all projects)",
               "# AGENTS.md for every project\n\n"
               "Rules every session follows in every project on this Tares. One line each; a "
               "project's own AGENTS.md adds to these.\n\n"),
    "memory": ("Memory (all projects)",
               "# Memory\n\n"
               "Facts and preferences that hold across projects. One line each, newest last.\n\n"),
}
# The grants for every project of a tares-factory crew (TR-419): on the cell, made with the first
# grant, and included in the projects that read their grants (not in every project, like the two
# above: only a crew-built project has grants).
GRANTS_DOC = ("Grants (all projects)",
              "# Grants\n\n"
              "What the person allows a tares-factory crew to do in every project, in their own "
              "words with the day they said it. Only the person's words become a grant.\n\n")
MAX_LINE = 500


def append_line(body: str, text) -> tuple[str, bool]:
    """(the body with `- text` added at the end, whether it changed). A line already there is
    not added twice."""
    line = " ".join(str(text or "").split())
    if not line:
        raise DocError("say what to add: one line")
    if len(line) > MAX_LINE:
        raise DocError(f"the line is {len(line)} characters; keep it to {MAX_LINE}")
    entry = f"- {line.lstrip('- ').strip()}"
    if entry in body.splitlines():
        return body, False
    sep = "" if not body or body.endswith("\n") else "\n"
    return f"{body}{sep}{entry}\n", True

TICKET_STATUSES = ("todo", "in_progress", "done", "canceled")
TICKET_OWNERS = ("tares", "linear")

MAX_DOC_BYTES = 256 * 1024
MAX_TITLE = 200


class DocError(ValueError):
    """Something the caller sent is not a valid doc or ticket; the message says what to fix."""


def title_ok(title) -> str:
    t = " ".join(str(title or "").split())
    if not t:
        raise DocError("a title is required")
    if len(t) > MAX_TITLE:
        raise DocError(f"the title is {len(t)} characters; keep it to {MAX_TITLE}")
    return t


def validate_doc(kind, title, body) -> tuple[str, str, str]:
    """(kind, title, body), cleaned, or DocError."""
    kind = str(kind or "").strip().lower()
    if kind not in DOC_KINDS:
        raise DocError(f"unknown kind {kind!r}; one of: " + ", ".join(KIND_ORDER))
    body = "" if body is None else str(body)
    size = len(body.encode("utf-8"))
    if size > MAX_DOC_BYTES:
        raise DocError(f"the doc is {size // 1024} KB; keep it under {MAX_DOC_BYTES // 1024} KB")
    return kind, title_ok(title), body


def status_ok(status) -> str:
    s = str(status or "todo").strip().lower().replace(" ", "_").replace("-", "_")
    if s not in TICKET_STATUSES:
        raise DocError(f"unknown status {status!r}; one of: " + ", ".join(TICKET_STATUSES))
    return s


def sort_docs(docs: list[dict]) -> list[dict]:
    rank = {k: i for i, k in enumerate(KIND_ORDER)}
    return sorted(docs, key=lambda d: (rank.get(d["kind"], 99), d["title"].lower()))

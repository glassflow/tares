"""The summary of an accepted milestone (TR-1645): the figures it is written from, and writing it
once in the background, so a model call never holds a request or the event loop.

The text itself, its number check and its template are report_words.py. It is kept in the session
store (`report_texts`, kind `milestone`, keyed by the milestone's id)."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time

from . import factory as factory_mod
from . import factory_waiting
from . import loop as loop_mod
from . import report_words
from .envelope import now_utc

log = logging.getLogger("tares.milestone_summary")

KIND = "milestone"

# how much of a large milestone goes into the figures; the counts are always the whole of it
MAX_DELIVERED = 40
MAX_DECISIONS = 20
MAX_LEFT_TICKETS = 20
MAX_TITLE = 160
MAX_TEXT = 300

# a page with many milestones accepted before this shipped asks for them all at once
MAX_AT_ONCE = 2
# a write that failed (the store, not the model) is not started again by a read before this
RETRY_S = 300

# the merge queue's own ask (merge_queue.py): a yes to it says nothing about the work
_MERGE_ASK = re.compile(r"^Merge PR #\d+ \(.*\) at [0-9a-f]{7}\?$", re.S)


def about_the_work(decision: dict, permissions: set) -> bool:
    """A decision the summary counts: not a command allowed to a station (`permissions`, the desk
    items that asked for one) and not a yes to a merge."""
    return (decision.get("desk_item") not in permissions
            and not _MERGE_ASK.match(str(decision.get("question") or "")))


def figures(store, uid: str, mid: str) -> dict | None:
    """What a milestone's summary is written from, or None when it is gone.

    delivered: its tickets that are merged, shipped or done. decisions: the project's decisions
    that name one of its tickets, newest first. left: the unfinished tickets of the milestones
    after it. Folder paths of the crew are hidden, as on the project page."""
    from .screens.factory_project import cleaner   # the screens import much of the daemon

    project = store.get_project(uid)
    milestones = store.list_milestones(uid)
    at = next((i for i, m in enumerate(milestones) if m["id"] == mid), None)
    if project is None or at is None:
        return None
    clean = cleaner(store.get_handover(uid))

    def say(text, most: int = MAX_TITLE) -> str:
        t = " ".join(str(clean(str(text or ""))).split())
        return t if len(t) <= most else t[:most - 3].rstrip() + "..."

    def ticket(t: dict) -> dict:
        return {"ref": factory_mod.label(t), "title": say(t.get("title"))}

    def name(m: dict) -> str:
        return say(factory_waiting.milestone_name(milestones, m["id"]))

    tickets = store.list_tickets(uid)
    mine = [t for t in tickets if t.get("milestone") == mid]
    delivered = [t for t in mine if factory_mod.delivered(t) and t.get("status") != "canceled"]

    labels = {factory_mod.label(t) for t in mine}
    permissions = {d["id"] for d in store.list_desk(None, uid) if d.get("command")}
    decisions = [d for d in store.list_decisions(uid)
                 if labels.intersection(d.get("tickets") or []) and about_the_work(d, permissions)]

    later, listed, open_count = [], 0, 0
    for m in milestones[at + 1:]:
        left = [t for t in tickets
                if t.get("milestone") == m["id"] and not factory_mod.finished(t)]
        if not left:
            continue
        shown = left[:max(0, MAX_LEFT_TICKETS - listed)]
        listed += len(shown)
        open_count += len(left)
        later.append({"name": name(m), "open_count": len(left),
                      "tickets": [ticket(t) for t in shown]})

    def decision(d: dict) -> dict:
        return {**({"ref": d["label"]} if d.get("label") else {}),
                "question": say(d.get("question"), MAX_TEXT),
                "answer": say(factory_mod.decision_words(d)["answer"], MAX_TEXT)}

    return {
        "project": say(project.get("name")),
        "milestone": {"name": name(milestones[at]),
                      "goal": say(milestones[at].get("goal"), MAX_TEXT)},
        "delivered": {"count": len(delivered),
                      "tickets": [ticket(t) for t in delivered[:MAX_DELIVERED]]},
        "decisions": {"count": len(decisions),
                      "items": [decision(d) for d in decisions[:MAX_DECISIONS]]},
        "left": {"count": open_count, "milestone_count": len(later), "later_milestones": later},
    }


class Summaries:
    """The summaries of one cell. At most one write per milestone is under way at a time, and
    MAX_AT_ONCE in all; whoever asks meanwhile is told it is being written."""

    def __init__(self, store, record_usage=None):
        self.store = store
        self.record_usage = record_usage
        self._lock = threading.Lock()
        self._writing: dict[str, list] = {}   # milestone id -> [its task]
        self._failed: dict[str, float] = {}   # milestone id -> when its last write failed
        self._turns = asyncio.Semaphore(MAX_AT_ONCE)

    def writing(self, mid: str) -> bool:
        with self._lock:
            return mid in self._writing

    def queue(self, uid: str, mid: str, replace: bool = False) -> bool:
        """Write the summary in the background, unless it is being written already; without
        `replace`, a summary that is there stays. True when a write is under way. Never raises:
        the acceptance or the read that asks carries on."""
        with self._lock:
            if mid in self._writing:
                return True
            task = self._writing[mid] = []
        try:
            task.append(loop_mod.spawn(self._write(uid, mid, replace)))
        except Exception:  # noqa: BLE001
            log.exception("could not start writing the summary of milestone %s", mid)
            self._done(mid)
            return False
        return True

    def _done(self, mid: str) -> None:
        with self._lock:
            self._writing.pop(mid, None)

    async def _write(self, uid: str, mid: str, replace: bool) -> None:
        try:
            async with self._turns:
                # another write may have finished between the read that queued this one and now
                if not replace and await asyncio.to_thread(self.store.get_report_text, KIND, mid):
                    return
                facts = await asyncio.to_thread(figures, self.store, uid, mid)
                if facts is None:
                    return
                text, source = await report_words.figures_text(
                    KIND, facts, store=self.store, record_usage=self.record_usage)
                await asyncio.to_thread(self._keep, uid, mid, text, source, facts, replace)
            self._failed.pop(mid, None)
        except Exception:  # noqa: BLE001 -- a later read, or "Write again", starts it again
            self._failed[mid] = time.monotonic()
            log.exception("could not write the summary of milestone %s", mid)
        finally:
            self._done(mid)

    def _keep(self, uid: str, mid: str, text: str, source: str, facts: dict, replace: bool) -> None:
        if self.store.get_milestone(uid, mid) is not None:   # deleted while the model wrote
            self.store.put_report_text(KIND, mid, text, source, facts, now_utc(), replace)

    def answer(self, mid: str, row: dict | None, writing: bool) -> dict:
        """What the routes return."""
        summary = ({k: row[k] for k in ("text", "source", "figures", "written_at")}
                   if row else None)
        return {"milestone": mid, "summary": summary, "writing": writing}

    def read(self, uid: str, mid: str) -> dict:
        """The kept summary of an accepted milestone. One that has none yet (accepted before this
        shipped, or by its checks) is queued by this read."""
        # asked before the row: a write that ends between the two costs one more poll, never a
        # reader left with the old text
        writing = self.writing(mid)
        row = self.store.get_report_text(KIND, mid)
        failed = self._failed.get(mid)
        if not row and not writing and (failed is None or time.monotonic() - failed >= RETRY_S):
            writing = self.queue(uid, mid)
        return self.answer(mid, row, writing)

    def rewrite(self, uid: str, mid: str) -> dict:
        """Write it again from today's figures. The kept text is returned until the new one
        replaces it."""
        row = self.store.get_report_text(KIND, mid)
        return self.answer(mid, row, self.queue(uid, mid, replace=True))

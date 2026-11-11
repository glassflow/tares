"""The Tares reviewer as the built-in review gate (TR-852).

`merge_ready` asks the review slot for a verdict and does not need to know it is the Tares
reviewer. This class answers from what the crew already records: the review rounds of the ticket
(the reviewer's `record_review`) and, when the ticket has none, the `factory/review` status the
reviewer put on the pull request, as the ticket's PR snapshot holds it.

The gate needs the cell's store: `TaresReview(store)`. Without one (the settings page builds it
bare to test the connection) only `check_connection` works.

The ticket and the project come from the PR reference (`pr.ticket`, `pr.project_uid`). A ticket
is looked up by any ref the store knows (id, TR-12, T3).
"""
from __future__ import annotations

import asyncio

from ..review import ReviewGate
from ..types import PRRef, Verdict
from ... import factory as factory_mod

# the review record's verdict -> the gate's state; `block` stops a merge like `changes` does
_RECORD_STATE = {"pass": "approved", "changes": "changes", "block": "changes"}
# the factory/review status on the PR snapshot (see factory_github.read_pr) -> the gate's state
_STATUS_STATE = {"pass": "approved", "changes": "changes", "pending": "pending"}
_SUMMARY_MAX = 200


def _short(head: str) -> str:
    return str(head or "")[:7]


def _finding_text(f: dict) -> str:
    """One plain sentence for a finding: its text, then where and how serious it is."""
    text = str(f.get("text") or "").strip()
    where = str(f.get("file") or "").strip()
    if where and f.get("line"):
        where += f":{f['line']}"
    tags = ", ".join(x for x in (str(f.get("severity") or "").strip(), where) if x)
    return f"{text} ({tags})" if tags and text else text or tags


class TaresReview(ReviewGate):
    name = "tares"
    title = "Tares reviewer"
    ee = False
    settings_schema: list = []

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Built in"

    # ── the interface ─────────────────────────────────────────────────────────
    async def verdict(self, pr: PRRef) -> Verdict:
        ticket, last = await asyncio.to_thread(self._read, pr)
        head = str(getattr(pr, "head", "") or "").strip()
        url = str(getattr(pr, "url", "") or "") or str(((ticket or {}).get("pr") or {}).get("url") or "")
        if ticket is None or not head:
            return Verdict("none", head=head, url=url)
        if last is not None:
            return self._from_record(last, head, url)
        return self._from_status((ticket.get("pr") or {}), head, url)

    async def findings(self, pr: PRRef) -> list[str]:
        _, last = await asyncio.to_thread(self._read, pr)
        return self._texts(last)

    # ── from the store ────────────────────────────────────────────────────────
    def _read(self, pr: PRRef) -> tuple[dict | None, dict | None]:
        """(the ticket, its latest review round). (None, None) when the PR reference names no
        ticket of a project, or the project has no such ticket."""
        if self.store is None:
            raise RuntimeError("the Tares review gate needs the cell's store")
        uid = str(getattr(pr, "project_uid", "") or "").strip()
        ref = str(getattr(pr, "ticket", "") or "").strip()
        if not uid or not ref:
            return None, None
        ticket = self.store.get_ticket(uid, ref)
        if ticket is None:
            return None, None
        rounds = self.store.ticket_reviews(ticket["id"])
        return ticket, (rounds[-1] if rounds else None)

    @staticmethod
    def _texts(last: dict | None) -> list[str]:
        return [t for t in (_finding_text(f) for f in (last or {}).get("findings") or []) if t]

    def _from_record(self, last: dict, head: str, url: str) -> Verdict:
        mine = str(last.get("head") or "")
        if not factory_mod.same_commit(mine, head):
            return Verdict(
                "stale", head=mine, by=str(last.get("by") or ""), url=url,
                summary=f"The review is for an older commit ({_short(mine)}); "
                        f"the pull request is now at {_short(head)}",
                findings=self._texts(last))
        state = _RECORD_STATE.get(str(last.get("verdict") or ""), "none")
        return Verdict(state, head=mine, by=str(last.get("by") or ""), url=url,
                       summary=str(last.get("verified") or "").strip()[:_SUMMARY_MAX],
                       findings=self._texts(last))

    @staticmethod
    def _from_status(snapshot: dict, head: str, url: str) -> Verdict:
        """No review round: the review status on the PR, when it was set on the current head."""
        said = _STATUS_STATE.get(str(snapshot.get("verdict") or ""))
        if said and factory_mod.same_commit(snapshot.get("verdict_head"), head):
            return Verdict(said, head=head, url=url,
                           summary=str(snapshot.get("verdict_text") or "").strip()[:_SUMMARY_MAX])
        return Verdict("none", head=head, url=url)

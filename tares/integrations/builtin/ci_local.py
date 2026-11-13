"""Local checks as the ci integration (TR-854).

CI is green when every command the project requires was recorded as a check on the pull
request's head commit, and the session recording agrees with what the check claims. Nothing here
runs a command: the builder runs the checks and records them on the ticket (`add_check`), and
Tares matches each claim against what the builder's session printed (`factory.match_check`).

The commands come from `required` when the caller passes some, else from the project's required
checks (`set_required_checks`). With none the answer is `none`: a Tares project on local CI says
in this way that it has no CI.

A required command counts when a recorded check is for it (the same command, or the last step of
a chain: `cd x && pytest -q` is `pytest -q`) and names the head commit. For each command the
latest such check decides:

- it says it failed, or the recording shows the command failing or printing other counts: the
  command is failing, and the state is `failure` (listed in `failing`)
- the recording does not show the command at all: `pending` ("Not in the session recording yet").
  Ingest lag, a reassigned holder or the recording's window can leave a run that passed out of it
- no check on this head: `pending`. A check only on an older commit says "checks are for an older
  commit, run them again"
- the recording cannot be read (the ticket has no session): `pending`, since nothing backs the claim
- every command passed: `success`

The integration needs the cell's store: `LocalCI(store)`. Without one (the settings page builds it
bare to test the connection) only `check_connection` works. `source` is the name of the cell's
Claude Code source, where the sessions' commands are recorded; it is "claude_code" unless the
caller says otherwise: the daemon resolves its own name (`_cc_source()`), so whatever builds
`LocalCI` for a project passes that. A bad `required` (not a list of strings) raises ValueError.
"""
from __future__ import annotations

import asyncio

from ..ci import CISource
from ..types import CIState, PRRef
from ... import factory as factory_mod
from ...factory_store import FactoryStore

DEFAULT_SOURCE = "claude_code"
OLDER_COMMIT = "checks are for an older commit, run them again"
_CHECKS_READ = 500    # the checks of one ticket that are looked at, newest last


class LocalCI(CISource):
    name = "local"
    title = "Local checks"
    ee = False
    settings_schema: list = []

    def __init__(self, store=None, source: str | None = None):
        self.store = store
        self.source = source or DEFAULT_SOURCE

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Uses checks recorded on Tares"

    # ── the interface ─────────────────────────────────────────────────────────
    async def state(self, pr: PRRef, required: list[str]) -> CIState:
        if self.store is None:
            raise RuntimeError("the local checks integration needs the cell's store")
        return await asyncio.to_thread(self._state, pr, required)

    # ── from the store ────────────────────────────────────────────────────────
    def _state(self, pr: PRRef, required) -> CIState:
        uid = str(getattr(pr, "project_uid", "") or "").strip()
        ref = str(getattr(pr, "ticket", "") or "").strip()
        head = str(getattr(pr, "head", "") or "").strip()
        url = str(getattr(pr, "url", "") or "")
        wanted = FactoryStore.clean_commands(required) or (self.store.get_required_checks(uid) if uid else [])
        if not wanted:
            return CIState("none", head=head, url=url,
                           summary="This project requires no checks")
        ticket = self.store.get_ticket(uid, ref) if uid and ref else None
        if ticket is None:
            return CIState("pending", head=head, url=url,
                           summary="No checks are recorded: the pull request has no ticket")
        if not head:
            return CIState("pending", head=head, url=url,
                           summary="The pull request has no head commit yet")
        recorded = self.store.ticket_checks(ticket["id"], _CHECKS_READ)
        failing: list[str] = []
        missing: list[str] = []
        older = unbacked = False
        unseen: list[str] = []     # claimed, but not in the session recording (yet)
        commands = since = None
        read = False     # the session recording is read once, and only when a check needs it
        for cmd in wanted:
            mine = [c for c in recorded if factory_mod.same_command(cmd, c.get("command"))]
            here = [c for c in mine if factory_mod.same_commit(c.get("commit"), head)]
            if not here:
                missing.append(cmd)
                older = older or any(c.get("commit") for c in mine)
                continue
            latest = here[-1]      # oldest first, so the last is the newest
            if factory_mod.says_failed(latest.get("result")):
                failing.append(cmd)
                continue
            if not read:
                from ... import factory_api
                commands, since = factory_api.commands_for(self.store, uid, ticket, self.source)
                read = True
            if commands is None:
                unbacked = True
                continue
            match, _ = factory_mod.match_check(latest, commands, since)
            if match == "not_found":
                unseen.append(cmd)       # not proof of a failure: the recording may lag
            elif match != "matched":
                failing.append(cmd)
        if failing:
            return CIState("failure", head=head, failing=failing, url=url,
                           summary="Failed, or the session recording shows something else: "
                                   + ", ".join(failing))
        if missing or unbacked or unseen:
            parts = []
            if older:
                parts.append(OLDER_COMMIT)
            if missing and not older:
                parts.append("Not recorded on this commit yet: " + ", ".join(missing))
            if unbacked:
                parts.append("No session recording to compare the checks with")
            if unseen:
                parts.append("Not in the session recording yet: " + ", ".join(unseen))
            return CIState("pending", head=head, url=url, summary=". ".join(parts))
        return CIState("success", head=head, url=url)

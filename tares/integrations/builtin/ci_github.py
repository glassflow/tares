"""GitHub Actions as the ci integration (TR-853).

`state` answers from the pull request's check runs and commit statuses as `factory_github.read_pr`
reads them with the cell's own GitHub credentials, so it needs the cell's store:
`GithubCI(store)`. Without one (the settings page builds it bare to test the connection) only
`check_connection` works.

A commit with no checks at all is ambiguous. It is also what a commit looks like in the first
seconds after a push, so `none` alone never means ready. The integration asks GitHub whether the
repository has workflow files at that commit: with files, CI has not started yet (`pending`);
without, the repository has no CI (`none`). The setting "This repository has no CI" says it
outright.

How a check counts: still running is pending; success, neutral and skipped pass; cancelled,
timed out, action required, startup failure, stale and failure fail. The reviewer's own
`factory/review` status is never a check.
"""
from __future__ import annotations

import logging

from ..ci import CISource
from ..types import CIState, Field, PRRef
from ... import factory_github as fg

log = logging.getLogger("tares.integrations.ci_github")


class GithubCI(CISource):
    name = "github"
    title = "GitHub Actions"
    ee = False
    settings_schema = [Field(name="no_ci", label="This repository has no CI", kind="bool",
                             required=False)]

    def __init__(self, store=None):
        self.store = store

    async def check_connection(self, settings: dict) -> tuple[bool, str]:
        return True, "Uses this Tares's GitHub credentials"

    # ── the interface ─────────────────────────────────────────────────────────
    async def state(self, pr: PRRef, required: list[str]) -> CIState:
        if self.store is None:
            raise RuntimeError("the GitHub Actions integration needs the cell's store")
        repo = str(getattr(pr, "repo", "") or "").strip()
        number = getattr(pr, "number", None)
        if not repo or not number:
            raise ValueError("a pull request needs its repository and number to read its checks")
        url_given = str(getattr(pr, "url", "") or "")
        if self._no_ci(pr):
            return CIState("none", head=str(getattr(pr, "head", "") or ""), url=url_given)
        got = await fg.read_pr(self.store, repo, int(number))
        if got.get("error"):
            raise RuntimeError(str(got["error"]))
        head = str(got.get("head") or "")
        url = f"{got.get('url') or f'https://github.com/{repo}/pull/{number}'}/checks"
        checks = [c for c in got.get("ci_checks") or [] if isinstance(c, dict)]
        if not checks:
            has = await fg.has_workflows(self.store, repo, head)
            # no workflow files: no CI. Files, or no answer: CI has not started, so wait.
            return CIState("none" if has is False else "pending", head=head, url=url)
        names = [n for n in (str(x or "").strip() for x in required or []) if n]
        missing = False
        if names:
            checks = [c for c in checks if c.get("name") in names]
            have = {c.get("name") for c in checks}
            missing = any(n not in have for n in names)
        failing = [str(c.get("name") or "") for c in checks if c.get("state") == "failure"]
        if failing:
            return CIState("failure", head=head, failing=failing, url=url)
        if missing or any(c.get("state") == "pending" for c in checks):
            return CIState("pending", head=head, url=url)
        return CIState("success", head=head, url=url)

    # ── settings ──────────────────────────────────────────────────────────────
    def _no_ci(self, pr: PRRef) -> bool:
        """The integration settings in force for the pull request's project say the repository
        has no CI. A store that cannot answer means no."""
        from ..resolve import effective
        try:
            eff = effective("ci", str(getattr(pr, "project_uid", "") or "") or None,
                            store=self.store)
        except Exception:
            log.warning("could not read the ci integration settings", exc_info=True)
            return False
        return eff.integration == self.name and bool((eff.settings or {}).get("no_ci"))

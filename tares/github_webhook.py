"""GitHub App webhook deliveries (Tares 2.0, TR-1807): every event of a GitHub App installation,
delivered by the App's webhook to /ingest/<key>. The ci_github integration owns them; this module
reads no source catalog. (Moved from tares/connectors/github_app.py, which re-exports it until the
connectors go.)

What happens to a delivery, besides the platform's ingest tail the route still runs (TR-1811 removes it):
- a `pull_request` event is kept in `pr_events` (repo, number, branch, action, when), 30 days, which
  the factory reads to link a ticket's pull request by its `factory/<ref>` branch;
- every event is handed to the subscribers of `HUB` (the brain sync takes pushes there).

The rest of this docstring is the connector's, kept as it was:

One source per App credential, created with the credential (the "Create GitHub App" flow, or
Connect GitHub on Tares Cloud), never added by hand. All repositories of all the App's
installations arrive here: pull requests, pushes, reviews, comments, issues, releases, workflow
runs. Projects use the one source and narrow it with trigger filters (`repo`, `event_type`,
`action`), so nothing is configured per repository.

Deliveries are signature-checked before they reach this connector (`X-Hub-Signature-256` with the
App's webhook secret; see webhook_verify.py), which is what lets GitHub post here without a Tares
key. The event name and delivery id only travel in headers, so this connector takes them
(`WANTS_HEADERS`). They are stamped into the stored payload (`_github_event`, `_github_delivery`) so
a relabel from stored payloads reproduces the labels.

Lifecycle deliveries update the credential instead of the timeline: `installation` (created,
deleted, suspend), `installation_repositories` (repo list changed), `ping`. A delivery for an
installation the credential does not know is dropped and counted, never stored.

Events follow the GitHub event contract in github_events.py, shared with the polling connector.
"""
from __future__ import annotations

import time
from collections import OrderedDict

from .envelope import Envelope
from . import github_events as gh

DEDUPE_SECONDS = 24 * 3600
DEDUPE_MAX = 10_000

# per source: delivery id -> when seen (GitHub redelivers on failure and on a manual redeliver)
_seen: dict[str, OrderedDict] = {}
# per source: what the console shows next to the credential ("last delivery 2 min ago")
STATS: dict[str, dict] = {}


def _stats(source: str) -> dict:
    return STATS.setdefault(source, {"deliveries": 0, "stored": 0, "duplicates": 0,
                                     "unknown_installation": 0, "last_at": None,
                                     "last_event": None})


class Hub:
    """Who hears of every GitHub App event the cell takes: callables (source, envelopes), the same
    shape core's runtime listeners had, so the brain sync moved over unchanged. A subscriber must
    return at once and never raise into the delivery."""

    def __init__(self):
        self.subscribers: list = []

    def subscribe(self, fn) -> None:
        if fn not in self.subscribers:
            self.subscribers.append(fn)

    def unsubscribe(self, fn) -> None:
        if fn in self.subscribers:
            self.subscribers.remove(fn)

    def tell(self, source: str, envelopes: list) -> None:
        for fn in list(self.subscribers):
            try:
                fn(source, envelopes)
            except Exception as e:   # noqa: BLE001  (a subscriber must not fail the delivery)
                print(f"taresd: a GitHub event subscriber failed: {type(e).__name__}")


HUB = Hub()

PR_EVENTS_DAYS = 30


def pr_rows(envelopes: list) -> list[dict]:
    """The `pr_events` rows of a batch: one per pull_request event that names its repo, number and
    head branch. The delivery id keys the row (a redelivery adds nothing); without one, the event's
    own key and time do."""
    out = []
    for e in envelopes or ():
        if getattr(e, "event_type", "") != "pull_request":
            continue
        lab = e.labels or {}
        p = e.payload if isinstance(e.payload, dict) else {}
        try:
            number = int(lab.get("number"))
        except (TypeError, ValueError):
            continue
        repo, branch = lab.get("repo"), lab.get("branch")
        if not repo or not branch:
            continue
        did = p.get("_github_delivery") or f"{repo}#{number}@{e.event_time.isoformat()}"
        out.append({"delivery_id": str(did), "repo": str(repo), "number": number, "branch": str(branch),
                    "action": str(lab.get("action") or p.get("action") or ""), "at": e.event_time})
    return out


class GithubAppDeliveries:
    """Maps one App credential's webhook deliveries. `source` names them (the 1.x catalog source
    name, `github`, until the catalog goes); `credential` is the App credential's name."""
    WANTS_HEADERS = True
    PROVIDES = gh.CONTRACT_LABELS

    def __init__(self, store, source: str, credential: str):
        self.store = store
        self.source = source
        self.source_type = "event_stream"
        self.credential = credential

    # ── the credential behind these deliveries ──
    def _cred(self) -> dict | None:
        name = self.credential
        return self.store.get_github_credential(name) if name else None

    def signature(self) -> tuple[str, str] | None:
        """(scheme, secret) every delivery must be signed with. A missing secret is returned as
        an empty string, which refuses every delivery: an App source never takes unsigned posts."""
        cred = self._cred() or {}
        return "github", str((cred.get("config") or {}).get("webhook_secret") or "")

    @staticmethod
    def _may_add(cred: dict, iid, account: str) -> bool:
        from .github_app import may_add_installation
        return may_add_installation(cred, int(iid), account)

    def _known_installation(self, cred: dict, iid) -> bool:
        from .github_app import installations
        return any(i["id"] == int(iid) for i in installations(cred))

    def _lifecycle(self, cred: dict, event: str, p: dict) -> None:
        """installation / installation_repositories: keep the credential's installations true."""
        from .github_app import forget_repos, with_installation, without_installation
        inst = p.get("installation") or {}
        iid = inst.get("id")
        if not iid or cred.get("kind") != "app":
            if event == "installation_repositories" and iid:
                forget_repos(cred["name"], iid)
            return
        action = p.get("action") or ""
        if event == "installation":
            if action == "deleted":
                cfg = without_installation(cred, iid)
                print(f"[github_app {self.source}] installation {iid} removed (uninstalled)")
            else:
                acct = inst.get("account") or {}
                if not self._may_add(cred, iid, acct.get("login") or ""):
                    # a stranger installed a public App: every delivery is signed with the same
                    # secret, so the signature says nothing about who. Not ours unless it is the
                    # App owner's account or came through our install link (the callback).
                    _stats(self.source)["unknown_installation"] += 1
                    print(f"[github_app {self.source}] installation {iid} on "
                          f"{acct.get('login')!r} not added: not the App owner's account; "
                          "use Install / Add an organization in Settings > GitHub")
                    return
                row = {"id": int(iid), "account": acct.get("login") or "",
                       "account_type": acct.get("type") or "",
                       "repository_selection": inst.get("repository_selection") or "",
                       **({"suspended": True} if action == "suspend" else {})}
                cfg = with_installation(cred, row)
            self.store.update_github_credential_config(cred["name"], cfg)
            forget_repos(cred["name"], iid)
        elif event == "installation_repositories":
            forget_repos(cred["name"], iid)

    def map_payload(self, payload, headers: dict | None = None) -> list[Envelope]:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        event = headers.get("x-github-event", "")
        delivery = headers.get("x-github-delivery", "")
        st = _stats(self.source)
        st["deliveries"] += 1
        st["last_at"], st["last_event"] = time.time(), event or None
        if not isinstance(payload, dict) or not event or event == "ping":
            return []
        if delivery and self._seen(delivery):
            st["duplicates"] += 1
            return []
        cred = self._cred()
        if cred is None:
            raise ValueError(f"GitHub credential {self.credential!r} not found "
                             "(Settings > GitHub)")
        if event in ("installation", "installation_repositories", "github_app_authorization"):
            self._lifecycle(cred, event, payload)
            return []
        iid = (payload.get("installation") or {}).get("id")
        if iid and not self._known_installation(cred, iid):
            st["unknown_installation"] += 1
            print(f"[github_app {self.source}] delivery for unknown installation {iid} dropped")
            return []
        stored = {**payload, "_github_event": event, **({"_github_delivery": delivery}
                                                        if delivery else {})}
        env = self._envelope(stored)
        if env is None:
            return []
        if delivery:
            # remembered only now: a delivery dropped above (an installation not known yet, a
            # missing credential) is taken when GitHub redelivers it
            _seen.setdefault(self.source, OrderedDict())[delivery] = time.time()
        st["stored"] += 1
        return [env]

    def _seen(self, delivery: str) -> bool:
        seen = _seen.setdefault(self.source, OrderedDict())
        now = time.time()
        while seen and (now - next(iter(seen.values())) > DEDUPE_SECONDS or len(seen) > DEDUPE_MAX):
            seen.popitem(last=False)
        return delivery in seen

    def label_context(self, payload: dict | None) -> dict:
        p = payload or {}
        built = gh.build(p.get("_github_event") or "", p)
        return (built or {}).get("labels", {})

    def labels_for(self, context: dict | None = None) -> dict:
        return dict(context or {})   # the contract's labels (no catalog labels in 2.0)

    def _primary_label(self):
        return None

    def _envelope(self, stored: dict) -> Envelope | None:
        built = gh.build(stored["_github_event"], stored)
        if built is None:
            return None
        labels = self.labels_for(built["labels"])
        key = built["key"]
        if self._primary_label():          # a person who marked a primary label keys by it
            key = labels.get(self._primary_label()) or key
        return Envelope(source=self.source, source_type=self.source_type, key_value=key,
                        event_type=built["event_type"], text=built["text"],
                        event_time=built["event_time"], payload=stored, labels=labels)

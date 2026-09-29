"""Reference connector — documents attached to entities, always surfaced (never time-windowed).

Unlike the event connectors, this is DECLARATIVE reference material: files (json / csv / md / txt)
keyed to an entity by their labels, so an agent correlating on that entity always gets them
regardless of any read time window (see store.read_window's `source_type = 'reference'` clause).

The config *is* the data: each attachment carries its own labels and content. Editing the source
re-materializes it — the stored rows always mirror the current config (runtime._materialize), so
there's no poll loop and no poll interval. Labels are per-attachment (one file may be service=navflow,
another service=glassflow) and become real Tares labels: they ride on the event AND on the payload
(so the source's Fields view shows them), and the UI declares them in `config.labels` so triggers can
correlate on them. There's no entity key — correlation is label-native.
"""
from __future__ import annotations

from ..envelope import Envelope, now_utc
from .base import Connector


class ReferenceConnector(Connector):
    CONFIG_SCHEMA = {
        "attachments": {
            "type": "list", "required": True,
            "help": "reference documents; each carries its own entity labels",
            "item": {
                "name": {"type": "string", "required": True, "help": "file name / title"},
                "format": {"type": "string", "help": "json | csv | md | txt"},
                "content": {"type": "string", "required": True, "help": "the document's text"},
                "labels": {"type": "object", "help": 'entity labels, e.g. {"service": "navflow"}'},
            },
        },
    }

    async def poll(self):
        return []  # declarative — materialized on save, never polled

    def materialize(self) -> list[Envelope]:
        """One event per attachment, labelled with its own labels. The labels ride on both the event
        (for correlation/entity_counts) and the payload (so `label_context` surfaces them as fields).
        `key_value` is auto — the first label's value — purely so the Entities page reads nicely;
        reference data is correlated by label, not by key."""
        out = []
        for a in self.cfg.config.get("attachments", []):
            labels = {k: str(v) for k, v in (a.get("labels") or {}).items()}
            key = next(iter(labels.values()), "unknown")
            name = a.get("name") or "document"
            content = a.get("content") or ""
            fmt = a.get("format") or "txt"
            header = f"{name} ({fmt})"
            out.append(Envelope(
                source=self.cfg.name, source_type=self.cfg.type,
                key_value=key, event_type="reference",
                text=f"{header}\n{content[:280]}".strip() if content else header,
                event_time=now_utc(),
                payload={"name": name, "format": fmt, "content": content, "labels": labels},
                labels=labels,
            ))
        return out

    def label_context(self, payload: dict | None) -> dict:
        """Expose each document's labels as top-level fields (service, environment…) plus name/format,
        so the source's Fields view surfaces them and `extract_labels` profiles the declared labels.
        Content is omitted — it's the document body, not an entity axis."""
        p = payload or {}
        labels = p.get("labels") if isinstance(p.get("labels"), dict) else {}
        return {**(labels or {}), "name": p.get("name"), "format": p.get("format")}

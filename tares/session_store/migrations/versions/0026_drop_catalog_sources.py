"""The source catalog goes (Tares 2.0, TR-1811).

`catalog_sources` held the cell's sources. The one thing a factory cell still needs from it is the
webhook key of each GitHub App credential: before TR-1805 that key was the `ingest_key` of the App's
`github_app` source, and GitHub delivers to `/ingest/<that key>`. So, first, every `github_app`
source with an ingest key and a `credential` in its config gets an `integration_keys` row
(`ci_github`, the credential, the source's name as `source`), unless the credential already has a
key there or the key is taken. Then the table is dropped. Nothing else in it is read any more.

The columns are written out here, not imported from tables.py: a revision is a record of what ran.

Downgrade: makes the table again, empty. The sources are not brought back.

Revision ID: 0026_drop_catalog_sources
Revises: 0025_integration_keys_pr_events
"""
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0026_drop_catalog_sources"
down_revision = "0025_integration_keys_pr_events"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def _config(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def upgrade() -> None:
    conn = op.get_bind()
    have = set(sa.inspect(conn).get_table_names())
    if "catalog_sources" not in have:
        return
    if "integration_keys" in have:
        src = sa.table("catalog_sources", sa.column("name"), sa.column("connector"),
                       sa.column("config"), sa.column("ingest_key"))
        keys = sa.table("integration_keys", sa.column("key"), sa.column("integration"),
                        sa.column("credential"), sa.column("source"), sa.column("created_at"))
        rows = conn.execute(sa.select(src.c.name, src.c.config, src.c.ingest_key)
                            .where(src.c.connector == "github_app").order_by(src.c.name)).all()
        for name, raw, key in rows:
            cred = str(_config(raw).get("credential") or "").strip()
            if not key or not cred:
                continue
            taken = conn.execute(sa.select(keys.c.key).where(sa.or_(
                keys.c.key == key,
                sa.and_(keys.c.integration == "ci_github", keys.c.credential == cred)))).first()
            if taken is not None:
                continue
            conn.execute(keys.insert().values(key=key, integration="ci_github", credential=cred,
                                              source=name, created_at=datetime.now(timezone.utc)))
    op.drop_table("catalog_sources")


def downgrade() -> None:
    op.create_table(
        "catalog_sources",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("type", sa.Text),
        sa.Column("connector", sa.Text),
        sa.Column("poll", sa.Text),
        sa.Column("config", sa.Text),
        sa.Column("paused", sa.Boolean, server_default=sa.false()),
        sa.Column("created_at", _TS),
        sa.Column("updated_at", _TS),
        sa.Column("ingest_key", sa.Text),
        sa.Column("owned_by", sa.Text),
        sa.Column("customized", sa.Boolean))

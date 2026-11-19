"""Alembic environment for the session store.

SessionStore.upgrade() passes the connection it already holds (inside its one transaction, with the
advisory lock taken on Postgres) as config.attributes["connection"]. This file never opens a
connection of its own, so the migration runs in that transaction on SQLite and Postgres alike, and
in the schema the connection is on (version_table_schema=None).
"""
from alembic import context

config = context.config
connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("the session store migrates through SessionStore.upgrade(); "
                       "there is no offline or stand-alone mode")

context.configure(
    connection=connection,
    target_metadata=None,
    version_table="session_store_version",
    version_table_schema=None,
    transaction_per_migration=False,
)
with context.begin_transaction():
    context.run_migrations()

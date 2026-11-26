"""Code a revision calls, frozen as it was when that revision was written (TR-1700).

A revision is a record of what ran: an old database must get the same result from it whatever the
rest of Tares becomes. So a revision imports nothing from live Tares code. What more than one
revision needs, or what is too long to sit in a revision, is copied here.

Never change what a module here does. A later revision that needs other behaviour gets a new module.
This directory is beside `versions/`, so Alembic does not read these as revisions.
tests/test_revision_imports.py fails when a revision or a module here imports live code.
"""

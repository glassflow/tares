"""Seed a scratch factory Tares for factory-project-controls.e2e.cjs (TR-1079): one active factory project
led by the cell's owner, two other people on the cell (one a member), two tickets. Invented names only.
Writes straight to the cell's database, so no daemon runs here. Prints the project id on the last line.
usage: python seed-factory-controls.py <data-dir>"""
import os, sys

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(os.path.join(sys.argv[1], "tares.duckdb"))
uid = "uc_atlas"
store.create_project(uid, "custom", "Atlas", {}, goal="Publish the answers library on the docs site.")
store.set_project_kind(uid, "software_factory")
store.update_project(uid, status="active")
if not store.get_person("local"):
    store.create_person("You", None)
kim = store.create_person("Kim Lee", "kim@example.com")["id"]
store.create_person("Sam Roe", "sam@example.com")
store.set_project_lead(uid, "local")
store.add_project_member(uid, kim, "local")
store.create_ticket(uid, "tares", "Add the header")
store.create_ticket(uid, "tares", "Add the footer")
print(uid)

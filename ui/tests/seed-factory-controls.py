"""Seed a scratch factory Tares for factory-project-controls.e2e.cjs (TR-1079): one active factory project
led by the cell's owner, three other people on the cell (one a member, one to add), two tickets. Two more
projects led by one of them that the owner is not on, a team one (uc_borealis) and a private one (uc_cobalt),
for joining (TR-1720). Invented names only.
Writes straight to the cell's database, so no daemon runs here. Prints the first project's id on the last line.
usage: python seed-factory-controls.py <data-dir>"""
import os, sys

sys.path.insert(0, os.getcwd())
from tares.store import Store  # noqa: E402

store = Store(sys.argv[1])
uid = "uc_atlas"
store.create_project(uid, "custom", "Atlas", {}, goal="Publish the answers library on the docs site.")
store.set_project_kind(uid, "software_factory")
store.update_project(uid, status="active")
if not store.get_person("local"):
    store.create_person("You", None)
kim = store.create_person("Kim Lee", "kim@example.com")["id"]
sam = store.create_person("Sam Roe", "sam@example.com")["id"]
store.create_person("Ana Ortiz", "ana@example.com")
store.set_project_lead(uid, "local")
store.add_project_member(uid, kim, "local")
store.create_ticket(uid, "tares", "Add the header")
store.create_ticket(uid, "tares", "Add the footer")
for other, name, scope in (("uc_borealis", "Borealis", "team"), ("uc_cobalt", "Cobalt", "private")):
    store.create_project(other, "custom", name, {}, goal="Publish the search page.")
    store.set_project_kind(other, "software_factory")
    store.update_project(other, status="active")
    store.set_project_lead(other, sam)
    store.set_project_scope(other, scope)
print(uid)

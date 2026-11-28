"""One module per console screen. A screen ticket adds tares/screens/<name>.py with
register(app, store, h, ctx) and uncomments its own line below. Keep the blank lines:
they keep two tickets from editing adjacent lines."""
import importlib

SCREENS = [
    "factory_projects",      # TR-733

    "factory_project",         # TR-734

    "factory_tickets",         # TR-806

    "factory_ticket",          # TR-760

    "factory_reviews",       # TR-735

    "factory_crew",            # TR-736

    "factory_docs",            # TR-737

    "factory_settings",        # TR-738

    "start",                 # TR-756

    "factory_people",          # TR-1011

    "factory_merge_queue",   # TR-1094

    "factory_setup",           # TR-871
]


def register_all(app, store, h, ctx):
    for name in SCREENS:
        importlib.import_module(f"tares.screens.{name}").register(app, store, h, ctx)

"""One module per edition screen. A screen ticket adds tares/screens/<name>.py with
register(app, store, h, ctx) and uncomments its own line below. Keep the blank lines:
they keep two tickets from editing adjacent lines."""
import importlib

SCREENS = [
    # "factory_projects",      # TR-733

    # "factory_project",       # TR-734

    # "factory_ticket",        # TR-760

    # "factory_reviews",       # TR-735

    # "factory_crew",          # TR-736

    # "factory_docs",          # TR-737

    # "factory_settings",      # TR-738

    # "start",                 # TR-756

    # "sre_home_actions",      # TR-748

    # "sre_services",          # TR-750

    # "sre_incidents",         # TR-751

    # "sre_timeline",          # TR-752

    # "sre_runbooks",          # TR-753

    # "sre_settings",          # TR-764
]


def register_all(app, store, h, ctx):
    for name in SCREENS:
        importlib.import_module(f"tares.screens.{name}").register(app, store, h, ctx)

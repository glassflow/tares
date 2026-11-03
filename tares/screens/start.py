"""POST and DELETE /api/home/start/hide (TR-756): hide the Start checklist, and show it again.

On the factory home each credential hides only its own checklist (the setting
`start_hidden:<its name>`; one cell with no auth has one person, "console"); on the AI SRE home
each person does (`start_hidden:<person id>`, the key's name for a key with no person), so a
teammate still sees theirs. Any signed-in credential may do it, a read key too: it changes nothing but what the caller's own
home shows (tares/daemon.py lets this path through as "any"). The answer is the caller's new start block, or
{"ok": true} on a cell whose edition has no Start block.
"""
import asyncio

from fastapi import Request

from .. import edition as edition_mod
from .. import factory_home, sre_home, start_steps

PATH = "/api/home/start/hide"


def register(app, store, h, ctx):
    def change(request: Request, hide: bool):
        value = edition_mod.edition(store)[0]
        # the same key the edition's home reads. The factory home: the credential's own name,
        # "console" with no auth. The AI SRE home: the person's id (a rename keeps the hide), else
        # the key's own name, "console" with nobody to name
        cred = (getattr(request.state, "credential", None) or {}).get("name") or "console"
        who = h.start_who(request) if value == "sre" else cred
        (start_steps.hide if hide else start_steps.unhide)(store, who)
        factory_home.drop_cache(store, request)
        if value == "sre":
            sre_home.clear_cache()
            return sre_home.start_block(store, ctx.runtime.catalog, who)
        if value != "factory":
            return {"ok": True}
        return factory_home.build(store, ctx.fx, request)["start"]

    @app.post(PATH)
    async def hide_start(request: Request):
        return await asyncio.to_thread(change, request, True)

    @app.delete(PATH)
    async def show_start(request: Request):
        return await asyncio.to_thread(change, request, False)

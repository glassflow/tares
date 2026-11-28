"""POST and DELETE /api/home/start/hide (TR-756): hide the Start checklist, and show it again.

On the factory home each credential hides only its own checklist (the setting
`start_hidden:<its name>`; one cell with no auth has one person, "console"). Any signed-in
credential may do it, a read key too: it changes nothing but what the caller's own home shows (tares/daemon.py lets this path through as "any"). The answer is the caller's new start block.
"""
import asyncio

from fastapi import Request

from .. import factory_home, start_steps
from ..deps import Deps

PATH = "/api/home/start/hide"


def register(app, h: Deps):
    store = h.store
    def change(request: Request, hide: bool):
        # the same key the factory home reads: the credential's own name, "console" with no auth
        who = (getattr(request.state, "credential", None) or {}).get("name") or "console"
        (start_steps.hide if hide else start_steps.unhide)(store, who)
        factory_home.drop_cache(store, request)
        return factory_home.build(store, h.fx, request)["start"]

    @app.post(PATH)
    async def hide_start(request: Request):
        return await asyncio.to_thread(change, request, True)

    @app.delete(PATH)
    async def show_start(request: Request):
        return await asyncio.to_thread(change, request, False)

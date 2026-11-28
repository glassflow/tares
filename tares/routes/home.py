"""The home screen's one read."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from .. import factory_home, request_memo
from ..deps import Deps


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store

    @r.get("/api/home")
    async def home(request: Request):
        """One read for the home screen."""
        # one page read: its repeated small reads run once (TR-1631)
        return await asyncio.to_thread(request_memo.run, factory_home.build, store, deps.fx, request)

    return r

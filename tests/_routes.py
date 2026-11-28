"""The app's routes in matching order, for the tests that read the route table (TR-1814).

make_app() includes one APIRouter per area. In newer FastAPI an included router is one entry of
app.routes and its routes stay on the router; in older FastAPI its routes are copied into
app.routes. flat_routes gives the same list either way."""


def flat_routes(routes):
    for r in routes:
        if hasattr(r, "original_router"):
            yield from flat_routes(r.original_router.routes)
        else:
            yield r

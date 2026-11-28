"""The daemon's routes, one APIRouter per area (TR-1814). Each module has `router(deps)`, which
make_app() includes in a fixed order: FastAPI answers with the first route that matches, so a
literal path goes before a parametrised one that would also match it, and the console's catch-all
(`console.router`) is included last."""

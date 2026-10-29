"""GET /api/home on an AI SRE cell. TR-748 fills this in."""


def build(store, catalog, health=None, now=None) -> dict:
    return {"edition": "sre"}

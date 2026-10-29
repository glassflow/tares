"""GET /api/home on a factory cell. TR-729 fills this in."""


def build(store, fx=None, request=None, now=None) -> dict:
    return {"edition": "factory"}

"""Small repeated reads kept for the length of one page read (TR-1631).

Home, Crew, Setup and Projects ask the same small questions many times while they build one answer
(who is on the cell, a setting, the list of projects), and on a cloud cell each is a round trip. A
route marked `page` keeps the answer of every Store method marked `kept` until it returns.

- Only a marked route has a memo. Nothing else does: not a write route, a stream, a long poll or a
  background task, so a reader that waits for a change always reads again.
- The first statement of the request that is not a read ends its memo for good: nothing kept before
  it is served again and nothing is kept after it. The statements are watched where they run (the
  session store's engine and the Postgres state connection), so a new write method is covered
  without being named here.
- Every caller gets its own copy, so one that changes what it was given changes no one else's.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import functools
import inspect
import re
from datetime import date, datetime
from decimal import Decimal

_MEMO: contextvars.ContextVar = contextvars.ContextVar("tares_request_memo", default=None)

# a statement that starts with one of these changes no row; anything else counts as a write
_READS = re.compile(r"\s*(select|begin|commit|rollback|savepoint|release|pragma|with)\b", re.I)
_CHANGES = re.compile(r"\b(insert|update|delete|merge)\b", re.I)    # inside a WITH
_UNCHANGING = (str, int, float, bool, bytes, type(None), datetime, date, Decimal)
_MISSING = object()


class _Memo:
    __slots__ = ("kept", "on")

    def __init__(self):
        self.kept: dict = {}
        self.on = True

    def off(self) -> None:
        # `on` first: a thread of this request that is about to read must not find a kept value
        self.on = False
        self.kept.clear()


@contextlib.contextmanager
def scope():
    """Keep the `kept` reads made inside until the block ends. A block inside another one shares
    its memo."""
    if _MEMO.get() is not None:
        yield
        return
    memo = _Memo()
    token = _MEMO.set(memo)
    try:
        yield
    finally:
        memo.off()       # a thread or task that copied this context must not go on using it
        _MEMO.reset(token)


def run(fn, *args, **kwargs):
    """`fn(*args, **kwargs)` as one page read."""
    with scope():
        return fn(*args, **kwargs)


def page(fn):
    """Mark a route function that reads one page. Not for an async one: FastAPI runs a plain one in
    a worker thread with no event loop, so it cannot start a task that outlives it."""
    @functools.wraps(fn)
    def route(*args, **kwargs):
        return run(fn, *args, **kwargs)
    # FastAPI reads the parameters off the wrapper; the oldest version allowed would look up
    # string annotations (`Request`) in this module, so hand it the ones already looked up
    route.__signature__ = inspect.signature(fn, eval_str=True)
    return route


def statement(sql: str) -> None:
    """Every statement run on a database a kept read comes from passes here."""
    memo = _MEMO.get()
    if memo is None or not memo.on:
        return
    head = _READS.match(sql)
    if head is None or (head.group(1).lower() == "with" and _CHANGES.search(sql)):
        memo.off()


class _Unkeyable(Exception):
    pass


def _frozen(v):
    if v is None or isinstance(v, (str, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return tuple(map(_frozen, v))
    if isinstance(v, (set, frozenset)):
        return frozenset(map(_frozen, v))
    # a generator, a view of a dict, an object: reading it to make a key could change the call
    raise _Unkeyable


def _own(v):
    """A copy a caller may change. Faster than deepcopy for rows of dicts and lists."""
    kind = type(v)
    if kind in _UNCHANGING:
        return v
    if kind is dict:
        return {k: _own(x) for k, x in v.items()}
    if kind is list:
        return [_own(x) for x in v]
    if kind is tuple:
        return tuple(map(_own, v))
    return copy.deepcopy(v)


def kept(method):
    """A Store method that only reads, from tables whose statements pass `statement`: inside a page
    read, the same arguments run it once."""
    name = method.__qualname__

    @functools.wraps(method)
    def read(self, *args, **kwargs):
        memo = _MEMO.get()
        if memo is None or not memo.on:
            return method(self, *args, **kwargs)
        try:
            key = (id(self), name, _frozen(args),
                   tuple((k, _frozen(v)) for k, v in sorted(kwargs.items())))
        except _Unkeyable:
            return method(self, *args, **kwargs)
        hit = memo.kept.get(key, _MISSING)
        if hit is not _MISSING:
            return _own(hit)
        value = method(self, *args, **kwargs)
        if memo.on:      # a write during the read ends the memo: keep nothing then
            memo.kept[key] = _own(value)
        return value
    return read


def kept_state(method):
    """`kept` for a read of the state tables that are still reached through `store.sc`. Their
    writes are seen only on the Postgres state connection (`watched`), so on a DuckDB state, where
    a read costs no round trip, the method runs every time."""
    memoized = kept(method)

    @functools.wraps(method)
    def read(self, *args, **kwargs):
        run = memoized if getattr(self.sc, "watched", False) else method
        return run(self, *args, **kwargs)
    return read

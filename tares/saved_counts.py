"""Entity counts kept in memory for the reads that leave rows out.

The maintained entity_counts table has no project, so a read with a filter (store._exclude_sql)
cannot use it. The first such read counts the events; what it counted is kept here under the exact
filter, and each appended batch is run through that same filter SQL and added to it. A rewrite of a
source's rows drops the counts that read it. Nothing is kept across a restart, and a change to what
the filter leaves out makes new SQL or parameters, so a new key.

A filter is (sql, params); a spot under it is (the sources read as a frozenset, or None for all,
and the label). The caller holds the store's lock for every method."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime

_FILTERS = 16            # filters kept; the one read longest ago goes first
_SPOTS = 128             # counts kept under one filter
_VALUES = 200_000        # values kept over all of them
_PARAMS = 10_000         # bound parameters over the kept filters, what each appended batch pays for
_CHUNK = 2000            # appended rows run through the filters per statement

# the events columns, in the order of an appended row
_COLUMNS = (("source", "VARCHAR"), ("source_type", "VARCHAR"), ("key_value", "VARCHAR"),
            ("event_type", "VARCHAR"), ("text", "VARCHAR"), ("payload", "JSON"), ("labels", "JSON"),
            ("event_time", "TIMESTAMPTZ"), ("ingest_time", "TIMESTAMPTZ"))
_NEW_ROWS = ("WITH new_rows AS (SELECT "
             + ", ".join(f"unnest(CAST(? AS {kind}[])) AS {name}" for name, kind in _COLUMNS) + ")")

UNSAVED = object()       # SavedCounts.get: nothing is known about this spot yet


def _naive(time) -> bool:
    return isinstance(time, datetime) and time.tzinfo is None


class SavedCounts:
    def __init__(self):
        # filter -> spot -> {value: [events, last_ingest]}, or None: counted live on every read
        self._filters: OrderedDict = OrderedDict()

    def get(self, filt: tuple, spot: tuple):
        """The spot's counts, None when it is to be counted live every time, or UNSAVED."""
        spots = self._filters.get(filt)
        if spots is None or spot not in spots:
            return UNSAVED
        self._filters.move_to_end(filt)
        spots.move_to_end(spot)
        return spots[spot]

    def put(self, filt: tuple, spot: tuple, counts: dict | None) -> None:
        spots = self._filters.setdefault(filt, OrderedDict())
        spots[spot] = counts
        spots.move_to_end(spot)
        self._filters.move_to_end(filt)
        while len(spots) > _SPOTS:
            spots.popitem(last=False)
        self._trim()

    def _trim(self) -> None:
        while self._filters and (len(self._filters) > _FILTERS or sum(
                len(c) for spots in self._filters.values() for c in spots.values() if c) > _VALUES
                or (len(self._filters) > 1
                    and sum(len(params) for _, params in self._filters) > _PARAMS)):
            self._filters.popitem(last=False)

    def drop(self, source: str) -> None:
        """Forget every count that reads `source`: its rows are about to be rewritten."""
        for filt, spots in list(self._filters.items()):
            for spot in [s for s in spots if s[0] is None or source in s[0]]:
                del spots[spot]
            if not spots:
                del self._filters[filt]

    def clear(self) -> None:
        self._filters.clear()

    def appended(self, con, rows: list[tuple], label_expr, cap: int) -> None:
        """Add an appended batch (rows in _COLUMNS order) to the counts that read its sources: one
        statement over the batch alone, with each kept filter's own SQL as a column saying whether
        the row stays. A count that grows past `cap` values is counted live from then on."""
        sources = {r[0] for r in rows}
        wanted = [(filt, spot, counts) for filt, spots in self._filters.items()
                  for spot, counts in spots.items()
                  if counts is not None and (spot[0] is None or spot[0] & sources)]
        if not wanted:
            return
        filters = list(dict.fromkeys(filt for filt, _, _ in wanted))
        labels = list(dict.fromkeys(spot[1] for _, spot, _ in wanted))
        select = ", ".join(["source", "ingest_time", *(label_expr(name) for name in labels),
                            *(f"(TRUE{sql})" for sql, _ in filters)])
        params = [p for _, filter_params in filters for p in filter_params]
        # A list holding both naive and zoned times would not read each one as the insert does,
        # so the two kinds go in separate statements.
        batches: dict[tuple, list] = {}
        for r in rows:
            batches.setdefault((_naive(r[7]), _naive(r[8])), []).append(r)
        found = []
        for batch in batches.values():
            for i in range(0, len(batch), _CHUNK):
                columns = [list(column) for column in zip(*batch[i:i + _CHUNK])]
                found += con.execute(f"{_NEW_ROWS} SELECT {select} FROM new_rows",
                                     [*columns, *params]).fetchall()
        for filt, spot, counts in wanted:
            at = 2 + labels.index(spot[1])
            kept = 2 + len(labels) + filters.index(filt)
            for row in found:
                value = row[at]
                if (value is None or row[kept] is not True
                        or (spot[0] is not None and row[0] not in spot[0])):
                    continue
                held = counts.get(value)
                if held is None:
                    counts[value] = [1, row[1]]
                else:
                    held[0] += 1
                    if row[1] is not None and (held[1] is None or row[1] > held[1]):
                        held[1] = row[1]
            if len(counts) > cap:
                self._filters[filt][spot] = None
        self._trim()

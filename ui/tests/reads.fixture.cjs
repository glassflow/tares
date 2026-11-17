/*
 * Mocks for the Reads page (TR-1395): the read log (/api/activity/queries) with known rows, a failure,
 * a slow answer or nothing. Times are set relative to the moment each answer is sent, so "2 minutes ago"
 * holds however long the suite has run.
 */
const MIN = 60_000;
// Newest first, as the daemon sends them. r_ = a read, s_ = a stats call.
const ROWS = [
  { id: "r_1", scope: "(read)", key: "service=checkout", window: "1h", rows_returned: 42, client: "mcp", ago: 2 * MIN },
  { id: "s_2", scope: "(read)", key: "repo=glassflow/tares", window: "24h", rows_returned: 1200, client: "mcp", ago: 10 * MIN },
  { id: "r_3", scope: "(read)", key: "service=payments", window: "15m", rows_returned: 3, client: "http", ago: 60 * MIN },
  { id: "r_4", scope: "(read)", key: "Checkout", window: "7d", rows_returned: 0, client: "ui", ago: 180 * MIN },
  { id: "r_5", scope: "(read)", key: "team=infra", window: "PT1H", rows_returned: 5, client: "mcp", ago: 1440 * MIN },
];
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const stamp = (rows) => rows.map(({ ago, ...r }) => ({ ...r, queried_at: new Date(Date.now() - (ago ?? 0)).toISOString() }));

/**
 * mockReads(page, opts) → calls ({ reads }: how many times the log was asked)
 *   rows: an array (default ROWS), "fail" (500 until `failUntil` calls failed), or "slow" (waits 4 s)
 */
async function mockReads(page, { rows = ROWS, failUntil = Infinity } = {}) {
  const calls = { reads: 0 };
  await page.route((u) => u.pathname === "/api/activity/queries", async (route) => {
    calls.reads++;
    if (rows === "slow") { await new Promise((r) => setTimeout(r, 4000)); return json(route, 200, stamp(ROWS)); }
    if (rows === "fail") return calls.reads <= failUntil ? json(route, 500, { detail: "database is locked" }) : json(route, 200, stamp(ROWS));
    return json(route, 200, stamp(rows));
  });
  return calls;
}

module.exports = { mockReads, ROWS };

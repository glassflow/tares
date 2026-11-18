/*
 * The network around /sources/discover (TR-1394), shared by source-discover.e2e.cjs and
 * source-discover.shots.cjs. The daemon is real; the Docker scan and creating a connection are mocked at
 * the network, so the page sees a known machine. Every POST /api/sources lands in `calls` with its body.
 */
const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

const dockerLogs = (service, project, container, preselect) => ({
  connector: "docker_logs", name: `${service}_logs`, preselect, from: `container ${container}`,
  summary: `${service} container logs · key=service`,
  config: { container, labels: [{ name: "service", const: service, primary: true }, { name: "project", const: project }] },
});

// What scan_docker() answers for a small Compose project: two services, a Prometheus, a database and a
// dashboard. The summary and reasons are the daemon's own strings, "source" included.
const SCAN = {
  provider: "docker",
  summary: { containers: 5, proposed: 3 },
  containers: [],
  proposed_sources: [
    dockerLogs("web", "shop", "shop-web-1", true),
    dockerLogs("worker", "shop", "shop-worker-1", false),
    { connector: "prometheus", name: "metrics", preselect: true, from: "container shop-prometheus-1",
      summary: "1204 metrics available at http://localhost:9090 · key=service · starts with `up`; pick more metrics on the source page",
      config: { url: "http://localhost:9090", queries: [{ promql: "up" }] } },
  ],
  skipped: [
    { service: "db", image: "postgres:16", reason: "a Postgres CDC connector would go here (not built yet)" },
    { service: "grafana", image: "grafana/grafana:11.1.0", reason: "a dashboard; nothing to ingest" },
  ],
};
const EMPTY = { provider: "docker", summary: { containers: 0, proposed: 0 }, containers: [], proposed_sources: [], skipped: [] };
const DOCKER_DOWN = "`docker ps` failed; is the Docker daemon running? (Cannot connect to the Docker daemon at unix:///var/run/docker.sock)";

// opts: scan (the answer, a string to fail the scan with it, or an array of those, one per scan), failOnce (names whose first create fails),
// connectorsFail (the kinds list errors), calls (array to record creates), scans ({n} counts scans).
async function mockDiscover(page, { scan = SCAN, failOnce = [], connectorsFail = false, calls = [], scans = { n: 0 } } = {}) {
  const failing = new Set(failOnce);
  await page.route((u) => u.pathname === "/api/discover/environment", (route) => {
    // An array is one answer per scan, the last repeating, so a test can look again into a new machine.
    const answer = Array.isArray(scan) ? scan[Math.min(scans.n, scan.length - 1)] : scan;
    scans.n++;
    return typeof answer === "string" ? json(route, 500, { detail: answer }) : json(route, 200, answer);
  });
  if (connectorsFail) await page.route((u) => u.pathname === "/api/connectors", (route) => json(route, 500, { detail: "down" }));
  await page.route((u) => u.pathname === "/api/sources", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = JSON.parse(route.request().postData() || "{}");
    calls.push(body);
    if (failing.delete(body.name)) return json(route, 409, { detail: `a connection named ${body.name} already exists` });
    return json(route, 200, { ok: true, name: body.name });
  });
  return { calls, scans };
}

// The add button on the old page ("Create 2 selected sources") and the new one.
const addButton = (page) => page.getByTestId("discover-add").or(page.getByRole("button", { name: /^Create \d+ selected/ }));

module.exports = { DOCKER_DOWN, EMPTY, SCAN, addButton, mockDiscover };

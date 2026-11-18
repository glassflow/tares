/* Test-only clock hook for the browser suites whose cell answers by its own clock (sre-timeline, sre-services).
 *
 * The cell groups events into 10-minute windows and says "N minutes ago" from the moment it is asked, so an
 * answer can change between two reads when the clock crosses a mark. Run a suite with TARES_E2E_CROSS_BOUNDARY=1
 * to make that happen on purpose: crossBoundary waits for the next mark at the place the suite calls it, so a
 * suite that compares the page with an answer read earlier fails every time, not one run in three. Without the
 * variable it does nothing. */
const FORCE = !!process.env.TARES_E2E_CROSS_BOUNDARY;

// Wait until the wall clock passes the next multiple of `ms` (a minute: 60e3, ten minutes: 600e3).
async function crossBoundary(ms, label = "") {
  if (!FORCE) return false;
  const wait = ms - (Date.now() % ms) + 1500;
  console.log(`  .. crossing the next ${ms / 1000}s mark${label ? ` (${label})` : ""}: waiting ${Math.round(wait / 1000)}s`);
  await new Promise((r) => setTimeout(r, wait));
  return true;
}

// Wait until the next mark is at most `within` ms away, so a page that polls every 30 s is drawn and checked
// before its next poll while crossBoundary still has a mark to cross.
async function approachBoundary(ms, within, label = "") {
  if (!FORCE) return false;
  const wait = ms - (Date.now() % ms) - within;
  if (wait <= 0) return true;
  console.log(`  .. approaching the next ${ms / 1000}s mark${label ? ` (${label})` : ""}: waiting ${Math.round(wait / 1000)}s`);
  await new Promise((r) => setTimeout(r, wait));
  return true;
}

module.exports = { crossBoundary, approachBoundary, FORCE };

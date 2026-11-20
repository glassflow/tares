// Chromium for the e2e suites (TR-1431). Every suite takes `chromium` from here, not from playwright,
// so every page it opens asks for reduced motion: under the guard in paper-motion.css a transition or an
// animation ends at once, and no check reads a dialog half faded or a row still gliding. A suite that
// tests motion itself (motion.e2e.cjs) passes `reducedMotion: "no-preference"` and gets it back.
// tests/motion.check.cjs fails a suite that loads playwright on its own.
const path = require("path");

const { chromium: real } = require(path.join(__dirname, "..", "node_modules", "playwright"));
const QUIET = { reducedMotion: "reduce" };

function quiet(browser) {
  const newContext = browser.newContext.bind(browser);
  const newPage = browser.newPage.bind(browser);
  browser.newContext = (opts = {}) => newContext({ ...QUIET, ...opts });
  browser.newPage = (opts = {}) => newPage({ ...QUIET, ...opts });
  return browser;
}

const chromium = Object.create(real);
chromium.launch = async (opts) => quiet(await real.launch(opts));

module.exports = { chromium };

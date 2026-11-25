// Screenshots every scene in scenes.cjs against a scratch factory cell. Run from the repo root.
// Usage: PORT=8851 PROJECT=<seeded id> node scripts/marketing-screenshots/capture.cjs <outDir> [slugRegex]
const fs = require('fs')
const path = require('path')

const { chromium } = require(path.join(__dirname, '../../ui/tests/browser.cjs'))
const scenes = require('./scenes.cjs')

const OUT = process.argv[2]
const only = process.argv[3] ? new RegExp(process.argv[3]) : null
const BASE = `http://127.0.0.1:${process.env.PORT ?? 8851}`
const PROJECT = process.env.PROJECT ?? ''
// Text that must never reach a shot. A hit is printed and judged by eye.
const FLAGS =
  /sample|glassflow|kiran|vladimir|ashish|pablo|armend|petr|meryem|\/Users\/|Couldn.t|Not found|404|went wrong|error loading|localhost|127\.0\.0\.1/gi

// Real names and repos some samples carry, rewritten in the page before the shot. Nothing in src/ changes.
const MASK = [
  ['glassflow/', 'acme/'],
  ['Ashish Bagri', 'Sam Ortiz'],
  ['ashish-mbp', 'sam-mbp'],
  ['Ashish', 'Sam'],
  ['glassflow', 'acme'],
]

async function mask(page) {
  await page.evaluate((rules) => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT)
    let node
    while ((node = walker.nextNode())) {
      let text = node.nodeValue
      for (const [from, to] of rules) text = text.split(from).join(to)
      if (text !== node.nodeValue) node.nodeValue = text
    }
  }, MASK)
}

async function main() {
  const browser = await chromium.launch()
  fs.mkdirSync(OUT, { recursive: true })
  for (const [slug, route, , , opt = {}] of scenes) {
    if (only && !only.test(slug)) continue
    const theme = opt.dark ? 'dark' : 'light'
    const context = await browser.newContext({
      viewport: { width: 1600, height: 1000 },
      deviceScaleFactor: 2,
      colorScheme: theme,
    })
    await context.addInitScript(
      ([flag, t]) => {
        try {
          if (flag) localStorage.setItem('taresSamples', '1')
          localStorage.setItem('tares-theme', t)
        } catch (e) {
          /* none */
        }
      },
      [!!opt.sample, theme],
    )
    const page = await context.newPage()
    await page.goto(BASE + route.replace('{project}', PROJECT), { waitUntil: 'networkidle', timeout: 45000 })
    await page.waitForTimeout(1200)
    try {
      for (const name of opt.click ?? []) await page.getByRole('button', { name }).first().click({ timeout: 5000 })
      for (const text of opt.clickText ?? []) await page.getByText(text, { exact: false }).first().click({ timeout: 5000 })
      if (opt.scrollText) {
        await page.getByText(opt.scrollText, { exact: false }).first().evaluate((el) => el.scrollIntoView({ block: 'start' }))
      }
    } catch (error) {
      console.log('ACTION FAILED', slug, error.message.split('\n')[0].slice(0, 100))
    }
    await page.waitForTimeout(700)
    await mask(page)
    await page.screenshot({ path: path.join(OUT, `${slug}.png`), fullPage: !!opt.full })
    const text = await page.innerText('body').catch(() => '')
    const flags = [...new Set(text.match(FLAGS) ?? [])]
    console.log(slug.padEnd(24), flags.length ? `FLAG: ${flags.join(' | ')}` : 'ok')
    await context.close()
  }
  await browser.close()
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})

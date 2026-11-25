// Contact sheet for reviewing shots by eye. Run from the repo root.
// Usage: node scripts/marketing-screenshots/sheet.cjs out.png <columns> file...
const fs = require('fs')
const path = require('path')

const { chromium } = require(path.join(__dirname, '../../ui/tests/browser.cjs'))
const [out, columns, ...files] = process.argv.slice(2)

async function main() {
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1800, height: 600 } })
  const cells = files.map((file) => {
    const data = fs.readFileSync(file).toString('base64')
    const image = `<img style="width:100%;border:1px solid #888" src="data:image/png;base64,${data}">`
    const caption = `<figcaption style="font:bold 13px sans-serif">${path.basename(file)}</figcaption>`
    return `<figure style="margin:0">${image}${caption}</figure>`
  })
  const grid = `display:grid;grid-template-columns:repeat(${columns},1fr);gap:8px;padding:8px;background:#fff`
  await page.setContent(`<div style="${grid}">${cells.join('')}</div>`)
  await page.waitForTimeout(1500)
  await page.screenshot({ path: out, fullPage: true })
  await browser.close()
}

main()

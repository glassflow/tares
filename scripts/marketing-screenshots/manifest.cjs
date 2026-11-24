// Writes manifest.json for a library folder. Fails on a listed file that is missing or a PNG not listed.
// Usage: node scripts/marketing-screenshots/manifest.cjs <libDir>. Update the constants per capture.
const fs = require('fs')
const path = require('path')
const { execSync } = require('child_process')
const scenes = require('./scenes.cjs')

const LIB = process.argv[2]
const CAPTURED = new Date().toISOString().slice(0, 10)
const SHA = execSync('git rev-parse --short HEAD').toString().trim()
const RELEASED = 'tares v1.60.0'

const NOTES = {
  '02-home-team': 'Mixes the sample team list with the seeded Bakery orders project.',
  '07-ticket-review': '"Sample set of 10 questions" is the ticket\'s title in the story, not a label on the screen.',
  '12-crew-team': 'The person and machine names are rewritten to Sam Ortiz / sam-mbp at capture.',
  '15-brain': 'Repository owner rewritten to acme at capture.',
}

const images = scenes.map(([slug, , feature, shows, opt = {}]) => ({
  file: `${slug}.png`,
  shows,
  feature,
  data: opt.sample ? 'illustrative' : 'illustrative-seeded',
  dont_quote_figures: true,
  released: true,
  captured_from: `tares main ${SHA}, ${opt.sample ? `console sample "${opt.sample}"` : 'seeded demo project "Bakery orders"'}`,
  theme: opt.dark ? 'dark' : 'light',
  size: '3200x2000',
  ...(NOTES[slug] ? { note: NOTES[slug] } : {}),
}))

const missing = images.filter((image) => !fs.existsSync(path.join(LIB, image.file)))
if (missing.length) throw new Error(`missing: ${missing.map((image) => image.file)}`)
const listed = new Set(images.map((image) => image.file))
const unlisted = fs.readdirSync(LIB).filter((file) => file.endsWith('.png') && !listed.has(file))
if (unlisted.length) throw new Error(`not in the manifest: ${unlisted}`)

const manifest = { product: 'Tares software factory, by GlassFlow', captured: CAPTURED, released_version: RELEASED, images }
fs.writeFileSync(path.join(LIB, 'manifest.json'), `${JSON.stringify(manifest, null, 1)}\n`)
console.log(`${images.length} images`)

// End-to-end UI smoke test against `python -m atfi_app --dev-browser`.
//
// Exercises the real React UI + in-process backend + /__host bridge in a
// headless Chromium (no WebView2 needed): import a PDF through the host's
// file dialog stand-in, PDF bytes over /__host/pdf, pdf.js parsing, source
// detection, NER field extraction, approval, the Verification page and the
// Settings page. Screenshots land in tests/ui/out/.
//
//   uv run python -m atfi_app --dev-browser --no-open --port 8765 --token devtoken
//     (with ATFI_DEV_PICK_PDFS=<abs path to a PDF with a reference list>)
//   node tests/ui/dev-browser-smoke.mjs [http://127.0.0.1:8765/]
//
// Requires the `playwright` package and a Chromium build.

import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { chromium } = require('playwright')

const url = process.argv[2] ?? 'http://127.0.0.1:8765/'
const out = join(dirname(fileURLToPath(import.meta.url)), 'out')
mkdirSync(out, { recursive: true })

const failures = []
const check = (ok, message) => { if (!ok) failures.push(message) }

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1400, height: 860 } })
page.on('pageerror', (e) => failures.push(`page error: ${e.message}`))
page.on('response', (r) => { if (r.status() >= 400) failures.push(`HTTP ${r.status()} ${r.url()}`) })

await page.goto(url)
await page.waitForSelector('text=Ayrıştırma')
check(await page.evaluate(() => typeof window.__ATFI__?.token === 'string'), 'boot info not injected')

await page.click('text=+')
const pdfRow = page.getByText(/\.pdf$/i).first()
await pdfRow.waitFor({ timeout: 20000 })
await pdfRow.click()
await page.waitForTimeout(2500)
const detected = await page.evaluate(() => document.body.innerText.match(/\[1\]/g)?.length ?? 0)
check(detected > 0, 'no references detected')
await page.screenshot({ path: join(out, 'parsing.png') })

await page.click('text=Onayla')
await page.click('text=Doğrulama')
await page.waitForTimeout(1500)
await page.screenshot({ path: join(out, 'verification.png') })

await page.click('[aria-label="Ayarlar"], [title="Ayarlar"]')
await page.waitForTimeout(1000)
await page.screenshot({ path: join(out, 'settings.png') })
check(await page.getByText(/^v\d+\.\d+\.\d+/).first().isVisible(), 'settings page did not render the version')

await browser.close()
if (failures.length) {
  console.error('FAILED:\n' + failures.join('\n'))
  process.exit(1)
}
console.log(`UI smoke test passed; screenshots in ${out}`)

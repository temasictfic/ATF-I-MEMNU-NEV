// One-shot PNG optimizer for renderer assets.
//
// Run manually (or via `npm run optimize:assets`) whenever the source images
// in src/assets change. Overwrites the PNGs in place with
// resized, compressed versions. The originals are tracked in git so any
// run can be diffed and reverted.
//
// Targets:
//   - tubitak-yatay.png: rendered ~228×40 px in the header brand area.
//     Resize to 512 px wide so it still looks crisp on HiDPI.
//   - icon.png: rendered as a small status badge. Resize to 128x128.

import sharp from 'sharp'
import { statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const __dirname = dirname(fileURLToPath(import.meta.url))
const assetsDir = resolve(__dirname, '..', 'src', 'assets')

/** @type {{ file: string, width: number, height?: number }[]} */
const jobs = [
  { file: 'tubitak-yatay.png', width: 512 },
  { file: 'icon.png', width: 128, height: 128 },
]

const fmt = (n) => `${(n / 1024).toFixed(1)} KB`

for (const { file, width, height } of jobs) {
  const path = resolve(assetsDir, file)
  const before = statSync(path).size

  const optimized = await sharp(path)
    .resize({ width, height, fit: 'inside', withoutEnlargement: true })
    .png({ compressionLevel: 9, palette: true, quality: 90, effort: 10 })
    .toBuffer()

  await sharp(optimized).toFile(path)
  const after = statSync(path).size
  const saved = before - after
  const pct = ((saved / before) * 100).toFixed(1)
  console.log(`${file}: ${fmt(before)} → ${fmt(after)}  (-${fmt(saved)}, -${pct}%)`)
}

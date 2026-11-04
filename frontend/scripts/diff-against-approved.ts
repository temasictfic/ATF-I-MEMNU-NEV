// Detector vs approved-cache diff harness.
//
// For each PDF we have a parsed-PDF fixture for, this runs the current
// `detectSources()` and diffs the output against the user-approved cache at
// %APPDATA%/atfi-memnu-app/output/cache/<pdf>.json. The approved cache is
// the ground truth: text in those JSONs has been hand-verified by the user.
//
// Note: fixtures under test-fixtures/parsed-pdfs/ were captured from the old
// Python parser (pre-pdfjs migration), so cosmetic whitespace artefacts in
// these results may not reproduce at runtime — pdfjs's parser.ts already does
// gap-aware separator insertion and whitespace collapse. Treat residual
// whitespace diffs as fixture staleness, not detector bugs.
//
// Run:
//     npx tsx scripts/diff-against-approved.ts                # all fixtures
//     npx tsx scripts/diff-against-approved.ts 126E156 ...    # subset
//
// Exit code is 0 always — this script is a diagnostic, not a pass/fail gate.

import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { SourceRectangle } from '../src/lib/api/types'
import { detectSources } from '../src/lib/pdf/source-detector'
import type { ParsedPdf } from '../src/lib/pdf/types'

interface ApprovedCache {
  pdf_id: string
  numbered?: boolean
  approved?: boolean
  sources: SourceRectangle[]
}

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '..')
const FIXTURE_DIR = join(REPO_ROOT, 'test-fixtures', 'parsed-pdfs')

function approvedCachePath(pdfId: string): string {
  const appdata = process.env.APPDATA
  if (!appdata) throw new Error('APPDATA env var missing — Windows-only script')
  return join(appdata, 'atfi-memnu-app', 'output', 'cache', `${pdfId}.json`)
}

function snippet(s: string, around: number, span = 60): string {
  const start = Math.max(0, around - 30)
  const end = Math.min(s.length, around + span)
  return s.slice(start, end).replace(/\n/g, '⏎')
}

function diffOne(approvedSrc: SourceRectangle, detected: SourceRectangle): string | null {
  const a = approvedSrc.text.trim()
  const d = detected.text.trim()
  if (a === d) return null
  let i = 0
  while (i < Math.min(a.length, d.length) && a[i] === d[i]) i++
  const dir = a.length > d.length ? 'A>D' : a.length < d.length ? 'D>A' : '='
  return `${dir} A=${a.length} D=${d.length} diverge@${i}\n      A: …${snippet(a, i)}\n      D: …${snippet(d, i)}`
}

interface PdfDiffSummary {
  pdfId: string
  approvedCount: number
  detectedCount: number
  contentDiffs: number
  exampleDiffs: Array<{ ref: number | null; detail: string }>
  missingInDetector: number[]
  extraInDetector: number[]
}

function runFixture(pdfId: string): PdfDiffSummary | string {
  const fixturePath = join(FIXTURE_DIR, `${pdfId}.json`)
  if (!existsSync(fixturePath)) return `[skip] no fixture: ${fixturePath}`

  const parsed = JSON.parse(readFileSync(fixturePath, 'utf8')) as ParsedPdf
  const detection = detectSources(parsed)

  const cachePath = approvedCachePath(pdfId)
  if (!existsSync(cachePath)) return `[skip] no approved cache: ${cachePath}`
  const approved = JSON.parse(readFileSync(cachePath, 'utf8')) as ApprovedCache

  const byApproved = new Map<number, SourceRectangle>()
  for (const s of approved.sources) byApproved.set(s.ref_number ?? -1, s)
  const byDetected = new Map<number, SourceRectangle>()
  for (const s of detection.sources) byDetected.set(s.ref_number ?? -1, s)

  let contentDiffs = 0
  const exampleDiffs: Array<{ ref: number | null; detail: string }> = []
  const missingInDetector: number[] = []
  for (const [rn, a] of byApproved) {
    const d = byDetected.get(rn)
    if (!d) {
      missingInDetector.push(rn)
      continue
    }
    const detail = diffOne(a, d)
    if (detail) {
      contentDiffs += 1
      if (exampleDiffs.length < 5) exampleDiffs.push({ ref: rn, detail })
    }
  }
  const extraInDetector: number[] = []
  for (const rn of byDetected.keys()) {
    if (!byApproved.has(rn)) extraInDetector.push(rn)
  }

  return {
    pdfId,
    approvedCount: approved.sources.length,
    detectedCount: detection.sources.length,
    contentDiffs,
    exampleDiffs,
    missingInDetector,
    extraInDetector,
  }
}

function main(): void {
  const args = process.argv.slice(2)
  let pdfIds: string[]
  if (args.length > 0) {
    pdfIds = args
  } else {
    if (!existsSync(FIXTURE_DIR)) {
      console.error(`[diff] fixture dir not found: ${FIXTURE_DIR}`)
      process.exit(2)
    }
    pdfIds = readdirSync(FIXTURE_DIR)
      .filter(f => f.endsWith('.json'))
      .map(f => f.replace(/\.json$/, ''))
      .sort()
  }

  let totalContentDiffs = 0
  let countMismatchPdfs = 0
  for (const pdfId of pdfIds) {
    const result = runFixture(pdfId)
    if (typeof result === 'string') {
      console.log(result)
      continue
    }
    const countTag = result.approvedCount === result.detectedCount ? '=' : '!='
    console.log(
      `[${pdfId}] approved=${result.approvedCount} ${countTag} detected=${result.detectedCount}, contentDiffs=${result.contentDiffs}` +
        (result.missingInDetector.length ? `, missingRefs=${result.missingInDetector.join(',')}` : '') +
        (result.extraInDetector.length ? `, extraRefs=${result.extraInDetector.join(',')}` : '')
    )
    for (const ex of result.exampleDiffs) {
      console.log(`   ref ${ex.ref}: ${ex.detail}`)
    }
    totalContentDiffs += result.contentDiffs
    if (result.approvedCount !== result.detectedCount) countMismatchPdfs += 1
  }

  console.log(
    `\n[diff] summary: ${totalContentDiffs} total content diffs across ${pdfIds.length} PDFs; ${countMismatchPdfs} with count mismatch`
  )
}

main()

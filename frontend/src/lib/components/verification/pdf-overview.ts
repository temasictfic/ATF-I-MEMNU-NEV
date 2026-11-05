// Per-PDF summary of the verification store for the PDF list (left panel),
// the export buttons and the running-state checks.
//
// VerificationPage used to subscribe to the whole `resultsByPdf` map, so
// every WebSocket event (sources × databases per run) re-rendered the page,
// even for a PDF that wasn't on screen. `selectPdfOverview` returns one
// entry per PDF and keeps each entry's identity until one of its values
// changes. With `useShallow` the page then re-renders when a count or a
// running flag changes, not on every event.

import type { VerificationResult } from '../../api/types'
import { effectiveDecisionTag } from '../../verification/tagState'

export interface PdfOverview {
  /** At least one source of the PDF is `in_progress`. */
  running: boolean
  /** The PDF has any verification result (even an in-progress one). */
  hasResults: boolean
  // Display counts. For sources that are `in_progress`, the pre-rerun
  // snapshot (priorResultsByPdf) is counted if there is one, so the pills
  // update smoothly during a re-verify of an already (partly) verified PDF:
  // each completing source replaces its snapshot's contribution with its
  // new result, instead of dragging the totals to zero on optimistic
  // in_progress and climbing back up. First-time verifies (no snapshot)
  // count settled sources only.
  high: number
  medium: number
  low: number
  valid: number
  citation: number
  fabricated: number
}

type ResultMap = Record<string, VerificationResult>

interface CacheEntry {
  results: ResultMap
  prior: ResultMap | undefined
  overview: PdfOverview
}

const cache = new Map<string, CacheEntry>()

function computeOverview(pdfResults: ResultMap, priorMap: ResultMap): PdfOverview {
  let high = 0, medium = 0, low = 0
  let valid = 0, citation = 0, fabricated = 0
  let running = false
  let hasResults = false
  for (const [sourceId, r] of Object.entries(pdfResults)) {
    hasResults = true
    if (r.status === 'in_progress') running = true
    // Pick the "effective" result: settled current result wins,
    // otherwise the pre-rerun snapshot, otherwise nothing.
    let eff: VerificationResult | undefined
    if (r.status === 'high' || r.status === 'medium' || r.status === 'low') {
      eff = r
    } else if (r.status === 'in_progress' && priorMap[sourceId]) {
      eff = priorMap[sourceId]
    }
    if (!eff) continue
    if (eff.status === 'high') high++
    else if (eff.status === 'medium') medium++
    else if (eff.status === 'low') low++
    // Skip cancelled-with-no-data rows so the Fabricated pill doesn't
    // inflate to the full ref count after a Stop on a partially-completed
    // run. Older cache files can still carry these; a cancel now deletes
    // the entry, so this guard stays only for that data.
    if (
      eff.status === 'low'
      && !eff.best_match
      && (eff.databases_searched?.length ?? 0) === 0
    ) continue
    const tag = effectiveDecisionTag(eff)
    if (tag === 'valid') valid++
    else if (tag === 'citation') citation++
    else if (tag === 'fabricated') fabricated++
  }
  return { running, hasResults, high, medium, low, valid, citation, fabricated }
}

function sameOverview(a: PdfOverview, b: PdfOverview): boolean {
  return a.running === b.running
    && a.hasResults === b.hasResults
    && a.high === b.high
    && a.medium === b.medium
    && a.low === b.low
    && a.valid === b.valid
    && a.citation === b.citation
    && a.fabricated === b.fabricated
}

const EMPTY: ResultMap = {}

export function selectPdfOverview(state: {
  resultsByPdf: Record<string, ResultMap>
  priorResultsByPdf: Record<string, ResultMap>
}): Record<string, PdfOverview> {
  const out: Record<string, PdfOverview> = {}
  for (const [pdfId, results] of Object.entries(state.resultsByPdf)) {
    const prior = state.priorResultsByPdf[pdfId]
    const cached = cache.get(pdfId)
    if (cached && cached.results === results && cached.prior === prior) {
      out[pdfId] = cached.overview
      continue
    }
    const next = computeOverview(results ?? EMPTY, prior ?? EMPTY)
    const overview = cached && sameOverview(cached.overview, next) ? cached.overview : next
    cache.set(pdfId, { results, prior, overview })
    out[pdfId] = overview
  }
  return out
}

import { create } from 'zustand'
import type { SourceRectangle } from '../api/types'
import { api } from '../api/rest-client'
import { makeSourceId } from '../utils/source-id'
import { usePdfStore } from './pdf-store'

interface SourcesState {
  sourcesByPdf: Record<string, SourceRectangle[]>
  originalSourcesByPdf: Record<string, SourceRectangle[]>
  historyByPdf: Record<string, SourceRectangle[][]>
}

export const useSourcesStore = create<SourcesState>()(() => ({
  sourcesByPdf: {},
  originalSourcesByPdf: {},
  historyByPdf: {},
}))

function updateSourceCount(pdfId: string, count: number): void {
  usePdfStore.getState().updateSourceCount(pdfId, count)
}

/**
 * Demote an approved PDF back to "parsed" when the user edits its
 * rectangles. Unlike `unapproveSources` — which is triggered by the user
 * explicitly clicking the Approved toggle and therefore reloads the cached
 * backend state — this variant does NOT touch the in-memory source list,
 * because the caller has just modified it locally and we would otherwise
 * clobber the edit. Status-only change; the backend unapprove is best-
 * effort in the background.
 */
function autoUnapproveOnEdit(pdfId: string): void {
  const pdf = usePdfStore.getState().pdfs.find(p => p.id === pdfId)
  if (!pdf || pdf.status !== 'approved') return
  usePdfStore.getState().updatePdfStatus(pdfId, 'parsed')
  api.unapprovePdf(pdfId).catch(err => {
    console.warn(`[sources-store] backend unapprove failed for ${pdfId}:`, err)
  })
}

function pushHistory(pdfId: string): void {
  useSourcesStore.setState(state => {
    const history = state.historyByPdf[pdfId] ?? []
    const current = state.sourcesByPdf[pdfId] ?? []
    return {
      historyByPdf: { ...state.historyByPdf, [pdfId]: [...history, [...current]] },
    }
  })
}

// Sort by reading order, then assign content-addressed IDs (hash of text +
// pdfId) and a 1-based ref_number. Duplicate texts within one PDF get
// `_2`, `_3` … appended in reading order — deterministic. Used as the single
// numbering pass for both fresh detection (setSources) and edits
// (renumberSources), so the suffix format can't drift between paths.
function renumberAndRehash(
  pdfId: string,
  sources: SourceRectangle[],
  trackId?: string,
): { renumbered: SourceRectangle[]; trackedNewId?: string } {
  const sorted = [...sources].sort((a, b) => {
    if (a.bbox.page !== b.bbox.page) return a.bbox.page - b.bbox.page
    return a.bbox.y0 - b.bbox.y0
  })
  const seen = new Map<string, number>()
  let trackedNewId: string | undefined
  const renumbered = sorted.map((s, i) => {
    const base = makeSourceId(pdfId, s.text)
    const n = (seen.get(base) ?? 0) + 1
    seen.set(base, n)
    const id = n > 1 ? `${base}_${n}` : base
    if (trackId !== undefined && s.id === trackId) trackedNewId = id
    return { ...s, ref_number: i + 1, id }
  })
  return { renumbered, trackedNewId }
}

// If `trackId` is provided, returns that source's new id after rehashing
// so callers updating selection don't have to re-search the store.
function renumberSources(pdfId: string, trackId?: string): string | undefined {
  let trackedNewId: string | undefined
  useSourcesStore.setState(state => {
    const sources = state.sourcesByPdf[pdfId]
    if (!sources) return state
    const result = renumberAndRehash(pdfId, sources, trackId)
    trackedNewId = result.trackedNewId
    return { sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: result.renumbered } }
  })
  return trackedNewId
}

export function getSources(pdfId: string): SourceRectangle[] {
  return useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
}

export function setSources(pdfId: string, sources: SourceRectangle[]): void {
  const { renumbered } = renumberAndRehash(pdfId, sources)
  useSourcesStore.setState(state => ({
    sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: renumbered },
    originalSourcesByPdf: state.originalSourcesByPdf[pdfId] !== undefined
      ? state.originalSourcesByPdf
      : { ...state.originalSourcesByPdf, [pdfId]: [...renumbered] },
  }))
  updateSourceCount(pdfId, renumbered.length)
}

// Returns the rect's id after renumberSources rehashes it (content-addressed
// from text), so callers tracking the new source — e.g. for selection — can
// use the returned id without re-searching the store.
export function addRectangle(pdfId: string, rect: SourceRectangle): string {
  pushHistory(pdfId)
  let newLength = 0
  useSourcesStore.setState(state => {
    const next = [...(state.sourcesByPdf[pdfId] ?? []), rect]
    newLength = next.length
    return { sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: next } }
  })
  const newId = renumberSources(pdfId, rect.id) ?? rect.id
  updateSourceCount(pdfId, newLength)
  autoUnapproveOnEdit(pdfId)
  return newId
}

/**
 * Merge two source rectangles by id. Joins their text in reading order
 * (sorted by page then y0), unions bboxes per page, renumbers the list,
 * and returns the id of the resulting merged source.
 */
function mergeTwo(pdfId: string, aId: string, bId: string): string | null {
  const all = useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
  const a = all.find(s => s.id === aId)
  const b = all.find(s => s.id === bId)
  if (!a || !b || a.id === b.id) return null

  pushHistory(pdfId)

  // Sort the pair by (page, y0) so text is joined in reading order
  const ordered = [a, b].sort((x, y) => {
    if (x.bbox.page !== y.bbox.page) return x.bbox.page - y.bbox.page
    return x.bbox.y0 - y.bbox.y0
  })
  const first = ordered[0]
  const second = ordered[1]

  // Build the merged bbox list — group by page, union bboxes per page
  const byPage = new Map<number, { x0: number; y0: number; x1: number; y1: number }>()
  const collectBboxes = (s: SourceRectangle): void => {
    const allBboxes = s.bboxes && s.bboxes.length > 0 ? s.bboxes : [s.bbox]
    for (const bb of allBboxes) {
      const existing = byPage.get(bb.page)
      if (existing) {
        existing.x0 = Math.min(existing.x0, bb.x0)
        existing.y0 = Math.min(existing.y0, bb.y0)
        existing.x1 = Math.max(existing.x1, bb.x1)
        existing.y1 = Math.max(existing.y1, bb.y1)
      } else {
        byPage.set(bb.page, { x0: bb.x0, y0: bb.y0, x1: bb.x1, y1: bb.y1 })
      }
    }
  }
  collectBboxes(first)
  collectBboxes(second)

  const mergedBboxes = Array.from(byPage.entries())
    .sort(([p1], [p2]) => p1 - p2)
    .map(([page, bb]) => ({ ...bb, page }))

  const mergedText = `${first.text.trim()} ${second.text.trim()}`.trim()

  const merged: SourceRectangle = {
    ...first,
    bbox: mergedBboxes[0],
    bboxes: mergedBboxes.length > 1 ? mergedBboxes : [],
    text: mergedText,
    status: 'edited',
  }

  let newLength = 0
  useSourcesStore.setState(state => {
    const next = (state.sourcesByPdf[pdfId] ?? [])
      .filter(s => s.id !== first.id && s.id !== second.id)
      .concat(merged)
    newLength = next.length
    return { sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: next } }
  })
  // merged carries first.id (spread above), so renumberSources can track it
  // and return the post-rehash id directly — immune to text-duplication
  // suffixing (`_2`/`_3`) that a find-by-text recovery would mishandle.
  const newId = renumberSources(pdfId, first.id) ?? null
  updateSourceCount(pdfId, newLength)
  autoUnapproveOnEdit(pdfId)
  return newId
}

/**
 * Merge a source rectangle with its closest neighbor (by bbox center distance).
 * Same-page neighbors are preferred. Returns the id of the resulting merged source,
 * or null if there is no other source to merge with.
 */
export function mergeWithClosest(pdfId: string, sourceId: string): string | null {
  const all = useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
  const target = all.find(s => s.id === sourceId)
  if (!target || all.length < 2) return null

  const targetCx = (target.bbox.x0 + target.bbox.x1) / 2
  const targetCy = (target.bbox.y0 + target.bbox.y1) / 2

  let closest: SourceRectangle | null = null
  let closestDist = Infinity
  for (const s of all) {
    if (s.id === sourceId) continue
    const cx = (s.bbox.x0 + s.bbox.x1) / 2
    const cy = (s.bbox.y0 + s.bbox.y1) / 2
    // Penalize different pages heavily so same-page neighbors win
    const pagePenalty = s.bbox.page === target.bbox.page ? 0 : 100000
    const dist = Math.hypot(cx - targetCx, cy - targetCy) + pagePenalty
    if (dist < closestDist) {
      closestDist = dist
      closest = s
    }
  }
  if (!closest) return null

  return mergeTwo(pdfId, target.id, closest.id)
}

/**
 * Merge a source rectangle with the one immediately preceding it in reading
 * order (ref_number - 1). The merged result inherits the previous source's
 * number, so the selected box does not introduce a new source. Returns
 * the id of the resulting merged source, or null if there is no previous.
 */
export function mergeWithPrevious(pdfId: string, sourceId: string): string | null {
  const all = useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
  const target = all.find(s => s.id === sourceId)
  if (!target || target.ref_number == null) return null
  const prevNumber = target.ref_number - 1
  const prev = all.find(s => s.ref_number === prevNumber)
  if (!prev) return null
  return mergeTwo(pdfId, target.id, prev.id)
}


export function removeRectangle(pdfId: string, sourceId: string): void {
  pushHistory(pdfId)
  let newLength = 0
  useSourcesStore.setState(state => {
    const next = (state.sourcesByPdf[pdfId] ?? []).filter(s => s.id !== sourceId)
    newLength = next.length
    return { sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: next } }
  })
  renumberSources(pdfId)
  updateSourceCount(pdfId, newLength)
  autoUnapproveOnEdit(pdfId)
}

// Returns the source's id after the update. Text changes trigger
// renumberSources, which content-rehashes the id, so the input sourceId
// can be stale on return — callers tracking the source (e.g. selection)
// should use the returned id.
export function updateRectangle(pdfId: string, sourceId: string, updates: Partial<SourceRectangle>): string {
  pushHistory(pdfId)
  useSourcesStore.setState(state => ({
    sourcesByPdf: {
      ...state.sourcesByPdf,
      [pdfId]: (state.sourcesByPdf[pdfId] ?? []).map(s =>
        s.id === sourceId ? { ...s, ...updates, status: 'edited' as const } : s
      ),
    },
  }))
  // Text edits produce a new content-hash ID; let renumberSources rehash
  // and dedup. Bbox/status-only updates stay stable.
  let newId = sourceId
  if (updates.text !== undefined) {
    newId = renumberSources(pdfId, sourceId) ?? sourceId
  }
  autoUnapproveOnEdit(pdfId)
  return newId
}

export function beginEdit(pdfId: string): void {
  pushHistory(pdfId)
}

export function updateRectangleSilent(pdfId: string, sourceId: string, updates: Partial<SourceRectangle>): void {
  useSourcesStore.setState(state => ({
    sourcesByPdf: {
      ...state.sourcesByPdf,
      [pdfId]: (state.sourcesByPdf[pdfId] ?? []).map(s =>
        s.id === sourceId ? { ...s, ...updates, status: 'edited' as const } : s
      ),
    },
  }))
  autoUnapproveOnEdit(pdfId)
}

export function revert(pdfId: string): void {
  let newLength: number | null = null
  useSourcesStore.setState(state => {
    const history = state.historyByPdf[pdfId]
    if (!history || history.length === 0) return state
    const prev = history[history.length - 1]
    newLength = prev.length
    return {
      sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: prev },
      historyByPdf: { ...state.historyByPdf, [pdfId]: history.slice(0, -1) },
    }
  })
  if (newLength !== null) updateSourceCount(pdfId, newLength)
  autoUnapproveOnEdit(pdfId)
}

export function canRevert(pdfId: string): boolean {
  return (useSourcesStore.getState().historyByPdf[pdfId]?.length ?? 0) > 0
}

export async function revertToOriginal(pdfId: string): Promise<void> {
  // Originals were captured the first time sources were set for this PDF
  // (see `setSources`). Use them directly and persist the reverted list to
  // the backend cache so re-imports stay consistent.
  const original = useSourcesStore.getState().originalSourcesByPdf[pdfId]
  if (!original) {
    console.warn(`[revertToOriginal] no original sources stored for ${pdfId}`)
    return
  }
  const cloned = original.map(s => ({ ...s, bbox: { ...s.bbox } }))
  useSourcesStore.setState(state => ({
    sourcesByPdf: { ...state.sourcesByPdf, [pdfId]: cloned },
    historyByPdf: { ...state.historyByPdf, [pdfId]: [] },
  }))
  updateSourceCount(pdfId, cloned.length)
  try {
    // Route through saveSources so revert participates in the per-PDF
    // coalescing instead of racing any in-flight edit save.
    await saveSources(pdfId)
  } catch (e) {
    console.error('Failed to persist revert:', e)
  }
  autoUnapproveOnEdit(pdfId)
}

// Per-PDF save coordination.
//
// At any time there is at most one save in flight per pdfId. Concurrent
// saveSources(pdfId) calls during that window mark a "pending" follow-up
// rather than racing — when the in-flight save resolves, exactly one
// trailing save fires that reads the latest store state. Bounds active
// saves at 2 per pdfId regardless of edit cadence and makes the latest
// store snapshot win on disk, killing the lost-edit race that occurred
// when network reordering let an older PUT overwrite a newer one.
const _saveInFlight = new Map<string, Promise<void>>()
const _savePending = new Set<string>()

export async function saveSources(pdfId: string): Promise<void> {
  const existing = _saveInFlight.get(pdfId)
  if (existing) {
    // Coalesce: schedule a trailing save and share the in-flight promise
    // so all callers' awaits resolve when the latest snapshot is on disk.
    _savePending.add(pdfId)
    return existing
  }

  const promise = (async () => {
    try {
      while (true) {
        const sources = useSourcesStore.getState().sourcesByPdf[pdfId]
        if (!sources) {
          // No in-memory state yet — refuse to overwrite backend with an empty list.
          console.warn(`[saveSources] skipped for ${pdfId}: no sources in store`)
          break
        }
        try {
          await api.updateSources(pdfId, sources)
        } catch (e) {
          console.error(`[saveSources] ✗ ${pdfId}:`, e)
          throw e
        }
        if (!_savePending.has(pdfId)) break
        // Another edit landed during our PUT — re-read state and save again.
        _savePending.delete(pdfId)
      }
    } finally {
      _saveInFlight.delete(pdfId)
      _savePending.delete(pdfId)
    }
  })()

  _saveInFlight.set(pdfId, promise)
  return promise
}

export async function approveSources(pdfId: string): Promise<void> {
  try {
    await api.approvePdf(pdfId)
  } catch (e) {
    console.error('Failed to approve sources:', e)
  }
}

export async function unapproveSources(pdfId: string): Promise<void> {
  try {
    await api.unapprovePdf(pdfId)
    await loadSources(pdfId)
  } catch (e) {
    console.error('Failed to unapprove sources:', e)
  }
}

// Returns the loaded sources when the backend had a cache entry, or null
// otherwise (cache miss or fetch error). Lets callers act on the result
// without re-reading the store.
export async function loadSources(pdfId: string): Promise<SourceRectangle[] | null> {
  try {
    const response = await api.getSources(pdfId)
    // Only seed the store when the backend actually had a cache entry.
    // Otherwise we'd overwrite freshly-detected client-side sources with []
    // on the cold path where a PDF is opened before the orchestrator has
    // finished persisting.
    if (!response.cached) return null
    setSources(pdfId, response.sources)
    return response.sources
  } catch (e) {
    console.error('Failed to load sources:', e)
    return null
  }
}

export function clearSourcesForPdf(pdfId: string): void {
  useSourcesStore.setState(state => {
    const sourcesByPdf = { ...state.sourcesByPdf }
    const originalSourcesByPdf = { ...state.originalSourcesByPdf }
    const historyByPdf = { ...state.historyByPdf }
    delete sourcesByPdf[pdfId]
    delete originalSourcesByPdf[pdfId]
    delete historyByPdf[pdfId]
    return {
      sourcesByPdf,
      originalSourcesByPdf,
      historyByPdf,
    }
  })
}

// Client-side parse orchestrator. Replaces the backend parse job: reads PDF
// bytes via the host (GET /__host/pdf), parses them with pdfjs-dist, and runs the
// TypeScript source detector. The caller merges the result into pdf-store
// / sources-store exactly like the old backend parse flow did.
//
// Existing backend source cache persistence is preserved: we check
// api.getSources() first, and only fall back to fresh detection when no
// cache entry exists, so user edits and approvals carry over between runs.

import { api } from '../api/rest-client'
import type { SourceRectangle } from '../api/types'
import { parseDocument } from './parser'
import { getOrLoadDocument } from './document-cache'
import { detectSources } from './source-detector'

export interface ParseOutcome {
  pdfId: string
  name: string
  path: string
  sources: SourceRectangle[]
  numbered: boolean
  approved: boolean
  fromCache: boolean
  pageCount: number
  error?: string
}

function nameFromPath(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath
}

/**
 * Stable per-path PDF identifier.
 *
 * Replaces the previous filename-stem ID, which made two PDFs named
 * `paper.pdf` in different directories collide on the same `pdfId` —
 * leading to source caches, approval state, verification results, and
 * pathsById entries silently overwriting each other.
 *
 * FNV-1a 64-bit over the case-folded, slash-normalized absolute path.
 * 16 hex chars is overkill for a per-machine local cache key on PDF
 * paths, and the output passes the backend's pdf_id sanitiser without
 * any special characters. Moving a file invalidates its cache, which
 * is acceptable: a re-import re-detects sources in seconds.
 */
export function pdfIdFromPath(filePath: string): string {
  const normalized = filePath.replace(/\\/g, '/').toLowerCase()
  let hash = 0xcbf29ce484222325n
  const PRIME = 0x100000001b3n
  const MASK = 0xffffffffffffffffn
  for (let i = 0; i < normalized.length; i++) {
    hash ^= BigInt(normalized.charCodeAt(i))
    hash = (hash * PRIME) & MASK
  }
  return hash.toString(16).padStart(16, '0')
}

export async function parseAndDetect(filePath: string): Promise<ParseOutcome> {
  const pdfId = pdfIdFromPath(filePath)
  const name = nameFromPath(filePath)

  try {
    // Check the backend source cache first so prior edits + approvals survive.
    let cacheResult:
      | { sources: SourceRectangle[]; numbered: boolean; approved: boolean; pageCount: number | null }
      | null = null
    // A miss is a 200 with `cached: false`. Any *error* means the backend
    // couldn't answer, not that there is no cache. Treating it as a miss
    // used to re-detect sources and PUT them over the user's edited or
    // approved cache, so retry once and otherwise fail this import.
    const res = await api.getSources(pdfId).catch(async () => {
      await new Promise(resolve => setTimeout(resolve, 1000))
      return api.getSources(pdfId)
    })
    if (res?.cached) {
      cacheResult = {
        sources: res.sources,
        numbered: res.numbered ?? res.sources.some(s => s.ref_number != null),
        approved: res.approved ?? false,
        pageCount: typeof res.page_count === 'number' ? res.page_count : null,
      }
    }

    // Cache hit with a stored page count: skip the full PDF parse entirely.
    // The renderer's PDF viewer loads the document on demand later (via
    // document-cache), so we don't need a parsed object here. This avoids
    // re-reading + re-parsing every PDF on every app launch when the user
    // already imported them.
    if (cacheResult !== null && cacheResult.pageCount !== null) {
      return {
        pdfId,
        name,
        path: filePath,
        sources: cacheResult.sources,
        numbered: cacheResult.numbered,
        approved: cacheResult.approved,
        fromCache: true,
        pageCount: cacheResult.pageCount,
      }
    }

    // Loaded through document-cache: the viewer then reuses this pdf.js
    // document instead of reading and parsing the file a second time.
    const entry = await getOrLoadDocument(filePath)

    // Cache hit but no stored page count (older cache file written before
    // page-count caching landed). Backfill it; the page count needs no text
    // parse. Subsequent imports take the fast path above.
    if (cacheResult !== null) {
      try {
        await api.updateSources(
          pdfId,
          cacheResult.sources,
          cacheResult.numbered,
          entry.numPages,
        )
      } catch (err) {
        console.warn(`[orchestrator] could not backfill page_count for ${pdfId}:`, err)
      }
      return {
        pdfId,
        name,
        path: filePath,
        sources: cacheResult.sources,
        numbered: cacheResult.numbered,
        approved: cacheResult.approved,
        fromCache: true,
        pageCount: entry.numPages,
      }
    }

    const parsed = await parseDocument(entry.doc, { id: pdfId, name, path: filePath })
    const { sources, numbered } = detectSources(parsed)

    // Persist freshly detected sources + numbered flag + page count to the
    // backend cache so they survive reloads and verification can pick them up.
    // Fire-and-forget on failure — users can manually re-save from the UI.
    try {
      await api.updateSources(pdfId, sources, numbered, parsed.pages.length)
    } catch (err) {
      console.warn(`[orchestrator] could not persist sources for ${pdfId}:`, err)
    }

    return {
      pdfId,
      name,
      path: filePath,
      sources,
      numbered,
      approved: false,
      fromCache: false,
      pageCount: parsed.pages.length,
    }
  } catch (err) {
    return {
      pdfId,
      name,
      path: filePath,
      sources: [],
      numbered: false,
      approved: false,
      fromCache: false,
      pageCount: 0,
      error: err instanceof Error ? err.message : String(err),
    }
  }
}

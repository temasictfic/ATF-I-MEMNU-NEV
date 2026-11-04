// Module-level LRU cache of pdfjs-dist PDFDocumentProxy instances keyed by
// absolute file path. Holding the parsed doc around lets the user flip
// between recently viewed PDFs without re-reading the file, re-parsing the
// PDF structure, or re-fetching page 1's metadata from the worker. That
// trio is the largest chunk of per-switch latency — the canvas raster is
// bounded by the number of visible pages, which is small.
//
// Bounded at `MAX_CACHED_DOCS` so worker memory stays tame. Eviction
// destroys the underlying loading task so pdfjs's WASM buffers are released.
//
// There is no cross-cutting owner other than this module. `ParsingPage`
// should NOT destroy a doc it obtained from the cache —
// eviction is managed here and wired to `pdf-store.removePdf` /
// `clearPdfs` so removing a PDF from the app tears its cached doc down.

import type { PDFDocumentProxy } from 'pdfjs-dist/types/src/display/api'
import { getPdfjs } from './pdfjs-setup'
import { SCALE } from './types'

export interface PdfDocumentEntry {
  doc: PDFDocumentProxy
  numPages: number
  firstPageWidth: number
  firstPageHeight: number
  /** Last page dimensions if they differ from the first page; `null` when
   *  the first and last pages have matching dimensions (uniform PDF, the
   *  common case). Used by callers to detect non-uniform documents without
   *  walking every page. */
  lastPageDimensions: { width: number; height: number } | null
}

const MAX_CACHED_DOCS = 5

// Insertion order = LRU order: oldest first, newest last. Re-inserting an
// entry moves it to the tail.
const cache = new Map<string, PdfDocumentEntry>()

// Deduplicate in-flight loads so rapid switches don't kick off parallel
// getDocument calls for the same file.
const pending = new Map<string, Promise<PdfDocumentEntry>>()

function touch(key: string, entry: PdfDocumentEntry): void {
  cache.delete(key)
  cache.set(key, entry)
}

function destroyEntry(entry: PdfDocumentEntry, context: string): void {
  entry.doc.loadingTask.destroy().catch((err: unknown) => {
    console.warn(`[pdf-cache] destroy on ${context} failed`, err)
  })
}

function evictIfNeeded(): void {
  while (cache.size > MAX_CACHED_DOCS) {
    const oldestKey = cache.keys().next().value as string | undefined
    if (!oldestKey) return
    const oldest = cache.get(oldestKey)
    cache.delete(oldestKey)
    if (oldest) {
      destroyEntry(oldest, 'evict')
    }
  }
}

/**
 * Return a cached document entry or load it through pdfjs-dist. Loads are
 * deduplicated — concurrent requests for the same path share one fetch.
 * The resulting entry is inserted at the head of the LRU.
 */
export async function getOrLoadDocument(localPath: string): Promise<PdfDocumentEntry> {
  const cached = cache.get(localPath)
  if (cached) {
    touch(localPath, cached)
    return cached
  }

  const inflight = pending.get(localPath)
  if (inflight) return inflight

  const promise = (async (): Promise<PdfDocumentEntry> => {
    try {
      const bytes = await window.electronAPI.readPdfFile(localPath)
      const pdfjsLib = getPdfjs()
      const doc = await pdfjsLib.getDocument({ data: bytes }).promise

      // Fast metadata probe: measure the first page for layout, then spot-check
      // the last page as a cheap uniformity test. Academic PDFs are overwhelmingly
      // uniform; checking all pages (as the old code did) serialized the worker
      // against the critical rasterization path and was the single biggest
      // source of perceived lag on switch.
      const firstPage = await doc.getPage(1)
      const firstVp = firstPage.getViewport({ scale: SCALE })
      firstPage.cleanup()

      let lastPageDimensions: { width: number; height: number } | null = null
      if (doc.numPages > 1) {
        const lastPage = await doc.getPage(doc.numPages)
        const lastVp = lastPage.getViewport({ scale: SCALE })
        lastPage.cleanup()
        if (
          Math.abs(lastVp.width - firstVp.width) > 1
          || Math.abs(lastVp.height - firstVp.height) > 1
        ) {
          lastPageDimensions = { width: lastVp.width, height: lastVp.height }
        }
      }

      const entry: PdfDocumentEntry = {
        doc,
        numPages: doc.numPages,
        firstPageWidth: firstVp.width,
        firstPageHeight: firstVp.height,
        lastPageDimensions,
      }
      cache.set(localPath, entry)
      evictIfNeeded()
      return entry
    } finally {
      pending.delete(localPath)
    }
  })()

  pending.set(localPath, promise)
  return promise
}

/**
 * Drop the cached entry for a file and destroy its underlying pdfjs loading task.
 * Call when a PDF is removed from the app (user deletes, rename, etc.) —
 * otherwise the doc will sit in the cache until evicted by the LRU.
 */
export function evictDocument(localPath: string): void {
  const entry = cache.get(localPath)
  if (!entry) return
  cache.delete(localPath)
  destroyEntry(entry, 'manual evict')
}

/**
 * Destroy and clear every cached document. Call when the entire PDF list
 * is reset (e.g. user loads a new directory).
 */
export function clearDocumentCache(): void {
  for (const entry of cache.values()) {
    entry.doc.loadingTask.destroy().catch(() => {
      // best-effort cleanup
    })
  }
  cache.clear()
  pending.clear()
}

import { create } from 'zustand'
import type { PdfDocument } from '../api/types'
import { parseAndDetect, pdfIdFromPath } from '../pdf/orchestrator'
import { evictDocument } from '../pdf/document-cache'
import { setSources } from './sources-store'
import { wsClient } from '../api/ws-client'
import { hostAPI } from '../host/bridge'

type ParsingSortKey = 'name' | 'status' | 'count' | 'numbered'

interface PdfState {
  pdfs: PdfDocument[]
  // Absolute file system paths keyed by pdf_id (path-derived hash). Populated
  // from loadFiles/loadDirectory so the renderer can read PDF bytes locally
  // without round-tripping through the backend.
  pathsById: Record<string, string>
  selectedPdfId: string | null
  loading: boolean
  parsingSortKey: ParsingSortKey
  parsingSortAsc: boolean
  selectPdf: (id: string | null) => void
  toggleParsingSort: (key: ParsingSortKey) => void
  removePdf: (pdfId: string) => void
  updatePdfStatus: (pdfId: string, status: PdfDocument['status'], sourceCount?: number, numbered?: boolean) => void
  updateSourceCount: (pdfId: string, count: number) => void
  loadDirectory: (directory: string) => Promise<void>
  loadFiles: (filePaths: string[]) => Promise<void>
}

function nameFromPath(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath
}

async function processPdfBatch(filePaths: string[]): Promise<void> {
  // Seed the store with "parsing" placeholders so the list updates immediately
  // and the user sees progress.
  usePdfStore.setState(state => {
    const nextPaths = { ...state.pathsById }
    const existing = new Map(state.pdfs.map(p => [p.id, p]))
    for (const fp of filePaths) {
      const id = pdfIdFromPath(fp)
      nextPaths[id] = fp
      if (!existing.has(id)) {
        existing.set(id, {
          id,
          name: nameFromPath(fp),
          path: fp,
          status: 'parsing',
          source_count: 0,
          numbered: false,
        })
      } else {
        existing.set(id, { ...existing.get(id)!, status: 'parsing' })
      }
    }
    return {
      pdfs: Array.from(existing.values()),
      pathsById: nextPaths,
      loading: true,
    }
  })

  // Process files sequentially so pdfjs-dist's single worker isn't swamped
  // and the UI stays responsive. Each file reads → parses → detects → saves.
  for (const filePath of filePaths) {
    const outcome = await parseAndDetect(filePath)

    if (outcome.error) {
      usePdfStore.getState().updatePdfStatus(outcome.pdfId, 'error')
      continue
    }

    // Seed the client-side sources store so the parsing page picks them up
    // without needing a second GET /api/parse/sources roundtrip.
    setSources(outcome.pdfId, outcome.sources)

    usePdfStore.getState().updatePdfStatus(
      outcome.pdfId,
      outcome.approved ? 'approved' : 'parsed',
      outcome.sources.length,
      outcome.numbered,
    )
  }

  usePdfStore.setState({ loading: false })
}

export const usePdfStore = create<PdfState>()((set, _get) => ({
  pdfs: [],
  pathsById: {},
  selectedPdfId: null,
  loading: false,
  parsingSortKey: 'name' as ParsingSortKey,
  parsingSortAsc: true,

  selectPdf: (id) => set({ selectedPdfId: id }),

  toggleParsingSort: (key) =>
    set(state => {
      if (state.parsingSortKey === key) return { parsingSortAsc: !state.parsingSortAsc }
      return { parsingSortKey: key, parsingSortAsc: false }
    }),

  removePdf: (pdfId) =>
    set(state => {
      const remaining = state.pdfs.filter(p => p.id !== pdfId)
      let nextSelectedPdfId = state.selectedPdfId

      if (state.selectedPdfId === pdfId) {
        if (remaining.length === 0) {
          nextSelectedPdfId = null
        } else {
          const removedIndex = state.pdfs.findIndex(p => p.id === pdfId)
          const fallbackIndex = Math.min(Math.max(removedIndex, 0), remaining.length - 1)
          nextSelectedPdfId = remaining[fallbackIndex]?.id ?? null
        }
      }

      // Drop the cached pdfjs doc for this file so pdfjs worker memory is
      // released instead of waiting for LRU eviction.
      const removedPath = state.pathsById[pdfId]
      if (removedPath) evictDocument(removedPath)

      const { [pdfId]: _dropped, ...remainingPaths } = state.pathsById
      return {
        pdfs: remaining,
        pathsById: remainingPaths,
        selectedPdfId: nextSelectedPdfId,
      }
    }),

  updatePdfStatus: (pdfId, status, sourceCount, numbered) =>
    set(state => ({
      pdfs: state.pdfs.map(p =>
        p.id === pdfId ? {
          ...p,
          status,
          source_count: sourceCount ?? p.source_count,
          numbered: numbered ?? p.numbered,
        } : p
      ),
    })),

  updateSourceCount: (pdfId, count) =>
    set(state => ({
      pdfs: state.pdfs.map(p => (p.id === pdfId ? { ...p, source_count: count } : p)),
    })),

  loadDirectory: async (directory) => {
    try {
      const paths = await hostAPI.listPdfsInDirectory(directory)
      if (paths.length === 0) {
        console.warn(`[pdf-store] no PDFs found in directory: ${directory}`)
        return
      }
      await processPdfBatch(paths)
    } catch (e) {
      console.error('Failed to load directory:', e)
      set({ loading: false })
    }
  },

  loadFiles: async (filePaths) => {
    try {
      await processPdfBatch(filePaths)
    } catch (e) {
      console.error('Failed to parse files:', e)
      set({ loading: false })
    }
  },
}))

// --- Listeners ---
//
// Parsing runs entirely in the renderer (see lib/pdf/orchestrator.ts), so the
// backend never emits parse_started / parse_progress / parse_complete /
// parse_error. Only the approval events come over the websocket — backend
// emits them from backend/api/parsing.py when the user approves or revokes
// approval of a PDF's source list.

export function initPdfListeners(): () => void {
  const unsubs = [
    wsClient.on('parse_approved', (data) => {
      usePdfStore.getState().updatePdfStatus(data.pdf_id as string, 'approved')
    }),
    wsClient.on('parse_unapproved', (data) => {
      usePdfStore.getState().updatePdfStatus(data.pdf_id as string, 'parsed')
    }),
  ]

  return () => {
    unsubs.forEach(fn => fn())
  }
}

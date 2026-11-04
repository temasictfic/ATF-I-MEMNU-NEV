import { create } from 'zustand'
import type { ScholarQueueItem, ScholarScanStatus } from '../services/scholar-scanner'
import { scholarScanner } from '../services/scholar-scanner'
import { useVerificationStore } from './verification-store'
import { useSourcesStore } from './sources-store'
import { useSettingsStore } from './settings-store'
import { sanitizeSourceTextForSearch } from '../utils/source-text'
import { computeExclusion } from '../verification/exclusion'
import { api } from '../api/rest-client'
import i18n from '../i18n'

interface ScholarScanState {
  status: ScholarScanStatus
  queue: ScholarQueueItem[]
  currentIndex: number
  totalInQueue: number
  foundCount: number
  captchaUrl: string | null
  currentSourceId: string | null
  lastDoneSourceId: string | null
  lastDoneUpdated: boolean | null
  completedPdfId: string | null
  completionSequence: number
  closeOverlayFn: (() => void) | null

  startScanForPdf: (pdfId: string) => Promise<void>
  startScanForSource: (pdfId: string, sourceId: string) => Promise<void>
  cancelScan: () => void
  resumeAfterCaptcha: () => void
  setCloseOverlayFn: (fn: (() => void) | null) => void
}

export const useScholarScanStore = create<ScholarScanState>((set, get) => {
  type PendingScan = { pdfId: string; sourceIds?: string[] }
  const pendingScans: PendingScan[] = []
  let preparingScan = false
  let scanGeneration = 0
  let activeScanPdfId: string | null = null

  // Wire up scanner callbacks
  scholarScanner.setCallbacks({
    onStatusChange: (status) => {
      if (status === 'done' && activeScanPdfId) {
        const completedPdfId = activeScanPdfId
        activeScanPdfId = null
        set(state => ({
          status,
          completedPdfId,
          completionSequence: state.completionSequence + 1,
        }))
        // Verify All can finish another PDF while the current Scholar scan is
        // still running. Start its queued scan only after the scanner is free.
        queueMicrotask(() => void runNextScan())
        return
      }
      set({ status })
    },
    onProgress: (current, total, foundCount) =>
      set({ currentIndex: current, totalInQueue: total, foundCount }),
    onCaptcha: (url) => set({ captchaUrl: url }),
    onCaptchaResolved: () => {
      const closeFn = useScholarScanStore.getState().closeOverlayFn
      if (closeFn) closeFn()
      set({ captchaUrl: null })
    },
    onError: (_sourceId, _error) => {
      // Errors are non-fatal; scanner continues to next item
    },
    onSourceDone: (sourceId, updated) => {
      set({ currentSourceId: sourceId, lastDoneSourceId: sourceId, lastDoneUpdated: updated })
      // Locally mark "Google Scholar" as searched for this source so the
      // DATABASE RESULTS list updates immediately, independent of any WS
      // broadcast race. The backend's verify_source_done update is still the
      // source of truth for best_match / all_results.
      useVerificationStore.setState((state) => {
        let foundPdfId: string | null = null
        for (const [pdfId, sourceResults] of Object.entries(state.resultsByPdf)) {
          if (sourceResults[sourceId]) {
            foundPdfId = pdfId
            break
          }
        }
        if (!foundPdfId) return state
        const prev = state.resultsByPdf[foundPdfId][sourceId]
        if (prev.databases_searched.includes('Google Scholar')) return state
        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [foundPdfId]: {
              ...state.resultsByPdf[foundPdfId],
              [sourceId]: {
                ...prev,
                databases_searched: [...prev.databases_searched, 'Google Scholar'],
              },
            },
          },
        }
      })
    },
  })

  // Build the scan queue. Each item's searchText is the NER-extracted title
  // of the source — the raw sanitized text is only used as a fallback when
  // extraction returns an empty title so the source is not silently skipped.
  async function buildQueue(pdfId: string, sourceIds?: string[]): Promise<ScholarQueueItem[]> {
    const verStore = useVerificationStore.getState()
    const srcStore = useSourcesStore.getState()
    const results = verStore.resultsByPdf[pdfId] ?? {}
    const sources = srcStore.sourcesByPdf[pdfId] ?? []
    const enabledSources = verStore.enabledSources
    const exclusionEntries = useSettingsStore.getState().exclusionEntries
    const muafReasons = {
      userDisabled: i18n.t('verification.excluded.userDisabled'),
      nonDoiUrl: i18n.t('verification.excluded.nonDoiUrl'),
      yearBefore: i18n.t('verification.excluded.yearBefore'),
    }

    const selected = sources.filter((s) => {
      // Drop Muaf cards regardless of which entry-point asked for the
      // scan. The single-source path used to short-circuit on the
      // explicit `sourceIds` argument and bypass every Muaf check —
      // which let a Muaf card slip through when a stale pendingRef
      // queued it from an earlier individual-verify click.
      const text = verStore.verifyTexts[s.id] ?? s.text ?? ''
      const info = computeExclusion(text, enabledSources[s.id], exclusionEntries, muafReasons)
      if (info.excluded) return false
      if (sourceIds) return sourceIds.includes(s.id)
      const r = results[s.id]
      return r && (r.status === 'low' || r.status === 'medium')
    })

    return Promise.all(
      selected.map(async (s) => {
        const rawText = verStore.verifyTexts[s.id] ?? s.text
        let searchText = ''
        try {
          const parsed = await api.extractFields(rawText)
          searchText = parsed?.title?.trim() ?? ''
        } catch (err) {
          console.warn(`[Scholar] extractFields failed for ${s.id}:`, err)
        }
        if (!searchText) {
          searchText = sanitizeSourceTextForSearch(rawText)
        } else {
          searchText = sanitizeSourceTextForSearch(searchText)
        }
        return { pdfId, sourceId: s.id, searchText, fullSourceText: rawText }
      }),
    )
  }

  async function runNextScan(): Promise<void> {
    if (preparingScan) return
    const scannerStatus = scholarScanner.getStatus()
    if (scannerStatus === 'scanning' || scannerStatus === 'captcha') return

    const request = pendingScans.shift()
    if (!request) return

    preparingScan = true
    activeScanPdfId = request.pdfId
    const generation = scanGeneration
    set({
      status: 'scanning',
      queue: [],
      totalInQueue: 0,
      currentIndex: 0,
      foundCount: 0,
      captchaUrl: null,
      lastDoneSourceId: null,
      lastDoneUpdated: null,
    })

    const queue = await buildQueue(request.pdfId, request.sourceIds)
    preparingScan = false
    if (generation !== scanGeneration) return

    if (queue.length === 0) {
      // An empty queue is still a completed Scholar pass for this PDF.
      activeScanPdfId = null
      set(state => ({
        status: 'done',
        completedPdfId: request.pdfId,
        completionSequence: state.completionSequence + 1,
      }))
      queueMicrotask(() => void runNextScan())
      return
    }

    set({ queue, totalInQueue: queue.length })
    await scholarScanner.startScan(queue)
  }

  function enqueueScan(request: PendingScan): void {
    const duplicate = pendingScans.some(
      pending => pending.pdfId === request.pdfId
        && JSON.stringify(pending.sourceIds) === JSON.stringify(request.sourceIds),
    )
    if (!duplicate) pendingScans.push(request)
    void runNextScan()
  }

  return {
    status: 'idle',
    queue: [],
    currentIndex: 0,
    totalInQueue: 0,
    foundCount: 0,
    captchaUrl: null,
    currentSourceId: null,
    lastDoneSourceId: null,
    lastDoneUpdated: null,
    completedPdfId: null,
    completionSequence: 0,
    closeOverlayFn: null,

    startScanForPdf: async (pdfId) => {
      enqueueScan({ pdfId })
    },

    startScanForSource: async (pdfId, sourceId) => {
      enqueueScan({ pdfId, sourceIds: [sourceId] })
    },

    cancelScan: () => {
      pendingScans.length = 0
      scanGeneration++
      preparingScan = false
      activeScanPdfId = null
      scholarScanner.cancel()
    },

    resumeAfterCaptcha: () => {
      // console.log('[Scholar] Store: resumeAfterCaptcha triggered')
      set({ captchaUrl: null })
      scholarScanner.resumeAfterCaptcha()
    },

    setCloseOverlayFn: (fn) => set({ closeOverlayFn: fn }),
  }
})

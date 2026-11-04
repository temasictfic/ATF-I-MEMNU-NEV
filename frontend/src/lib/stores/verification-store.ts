import { create } from 'zustand'
import type {
  VerificationResult, PdfVerificationSummary, VerifyStatus,
  SourceRectangle, SourceVerifyProgress, DbCheckStatus, DbCheckEntry,
  DecisionTag,
} from '../api/types'
import { api } from '../api/rest-client'
import { wsClient } from '../api/ws-client'
import { POLL_INTERVAL_MS } from '../constants/timings'
import { sanitizeSourceText } from '../utils/source-text'
import { effectiveTagOn, effectiveDecisionTag } from '../verification/tagState'
import { computeExclusion } from '../verification/exclusion'
import { useSettingsStore } from './settings-store'
import { useSourcesStore } from './sources-store'
import i18n from '../i18n'
import { usePdfStore } from './pdf-store'

type CardSortKey = 'status' | 'ref' | 'enabled' | 'decision' | 'manual'
type PdfSortKey = 'name' | 'status' | 'high' | 'medium' | 'low' | 'valid' | 'citation' | 'fabricated' | 'free'

interface VerificationState {
  resultsByPdf: Record<string, Record<string, VerificationResult>>
  summaries: Record<string, PdfVerificationSummary>
  sourceProgress: Record<string, SourceVerifyProgress>
  // Source IDs the user asked to cancel. The WS event listeners ignore
  // late-arriving events (verify_started, verify_db_checking, verify_db_checked,
  // verify_source_done) for these sources so optimistic cancel updates aren't
  // overwritten by tasks that were mid-flight on the backend. Cleared per
  // source when verification is restarted for that source.
  cancelledSourceIds: Set<string>
  // Tracks the currently-active backend job_id for each source / PDF. Set
  // synchronously by start* actions; checked by WS handlers to drop late
  // events from a superseded run. This makes cancelSource → reverify safe
  // even when the cancelled run's late `verify_source_done` arrives after
  // the cancel-flag has been cleared by the new run.
  activeJobIdBySource: Record<string, string>
  activeJobIdByPdf: Record<string, string>
  // Snapshot of each source's result taken at the moment a re-run starts.
  // Used by cancel actions to instantly restore the pre-rerun state (status
  // colour, best_match, decision tag, etc.) for sources that didn't actually
  // finish in the new run — so Stop reverts queued sources to their prior
  // appearance without a backend round-trip. Entry is cleared when the
  // source receives a fresh verify_completed in the new run, or on cancel
  // restoration.
  priorResultsByPdf: Record<string, Record<string, VerificationResult>>
  selectedSourceId: string | null
  verifyTexts: Record<string, string>
  sourceOriginalTexts: Record<string, string>
  enabledSources: Record<string, boolean>
  sourceOrder: Record<string, string[]>
  cardSortKey: CardSortKey
  cardSortAsc: boolean
  pdfSortKey: PdfSortKey
  pdfSortAsc: boolean
  pdfOrder: string[]
  verifyCutoffIndex: number

  selectSource: (id: string | null) => void
  setVerifyCutoffIndex: (n: number) => void
  setVerifyText: (sourceId: string, text: string) => void
  resetVerifyText: (sourceId: string) => void
  toggleSourceEnabled: (sourceId: string) => void
  setAllEnabled: (pdfId: string, enabled: boolean) => void
  clearEnabledOverrides: (pdfId: string) => void
  reorderSources: (pdfId: string, fromIndex: number, toIndex: number) => void
  reorderPdfs: (fromIndex: number, toIndex: number, baseOrder: string[]) => void
  freezePdfOrder: (baseOrder: string[]) => void
  toggleCardSort: (key: CardSortKey) => void
  togglePdfSort: (key: PdfSortKey) => void
  initSourceVerifyState: (pdfId: string, sources: SourceRectangle[]) => void
  startVerification: (pdfIds: string[]) => Promise<boolean>
  startVerificationNonHighForPdf: (pdfId: string) => Promise<boolean>
  reverifySource: (pdfId: string, sourceId: string, text?: string) => Promise<boolean>
  reverifyPdf: (pdfId: string) => Promise<void>
  overrideStatus: (pdfId: string, sourceId: string, status: 'high' | 'medium' | 'low') => Promise<void>
  toggleTag: (pdfId: string, sourceId: string, tag: 'authors' | 'year' | 'title' | 'journal' | 'doi/arXiv') => Promise<void>
  cycleDecisionTag: (pdfId: string, sourceId: string) => Promise<void>
  cancelAll: () => Promise<void>
  cancelPdf: (pdfId: string) => Promise<void>
  cancelSource: (sourceId: string) => Promise<void>
  loadResults: (pdfId: string) => Promise<void>
}

interface PollingState {
  jobId: string
  pdfIds: string[]
  errors: number
  timer: ReturnType<typeof setInterval>
}

const MAX_POLLING_CONSECUTIVE_ERRORS = 3

// At most one polling job is in flight at a time. startPolling clears the
// previous state before starting a new tick, so the three pieces (jobId,
// pdfIds, errors, timer) can never disagree about which job is being polled.
let polling: PollingState | null = null

function stopPolling(): void {
  if (polling) {
    clearInterval(polling.timer)
    polling = null
  }
}

function startPolling(pdfIds: string[], jobId: string): void {
  stopPolling()

  const timer = setInterval(async () => {
    const current = polling
    // Skip if polling was stopped between ticks
    if (!current) return
    try {
      // Check local state first — WS events may have already completed everything
      const localState = useVerificationStore.getState()
      const localDone = current.pdfIds.every(pdfId => {
        const pdfResults = localState.resultsByPdf[pdfId] ?? {}
        const vals = Object.values(pdfResults)
        return vals.length > 0 && vals.every(r => r.status !== 'in_progress')
      })
      if (localDone) {
        if (polling !== current) return
        // Mark summaries as completed and stop
        useVerificationStore.setState(state => {
          const next = { ...state.summaries }
          for (const pdfId of current.pdfIds) {
            if (next[pdfId] && !next[pdfId].completed) {
              next[pdfId] = { ...next[pdfId], in_progress: 0, completed: true }
            }
          }
          return { summaries: next }
        })
        if (polling === current) stopPolling()
        return
      }

      const statusResp = await api.verifyStatus(current.jobId)
      if (polling !== current) return
      useVerificationStore.setState(state => {
        const next = { ...state.summaries }
        for (const s of statusResp.pdfs) {
          const localInProgress = Object.values(state.resultsByPdf[s.pdf_id] ?? {})
            .filter(result => result.status === 'in_progress').length
          // A completed backend summary is not enough to advance Verify All:
          // a dropped verify_source_done event can leave a local row running.
          // Keep the PDF pending until the authoritative results fetch below
          // has reconciled every source.
          next[s.pdf_id] = {
            ...s,
            in_progress: Math.max(s.in_progress, localInProgress),
            completed: s.completed && localInProgress === 0,
          }
        }
        return { summaries: next }
      })

      for (const pdfId of current.pdfIds) {
        try {
          const resultResp = await api.verifyResults(pdfId)
          if (polling !== current) return
          const newResults = resultResp.results
          const prevResults = useVerificationStore.getState().resultsByPdf[pdfId] ?? {}

          const newKeys = Object.keys(newResults)
          const prevKeys = Object.keys(prevResults)
          const hasChanges =
            newKeys.length !== prevKeys.length ||
            newKeys.some(k => {
              const n = newResults[k]
              const p = prevResults[k]
              return !p || n.status !== p.status
            })

          if (hasChanges) {
            const backendSummary = statusResp.pdfs.find(s => s.pdf_id === pdfId)
            useVerificationStore.setState(state => ({
              resultsByPdf: { ...state.resultsByPdf, [pdfId]: newResults },
              summaries: backendSummary
                ? {
                    ...state.summaries,
                    [pdfId]: {
                      ...backendSummary,
                      completed: backendSummary.completed
                        && Object.values(newResults).every(r => r.status !== 'in_progress'),
                    },
                  }
                : state.summaries,
            }))
          }
        } catch {
          // Individual PDF result fetch failed, skip
        }
      }

      const allDone = statusResp.pdfs.length > 0 && statusResp.pdfs.every(s => s.completed)
      const localResultsDone = current.pdfIds.every(pdfId =>
        Object.values(useVerificationStore.getState().resultsByPdf[pdfId] ?? {})
          .every(result => result.status !== 'in_progress')
      )
      if (allDone && localResultsDone && polling === current) stopPolling()
      // Successful tick — reset only this job's consecutive-error counter.
      if (polling === current) current.errors = 0
    } catch (e) {
      if (polling !== current) return
      current.errors++
      console.error(
        `[Poll] Polling error (${current.errors}/${MAX_POLLING_CONSECUTIVE_ERRORS}):`,
        e,
      )
      // Backend is probably down (restart, crash, network hiccup). Don't
      // spin forever hammering a dead endpoint — stop after a few failures.
      if (current.errors >= MAX_POLLING_CONSECUTIVE_ERRORS) {
        console.warn('[Poll] Too many consecutive errors, stopping poll loop')
        if (polling === current) stopPolling()
      }
    }
  }, POLL_INTERVAL_MS)

  polling = { jobId, pdfIds, errors: 0, timer }
}

// --- Buffered verification console logging ---

interface DbCheckLog {
  database: string
  dbStatus: DbCheckStatus
  match: Record<string, unknown> | null
  searchUrl: string
}

interface SourceLog {
  sourceId: string
  sourceText: string
  dbChecks: DbCheckLog[]
  finalStatus: string | null
}

interface PdfVerifyLog {
  pdfId: string
  sources: Map<string, SourceLog>
}

interface VerifyBatch {
  totalSources: number
  pdfs: Map<string, PdfVerifyLog>
  expectedPdfCount: number
  completedPdfs: number
  groupOpened: boolean
}

let verifyBatch: VerifyBatch | null = null

function initVerifyBatch(totalSources: number, pdfCount: number): void {
  // Flush any leftover batch
  if (verifyBatch?.groupOpened) {
    console.groupEnd()
  }
  verifyBatch = {
    totalSources,
    pdfs: new Map(),
    expectedPdfCount: pdfCount,
    completedPdfs: 0,
    groupOpened: false,
  }
}

function flushPdfVerifyLog(pdfId: string): void {
  if (!verifyBatch) return
  const pdfLog = verifyBatch.pdfs.get(pdfId)
  if (!pdfLog) return
}

// --- Store ---

export const useVerificationStore = create<VerificationState>()((set, get) => ({
  resultsByPdf: {},
  summaries: {},
  sourceProgress: {},
  cancelledSourceIds: new Set<string>(),
  activeJobIdBySource: {},
  activeJobIdByPdf: {},
  priorResultsByPdf: {},
  selectedSourceId: null,
  verifyTexts: {},
  sourceOriginalTexts: {},
  enabledSources: {},
  sourceOrder: {},
  cardSortKey: 'ref' as CardSortKey,
  cardSortAsc: true,
  pdfSortKey: 'name' as PdfSortKey,
  pdfSortAsc: true,
  pdfOrder: [],
  verifyCutoffIndex: Number.POSITIVE_INFINITY,

  selectSource: (id) => set({ selectedSourceId: id }),

  setVerifyCutoffIndex: (n) => set({ verifyCutoffIndex: n < 0 ? 0 : n }),

  setVerifyText: (sourceId, text) =>
    set(state => ({ verifyTexts: { ...state.verifyTexts, [sourceId]: text } })),

  resetVerifyText: (sourceId) =>
    set(state => ({
      verifyTexts: {
        ...state.verifyTexts,
        [sourceId]: state.sourceOriginalTexts[sourceId] ?? '',
      },
    })),

  toggleSourceEnabled: (sourceId) =>
    set(state => {
      // Flip the *effective* Muaf state — not just the boolean. For an
      // auto-Muaf card with no override yet, the first click sets `true`
      // (force enable, hides Muaf); the next click sets `false` (user
      // disable, but auto reason still wins for display per
      // computeExclusion's priority).
      const override = state.enabledSources[sourceId]
      let next: boolean
      if (override === true) {
        next = false
      } else if (override === false) {
        next = true
      } else {
        const text = state.verifyTexts[sourceId] ?? state.sourceOriginalTexts[sourceId] ?? ''
        const info = computeExclusion(
          text,
          undefined,
          useSettingsStore.getState().exclusionEntries,
          {
            userDisabled: i18n.t('verification.excluded.userDisabled'),
            nonDoiUrl: i18n.t('verification.excluded.nonDoiUrl'),
            yearBefore: i18n.t('verification.excluded.yearBefore'),
          },
        )
        next = info.excluded
      }
      return {
        enabledSources: { ...state.enabledSources, [sourceId]: next },
      }
    }),

  setAllEnabled: (pdfId, enabled) =>
    set(state => {
      const order = state.sourceOrder[pdfId] ?? []
      const updated = { ...state.enabledSources }
      for (const id of order) updated[id] = enabled
      return { enabledSources: updated }
    }),

  clearEnabledOverrides: (pdfId) =>
    set(state => {
      const order = state.sourceOrder[pdfId] ?? []
      const updated = { ...state.enabledSources }
      for (const id of order) delete updated[id]
      return { enabledSources: updated }
    }),

  reorderSources: (pdfId, fromIndex, toIndex) =>
    set(state => {
      const order = [...(state.sourceOrder[pdfId] ?? [])]
      const [moved] = order.splice(fromIndex, 1)
      order.splice(toIndex, 0, moved)
      return { sourceOrder: { ...state.sourceOrder, [pdfId]: order } }
    }),

  reorderPdfs: (fromIndex, toIndex, baseOrder) =>
    set(() => {
      const order = [...baseOrder]
      const [moved] = order.splice(fromIndex, 1)
      order.splice(toIndex, 0, moved)
      return { pdfOrder: order, pdfSortKey: 'free' }
    }),

  freezePdfOrder: (baseOrder) =>
    set(() => ({ pdfOrder: [...baseOrder], pdfSortKey: 'free' })),

  toggleCardSort: (key) =>
    set(state => {
      if (state.cardSortKey === key) return { cardSortAsc: !state.cardSortAsc }
      return { cardSortKey: key, cardSortAsc: false }
    }),

  togglePdfSort: (key) =>
    set(state => {
      if (state.pdfSortKey === key) return { pdfSortAsc: !state.pdfSortAsc }
      return { pdfSortKey: key, pdfSortAsc: false }
    }),

  initSourceVerifyState: (pdfId, sources) =>
    set(state => {
      const texts = { ...state.verifyTexts }
      const originals = { ...state.sourceOriginalTexts }
      const enabled = { ...state.enabledSources }
      const currentSourceIds = new Set(sources.map(s => s.id))

      for (const source of sources) {
        const cleanedSourceText = sanitizeSourceText(source.text)
        const prevOriginal = originals[source.id]
        const currentText = texts[source.id]

        if (prevOriginal === undefined) {
          // New source — initialize both
          texts[source.id] = cleanedSourceText
        } else if (currentText === prevOriginal) {
          // User hasn't edited — sync from parsing page
          texts[source.id] = cleanedSourceText
        }
        // else: user has edited (currentText !== prevOriginal) — keep user's edit

        // Always update original to latest parsing text
        originals[source.id] = cleanedSourceText

        // Leave `enabled[source.id]` unset for new sources. The renderer
        // treats missing entries as "follow auto-Muaf detection" — text
        // matching the exclusion list, or non-DOI URL, makes the card
        // Muaf without an explicit user choice. An entry of `true` means
        // "user force-enabled, override Muaf"; `false` means "user
        // disabled". See verification/exclusion.ts.
      }

      // Clean up stale entries for sources that no longer exist
      const prevOrder = state.sourceOrder[pdfId] ?? []
      for (const oldId of prevOrder) {
        if (!currentSourceIds.has(oldId)) {
          delete texts[oldId]
          delete originals[oldId]
          delete enabled[oldId]
        }
      }

      // IDs are content-addressed: any text edit or merge produces a new
      // ID that isn't in the results map, so filtering by ID presence is
      // enough to drop stale entries. Unrelated sources keep their
      // cached best_match across parse fixes instead of being wiped.
      let updatedResults = state.resultsByPdf
      const pdfResults = state.resultsByPdf[pdfId]
      if (pdfResults) {
        const cleaned: Record<string, VerificationResult> = {}
        for (const id of currentSourceIds) {
          if (pdfResults[id]) cleaned[id] = pdfResults[id]
        }
        if (Object.keys(cleaned).length !== Object.keys(pdfResults).length) {
          updatedResults = { ...state.resultsByPdf, [pdfId]: cleaned }
        }
      }

      const order = sources.map(s => s.id)

      // Recompute summary if results changed
      let updatedSummaries = state.summaries
      if (updatedResults !== state.resultsByPdf) {
        const results = updatedResults[pdfId] ?? {}
        const count = Object.keys(results).length
        let high = 0, medium = 0, low = 0
        for (const r of Object.values(results)) {
          if (r.status === 'high') high++
          else if (r.status === 'medium') medium++
          else if (r.status === 'low') low++
        }
        updatedSummaries = {
          ...state.summaries,
          [pdfId]: {
            pdf_id: pdfId, high, medium, low,
            in_progress: 0, total: count, completed: count > 0,
          },
        }
      }

      return {
        verifyTexts: texts,
        sourceOriginalTexts: originals,
        enabledSources: enabled,
        sourceOrder: { ...state.sourceOrder, [pdfId]: order },
        resultsByPdf: updatedResults,
        summaries: updatedSummaries,
      }
    }),

  startVerification: async (pdfIds) => {
    // Snapshot the pieces of state we're about to mutate optimistically so
    // we can roll back if the backend call fails. Without this, a network
    // error on api.verifyBatch leaves every targeted source stuck on
    // status='in_progress' with no way to recover short of a page reload.
    const rollback = (() => {
      const s = get()
      return {
        summaries: s.summaries,
        sourceProgress: s.sourceProgress,
        resultsByPdf: s.resultsByPdf,
        cancelledSourceIds: s.cancelledSourceIds,
        priorResultsByPdf: s.priorResultsByPdf,
        activeJobIdBySource: s.activeJobIdBySource,
        activeJobIdByPdf: s.activeJobIdByPdf,
      }
    })()
    try {
      const { verifyTexts, enabledSources, sourceOrder } = get()
      const texts: Record<string, string> = {}
      for (const [sourceId, text] of Object.entries(verifyTexts)) {
        texts[sourceId] = sanitizeSourceText(text)
      }

      // Build the excluded set via the same Muaf rules the renderer uses.
      // Pass `enabledSources[id]` raw (boolean | undefined) — undefined
      // means "no user choice", which lets auto-Muaf detection apply.
      const exclusionEntries = useSettingsStore.getState().exclusionEntries
      const muafReasons = {
        userDisabled: i18n.t('verification.excluded.userDisabled'),
        nonDoiUrl: i18n.t('verification.excluded.nonDoiUrl'),
        yearBefore: i18n.t('verification.excluded.yearBefore'),
      }
      const sourcesByPdf = useSourcesStore.getState().sourcesByPdf
      const excludedSet = new Set<string>()
      for (const pdfId of pdfIds) {
        const sources = sourcesByPdf[pdfId] ?? []
        for (const source of sources) {
          const text = verifyTexts[source.id] ?? source.text ?? ''
          const info = computeExclusion(
            text,
            enabledSources[source.id],
            exclusionEntries,
            muafReasons,
          )
          if (info.excluded) excludedSet.add(source.id)
        }
      }
      const excludedIds = Array.from(excludedSet)

      // Count total enabled sources for logging
      let totalEnabled = 0
      for (const pdfId of pdfIds) {
        const order = sourceOrder[pdfId] ?? []
        for (const sourceId of order) {
          if (!excludedSet.has(sourceId)) totalEnabled++
        }
      }

      // Initialize verify log batch
      initVerifyBatch(totalEnabled, pdfIds.length)

      // Sentinel job_id installed synchronously *before* the API call so any
      // late events from a superseded run can't sneak through the WS-handler
      // filter during the API round-trip — the sentinel won't match any real
      // backend job_id (which is an 8-char UUID), so events with the OLD
      // job_id are dropped. Replaced with the real job_id once the API
      // returns. See the WS handlers' job-id check.
      const pendingJobId = `pending-${Math.random().toString(36).slice(2)}`

      // Track which (pdfId, sourceId) we install the sentinel for so we can
      // safely replace just those entries after the API resolves, without
      // clobbering activeJobIdBySource entries set by an unrelated concurrent
      // start* action.
      const trackedSources: string[] = []
      const trackedPdfs: string[] = pdfIds.slice()

      // Clear progress and summaries BEFORE the API call so we don't
      // race with WebSocket events that arrive during the await.
      // Also eagerly create sourceProgress + in_progress results for all
      // enabled sources so dots appear immediately.
      set(state => {
        const summaries = { ...state.summaries }
        const newProgress: Record<string, { currentDb: string | null; checkedDbs: DbCheckEntry[] }> = {}
        const newResults: Record<string, Record<string, VerificationResult>> = { ...state.resultsByPdf }
        const newPriorResults: Record<string, Record<string, VerificationResult>> = { ...state.priorResultsByPdf }
        // Clear cancelled flags for sources we are about to re-run so late
        // events from the *previous* run no longer suppress this new run.
        const nextCancelled = new Set(state.cancelledSourceIds)
        const newJobIdBySource = { ...state.activeJobIdBySource }
        const newJobIdByPdf = { ...state.activeJobIdByPdf }
        for (const id of pdfIds) {
          summaries[id] = {
            pdf_id: id, high: 0, medium: 0, low: 0,
            in_progress: 0, total: 0, completed: false,
          }
          newJobIdByPdf[id] = pendingJobId
          // Create progress + in_progress result for each enabled source.
          // Preserve existing results for excluded (disabled) sources.
          const order = state.sourceOrder[id] ?? []
          const prevPdfResults = state.resultsByPdf[id] ?? {}
          newResults[id] = {}
          const pdfPriorSnapshot = { ...(state.priorResultsByPdf[id] ?? {}) }
          for (const sourceId of order) {
            if (excludedSet.has(sourceId) || state.enabledSources[sourceId] === false) {
              if (prevPdfResults[sourceId]) {
                newResults[id][sourceId] = prevPdfResults[sourceId]
              }
              continue
            }
            nextCancelled.delete(sourceId)
            newJobIdBySource[sourceId] = pendingJobId
            trackedSources.push(sourceId)
            newProgress[sourceId] = { currentDb: null, checkedDbs: [] }
            // Preserve prior fields (best_match, problem_tags, etc.) so the
            // UI keeps showing the last-known-good decision tag while the
            // re-run is in flight, and so a Stop mid-run leaves queued
            // sources looking like they did before — instead of empty
            // results that classify as 'fabricated'.
            const prev = prevPdfResults[sourceId]
            // Snapshot the pre-rerun result so cancel can restore the row
            // (status colour included) without a backend round-trip. Skip
            // sources that don't have a settled prior result yet.
            if (prev && prev.status !== 'in_progress') {
              pdfPriorSnapshot[sourceId] = prev
            }
            newResults[id][sourceId] = prev
              ? {
                  ...prev,
                  source_id: sourceId,
                  status: 'in_progress' as VerifyStatus,
                  all_results: [],
                  databases_searched: [],
                }
              : {
                  source_id: sourceId,
                  status: 'in_progress' as VerifyStatus,
                  problem_tags: [],
                  url_liveness: {},
                  all_results: [],
                  databases_searched: [],
                }
          }
          newPriorResults[id] = pdfPriorSnapshot
        }
        return {
          summaries,
          sourceProgress: newProgress,
          resultsByPdf: newResults,
          cancelledSourceIds: nextCancelled,
          priorResultsByPdf: newPriorResults,
          activeJobIdBySource: newJobIdBySource,
          activeJobIdByPdf: newJobIdByPdf,
        }
      })

      const response = await api.verifyBatch(pdfIds, texts, excludedIds)

      // Replace the sentinel with the real job_id so subsequent WS events
      // for this run pass the renderer-side filter. Only touch entries we
      // installed ourselves — never overwrite activeJobIdBy{Source,Pdf}
      // values written by an unrelated concurrent start* action.
      set(state => {
        const bySource = { ...state.activeJobIdBySource }
        const byPdf = { ...state.activeJobIdByPdf }
        for (const id of trackedSources) {
          if (bySource[id] === pendingJobId) bySource[id] = response.job_id
        }
        for (const id of trackedPdfs) {
          if (byPdf[id] === pendingJobId) byPdf[id] = response.job_id
        }
        return { activeJobIdBySource: bySource, activeJobIdByPdf: byPdf }
      })

      startPolling(pdfIds, response.job_id)
      return true
    } catch (e) {
      console.error('Failed to start verification:', e)
      set(rollback)
      return false
    }
  },

  startVerificationNonHighForPdf: async (pdfId) => {
    const rollback = (() => {
      const s = get()
      return {
        summaries: s.summaries,
        sourceProgress: s.sourceProgress,
        resultsByPdf: s.resultsByPdf,
        priorResultsByPdf: s.priorResultsByPdf,
        cancelledSourceIds: s.cancelledSourceIds,
        activeJobIdBySource: s.activeJobIdBySource,
        activeJobIdByPdf: s.activeJobIdByPdf,
      }
    })()
    try {
      const { verifyTexts, enabledSources, sourceOrder, resultsByPdf } = get()
      const texts: Record<string, string> = {}
      for (const [sourceId, text] of Object.entries(verifyTexts)) {
        texts[sourceId] = sanitizeSourceText(text)
      }

      const orderedIds = sourceOrder[pdfId] ?? []
      const knownResultIds = Object.keys(resultsByPdf[pdfId] ?? {})
      const sourceIds = orderedIds.length > 0
        ? orderedIds
        : knownResultIds

      if (sourceIds.length === 0) return false

      // Filter out Muaf sources alongside user-disabled and already-high
      // ones. Without this, "verify non-High" would still queue auto-Muaf
      // cards (word match / non-DOI URL) — the same gate the full-batch
      // and single-source paths apply.
      const exclusionEntries = useSettingsStore.getState().exclusionEntries
      const muafReasons = {
        userDisabled: i18n.t('verification.excluded.userDisabled'),
        nonDoiUrl: i18n.t('verification.excluded.nonDoiUrl'),
        yearBefore: i18n.t('verification.excluded.yearBefore'),
      }
      const sourcesByPdf = useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
      const sourceTextById = new Map(sourcesByPdf.map(s => [s.id, s.text]))

      const includeIds = sourceIds.filter(sourceId => {
        const result = resultsByPdf[pdfId]?.[sourceId]
        if (result?.status === 'high') return false
        const text = verifyTexts[sourceId] ?? sourceTextById.get(sourceId) ?? ''
        const info = computeExclusion(
          text,
          enabledSources[sourceId],
          exclusionEntries,
          muafReasons,
        )
        return !info.excluded
      })

      if (includeIds.length === 0) return false

      const includeSet = new Set(includeIds)
      const excludedIds = sourceIds.filter(sourceId => !includeSet.has(sourceId))

      // Initialize verify log batch for this targeted run.
      initVerifyBatch(includeIds.length, 1)

      // Sentinel job_id installed before the API call — see startVerification
      // for the rationale. Replaced with the real job_id after the response.
      const pendingJobId = `pending-${Math.random().toString(36).slice(2)}`

      set(state => {
        const pdfResults = { ...(state.resultsByPdf[pdfId] ?? {}) }
        const sourceProgress = { ...state.sourceProgress }
        const pdfPriorSnapshot = { ...(state.priorResultsByPdf[pdfId] ?? {}) }
        // Clear the cancelled-flag for sources we're about to re-run so late
        // events from the *previous* run no longer suppress this new run's
        // WS updates. Mirrors startVerification / reverifySource / reverifyPdf.
        const nextCancelled = new Set(state.cancelledSourceIds)
        const newJobIdBySource = { ...state.activeJobIdBySource }

        for (const sourceId of includeIds) {
          nextCancelled.delete(sourceId)
          newJobIdBySource[sourceId] = pendingJobId
          // Preserve prior fields so a re-run on an already-verified source
          // keeps showing the last-known-good decision tag during in_progress
          // and on Stop, instead of flashing as 'fabricated'.
          const prev = pdfResults[sourceId]
          // Snapshot the pre-rerun result so cancel can restore the row
          // without a backend round-trip.
          if (prev && prev.status !== 'in_progress') {
            pdfPriorSnapshot[sourceId] = prev
          }
          pdfResults[sourceId] = prev
            ? {
                ...prev,
                source_id: sourceId,
                status: 'in_progress' as VerifyStatus,
                all_results: [],
                databases_searched: [],
              }
            : {
                source_id: sourceId,
                status: 'in_progress' as VerifyStatus,
                problem_tags: [],
                url_liveness: {},
                all_results: [],
                databases_searched: [],
              }
          sourceProgress[sourceId] = { currentDb: null, checkedDbs: [] }
        }

        let high = 0, medium = 0, low = 0, inProgress = 0
        for (const r of Object.values(pdfResults)) {
          if (r.status === 'high') high++
          else if (r.status === 'medium') medium++
          else if (r.status === 'low') low++
          else if (r.status === 'in_progress') inProgress++
        }

        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: pdfResults,
          },
          sourceProgress,
          summaries: {
            ...state.summaries,
            [pdfId]: {
              pdf_id: pdfId,
              high,
              medium,
              low,
              in_progress: inProgress,
              total: Object.keys(pdfResults).length,
              completed: false,
            },
          },
          priorResultsByPdf: {
            ...state.priorResultsByPdf,
            [pdfId]: pdfPriorSnapshot,
          },
          cancelledSourceIds: nextCancelled,
          activeJobIdBySource: newJobIdBySource,
          activeJobIdByPdf: { ...state.activeJobIdByPdf, [pdfId]: pendingJobId },
        }
      })

      const response = await api.verifyBatch([pdfId], texts, excludedIds)

      // Replace sentinel with real job_id, only for entries we wrote.
      set(state => {
        const bySource = { ...state.activeJobIdBySource }
        const byPdf = { ...state.activeJobIdByPdf }
        for (const id of includeIds) {
          if (bySource[id] === pendingJobId) bySource[id] = response.job_id
        }
        if (byPdf[pdfId] === pendingJobId) byPdf[pdfId] = response.job_id
        return { activeJobIdBySource: bySource, activeJobIdByPdf: byPdf }
      })

      startPolling([pdfId], response.job_id)
      return true
    } catch (e) {
      console.error('Failed to start non-high PDF verification:', e)
      set(rollback)
      return false
    }
  },

  reverifySource: async (pdfId, sourceId, text) => {
    // Short-circuit if the card is Muaf (manually disabled or matched by
    // the exclusion list). Without this, a single-card Verify on a Muaf
    // card would queue work the batch gate already blocks.
    {
      const state = get()
      const sources = useSourcesStore.getState().sourcesByPdf[pdfId] ?? []
      const source = sources.find(s => s.id === sourceId)
      const sourceText = text ?? state.verifyTexts[sourceId] ?? source?.text ?? ''
      const info = computeExclusion(
        sourceText,
        state.enabledSources[sourceId],
        useSettingsStore.getState().exclusionEntries,
        {
          userDisabled: i18n.t('verification.excluded.userDisabled'),
          nonDoiUrl: i18n.t('verification.excluded.nonDoiUrl'),
          yearBefore: i18n.t('verification.excluded.yearBefore'),
        },
      )
      if (info.excluded) return false
    }
    const rollback = (() => {
      const s = get()
      return {
        resultsByPdf: s.resultsByPdf,
        sourceProgress: s.sourceProgress,
        cancelledSourceIds: s.cancelledSourceIds,
        priorResultsByPdf: s.priorResultsByPdf,
        activeJobIdBySource: s.activeJobIdBySource,
      }
    })()
    try {
      // Init a mini batch for single verify logging
      initVerifyBatch(1, 1)

      // Sentinel job_id installed before the API call so a late
      // verify_source_done from a just-cancelled prior run (which carries
      // the old job_id) gets dropped by the WS-handler filter — closing
      // the cancel-then-reverify race.
      const pendingJobId = `pending-${Math.random().toString(36).slice(2)}`

      set(state => {
        const nextCancelled = new Set(state.cancelledSourceIds)
        nextCancelled.delete(sourceId)
        const prev = state.resultsByPdf[pdfId]?.[sourceId]
        const pdfPriorSnapshot = { ...(state.priorResultsByPdf[pdfId] ?? {}) }
        if (prev && prev.status !== 'in_progress') {
          pdfPriorSnapshot[sourceId] = prev
        }
        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: {
              ...(state.resultsByPdf[pdfId] ?? {}),
              [sourceId]: prev
                ? {
                    ...prev,
                    source_id: sourceId,
                    status: 'in_progress' as VerifyStatus,
                    all_results: [],
                    databases_searched: [],
                  }
                : {
                    source_id: sourceId,
                    status: 'in_progress' as VerifyStatus,
                    problem_tags: [],
                    url_liveness: {},
                    all_results: [],
                    databases_searched: [],
                  },
            },
          },
          sourceProgress: {
            ...state.sourceProgress,
            [sourceId]: { currentDb: null, checkedDbs: [] },
          },
          cancelledSourceIds: nextCancelled,
          priorResultsByPdf: {
            ...state.priorResultsByPdf,
            [pdfId]: pdfPriorSnapshot,
          },
          activeJobIdBySource: { ...state.activeJobIdBySource, [sourceId]: pendingJobId },
        }
      })
      const response = await api.verifySource(pdfId, sourceId, sanitizeSourceText(text ?? ''))

      // Replace sentinel with real job_id, but only if our sentinel is still
      // there — a concurrent action may have already moved on.
      set(state => {
        if (state.activeJobIdBySource[sourceId] !== pendingJobId) return state
        return {
          activeJobIdBySource: { ...state.activeJobIdBySource, [sourceId]: response.job_id },
          activeJobIdByPdf: { ...state.activeJobIdByPdf, [pdfId]: response.job_id },
        }
      })
      startPolling([pdfId], response.job_id)
      return true
    } catch (e) {
      console.error('Failed to verify source:', e)
      set(rollback)
      return false
    }
  },

  reverifyPdf: async (pdfId) => {
    try {
      const pendingJobId = `pending-${Math.random().toString(36).slice(2)}`
      const trackedSources: string[] = []
      set(state => {
        const nextCancelled = new Set(state.cancelledSourceIds)
        const newJobIdBySource = { ...state.activeJobIdBySource }
        for (const sourceId of state.sourceOrder[pdfId] ?? []) {
          nextCancelled.delete(sourceId)
          newJobIdBySource[sourceId] = pendingJobId
          trackedSources.push(sourceId)
        }
        return {
          cancelledSourceIds: nextCancelled,
          activeJobIdBySource: newJobIdBySource,
          activeJobIdByPdf: { ...state.activeJobIdByPdf, [pdfId]: pendingJobId },
        }
      })
      const response = await api.verifyPdf(pdfId)
      set(state => {
        const bySource = { ...state.activeJobIdBySource }
        const byPdf = { ...state.activeJobIdByPdf }
        for (const id of trackedSources) {
          if (bySource[id] === pendingJobId) bySource[id] = response.job_id
        }
        if (byPdf[pdfId] === pendingJobId) byPdf[pdfId] = response.job_id
        return { activeJobIdBySource: bySource, activeJobIdByPdf: byPdf }
      })
    } catch (e) {
      console.error('Failed to verify PDF:', e)
    }
  },

  overrideStatus: async (pdfId, sourceId, status) => {
    // Apply optimistically FIRST so a late `verify_source_done` WS event
    // arriving between the API call and the set() can't clobber the user's
    // manual override. If the API call fails we roll back.
    let previousStatus: VerifyStatus | undefined
    set(state => {
      const prev = state.resultsByPdf[pdfId]?.[sourceId]
      if (!prev) return state
      previousStatus = prev.status
      return {
        resultsByPdf: {
          ...state.resultsByPdf,
          [pdfId]: {
            ...state.resultsByPdf[pdfId],
            [sourceId]: { ...prev, status },
          },
        },
      }
    })
    try {
      await api.overrideStatus(pdfId, sourceId, status)
    } catch (e) {
      console.error('Failed to override status:', e)
      if (previousStatus !== undefined) {
        set(state => {
          const prev = state.resultsByPdf[pdfId]?.[sourceId]
          if (!prev) return state
          return {
            resultsByPdf: {
              ...state.resultsByPdf,
              [pdfId]: {
                ...state.resultsByPdf[pdfId],
                [sourceId]: { ...prev, status: previousStatus! },
              },
            },
          }
        })
      }
    }
  },

  toggleTag: async (pdfId, sourceId, tag) => {
    const current = useVerificationStore.getState().resultsByPdf[pdfId]?.[sourceId]
    if (!current) return
    const wasOn = effectiveTagOn(current, tag)
    const next = !wasOn
    const prevOverrides = current.tag_overrides ?? {}
    const prevDecisionOverride = current.decision_tag_override ?? null
    const newOverrides = { ...prevOverrides, [tag]: next }

    set(state => {
      const prev = state.resultsByPdf[pdfId]?.[sourceId]
      if (!prev) return state
      return {
        resultsByPdf: {
          ...state.resultsByPdf,
          [pdfId]: {
            ...state.resultsByPdf[pdfId],
            [sourceId]: {
              ...prev,
              tag_overrides: newOverrides,
              decision_tag_override: null,
            },
          },
        },
      }
    })

    try {
      await api.setTagOverride(pdfId, sourceId, tag, next)
    } catch (e) {
      console.error('Failed to toggle tag:', e)
      set(state => {
        const prev = state.resultsByPdf[pdfId]?.[sourceId]
        if (!prev) return state
        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: {
              ...state.resultsByPdf[pdfId],
              [sourceId]: {
                ...prev,
                tag_overrides: prevOverrides,
                decision_tag_override: prevDecisionOverride,
              },
            },
          },
        }
      })
    }
  },

  cycleDecisionTag: async (pdfId, sourceId) => {
    const CYCLE: Record<DecisionTag, DecisionTag> = {
      valid: 'citation',
      citation: 'fabricated',
      fabricated: 'valid',
    }
    const current = useVerificationStore.getState().resultsByPdf[pdfId]?.[sourceId]
    if (!current) return
    const next = CYCLE[effectiveDecisionTag(current)]
    const prevOverride = current.decision_tag_override ?? null

    set(state => {
      const prev = state.resultsByPdf[pdfId]?.[sourceId]
      if (!prev) return state
      return {
        resultsByPdf: {
          ...state.resultsByPdf,
          [pdfId]: {
            ...state.resultsByPdf[pdfId],
            [sourceId]: { ...prev, decision_tag_override: next },
          },
        },
      }
    })

    try {
      await api.setDecisionOverride(pdfId, sourceId, next)
    } catch (e) {
      console.error('Failed to cycle decision tag:', e)
      set(state => {
        const prev = state.resultsByPdf[pdfId]?.[sourceId]
        if (!prev) return state
        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: {
              ...state.resultsByPdf[pdfId],
              [sourceId]: { ...prev, decision_tag_override: prevOverride },
            },
          },
        }
      })
    }
  },

  cancelAll: async () => {
    stopPolling()
    set(state => {
      const newResults = { ...state.resultsByPdf }
      const newSummaries = { ...state.summaries }
      const newProgress = { ...state.sourceProgress }
      const newPriorResults = { ...state.priorResultsByPdf }
      const nextCancelled = new Set(state.cancelledSourceIds)
      for (const pdfId of Object.keys(newResults)) {
        const pdfResults = { ...newResults[pdfId] }
        const pdfPriorSnapshot = { ...(state.priorResultsByPdf[pdfId] ?? {}) }
        let changed = false
        for (const sourceId of Object.keys(pdfResults)) {
          if (pdfResults[sourceId].status === 'in_progress') {
            const snapshot = pdfPriorSnapshot[sourceId]
            if (snapshot) {
              pdfResults[sourceId] = snapshot
              delete pdfPriorSnapshot[sourceId]
            } else {
              // First-time verify cancelled before this source produced any
              // result — drop the optimistic in_progress entry entirely so
              // the row reads as unverified, matching the disk state on
              // restart (the orchestrator never persisted an entry because
              // _verify_source never ran far enough). The previous 'low'
              // fallback rendered as low/fabricated due to the missing
              // decision_tag, which mis-represented sources the user
              // explicitly cancelled before they ever started.
              delete pdfResults[sourceId]
            }
            newProgress[sourceId] = { currentDb: null, checkedDbs: newProgress[sourceId]?.checkedDbs ?? [] }
            nextCancelled.add(sourceId)
            changed = true
          }
        }
        if (changed) {
          newResults[pdfId] = pdfResults
          newPriorResults[pdfId] = pdfPriorSnapshot
          let high = 0, medium = 0, low = 0
          for (const r of Object.values(pdfResults)) {
            if (r.status === 'high') high++
            else if (r.status === 'medium') medium++
            else if (r.status === 'low') low++
          }
          newSummaries[pdfId] = { pdf_id: pdfId, high, medium, low, in_progress: 0, total: Object.keys(pdfResults).length, completed: true }
        }
      }
      return {
        resultsByPdf: newResults,
        summaries: newSummaries,
        sourceProgress: newProgress,
        cancelledSourceIds: nextCancelled,
        priorResultsByPdf: newPriorResults,
      }
    })
    if (verifyBatch?.groupOpened) console.groupEnd()
    verifyBatch = null
    try { await api.cancelVerification() } catch (e) { console.error('Failed to cancel:', e) }
  },

  cancelPdf: async (pdfId) => {
    set(state => {
      const pdfResults = { ...(state.resultsByPdf[pdfId] ?? {}) }
      const pdfPriorSnapshot = { ...(state.priorResultsByPdf[pdfId] ?? {}) }
      const newProgress = { ...state.sourceProgress }
      const nextCancelled = new Set(state.cancelledSourceIds)
      let changed = false
      // Cancel every source belonging to this PDF, not only the ones that
      // are currently marked in_progress — a source whose `verify_started`
      // event has not yet arrived on the client side would otherwise still
      // flip to in_progress after the user hit Stop.
      const allSourceIds = state.sourceOrder[pdfId] ?? Object.keys(pdfResults)
      for (const sourceId of allSourceIds) {
        nextCancelled.add(sourceId)
        if (pdfResults[sourceId]?.status === 'in_progress') {
          // Restore the pre-rerun snapshot when one exists so the row colour
          // and decision tag match what they looked like before the re-run.
          // For first-time verifies (no snapshot) drop the optimistic entry
          // entirely so the row reads as unverified — see the matching
          // comment in cancelAll for why we no longer fall back to 'low'.
          const snapshot = pdfPriorSnapshot[sourceId]
          if (snapshot) {
            pdfResults[sourceId] = snapshot
            delete pdfPriorSnapshot[sourceId]
          } else {
            delete pdfResults[sourceId]
          }
          newProgress[sourceId] = { currentDb: null, checkedDbs: newProgress[sourceId]?.checkedDbs ?? [] }
          changed = true
        }
      }
      if (!changed) {
        return { cancelledSourceIds: nextCancelled }
      }
      let high = 0, medium = 0, low = 0
      for (const r of Object.values(pdfResults)) {
        if (r.status === 'high') high++
        else if (r.status === 'medium') medium++
        else if (r.status === 'low') low++
      }
      const newResults = { ...state.resultsByPdf, [pdfId]: pdfResults }
      const newSummaries = { ...state.summaries, [pdfId]: { pdf_id: pdfId, high, medium, low, in_progress: 0, total: Object.keys(pdfResults).length, completed: true } }
      // Stop polling if no more in-progress across all PDFs
      const anyStillRunning = Object.entries(newResults).some(([id, res]) =>
        id !== pdfId && Object.values(res).some(r => r.status === 'in_progress')
      )
      if (!anyStillRunning) stopPolling()
      return {
        resultsByPdf: newResults,
        summaries: newSummaries,
        sourceProgress: newProgress,
        cancelledSourceIds: nextCancelled,
        priorResultsByPdf: { ...state.priorResultsByPdf, [pdfId]: pdfPriorSnapshot },
      }
    })
    try { await api.cancelPdfVerification(pdfId) } catch (e) { console.error('Failed to cancel PDF:', e) }
  },

  cancelSource: async (sourceId) => {
    set(state => {
      const newResults = { ...state.resultsByPdf }
      const newSummaries = { ...state.summaries }
      const newProgress = { ...state.sourceProgress }
      const nextCancelled = new Set(state.cancelledSourceIds)
      nextCancelled.add(sourceId)
      let targetPdfId: string | null = null
      let nextPriorResults = state.priorResultsByPdf
      for (const pdfId of Object.keys(newResults)) {
        const pdfResults = newResults[pdfId]
        if (pdfResults[sourceId]?.status === 'in_progress') {
          // Restore from snapshot when available; otherwise drop the
          // optimistic entry entirely so the row reads as unverified
          // (see cancelAll comment for the rationale).
          const snapshot = state.priorResultsByPdf[pdfId]?.[sourceId]
          if (snapshot) {
            newResults[pdfId] = { ...pdfResults, [sourceId]: snapshot }
            const pdfPriorSnapshot = { ...(state.priorResultsByPdf[pdfId] ?? {}) }
            delete pdfPriorSnapshot[sourceId]
            nextPriorResults = { ...state.priorResultsByPdf, [pdfId]: pdfPriorSnapshot }
          } else {
            const { [sourceId]: _dropped, ...rest } = pdfResults
            newResults[pdfId] = rest
          }
          newProgress[sourceId] = { currentDb: null, checkedDbs: newProgress[sourceId]?.checkedDbs ?? [] }
          targetPdfId = pdfId
          break
        }
      }
      if (!targetPdfId) return { cancelledSourceIds: nextCancelled }
      let high = 0, medium = 0, low = 0
      for (const r of Object.values(newResults[targetPdfId])) {
        if (r.status === 'high') high++
        else if (r.status === 'medium') medium++
        else if (r.status === 'low') low++
      }
      newSummaries[targetPdfId] = { pdf_id: targetPdfId, high, medium, low, in_progress: Object.values(newResults[targetPdfId]).filter(r => r.status === 'in_progress').length, total: Object.keys(newResults[targetPdfId]).length, completed: Object.values(newResults[targetPdfId]).every(r => r.status !== 'in_progress') }
      return {
        resultsByPdf: newResults,
        summaries: newSummaries,
        sourceProgress: newProgress,
        cancelledSourceIds: nextCancelled,
        priorResultsByPdf: nextPriorResults,
      }
    })
    try { await api.cancelSourceVerification(sourceId) } catch (e) { console.error('Failed to cancel source:', e) }
  },

  loadResults: async (pdfId) => {
    // If a verification is in flight for this PDF, skip the cache load — the
    // live store (websocket events + optimistic in_progress entries from
    // startVerification) is the source of truth, and the backend cache can
    // only lag it. Without this skip, switching PDFs and switching back
    // mid-run would clobber live in_progress entries with a stale snapshot.
    if (get().summaries[pdfId]?.completed === false) return
    try {
      const response = await api.verifyResults(pdfId)
      // `in_progress` is a live-only state — by the time we're hydrating from
      // cache, no verification is actually running for these sources. Any
      // `in_progress` here is residue from a previous session that didn't
      // finish (force-quit / crash). Drop them so they fall back to the
      // "never searched" gray state, leaving the PDF as cleanly "partially
      // ran" rather than fake-pinned to running.
      const results: Record<string, VerificationResult> = {}
      for (const [sourceId, r] of Object.entries(response.results)) {
        if (r.status !== 'in_progress') results[sourceId] = r
      }
      const count = Object.keys(results).length
      if (count > 0) {
        console.log('%c[Loaded Verify Cache] %s (%d sources)', 'color: #60a5fa; font-weight: bold', pdfId, count)
        let high = 0, medium = 0, low = 0
        for (const r of Object.values(results)) {
          if (r.status === 'high') high++
          else if (r.status === 'medium') medium++
          else if (r.status === 'low') low++
        }
        set(state => ({
          resultsByPdf: { ...state.resultsByPdf, [pdfId]: results },
          summaries: {
            ...state.summaries,
            [pdfId]: {
              pdf_id: pdfId,
              high, medium, low,
              in_progress: 0,
              total: count,
              completed: true,
            },
          },
        }))
      } else {
        set(state => ({
          resultsByPdf: { ...state.resultsByPdf, [pdfId]: results },
        }))
      }
    } catch (e) {
      console.error('Failed to load results:', e)
    }
  },
}))

// Drop WS events whose `job_id` doesn't match the active run for this
// source/PDF — closes the cancel-then-reverify race where a late
// `verify_source_done` from the cancelled run would otherwise clobber the
// new run's optimistic state. Events without a `job_id` (manual overrides,
// score_scholar follow-ups, etc.) are always accepted.
function isStaleSourceEvent(data: Record<string, unknown>, sourceId: string): boolean {
  const jobId = typeof data.job_id === 'string' ? data.job_id : ''
  if (!jobId) return false
  const active = useVerificationStore.getState().activeJobIdBySource[sourceId]
  return Boolean(active && active !== jobId)
}

function isStalePdfEvent(data: Record<string, unknown>, pdfId: string): boolean {
  const jobId = typeof data.job_id === 'string' ? data.job_id : ''
  if (!jobId) return false
  const active = useVerificationStore.getState().activeJobIdByPdf[pdfId]
  return Boolean(active && active !== jobId)
}

export function initVerificationListeners(): () => void {
  const unsubs = [
    wsClient.on('verify_started', (data) => {
      const sourceId = data.source_id as string
      const pdfId = data.pdf_id as string

      if (isStaleSourceEvent(data, sourceId)) return

      // Drop late events for sources the user has already cancelled — the
      // backend task may still broadcast verify_started before the cancel
      // reaches it, and we must not reset a cancelled source back to
      // in_progress.
      if (useVerificationStore.getState().cancelledSourceIds.has(sourceId)) {
        return
      }

      // Buffer logging
      if (verifyBatch) {
        if (!verifyBatch.groupOpened) {
          console.groupCollapsed(`%c[Verify] ${verifyBatch.totalSources}`, 'color: #60a5fa; font-weight: bold')
          verifyBatch.groupOpened = true
        }
        if (!verifyBatch.pdfs.has(pdfId)) {
          verifyBatch.pdfs.set(pdfId, { pdfId, sources: new Map() })
        }
        verifyBatch.pdfs.get(pdfId)!.sources.set(sourceId, {
          sourceId,
          sourceText: (data.source_text as string) ?? '',
          dbChecks: [],
          finalStatus: null,
        })
      }

      useVerificationStore.setState(state => {
        const prev = state.resultsByPdf[pdfId]?.[sourceId]
        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: {
              ...(state.resultsByPdf[pdfId] ?? {}),
              [sourceId]: prev
                ? {
                    ...prev,
                    source_id: sourceId,
                    status: 'in_progress' as VerifyStatus,
                    all_results: [],
                    databases_searched: [],
                  }
                : {
                    source_id: sourceId,
                    status: 'in_progress' as VerifyStatus,
                    problem_tags: [],
                    url_liveness: {},
                    all_results: [],
                    databases_searched: [],
                  },
            },
          },
          sourceProgress: {
            ...state.sourceProgress,
            [sourceId]: { currentDb: null, checkedDbs: [] },
          },
        }
      })
    }),

    wsClient.on('verify_db_checking', (data) => {
      const sourceId = data.source_id as string
      const db = data.database as string

      if (isStaleSourceEvent(data, sourceId)) return

      // Skip late events for cancelled sources.
      if (useVerificationStore.getState().cancelledSourceIds.has(sourceId)) {
        return
      }

      useVerificationStore.setState(state => ({
        sourceProgress: {
          ...state.sourceProgress,
          [sourceId]: {
            ...(state.sourceProgress[sourceId] ?? { currentDb: null, checkedDbs: [] }),
            currentDb: db,
          },
        },
      }))
    }),

    wsClient.on('verify_db_checked', (data) => {
      const pdfId = data.pdf_id as string
      const sourceId = data.source_id as string
      const dbName = data.database as string
      const dbStatus = (data.db_status as DbCheckStatus) || (data.found ? 'high' : 'low')

      if (isStaleSourceEvent(data, sourceId)) return

      // Skip late events for cancelled sources.
      if (useVerificationStore.getState().cancelledSourceIds.has(sourceId)) {
        return
      }

      // Buffer logging
      if (verifyBatch) {
        const pdfLog = verifyBatch.pdfs.get(pdfId)
        const sourceLog = pdfLog?.sources.get(sourceId)
        if (sourceLog) {
          sourceLog.dbChecks.push({
            database: dbName,
            dbStatus,
            match: (data.match as Record<string, unknown>) ?? null,
            searchUrl: (data.search_url as string | undefined)
              ?? ((data.match as Record<string, unknown> | undefined)?.search_url as string | undefined)
              ?? '',
          })
        }
      }

      useVerificationStore.setState(state => {
        const prev = state.resultsByPdf[pdfId]?.[sourceId]
        const prog = state.sourceProgress[sourceId]
        const newEntry: DbCheckEntry = {
          name: dbName,
          status: dbStatus,
          searchUrl: (data.search_url as string)
            ?? ((data.match as Record<string, unknown> | undefined)?.search_url as string | undefined)
            ?? '',
          errorMessage: (data.error_message as string | undefined) ?? undefined,
        }

        return {
          resultsByPdf: prev
            ? {
                ...state.resultsByPdf,
                [pdfId]: {
                  ...state.resultsByPdf[pdfId],
                  [sourceId]: {
                    ...prev,
                    databases_searched: [...prev.databases_searched, dbName],
                    all_results: data.match ? [...prev.all_results, data.match as any] : prev.all_results,
                  },
                },
              }
            : state.resultsByPdf,
          sourceProgress: {
            ...state.sourceProgress,
            [sourceId]: {
              currentDb: null,
              checkedDbs: [...(prog?.checkedDbs ?? []), newEntry],
            },
          },
        }
      })
    }),

    wsClient.on('verify_source_done', (data) => {
      const pdfId = data.pdf_id as string
      const sourceId = data.source_id as string
      const status = data.status as string

      // Drop late events from a superseded run (cancel-then-reverify race).
      // Events without a `job_id` (manual override / score_scholar) skip
      // this filter and are always applied.
      if (isStaleSourceEvent(data, sourceId)) return

      // Cancel handlers already restored the correct UI state (snapshot for
      // re-runs; entry deleted for first-time verifies). The backend's late
      // verify_source_done from _finalize_result carries a partial-pass
      // scoring (typically low/fabricated) that is strictly worse, so ignore
      // it. Resolve the in-progress dot and bail. Don't remove sourceId from
      // cancelledSourceIds — startVerification / reverifySource / reverifyPdf
      // clear it themselves on the next run, and leaving it set means
      // subsequent late events for this run also drop.
      if (useVerificationStore.getState().cancelledSourceIds.has(sourceId)) {
        useVerificationStore.setState(state => ({
          sourceProgress: {
            ...state.sourceProgress,
            [sourceId]: {
              currentDb: null,
              checkedDbs: state.sourceProgress[sourceId]?.checkedDbs ?? [],
            },
          },
        }))
        return
      }

      // Buffer logging
      if (verifyBatch) {
        const pdfLog = verifyBatch.pdfs.get(pdfId)
        const sourceLog = pdfLog?.sources.get(sourceId)
        if (sourceLog) {
          sourceLog.finalStatus = status
        }
      }

      useVerificationStore.setState(state => {
        const existing = state.resultsByPdf[pdfId] ?? {}
        const updatedPdfResults = {
          ...existing,
          [sourceId]: {
            source_id: sourceId,
            status: data.status as VerifyStatus,
            problem_tags: (data.problem_tags as string[]) ?? [],
            decision_tag: (data.decision_tag as DecisionTag | undefined),
            decision_tag_override:
              (data.decision_tag_override as DecisionTag | null | undefined) ?? null,
            tag_overrides: (data.tag_overrides as Record<string, boolean> | undefined)
              ?? existing[sourceId]?.tag_overrides
              ?? {},
            url_liveness: (data.url_liveness as Record<string, boolean>) ?? {},
            best_match: data.best_match as any,
            all_results: (data.all_results as any[]) ?? [],
            databases_searched: (data.databases_searched as string[] | undefined)
              ?? existing[sourceId]?.databases_searched
              ?? [],
            scholar_url: (data.scholar_url as string | undefined) || existing[sourceId]?.scholar_url,
            google_url: (data.google_url as string | undefined) || existing[sourceId]?.google_url,
          },
        }
        // The source completed in the new run — its fresh result is now
        // authoritative, so the pre-rerun snapshot is no longer needed.
        const pdfPriorSnapshot = state.priorResultsByPdf[pdfId]
        let nextPriorResults = state.priorResultsByPdf
        if (pdfPriorSnapshot && sourceId in pdfPriorSnapshot) {
          const { [sourceId]: _dropped, ...rest } = pdfPriorSnapshot
          nextPriorResults = { ...state.priorResultsByPdf, [pdfId]: rest }
        }

        let high = 0, medium = 0, low = 0, inProgress = 0
        for (const r of Object.values(updatedPdfResults)) {
          if (r.status === 'high') high++
          else if (r.status === 'medium') medium++
          else if (r.status === 'low') low++
          else if (r.status === 'in_progress') inProgress++
        }
        const prevSummary = state.summaries[pdfId]
        const total = prevSummary?.total && prevSummary.total > 0
          ? prevSummary.total
          : Object.keys(updatedPdfResults).length

        return {
          resultsByPdf: {
            ...state.resultsByPdf,
            [pdfId]: updatedPdfResults,
          },
          sourceProgress: {
            ...state.sourceProgress,
            [sourceId]: {
              currentDb: null,
              checkedDbs: state.sourceProgress[sourceId]?.checkedDbs ?? [],
            },
          },
          summaries: {
            ...state.summaries,
            [pdfId]: {
              pdf_id: pdfId,
              high,
              medium,
              low,
              in_progress: inProgress,
              total,
              completed: inProgress === 0 && (prevSummary?.completed ?? false),
            },
          },
          priorResultsByPdf: nextPriorResults,
        }
      })
    }),

    wsClient.on('verify_pdf_done', (data) => {
      const pdfId = data.pdf_id as string

      if (isStalePdfEvent(data, pdfId)) return

      const high = (data.high as number | undefined) ?? 0
      const medium = (data.medium as number | undefined) ?? 0
      const low = (data.low as number | undefined) ?? 0
      const localInProgress = Object.values(
        useVerificationStore.getState().resultsByPdf[pdfId] ?? {},
      ).filter(result => result.status === 'in_progress').length

      useVerificationStore.setState(state => ({
        summaries: {
          ...state.summaries,
          [pdfId]: {
            pdf_id: pdfId,
            high,
            medium,
            low,
            in_progress: localInProgress,
            total: Math.max(high + medium + low, Object.keys(state.resultsByPdf[pdfId] ?? {}).length),
            completed: localInProgress === 0,
          },
        },
      }))

      if (localInProgress > 0) {
        // The PDF-level event can arrive even when an individual source event
        // was dropped during a WebSocket reconnect. Pull the authoritative
        // rows before completing the PDF so no card is stranded as running.
        void api.verifyResults(pdfId).then(response => {
          if (isStalePdfEvent(data, pdfId)) return
          const results = response.results
          const stillInProgress = Object.values(results)
            .filter(result => result.status === 'in_progress').length
          useVerificationStore.setState(state => ({
            resultsByPdf: { ...state.resultsByPdf, [pdfId]: results },
            summaries: {
              ...state.summaries,
              [pdfId]: {
                pdf_id: pdfId,
                high: Object.values(results).filter(r => r.status === 'high').length,
                medium: Object.values(results).filter(r => r.status === 'medium').length,
                low: Object.values(results).filter(r => r.status === 'low').length,
                in_progress: stillInProgress,
                total: Object.keys(results).length,
                completed: stillInProgress === 0,
              },
            },
          }))

          if (polling) {
            const state = useVerificationStore.getState()
            const allReconciled = polling.pdfIds.every(id =>
              state.summaries[id]?.completed
              && Object.values(state.resultsByPdf[id] ?? {})
                .every(result => result.status !== 'in_progress')
            )
            if (allReconciled) stopPolling()
          }
        }).catch(error => {
          // Leave polling active; its next tick will retry reconciliation.
          console.error(`[Verify] Failed to reconcile completed PDF ${pdfId}:`, error)
        })
      } else if (polling) {
        const state = useVerificationStore.getState()
        const allReconciled = polling.pdfIds.every(id =>
          state.summaries[id]?.completed
          && Object.values(state.resultsByPdf[id] ?? {})
            .every(result => result.status !== 'in_progress')
        )
        if (allReconciled) stopPolling()
      }

      // Flush buffered log for this PDF
      if (verifyBatch) {
        flushPdfVerifyLog(pdfId)
        verifyBatch.completedPdfs++
        if (verifyBatch.completedPdfs >= verifyBatch.expectedPdfCount) {
          if (verifyBatch.groupOpened) console.groupEnd() // close [Verify] group
          verifyBatch = null
        }
      }
    }),

    wsClient.on('verify_pdf_updated', (data) => {
      const pdfId = data.pdf_id as string
      if (isStalePdfEvent(data, pdfId)) return
      useVerificationStore.setState(state => {
        if (!state.summaries[pdfId]) return state
        const high = (data.high as number | undefined) ?? state.summaries[pdfId].high
        const medium = (data.medium as number | undefined) ?? state.summaries[pdfId].medium
        const low = (data.low as number | undefined) ?? state.summaries[pdfId].low
        return {
          summaries: {
            ...state.summaries,
            [pdfId]: {
              ...state.summaries[pdfId],
              high,
              medium,
              low,
              total: high + medium + low,
            },
          },
        }
      })

      // For verify (single source), also flush
      if (verifyBatch) {
        flushPdfVerifyLog(pdfId)
        verifyBatch.completedPdfs++
        if (verifyBatch.completedPdfs >= verifyBatch.expectedPdfCount) {
          if (verifyBatch.groupOpened) console.groupEnd()
          verifyBatch = null
        }
      }
    }),
  ]

  return () => {
    unsubs.forEach(fn => fn())
    stopPolling()
  }
}

export function clearVerificationForPdf(pdfId: string): void {
  if (polling) {
    polling.pdfIds = polling.pdfIds.filter(id => id !== pdfId)
    if (polling.pdfIds.length === 0) {
      stopPolling()
    }
  }

  useVerificationStore.setState(state => {
    const sourceIds = state.sourceOrder[pdfId] ?? []

    const resultsByPdf = { ...state.resultsByPdf }
    const summaries = { ...state.summaries }
    const sourceOrder = { ...state.sourceOrder }
    const sourceProgress = { ...state.sourceProgress }
    const verifyTexts = { ...state.verifyTexts }
    const sourceOriginalTexts = { ...state.sourceOriginalTexts }
    const enabledSources = { ...state.enabledSources }

    delete resultsByPdf[pdfId]
    delete summaries[pdfId]
    delete sourceOrder[pdfId]

    for (const sourceId of sourceIds) {
      delete sourceProgress[sourceId]
      delete verifyTexts[sourceId]
      delete sourceOriginalTexts[sourceId]
      delete enabledSources[sourceId]
    }

    const shouldClearSelection =
      state.selectedSourceId !== null && sourceIds.includes(state.selectedSourceId)

    return {
      resultsByPdf,
      summaries,
      sourceOrder,
      sourceProgress,
      verifyTexts,
      sourceOriginalTexts,
      enabledSources,
      selectedSourceId: shouldClearSelection ? null : state.selectedSourceId,
    }
  })
}

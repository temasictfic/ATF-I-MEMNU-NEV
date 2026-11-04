// Client-side notes store. Notes are user-authored annotations (highlights
// over text regions, free-text callouts) that can be exported into a copy of
// the original PDF via the pdf-lib annotation writer.
//
// Persistence is in-memory only for now — we can add a backend JSON sidecar
// later once the core UX is working.

import { create } from 'zustand'

import type { DecisionTag } from '../api/types'

export type NoteKind = 'highlight' | 'callout'

// Subset of DecisionTag that the per-decision-tag auto-annotate buttons emit.
// 'valid' never produces auto notes — only flagged sources do.
export type AutoDecisionTag = Extract<DecisionTag, 'fabricated' | 'citation'>

export interface NoteQuad {
  // A single highlight rectangle in page-local pixel coordinates (SCALE space),
  // top-left origin. Multiple quads allow multi-line text selections.
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface Note {
  id: string
  pdfId: string
  // 0-indexed page number, matching SourceRectangle.bbox.page
  pageNum: number
  kind: NoteKind
  // Overall bbox (union of quads for highlights, or the drawn rect for callouts)
  bbox: NoteQuad
  // For multi-line highlights — one entry per selection rect. When absent, the
  // exporter falls back to `bbox` as a single-quad highlight.
  quads?: NoteQuad[]
  // Body text: optional comment for highlights, visible text for callouts.
  // May contain `\n` for multi-line callouts.
  text: string
  // Hex color (e.g. "#fde68a"). Applied to both the annotation appearance and
  // the in-app overlay.
  color: string
  // Callout-only typography. Highlights ignore these.
  fontSize?: number // defaults to DEFAULT_CALLOUT_FONT_SIZE
  bold?: boolean // defaults to false
  textColor?: string // defaults to DEFAULT_CALLOUT_TEXT_COLOR
  // Per-callout background alpha (0..1). Seeded from `calloutOpacity` at
  // creation; editing the slider with a callout selected updates just
  // that note. Highlights ignore this field.
  opacity?: number
  // Set when the note was produced by the auto-annotate action for a specific
  // SourceRectangle. Used to dedupe on re-run so clicking auto-annotate
  // multiple times doesn't stack duplicate markup.
  autoForSourceId?: string
  // Which decision-tag button produced this note. Lets the per-category auto
  // remover wipe its own batch without touching the other category's notes.
  // Absent on the shared title callout and on manual notes.
  autoDecisionTag?: AutoDecisionTag
  createdAt: number
}

export const DEFAULT_CALLOUT_FONT_SIZE = 11
export const CALLOUT_FONT_SIZE_MIN = 6
export const CALLOUT_FONT_SIZE_MAX = 48
export const DEFAULT_CALLOUT_TEXT_COLOR = '#13151f'
export const DEFAULT_HIGHLIGHT_COLOR = '#fde68a'
export const DEFAULT_CALLOUT_COLOR = '#fca5a5'

// Default callout background alpha (0..1). User-adjustable via the Notes panel.
export const DEFAULT_CALLOUT_OPACITY = 1.0

interface NotesState {
  notesByPdf: Record<string, Note[]>
  // Undo history: stack of snapshots of `notesByPdf[pdfId]` captured *before*
  // each undoable user action. `revertNotes(pdfId)` pops the top entry and
  // restores it. Deep-copied on capture so later mutations don't mutate
  // previously-captured snapshots.
  notesHistoryByPdf: Record<string, Note[][]>
  activeKind: NoteKind | null
  // Mirror of the active kind's color (highlightColor when activeKind is
  // 'highlight', calloutColor when 'callout'). Lets the drag-preview
  // renderer pick the right color without switching on activeKind itself.
  activeColor: string
  // Per-kind persisted user choices. `setActiveColor` updates whichever
  // one matches `activeKind`. Manual note creation and auto-annotate
  // read these so the user's picks apply uniformly.
  highlightColor: string
  calloutColor: string
  calloutTextColor: string
  calloutFontSize: number
  calloutBold: boolean
  calloutOpacity: number
}

const MAX_NOTES_HISTORY = 100

function cloneNotes(notes: Note[]): Note[] {
  return notes.map(n => ({
    ...n,
    bbox: { ...n.bbox },
    quads: n.quads ? n.quads.map(q => ({ ...q })) : undefined,
  }))
}

export const useNotesStore = create<NotesState>()(() => ({
  notesByPdf: {},
  notesHistoryByPdf: {},
  activeKind: null,
  activeColor: DEFAULT_HIGHLIGHT_COLOR,
  highlightColor: DEFAULT_HIGHLIGHT_COLOR,
  calloutColor: DEFAULT_CALLOUT_COLOR,
  calloutTextColor: DEFAULT_CALLOUT_TEXT_COLOR,
  calloutFontSize: DEFAULT_CALLOUT_FONT_SIZE,
  calloutBold: false,
  calloutOpacity: DEFAULT_CALLOUT_OPACITY,
}))

// Capture the current notes list for `pdfId` into the undo history. Must
// be called BEFORE any undoable mutation (add/update/remove/reset/etc.).
// The store's mutators do not auto-snapshot — each caller decides the
// granularity of an "undo step".
export function beginNoteEdit(pdfId: string): void {
  useNotesStore.setState(state => {
    const current = state.notesByPdf[pdfId] ?? []
    const snapshot = cloneNotes(current)
    const existing = state.notesHistoryByPdf[pdfId] ?? []
    const next = [...existing, snapshot]
    // Bound history length so long editing sessions don't leak memory.
    if (next.length > MAX_NOTES_HISTORY) next.splice(0, next.length - MAX_NOTES_HISTORY)
    return {
      notesHistoryByPdf: { ...state.notesHistoryByPdf, [pdfId]: next },
    }
  })
}

// Undo the last beginNoteEdit snapshot: pops the history stack and restores
// the previous notes state for the PDF.
export function revertNotes(pdfId: string): void {
  useNotesStore.setState(state => {
    const hist = state.notesHistoryByPdf[pdfId]
    if (!hist || hist.length === 0) return state
    const prev = hist[hist.length - 1]
    const nextHist = hist.slice(0, -1)
    return {
      notesByPdf: { ...state.notesByPdf, [pdfId]: prev },
      notesHistoryByPdf: { ...state.notesHistoryByPdf, [pdfId]: nextHist },
    }
  })
}

export function canRevertNotes(pdfId: string): boolean {
  return (useNotesStore.getState().notesHistoryByPdf[pdfId]?.length ?? 0) > 0
}

// Clear all notes for a PDF, pushing a snapshot first so it can be undone.
export function resetNotes(pdfId: string): void {
  const list = useNotesStore.getState().notesByPdf[pdfId]
  if (!list || list.length === 0) return
  beginNoteEdit(pdfId)
  useNotesStore.setState(state => ({
    notesByPdf: { ...state.notesByPdf, [pdfId]: [] },
  }))
}

export function getNotes(pdfId: string): Note[] {
  return useNotesStore.getState().notesByPdf[pdfId] ?? []
}

export function addNote(note: Omit<Note, 'id' | 'createdAt'>): Note {
  const full: Note = {
    ...note,
    id: `note_${note.pdfId}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
    createdAt: Date.now(),
  }
  useNotesStore.setState(state => {
    const list = state.notesByPdf[full.pdfId] ?? []
    return {
      notesByPdf: { ...state.notesByPdf, [full.pdfId]: [...list, full] },
    }
  })
  return full
}

export function updateNote(pdfId: string, noteId: string, patch: Partial<Note>): void {
  useNotesStore.setState(state => {
    const list = state.notesByPdf[pdfId]
    if (!list) return state
    return {
      notesByPdf: {
        ...state.notesByPdf,
        [pdfId]: list.map(n => (n.id === noteId ? { ...n, ...patch } : n)),
      },
    }
  })
}

export function removeNote(pdfId: string, noteId: string): void {
  useNotesStore.setState(state => {
    const list = state.notesByPdf[pdfId]
    if (!list) return state
    return {
      notesByPdf: {
        ...state.notesByPdf,
        [pdfId]: list.filter(n => n.id !== noteId),
      },
    }
  })
}

export function clearNotesForPdf(pdfId: string): void {
  useNotesStore.setState(state => {
    const { [pdfId]: _dropped, ...rest } = state.notesByPdf
    return { notesByPdf: rest }
  })
}

// Drop every auto-generated note for a PDF. Used by the auto-annotate action
// so re-running it replaces the previous batch instead of stacking duplicates.
export function removeAutoNotesForPdf(pdfId: string): void {
  useNotesStore.setState(state => {
    const list = state.notesByPdf[pdfId]
    if (!list) return state
    const filtered = list.filter(n => !n.autoForSourceId)
    if (filtered.length === list.length) return state
    return {
      notesByPdf: { ...state.notesByPdf, [pdfId]: filtered },
    }
  })
}

// Drop only the auto-generated notes belonging to one decision-tag category.
// Lets the two per-category buttons re-run independently without wiping each
// other. The shared title callout is managed separately by auto-notes.ts so
// clicking a category that has zero matching refs doesn't delete the title
// stamped by the other category.
export function removeAutoNotesForPdfByDecisionTag(
  pdfId: string,
  decisionTag: AutoDecisionTag,
): void {
  useNotesStore.setState(state => {
    const list = state.notesByPdf[pdfId]
    if (!list) return state
    const filtered = list.filter(n => n.autoDecisionTag !== decisionTag)
    if (filtered.length === list.length) return state
    return {
      notesByPdf: { ...state.notesByPdf, [pdfId]: filtered },
    }
  })
}

export function setActiveKind(kind: NoteKind | null): void {
  useNotesStore.setState(state => {
    if (kind === null) return { activeKind: null }
    // Switch the "active color" mirror to whatever the user last chose
    // for this kind. Each kind has its own persisted color; switching
    // tools no longer forgets it.
    const nextColor =
      kind === 'callout' ? state.calloutColor : state.highlightColor
    return { activeKind: kind, activeColor: nextColor }
  })
}

// Update the color for the currently-active kind. If no tool is selected
// yet, default to editing the highlight color.
export function setActiveColor(color: string): void {
  useNotesStore.setState(state => {
    if (state.activeKind === 'callout') {
      return { activeColor: color, calloutColor: color }
    }
    return { activeColor: color, highlightColor: color }
  })
}

export function setHighlightColor(color: string): void {
  useNotesStore.setState(state => ({
    highlightColor: color,
    activeColor: state.activeKind === 'callout' ? state.activeColor : color,
  }))
}

export function setCalloutColor(color: string): void {
  useNotesStore.setState(state => ({
    calloutColor: color,
    activeColor: state.activeKind === 'callout' ? color : state.activeColor,
  }))
}

export function setCalloutTextColor(color: string): void {
  useNotesStore.setState({ calloutTextColor: color })
}

export function setCalloutFontSize(size: number): void {
  const clamped = Math.max(
    CALLOUT_FONT_SIZE_MIN,
    Math.min(CALLOUT_FONT_SIZE_MAX, Math.round(size) || DEFAULT_CALLOUT_FONT_SIZE),
  )
  useNotesStore.setState({ calloutFontSize: clamped })
}

export function setCalloutBold(bold: boolean): void {
  useNotesStore.setState({ calloutBold: bold })
}

export function setCalloutOpacity(opacity: number): void {
  const clamped = Math.max(0, Math.min(1, opacity))
  useNotesStore.setState({ calloutOpacity: clamped })
}

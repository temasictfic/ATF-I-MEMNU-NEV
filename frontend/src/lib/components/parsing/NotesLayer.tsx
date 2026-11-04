// Renders notes (highlights + callouts) over a single PDF page, parallel to
// the existing source-rectangle overlay. Also owns the draw-rect gesture for
// callout creation; highlight creation lives in ParsingPage because selections
// can span pages.
//
// Callouts are interactive: once in any Notes mode, clicking a callout selects
// it, dragging the body moves it, and dragging one of 8 handles resizes it.
// Highlights remain passive markings (click to select only).

import { useEffect, useRef, useState } from 'react'
import type { Note, NoteKind, NoteQuad } from '../../stores/notes-store'
import {
  DEFAULT_CALLOUT_FONT_SIZE,
  DEFAULT_CALLOUT_OPACITY,
} from '../../stores/notes-store'
import { SCALE } from '../../pdf/types'
import styles from './ParsingPage.module.css'

// Outline color shared by selected highlights and selected callouts.
const SELECTED_OUTLINE = '#1f2937'

interface Props {
  notes: Note[]
  scale: number
  activeKind: NoteKind | null
  activeColor: string
  // Width/height of the page at natural (SCALE) pixel resolution, before the
  // user zoom factor. Used to clamp callout draws.
  pageWidth: number
  pageHeight: number
  onCreateCallout: (bbox: { x0: number; y0: number; x1: number; y1: number }) => void
  onCreateHighlight: (bbox: { x0: number; y0: number; x1: number; y1: number }) => void
  onSelectNote: (noteId: string) => void
  selectedNoteId: string | null
  onUpdateNoteBbox: (noteId: string, bbox: NoteQuad) => void
  onMoveNoteToPage: (noteId: string, pageNum: number, bbox: NoteQuad) => void
  onBeginNoteEdit: () => void
}

interface DrawState {
  startX: number
  startY: number
  currentX: number
  currentY: number
}

const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const
type Handle = (typeof HANDLES)[number]

// Minimum on-screen size (SCALE pixels) for a callout. Matches the creation
// threshold in onMouseUp so drag-resizing can't shrink a callout below the
// size it could have been drawn at.
const MIN_CALLOUT_SIZE_PX = 8

export function NotesLayer({
  notes,
  scale,
  activeKind,
  activeColor,
  pageWidth,
  pageHeight,
  onCreateCallout,
  onCreateHighlight,
  onSelectNote,
  selectedNoteId,
  onUpdateNoteBbox,
  onMoveNoteToPage,
  onBeginNoteEdit,
}: Props) {
  const [drawing, setDrawing] = useState<DrawState | null>(null)
  const containerRef = useRef<HTMLDivElement | null>(null)

  const isDrawKind = activeKind === 'callout' || activeKind === 'highlight'

  const onMouseDown = (e: React.MouseEvent) => {
    if (!isDrawKind) return
    if (e.button !== 0) return
    const container = containerRef.current
    if (!container) return
    e.preventDefault()
    e.stopPropagation()
    const rect = container.getBoundingClientRect()
    const x = (e.clientX - rect.left) / scale
    const y = (e.clientY - rect.top) / scale
    setDrawing({ startX: x, startY: y, currentX: x, currentY: y })
  }

  const onMouseMove = (e: React.MouseEvent) => {
    if (!drawing) return
    const container = containerRef.current
    if (!container) return
    const rect = container.getBoundingClientRect()
    const x = Math.max(0, Math.min(pageWidth, (e.clientX - rect.left) / scale))
    const y = Math.max(0, Math.min(pageHeight, (e.clientY - rect.top) / scale))
    setDrawing({ ...drawing, currentX: x, currentY: y })
  }

  const onMouseUp = () => {
    if (!drawing) return
    const x0 = Math.min(drawing.startX, drawing.currentX)
    const y0 = Math.min(drawing.startY, drawing.currentY)
    const x1 = Math.max(drawing.startX, drawing.currentX)
    const y1 = Math.max(drawing.startY, drawing.currentY)
    setDrawing(null)
    // Minimum area; a missed click shouldn't spawn a note.
    if (x1 - x0 < MIN_CALLOUT_SIZE_PX || y1 - y0 < MIN_CALLOUT_SIZE_PX) return
    if (activeKind === 'highlight') {
      onCreateHighlight({ x0, y0, x1, y1 })
    } else {
      onCreateCallout({ x0, y0, x1, y1 })
    }
  }

  const showDrawPreview = drawing !== null
  const drawStyle = drawing
    ? {
        left: Math.min(drawing.startX, drawing.currentX) * scale,
        top: Math.min(drawing.startY, drawing.currentY) * scale,
        width: Math.abs(drawing.currentX - drawing.startX) * scale,
        height: Math.abs(drawing.currentY - drawing.startY) * scale,
      }
    : undefined

  // In both highlight and callout modes the layer captures pointer events so
  // it can own the draw-rect gesture. Outside those modes it stays click-
  // through; only the per-callout elements (below) opt in via pointer events
  // so they can still be selected/dragged.
  const layerPointerEvents = isDrawKind ? 'auto' : 'none'
  const interactiveCallouts = activeKind !== null

  return (
    <div
      ref={containerRef}
      className={styles['notes-layer']}
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: layerPointerEvents,
        cursor: isDrawKind ? 'crosshair' : 'default',
      }}
      onMouseDown={onMouseDown}
      onMouseMove={onMouseMove}
      onMouseUp={onMouseUp}
      onMouseLeave={() => setDrawing(null)}
    >
      {/* Render highlights first, callouts second: later DOM nodes sit on
          top, so overlapping click targets resolve to the callout first —
          which is what the user wants because callouts usually need to
          be moved/edited, highlights are static markers. */}
      {[...notes]
        .sort((a, b) => {
          if (a.kind === b.kind) return 0
          return a.kind === 'highlight' ? -1 : 1
        })
        .map(note =>
          note.kind === 'highlight' ? (
            <HighlightBox
              key={note.id}
              note={note}
              scale={scale}
              selected={note.id === selectedNoteId}
              onSelect={onSelectNote}
            />
          ) : (
            <CalloutBox
              key={note.id}
              note={note}
              scale={scale}
              pageWidth={pageWidth}
              pageHeight={pageHeight}
              selected={note.id === selectedNoteId}
              interactive={interactiveCallouts}
              opacity={note.opacity ?? DEFAULT_CALLOUT_OPACITY}
              onSelect={onSelectNote}
              onUpdateBbox={onUpdateNoteBbox}
              onMoveToPage={onMoveNoteToPage}
              onBeginEdit={onBeginNoteEdit}
              containerRef={containerRef}
            />
          ),
      )}
      {showDrawPreview && drawStyle && (
        <div
          className={styles['notes-draw-preview']}
          style={{
            position: 'absolute',
            ...drawStyle,
            border: `2px dashed ${activeColor}`,
            background: `${activeColor}40`,
            pointerEvents: 'none',
          }}
        />
      )}
    </div>
  )
}

interface HighlightBoxProps {
  note: Note
  scale: number
  selected: boolean
  onSelect: (id: string) => void
}

function HighlightBox({ note, scale, selected, onSelect }: HighlightBoxProps) {
  const quads = note.quads && note.quads.length > 0 ? note.quads : [note.bbox]
  return (
    <span title={note.text || '(highlight)'}>
      {quads.map((q, i) => (
        <div
          key={`${note.id}_${i}`}
          data-note-id={note.id}
          style={{
            position: 'absolute',
            left: q.x0 * scale,
            top: q.y0 * scale,
            width: (q.x1 - q.x0) * scale,
            height: (q.y1 - q.y0) * scale,
            background: note.color,
            opacity: 0.4,
            mixBlendMode: 'multiply',
            outline: selected ? `1px solid ${SELECTED_OUTLINE}` : undefined,
            pointerEvents: 'auto',
          }}
          onMouseDown={e => {
            e.stopPropagation()
            onSelect(note.id)
          }}
        />
      ))}
    </span>
  )
}

interface CalloutBoxProps {
  note: Note
  scale: number
  pageWidth: number
  pageHeight: number
  selected: boolean
  interactive: boolean
  opacity: number
  onSelect: (id: string) => void
  onUpdateBbox: (noteId: string, bbox: NoteQuad) => void
  onMoveToPage: (noteId: string, pageNum: number, bbox: NoteQuad) => void
  onBeginEdit: () => void
  containerRef: React.RefObject<HTMLDivElement | null>
}

interface DragState {
  mode: 'move' | 'resize'
  handle?: Handle
  startClientX: number
  startClientY: number
  origBbox: NoteQuad
}

function CalloutBox({
  note,
  scale,
  pageWidth,
  pageHeight,
  selected,
  interactive,
  opacity,
  onSelect,
  onUpdateBbox,
  onMoveToPage,
  onBeginEdit,
  containerRef,
}: CalloutBoxProps) {
  // Drag/resize state and latest props live on refs so the window-level
  // mousemove/mouseup listeners always see fresh values without re-binding.
  const dragRef = useRef<DragState | null>(null)
  const latestRef = useRef({
    note,
    scale,
    pageWidth,
    pageHeight,
    onUpdateBbox,
    onMoveToPage,
    containerRef,
  })
  latestRef.current = {
    note,
    scale,
    pageWidth,
    pageHeight,
    onUpdateBbox,
    onMoveToPage,
    containerRef,
  }

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const state = dragRef.current
      if (!state) return
      const { scale: s, pageWidth: pw, pageHeight: ph } = latestRef.current
      const dxPx = (e.clientX - state.startClientX) / s
      const dyPx = (e.clientY - state.startClientY) / s
      const ob = state.origBbox

      let next: NoteQuad
      if (state.mode === 'move') {
        const width = ob.x1 - ob.x0
        const height = ob.y1 - ob.y0
        // Don't clamp during a move — the callout needs to visually
        // follow the cursor even when dragged past the current page's
        // edge, otherwise it looks like it's stuck at the boundary. The
        // per-page container has no overflow clip, so rendering outside
        // the page box is fine. On mouseup the hit-test below teleports
        // the note to whichever page the cursor actually landed on.
        const x0 = ob.x0 + dxPx
        const y0 = ob.y0 + dyPx
        next = { x0, y0, x1: x0 + width, y1: y0 + height }
      } else {
        const h = state.handle ?? 'se'
        let { x0, y0, x1, y1 } = ob
        if (h.includes('w')) x0 = Math.min(ob.x0 + dxPx, x1 - MIN_CALLOUT_SIZE_PX)
        if (h.includes('e')) x1 = Math.max(ob.x1 + dxPx, x0 + MIN_CALLOUT_SIZE_PX)
        if (h.includes('n')) y0 = Math.min(ob.y0 + dyPx, y1 - MIN_CALLOUT_SIZE_PX)
        if (h.includes('s')) y1 = Math.max(ob.y1 + dyPx, y0 + MIN_CALLOUT_SIZE_PX)
        // Clamp to page bounds.
        x0 = Math.max(0, x0)
        y0 = Math.max(0, y0)
        x1 = Math.min(pw, x1)
        y1 = Math.min(ph, y1)
        next = { x0, y0, x1, y1 }
      }
      latestRef.current.onUpdateBbox(latestRef.current.note.id, next)
    }
    const onUp = (e: MouseEvent) => {
      const state = dragRef.current
      dragRef.current = null
      if (!state || state.mode !== 'move') return
      // Cross-page teleport: if the cursor is released over a page other
      // than the one this callout belongs to, move the note onto that page
      // and recompute its bbox in the target page's coordinate space.
      const targetEl = document
        .elementFromPoint(e.clientX, e.clientY)
        ?.closest('[data-page-num]') as HTMLElement | null
      if (!targetEl) return
      const targetPageNum = Number(targetEl.dataset.pageNum)
      if (!Number.isFinite(targetPageNum)) return
      const {
        note: latestNote,
        scale: s,
        onMoveToPage: moveCb,
      } = latestRef.current
      if (targetPageNum === latestNote.pageNum) return
      const rect = targetEl.getBoundingClientRect()
      const width = latestNote.bbox.x1 - latestNote.bbox.x0
      const height = latestNote.bbox.y1 - latestNote.bbox.y0
      // Anchor the moved callout so the cursor sits roughly where it was
      // grabbed relative to the callout body — approximate by centering.
      let x0 = (e.clientX - rect.left) / s - width / 2
      let y0 = (e.clientY - rect.top) / s - height / 2
      const targetW = rect.width / s
      const targetH = rect.height / s
      x0 = Math.max(0, Math.min(x0, targetW - width))
      y0 = Math.max(0, Math.min(y0, targetH - height))
      moveCb(latestNote.id, targetPageNum, {
        x0,
        y0,
        x1: x0 + width,
        y1: y0 + height,
      })
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
  }, [])

  const startGesture = (
    e: React.MouseEvent,
    mode: 'move' | 'resize',
    handle?: Handle
  ) => {
    if (e.button !== 0) return
    e.preventDefault()
    e.stopPropagation()
    onSelect(note.id)
    // Snapshot pre-drag state so the whole move/resize is one undo step.
    onBeginEdit()
    dragRef.current = {
      mode,
      handle,
      startClientX: e.clientX,
      startClientY: e.clientY,
      origBbox: { ...note.bbox },
    }
  }

  // Match the exporter's visual: font size is in PDF points, and the PDF is
  // rendered at SCALE pixels-per-point, so the on-screen size has to account
  // for SCALE and the user's zoom factor to line up with what pdf-lib draws.
  const fontSizePt = note.fontSize ?? DEFAULT_CALLOUT_FONT_SIZE
  const fontSizePx = fontSizePt * SCALE * scale
  const alphaHex = Math.round(Math.max(0, Math.min(1, opacity)) * 255)
    .toString(16)
    .padStart(2, '0')
    .toUpperCase()

  return (
    <div
      data-note-id={note.id}
      title={note.text || '(callout)'}
      style={{
        position: 'absolute',
        left: note.bbox.x0 * scale,
        top: note.bbox.y0 * scale,
        width: (note.bbox.x1 - note.bbox.x0) * scale,
        height: (note.bbox.y1 - note.bbox.y0) * scale,
        background: `${note.color}${alphaHex}`,
        border: `1px solid ${note.color}`,
        borderRadius: 4,
        padding: 4 * scale,
        fontSize: fontSizePx,
        fontFamily: 'Helvetica, Arial, sans-serif',
        fontWeight: note.bold ? 700 : 400,
        lineHeight: 1.2,
        color: note.textColor ?? '#13151f',
        overflow: 'hidden',
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
        outline: selected ? `2px solid ${SELECTED_OUTLINE}` : undefined,
        pointerEvents: interactive ? 'auto' : 'none',
        cursor: interactive ? 'move' : 'default',
        userSelect: 'none',
      }}
      onMouseDown={e => {
        if (!interactive) return
        startGesture(e, 'move')
      }}
    >
      {note.text || ''}
      {selected && interactive &&
        HANDLES.map(h => (
          <div
            key={h}
            className={`${styles['resize-handle']} ${styles[`rh-${h}`]}`}
            onMouseDown={e => startGesture(e, 'resize', h)}
          />
        ))}
    </div>
  )
}

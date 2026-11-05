// pdf-lib-based writer that exports notes into a PDF copy.
//
// Highlights are written as real `/Subtype /Highlight` annotations sitting on
// top of the original text layer — clicking them in Acrobat behaves like a
// native highlight and the underlying text stays selectable.
//
// Callouts are drawn directly into each page's content stream via
// `page.drawRectangle` + `page.drawText`. This differs from the highlight
// approach because FreeText annotations require an `/AP` appearance stream
// to render their text in most viewers (pdf-lib does not generate one, so
// `/Subtype /FreeText` callouts used to show as empty coloured boxes).
// Drawing into the page content stream renders reliably in every viewer,
// supports embedded fonts, adjustable size, bold, and newline-separated
// multi-line text, at the cost of the callout being a permanent part of the
// exported page rather than a togglable annotation. For the
// "export annotated PDF" use case this is the desired behaviour.
//
// Coordinate systems:
//   - Notes are stored in PIXEL coordinates at SCALE = 150/72 with a top-left
//     origin (matching the rest of the app — SourceRectangle bboxes use the
//     same space).
//   - PDF user space has a bottom-left origin and uses points. Conversion goes
//     through `makePageFrame` (page-frame.ts), the exact inverse of pdf.js's
//     viewport, so pages with a /Rotate or a MediaBox/CropBox that doesn't
//     start at (0, 0) are handled. The old `page_height_pt - y` flip misplaced
//     notes on those pages.

import {
  PDFDocument,
  PDFName,
  degrees,
  rgb,
  type PDFFont,
  type RGB,
} from 'pdf-lib'
import fontkit from '@pdf-lib/fontkit'
import type { Note, NoteQuad } from '../stores/notes-store'
import { DEFAULT_CALLOUT_FONT_SIZE, DEFAULT_CALLOUT_OPACITY } from '../stores/notes-store'
import { SCALE } from './types'
import { getLiberationFontBytes } from './fonts'
import { makePageFrame, visibleBox, type PageFrame } from './page-frame'

function hexToRgb(hex: string): RGB {
  const clean = hex.replace('#', '').trim()
  if (clean.length === 3) {
    const r = parseInt(clean[0] + clean[0], 16)
    const g = parseInt(clean[1] + clean[1], 16)
    const b = parseInt(clean[2] + clean[2], 16)
    return rgb(r / 255, g / 255, b / 255)
  }
  if (clean.length === 6) {
    const r = parseInt(clean.slice(0, 2), 16)
    const g = parseInt(clean.slice(2, 4), 16)
    const b = parseInt(clean.slice(4, 6), 16)
    if ([r, g, b].every(n => !Number.isNaN(n))) {
      return rgb(r / 255, g / 255, b / 255)
    }
  }
  return rgb(1, 0.87, 0.52) // #fde68a fallback
}

interface PdfRect {
  x0: number
  y0: number
  x1: number
  y1: number
}

// The four corners of a pixel-space quad (as the viewer sees it: top-left,
// top-right, bottom-left, bottom-right) in PDF user space.
function quadCorners(quad: NoteQuad, frame: PageFrame) {
  const xl = Math.min(quad.x0, quad.x1)
  const xr = Math.max(quad.x0, quad.x1)
  const yt = Math.min(quad.y0, quad.y1)
  const yb = Math.max(quad.y0, quad.y1)
  return {
    tl: frame.toPdf(xl, yt),
    tr: frame.toPdf(xr, yt),
    bl: frame.toPdf(xl, yb),
    br: frame.toPdf(xr, yb),
  }
}

// Axis-aligned PDF-space rect covering a pixel-space quad.
function pixelQuadToPdfRect(quad: NoteQuad, frame: PageFrame): PdfRect {
  const { tl, tr, bl, br } = quadCorners(quad, frame)
  const xs = [tl.x, tr.x, bl.x, br.x]
  const ys = [tl.y, tr.y, bl.y, br.y]
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) }
}

function frameForPage(page: PDFPage): PageFrame {
  const media = page.getMediaBox()
  const crop = page.getCropBox()
  const view = visibleBox(
    { x0: media.x, y0: media.y, x1: media.x + media.width, y1: media.y + media.height },
    { x0: crop.x, y0: crop.y, x1: crop.x + crop.width, y1: crop.y + crop.height },
  )
  return makePageFrame(view, page.getRotation().angle, SCALE)
}

/**
 * Load a PDF, write all given notes (highlights as annotations + callouts as
 * drawn content), return the serialized bytes. The original file on disk is
 * not modified — callers control where to write.
 */
export async function writeNotesToPdf(
  originalBytes: Uint8Array,
  notes: Note[]
): Promise<Uint8Array> {
  const pdfDoc = await PDFDocument.load(originalBytes, {
    ignoreEncryption: true,
    updateMetadata: false,
  })
  const { context } = pdfDoc

  // Embed Liberation Sans (Unicode, full Latin Extended coverage) via
  // fontkit. The standard Helvetica base fonts are WinAnsi-only and cannot
  // encode characters like Turkish ş/ı/ğ/ü/ö/ç. fontkit handles subsetting
  // so only the glyphs actually used end up in the output PDF.
  pdfDoc.registerFontkit(fontkit)
  const { regular, bold } = await getLiberationFontBytes()
  const helvetica = await pdfDoc.embedFont(regular, { subset: true })
  const helveticaBold = await pdfDoc.embedFont(bold, { subset: true })

  // Group notes by page so we touch each page once.
  const notesByPage = new Map<number, Note[]>()
  for (const note of notes) {
    const list = notesByPage.get(note.pageNum) ?? []
    list.push(note)
    notesByPage.set(note.pageNum, list)
  }

  const pages = pdfDoc.getPages()
  for (const [pageNum, pageNotes] of notesByPage) {
    if (pageNum < 0 || pageNum >= pages.length) continue
    const page = pages[pageNum]
    const frame = frameForPage(page)

    // Highlights: build annotation dict refs and append to the page /Annots.
    const highlightRefs: ReturnType<typeof context.register>[] = []
    for (const note of pageNotes) {
      if (note.kind !== 'highlight') continue
      highlightRefs.push(buildHighlightAnnotation(context, note, frame))
    }
    if (highlightRefs.length > 0) {
      const existing = page.node.Annots()
      if (existing) {
        for (const ref of highlightRefs) existing.push(ref)
      } else {
        page.node.set(PDFName.of('Annots'), context.obj(highlightRefs))
      }
    }

    // Callouts: draw straight into the page content stream.
    for (const note of pageNotes) {
      if (note.kind !== 'callout') continue
      drawCalloutOnPage(page, note, frame, helvetica, helveticaBold)
    }
  }

  return pdfDoc.save({ useObjectStreams: false })
}

function clampOpacity(opacity: number): number {
  if (!Number.isFinite(opacity)) return DEFAULT_CALLOUT_OPACITY
  return Math.max(0, Math.min(1, opacity))
}

type PDFContext = PDFDocument['context']
type PDFPage = ReturnType<PDFDocument['getPages']>[number]

function buildHighlightAnnotation(
  context: PDFContext,
  note: Note,
  frame: PageFrame
) {
  const quads = note.quads && note.quads.length > 0 ? note.quads : [note.bbox]
  const pdfRects = quads.map(q => pixelQuadToPdfRect(q, frame))

  const rectBounds = pdfRects.reduce(
    (acc, r) => ({
      x0: Math.min(acc.x0, r.x0),
      y0: Math.min(acc.y0, r.y0),
      x1: Math.max(acc.x1, r.x1),
      y1: Math.max(acc.y1, r.y1),
    }),
    pdfRects[0]
  )

  // QuadPoints: 8 numbers per quad in the order
  //   x1 y1  x2 y2  x3 y3  x4 y4
  // which Acrobat interprets as top-left, top-right, bottom-left, bottom-right
  // *of the text as displayed*. Mapping the displayed corners keeps that
  // orientation right on rotated pages too.
  const quadPoints: number[] = []
  for (const q of quads) {
    const { tl, tr, bl, br } = quadCorners(q, frame)
    quadPoints.push(tl.x, tl.y, tr.x, tr.y, bl.x, bl.y, br.x, br.y)
  }

  const color = hexToRgb(note.color)
  const dict = context.obj({
    Type: 'Annot',
    Subtype: 'Highlight',
    Rect: [rectBounds.x0, rectBounds.y0, rectBounds.x1, rectBounds.y1],
    QuadPoints: quadPoints,
    C: [color.red, color.green, color.blue],
    F: 4,
    Contents: note.text || '',
    CA: 0.4,
  })
  return context.register(dict)
}

function drawCalloutOnPage(
  page: PDFPage,
  note: Note,
  frame: PageFrame,
  helvetica: PDFFont,
  helveticaBold: PDFFont
) {
  const rect = pixelQuadToPdfRect(note.bbox, frame)
  const fillColor = hexToRgb(note.color)

  const effectiveOpacity = clampOpacity(note.opacity ?? DEFAULT_CALLOUT_OPACITY)

  // Translucent background rect. Border stays fully opaque so the callout
  // outline remains visible even when the fill is almost transparent.
  page.drawRectangle({
    x: rect.x0,
    y: rect.y0,
    width: rect.x1 - rect.x0,
    height: rect.y1 - rect.y0,
    color: fillColor,
    opacity: effectiveOpacity,
    borderColor: fillColor,
    borderOpacity: 1,
    borderWidth: 1,
  })

  const text = note.text ?? ''
  if (!text.trim()) return

  const font = note.bold ? helveticaBold : helvetica
  const fontSize = note.fontSize ?? DEFAULT_CALLOUT_FONT_SIZE
  const lineHeight = fontSize * 1.2
  const padding = 4

  // Layout happens in *display* space (as the user drew the callout), then
  // each line's baseline origin is mapped into PDF space. On rotated pages
  // the text is drawn rotated by the page rotation so it reads upright.
  const box = note.bbox
  const left = Math.min(box.x0, box.x1)
  const top = Math.min(box.y0, box.y1)
  const bottom = Math.max(box.y0, box.y1)
  const displayWidthPt = Math.abs(box.x1 - box.x0) / SCALE

  // Wrap lines to the rect width: split on user-provided newlines first,
  // then wrap each line to fit the rect horizontally.
  const maxTextWidth = Math.max(1, displayWidthPt - padding * 2)
  const lines: string[] = []
  for (const userLine of text.split('\n')) {
    lines.push(...wrapLine(userLine, font, fontSize, maxTextWidth))
  }

  // Draw each line from the top of the rect downward, clipping anything
  // that overflows the bottom. Text color comes from the note if set,
  // otherwise falls back to near-black.
  const textColor = note.textColor ? hexToRgb(note.textColor) : rgb(0.07, 0.09, 0.15)
  const xPx = left + padding * SCALE
  const bottomLimitPx = bottom - padding * SCALE

  let baselinePx = top + (padding + fontSize) * SCALE
  for (const line of lines) {
    if (baselinePx > bottomLimitPx) break
    const origin = frame.toPdf(xPx, baselinePx)
    page.drawText(line, {
      x: origin.x,
      y: origin.y,
      size: fontSize,
      font,
      color: textColor,
      rotate: degrees(frame.rotation),
    })
    baselinePx += lineHeight * SCALE
  }
}

function wrapLine(
  line: string,
  font: PDFFont,
  fontSize: number,
  maxWidth: number
): string[] {
  if (line.length === 0) return ['']
  const words = line.split(/(\s+)/) // keep whitespace tokens for accurate widths
  const out: string[] = []
  let current = ''
  for (const token of words) {
    const candidate = current + token
    if (font.widthOfTextAtSize(candidate, fontSize) <= maxWidth) {
      current = candidate
      continue
    }
    // `candidate` overflows — commit `current` and start a new line with the token.
    if (current.trimEnd().length > 0) {
      out.push(current.trimEnd())
    }
    // If the single token itself is wider than the line, hard-break it
    // character-by-character.
    if (font.widthOfTextAtSize(token, fontSize) > maxWidth) {
      let buf = ''
      for (const ch of token) {
        if (font.widthOfTextAtSize(buf + ch, fontSize) > maxWidth) {
          if (buf.length > 0) out.push(buf)
          buf = ch
        } else {
          buf += ch
        }
      }
      current = buf
    } else {
      current = token.trimStart()
    }
  }
  if (current.trimEnd().length > 0) out.push(current.trimEnd())
  return out.length > 0 ? out : ['']
}

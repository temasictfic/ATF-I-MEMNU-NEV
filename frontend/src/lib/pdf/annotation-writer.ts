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
//   - PDF user space has a bottom-left origin and uses points. To convert,
//     divide pixels by SCALE and flip y via `page_height_pt - y`.

import {
  PDFDocument,
  PDFName,
  rgb,
  type PDFFont,
  type RGB,
} from 'pdf-lib'
import fontkit from '@pdf-lib/fontkit'
import type { Note, NoteQuad } from '../stores/notes-store'
import { DEFAULT_CALLOUT_FONT_SIZE, DEFAULT_CALLOUT_OPACITY } from '../stores/notes-store'
import { SCALE } from './types'
import { getLiberationFontBytes } from './fonts'

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

// Convert a pixel-space quad (top-left origin, SCALE pixels) into a PDF-space
// rect (bottom-left origin, points).
function pixelQuadToPdfRect(quad: NoteQuad, pageHeightPt: number): PdfRect {
  const x0 = quad.x0 / SCALE
  const x1 = quad.x1 / SCALE
  // Flip Y: pixel top edge (smallest y in top-left space) becomes the PDF top
  // edge (largest y in bottom-left space).
  const yTop = pageHeightPt - quad.y0 / SCALE
  const yBottom = pageHeightPt - quad.y1 / SCALE
  return { x0, y0: Math.min(yTop, yBottom), x1, y1: Math.max(yTop, yBottom) }
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
    const { height: pageHeightPt } = page.getSize()

    // Highlights: build annotation dict refs and append to the page /Annots.
    const highlightRefs: ReturnType<typeof context.register>[] = []
    for (const note of pageNotes) {
      if (note.kind !== 'highlight') continue
      highlightRefs.push(buildHighlightAnnotation(context, note, pageHeightPt))
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
      drawCalloutOnPage(page, note, pageHeightPt, helvetica, helveticaBold)
    }
  }

  return pdfDoc.save({ useObjectStreams: false })
}

function clampOpacity(opacity: number): number {
  if (!Number.isFinite(opacity)) return DEFAULT_CALLOUT_OPACITY
  return Math.max(0, Math.min(1, opacity))
}

type PDFContext = PDFDocument['context']

function buildHighlightAnnotation(
  context: PDFContext,
  note: Note,
  pageHeightPt: number
) {
  const quads = note.quads && note.quads.length > 0 ? note.quads : [note.bbox]
  const pdfRects = quads.map(q => pixelQuadToPdfRect(q, pageHeightPt))

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
  // which Acrobat interprets as top-left, top-right, bottom-left, bottom-right.
  const quadPoints: number[] = []
  for (const r of pdfRects) {
    quadPoints.push(
      r.x0, r.y1, // top-left
      r.x1, r.y1, // top-right
      r.x0, r.y0, // bottom-left
      r.x1, r.y0  // bottom-right
    )
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

type PDFPage = ReturnType<PDFDocument['getPages']>[number]

function drawCalloutOnPage(
  page: PDFPage,
  note: Note,
  pageHeightPt: number,
  helvetica: PDFFont,
  helveticaBold: PDFFont
) {
  const rect = pixelQuadToPdfRect(note.bbox, pageHeightPt)
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

  // Wrap lines to the rect width: split on user-provided newlines first,
  // then wrap each line to fit the rect horizontally.
  const maxTextWidth = Math.max(1, rect.x1 - rect.x0 - padding * 2)
  const lines: string[] = []
  for (const userLine of text.split('\n')) {
    lines.push(...wrapLine(userLine, font, fontSize, maxTextWidth))
  }

  // Draw each line from the top of the rect downward, clipping anything
  // that overflows the bottom. Text color comes from the note if set,
  // otherwise falls back to near-black.
  const textColor = note.textColor ? hexToRgb(note.textColor) : rgb(0.07, 0.09, 0.15)
  const topY = rect.y1 - padding - fontSize
  const bottomY = rect.y0 + padding

  let y = topY
  for (const line of lines) {
    if (y < bottomY) break
    page.drawText(line, {
      x: rect.x0 + padding,
      y,
      size: fontSize,
      font,
      color: textColor,
    })
    y -= lineHeight
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

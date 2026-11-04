// Parse a PDF into the same PageContent + TextBlock shape the Python backend
// produces today. Output is line-grouped, in pixel coordinates at SCALE,
// with a top-left origin — so existing coordinate math in ParsingPage keeps
// working unchanged.

import type { PDFDocumentProxy, PDFPageProxy, TextItem } from 'pdfjs-dist/types/src/display/api'
import { getPdfjs } from './pdfjs-setup'
import { SCALE, type PageContent, type ParsedPdf, type TextBlock } from './types'

// Two text items belong to the same line if their baseline y values differ
// by less than this fraction of the line's font height. Tuned to match the
// visual line grouping PyMuPDF produces.
const LINE_Y_TOLERANCE = 0.5

// Text items within a line are joined with a single space unless their
// x-gap is smaller than this fraction of font height (glyphs abutting).
const INLINE_SPACE_GAP_RATIO = 0.3

interface TextItemBbox {
  text: string
  x0: number
  y0: number
  x1: number
  y1: number
  fontHeight: number
  fontName: string
  isBold: boolean
}

// Parse a PDF file from its raw bytes.
//
// `id` is the caller-supplied identifier (filename stem, matching backend).
// `name` and `path` are stored on the result for parity with the backend
// PdfDocument model but are not interpreted here.
export async function parsePdf(
  bytes: Uint8Array,
  { id, name, path }: { id: string; name: string; path: string }
): Promise<ParsedPdf> {
  const pdfjsLib = getPdfjs()
  const loadingTask = pdfjsLib.getDocument({ data: bytes })
  const doc: PDFDocumentProxy = await loadingTask.promise

  try {
    const pages: PageContent[] = []
    for (let pageNum = 0; pageNum < doc.numPages; pageNum++) {
      pages.push(await parsePage(doc, pageNum))
    }

    await doc.cleanup()

    return { id, name, path, pages }
  } finally {
    await loadingTask.destroy()
  }
}

async function parsePage(doc: PDFDocumentProxy, pageIndex: number): Promise<PageContent> {
  const page: PDFPageProxy = await doc.getPage(pageIndex + 1)
  const viewport = page.getViewport({ scale: SCALE })
  const textContent = await page.getTextContent()

  const boldFontNames = new Set<string>()
  for (const [fontName, style] of Object.entries(textContent.styles ?? {})) {
    const family = (style as { fontFamily?: string }).fontFamily ?? ''
    if (/bold/i.test(family) || /bold/i.test(fontName)) {
      boldFontNames.add(fontName)
    }
  }

  const pdfjsLib = getPdfjs()
  const items: TextItemBbox[] = []
  for (const raw of textContent.items) {
    if (!('str' in raw)) continue
    const item = raw as TextItem
    if (!item.str) continue

    // Combine the item's text matrix with the viewport's matrix so (e, f) is
    // the baseline in pixel coordinates with top-left origin.
    const tx = pdfjsLib.Util.transform(viewport.transform, item.transform as number[])
    const fontHeight = Math.hypot(tx[2], tx[3])
    if (fontHeight === 0) continue

    const baselineX = tx[4]
    const baselineY = tx[5]
    const widthPx = item.width * SCALE

    items.push({
      text: item.str,
      x0: baselineX,
      y0: baselineY - fontHeight,
      x1: baselineX + widthPx,
      y1: baselineY,
      fontHeight,
      fontName: item.fontName,
      isBold: boldFontNames.has(item.fontName),
    })
  }

  const lines = groupItemsIntoLines(items)
  const text_blocks: TextBlock[] = lines.map(line => lineToTextBlock(line, pageIndex))

  page.cleanup()

  return {
    page_num: pageIndex,
    width: viewport.width,
    height: viewport.height,
    text_blocks,
    page_width_pt: page.view[2] - page.view[0],
    page_height_pt: page.view[3] - page.view[1],
  }
}

function groupItemsIntoLines(items: TextItemBbox[]): TextItemBbox[][] {
  // Sort top-to-bottom, then left-to-right. pdfjs-dist returns items in reading
  // order most of the time, but we cannot rely on it for multi-column PDFs.
  const sorted = [...items].sort((a, b) => {
    if (Math.abs(a.y1 - b.y1) > 1) return a.y1 - b.y1
    return a.x0 - b.x0
  })

  const lines: TextItemBbox[][] = []
  for (const item of sorted) {
    const last = lines[lines.length - 1]
    if (last && sameLine(last, item)) {
      last.push(item)
    } else {
      lines.push([item])
    }
  }
  for (const line of lines) line.sort((a, b) => a.x0 - b.x0)
  return lines
}

function sameLine(line: TextItemBbox[], item: TextItemBbox): boolean {
  const ref = line[line.length - 1]
  const avgHeight = (ref.fontHeight + item.fontHeight) / 2
  return Math.abs(ref.y1 - item.y1) <= avgHeight * LINE_Y_TOLERANCE
}

function lineToTextBlock(line: TextItemBbox[], page: number): TextBlock {
  const parts: string[] = []
  let prev: TextItemBbox | null = null
  for (const item of line) {
    if (prev) {
      const gap = item.x0 - prev.x1
      const avgHeight = (prev.fontHeight + item.fontHeight) / 2
      if (gap > avgHeight * INLINE_SPACE_GAP_RATIO) parts.push(' ')
    }
    parts.push(item.text)
    prev = item
  }
  const text = parts.join('').replace(/\s+/g, ' ').trim()

  const x0 = Math.min(...line.map(i => i.x0))
  const y0 = Math.min(...line.map(i => i.y0))
  const x1 = Math.max(...line.map(i => i.x1))
  const y1 = Math.max(...line.map(i => i.y1))

  // Font size: max across spans, matching Python's `max(font_size, span.size)`.
  const font_size = Math.max(...line.map(i => i.fontHeight))
  // Font name & bold: take the last span's attributes, again matching Python.
  const last = line[line.length - 1]
  const is_bold = line.some(i => i.isBold)

  return {
    text,
    bbox: [x0, y0, x1, y1],
    page,
    font_size,
    font_name: last.fontName,
    is_bold,
  }
}

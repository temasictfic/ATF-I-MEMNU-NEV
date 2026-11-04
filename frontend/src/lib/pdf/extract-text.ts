// Client-side replacement for the backend's `/api/parse/extract-text` endpoint.
// Given a loaded pdfjs-dist document and a bbox in pixel (SCALE) coordinates,
// returns the concatenated text of items whose rendered rect intersects the
// bbox, joined in reading order.

import type { PDFDocumentProxy, TextItem } from 'pdfjs-dist/types/src/display/api'
import { getPdfjs } from './pdfjs-setup'
import { SCALE } from './types'

interface PixelBbox {
  x0: number
  y0: number
  x1: number
  y1: number
}

export async function extractTextInBbox(
  doc: PDFDocumentProxy,
  pageIndex: number, // 0-indexed, matching SourceRectangle.bbox.page
  bbox: PixelBbox
): Promise<string> {
  const page = await doc.getPage(pageIndex + 1)
  try {
    const viewport = page.getViewport({ scale: SCALE })
    const textContent = await page.getTextContent()
    const pdfjsLib = getPdfjs()

    interface Hit {
      text: string
      y: number
      x: number
    }
    const hits: Hit[] = []

    for (const raw of textContent.items) {
      if (!('str' in raw)) continue
      const item = raw as TextItem
      if (!item.str) continue

      // Transform the item's text matrix into viewport (pixel) coordinates.
      // Same math as parser.ts.
      const tx = pdfjsLib.Util.transform(viewport.transform, item.transform as number[])
      const fontHeight = Math.hypot(tx[2], tx[3])
      if (fontHeight === 0) continue

      const baselineX = tx[4]
      const baselineY = tx[5]
      const widthPx = item.width * SCALE
      const itemX0 = baselineX
      const itemY0 = baselineY - fontHeight
      const itemX1 = baselineX + widthPx
      const itemY1 = baselineY

      // Intersection test with a small tolerance so items sitting just at the
      // edge of a user-drawn box still get picked up.
      const tol = 2
      const intersects =
        itemX1 > bbox.x0 - tol &&
        itemX0 < bbox.x1 + tol &&
        itemY1 > bbox.y0 - tol &&
        itemY0 < bbox.y1 + tol
      if (!intersects) continue

      hits.push({
        text: item.str,
        y: itemY1, // baseline — stable for line grouping
        x: itemX0,
      })
    }

    // Sort reading order (top-to-bottom, then left-to-right) and join.
    hits.sort((a, b) => {
      if (Math.abs(a.y - b.y) > 3) return a.y - b.y
      return a.x - b.x
    })

    // Re-join with spaces, collapsing doubles.
    return hits
      .map(h => h.text)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim()
  } finally {
    page.cleanup()
  }
}

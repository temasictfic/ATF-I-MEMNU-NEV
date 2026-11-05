// Checks lib/pdf/page-frame.ts against pdf.js's own viewport on real PDFs
// with offset boxes, crop boxes and every rotation (run: npm test).
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { PDFDocument, degrees } from 'pdf-lib'
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs'
import { makePageFrame, visibleBox } from '../src/lib/pdf/page-frame.ts'

const SCALE = 150 / 72 // src/lib/pdf/types.ts

const CASES = [
  { name: 'plain A4', media: [0, 0, 595, 842], rotate: 0 },
  { name: 'offset MediaBox', media: [100, 200, 712, 992], rotate: 0 },
  { name: 'rotate 90', media: [0, 0, 595, 842], rotate: 90 },
  { name: 'rotate 180 + offset', media: [36, 18, 648, 810], rotate: 180 },
  { name: 'rotate 270 + offset', media: [100, 200, 712, 992], rotate: 270 },
  { name: 'CropBox inside MediaBox', media: [0, 0, 612, 792], crop: [50, 60, 550, 700], rotate: 90 },
]

async function buildPdf() {
  const doc = await PDFDocument.create()
  for (const c of CASES) {
    const [x0, y0, x1, y1] = c.media
    const page = doc.addPage([x1 - x0, y1 - y0])
    page.setMediaBox(x0, y0, x1 - x0, y1 - y0)
    if (c.crop) page.setCropBox(c.crop[0], c.crop[1], c.crop[2] - c.crop[0], c.crop[3] - c.crop[1])
    page.setRotation(degrees(c.rotate))
  }
  return doc.save()
}

const close = (a, b, msg) => assert.ok(Math.abs(a - b) < 1e-6, `${msg}: ${a} vs ${b}`)

test('page frame matches the pdf.js viewport in both directions', async () => {
  const pdf = await getDocument({ data: await buildPdf(), isEvalSupported: false }).promise
  for (let i = 0; i < CASES.length; i++) {
    const c = CASES[i]
    const page = await pdf.getPage(i + 1)
    const viewport = page.getViewport({ scale: SCALE })
    const [vx0, vy0, vx1, vy1] = page.view
    const media = { x0: c.media[0], y0: c.media[1], x1: c.media[2], y1: c.media[3] }
    const crop = c.crop ? { x0: c.crop[0], y0: c.crop[1], x1: c.crop[2], y1: c.crop[3] } : null
    const view = visibleBox(media, crop)
    assert.deepEqual([view.x0, view.y0, view.x1, view.y1], [vx0, vy0, vx1, vy1], `${c.name}: visible box`)

    const frame = makePageFrame(view, page.rotate, SCALE)
    for (const [x, y] of [[vx0, vy0], [vx1, vy1], [(vx0 + vx1) / 2, vy0 + 10], [vx0 + 33.3, vy1 - 71.5]]) {
      const [epx, epy] = viewport.convertToViewportPoint(x, y)
      const { px, py } = frame.toPixel(x, y)
      close(px, epx, `${c.name} toPixel x`)
      close(py, epy, `${c.name} toPixel y`)
      const back = frame.toPdf(epx, epy)
      const [ex, ey] = viewport.convertToPdfPoint(epx, epy)
      close(back.x, ex, `${c.name} toPdf x`)
      close(back.y, ey, `${c.name} toPdf y`)
    }
  }
})

test('a note drawn at the visible top-left lands at the visible top-left in PDF space', () => {
  // Rotated 90°: the displayed top-left corner is the box's bottom-left
  // in user space (the page is shown turned clockwise).
  const frame = makePageFrame({ x0: 100, y0: 200, x1: 712, y1: 992 }, 90, SCALE)
  const p = frame.toPdf(0, 0)
  close(p.x, 100, 'x')
  close(p.y, 200, 'y')
})

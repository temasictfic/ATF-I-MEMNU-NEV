// Mapping between the app's pixel space and PDF user space for one page.
//
// Notes and source boxes are stored in pdf.js *viewport* pixels: top-left
// origin, scaled by SCALE, with the page's /Rotate already applied and the
// visible box (CropBox ∩ MediaBox) as the frame. Converting back by just
// flipping Y with the page height (the old annotation writer) is only right
// for unrotated pages whose box starts at (0, 0). On rotated pages, or pages
// whose MediaBox/CropBox starts elsewhere, exported highlights and callouts
// landed in the wrong place.
//
// This is the exact inverse of pdf.js's PageViewport transform (see
// pdfjs-dist src/display/display_utils.js, PageViewport constructor) and has
// no dependencies, so it is unit-tested against pdf.js itself
// (frontend/tests/page-frame.test.mjs).

export interface PageBox {
  x0: number
  y0: number
  x1: number
  y1: number
}

export interface PageFrame {
  /** Page rotation in degrees (0, 90, 180, 270), as the viewer shows it. */
  rotation: number
  /** Viewport pixel → PDF user-space point. */
  toPdf(px: number, py: number): { x: number; y: number }
  /** PDF user-space point → viewport pixel (used by tests). */
  toPixel(x: number, y: number): { px: number; py: number }
}

/** pdf.js shows CropBox ∩ MediaBox (`page.view`). */
export function visibleBox(mediaBox: PageBox, cropBox: PageBox | null): PageBox {
  if (!cropBox) return mediaBox
  const box = {
    x0: Math.max(Math.min(cropBox.x0, cropBox.x1), Math.min(mediaBox.x0, mediaBox.x1)),
    y0: Math.max(Math.min(cropBox.y0, cropBox.y1), Math.min(mediaBox.y0, mediaBox.y1)),
    x1: Math.min(Math.max(cropBox.x0, cropBox.x1), Math.max(mediaBox.x0, mediaBox.x1)),
    y1: Math.min(Math.max(cropBox.y0, cropBox.y1), Math.max(mediaBox.y0, mediaBox.y1)),
  }
  // Degenerate intersection: pdf.js falls back to the MediaBox.
  return box.x1 > box.x0 && box.y1 > box.y0 ? box : mediaBox
}

export function makePageFrame(view: PageBox, rotationDeg: number, scale: number): PageFrame {
  const rotation = (((Math.round(rotationDeg / 90) * 90) % 360) + 360) % 360
  const { x0, y0, x1, y1 } = view
  const centerX = (x1 + x0) / 2
  const centerY = (y1 + y0) / 2

  let a: number, b: number, c: number, d: number
  switch (rotation) {
    case 90: [a, b, c, d] = [0, 1, 1, 0]; break
    case 180: [a, b, c, d] = [-1, 0, 0, 1]; break
    case 270: [a, b, c, d] = [0, -1, -1, 0]; break
    default: [a, b, c, d] = [1, 0, 0, -1]
  }

  let offsetX: number, offsetY: number
  if (a === 0) {
    offsetX = Math.abs(centerY - y0) * scale
    offsetY = Math.abs(centerX - x0) * scale
  } else {
    offsetX = Math.abs(centerX - x0) * scale
    offsetY = Math.abs(centerY - y0) * scale
  }

  // Same matrix as PageViewport.transform: [A, B, C, D, E, F]
  const A = a * scale
  const B = b * scale
  const C = c * scale
  const D = d * scale
  const E = offsetX - A * centerX - C * centerY
  const F = offsetY - B * centerX - D * centerY
  const det = A * D - B * C

  return {
    rotation,
    toPixel(x, y) {
      return { px: A * x + C * y + E, py: B * x + D * y + F }
    },
    toPdf(px, py) {
      const u = px - E
      const v = py - F
      return { x: (D * u - C * v) / det, y: (-B * u + A * v) / det }
    },
  }
}

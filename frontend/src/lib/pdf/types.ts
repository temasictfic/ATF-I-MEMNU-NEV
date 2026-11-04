// Client-side parsed-PDF shape. Mirrors backend/models/pdf_document.py so the
// source detector port and the rest of the parsing UI can keep using the
// same field names that exist server-side today.

export interface TextBlock {
  text: string
  bbox: [number, number, number, number] // [x0, y0, x1, y1] in pixel coords at SCALE = 150/72
  page: number
  font_size: number
  font_name: string
  is_bold: boolean
}

export interface PageContent {
  page_num: number
  width: number // pixels at SCALE
  height: number // pixels at SCALE
  text_blocks: TextBlock[]
  page_width_pt: number // PDF-space width, needed when writing annotations
  page_height_pt: number // PDF-space height, needed for y-flip on annotation export
}

export interface ParsedPdf {
  id: string
  name: string
  path: string
  pages: PageContent[]
}

// Matches backend DPI = 150, SCALE = DPI / 72. Keep identical so existing
// stored source-rect bboxes remain valid in the new pipeline.
export const PDF_DPI = 150
export const SCALE = PDF_DPI / 72 // ≈ 2.0833

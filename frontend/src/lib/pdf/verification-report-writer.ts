// Generates a from-scratch PDF report of verification results using pdf-lib.
//
// Each source is rendered inside a bordered box:
//   ┌──────────────────────────────────────┐
//   │ [N] raw source text that wraps    │
//   │     across multiple lines …          │
//   ├──────────────────────────────────────┤
//   │ title:87%  Citation  !authors,!year  │
//   │ ┌─ best match card ────────────────┐ │
//   │ │ title / authors / journal        │ │
//   │ │ year  DOI  |  db  score          │ │
//   │ │ https://url.underlined           │ │
//   │ └──────────────────────────────────┘ │
//   └──────────────────────────────────────┘

import { PDFDocument, PDFName, PDFString, PDFArray, PDFNumber, PDFDict, rgb, type PDFFont, type PDFPage, type RGB } from 'pdf-lib'
import fontkit from '@pdf-lib/fontkit'
import { STATUS_RGB, DECISION_RGB } from '../constants/colors'
import { dbScoreRgbTuple, verifyStatusRgbTuple } from '../utils/status-helpers'
import { getLiberationFontBytes } from './fonts'

// A4
const PAGE_W = 595.28
const PAGE_H = 841.89
const MARGIN_L = 50
const MARGIN_R = 50
const MARGIN_T = 50
const MARGIN_B = 50
const CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

// Outer ref box
const BOX_PAD = 8
const BOX_INNER_W = CONTENT_W - BOX_PAD * 2

// Inner best-match card — flush with box content left edge
const CARD_PAD = 7
const CARD_W = BOX_INNER_W
const CARD_INNER_W = CARD_W - CARD_PAD * 2

const TITLE_SIZE = 16
const SUBTITLE_SIZE = 12
const BODY_SIZE = 10
const SMALL_SIZE = 9
const TAG_SIZE = 8
const TINY_SIZE = 7.5
const LH = 1.4

const tup = (t: readonly [number, number, number]): RGB => rgb(t[0], t[1], t[2])

const COLOR_HIGH: RGB         = tup(STATUS_RGB.high)
const COLOR_MEDIUM: RGB       = tup(STATUS_RGB.medium)
const COLOR_LOW: RGB          = tup(STATUS_RGB.low)
const COLOR_TEXT: RGB         = rgb(0.1, 0.1, 0.1)
const COLOR_MUTED: RGB        = rgb(0.47, 0.44, 0.40)
const COLOR_DARK: RGB         = rgb(0.267, 0.251, 0.235)
const COLOR_SOURCE: RGB       = rgb(0.341, 0.325, 0.310)
const COLOR_LINK: RGB         = rgb(0.161, 0.404, 0.749)  // #2967bf (DOI + URL)
const COLOR_DB_TEXT: RGB      = rgb(0.851, 0.467, 0.024)  // #d97706 (db badge)
const COLOR_BORDER: RGB       = rgb(0.82, 0.82, 0.82)
const COLOR_CARD_BORDER: RGB  = rgb(0.906, 0.898, 0.890)
const COLOR_CARD_BG: RGB      = rgb(0.980, 0.980, 0.978)
const COLOR_DB_BG: RGB        = rgb(0.996, 0.953, 0.780)
const COLOR_NOTICE_BG: RGB    = rgb(0.937, 0.969, 1.0)
const COLOR_NOTICE_BORDER: RGB = rgb(0.557, 0.714, 0.886)
const COLOR_NOTICE_ICON: RGB  = rgb(0.114, 0.357, 0.667)
const COLOR_TITLE_TAG: RGB      = rgb(1.0, 0.325, 0.286)  // #FF5349 (scarlet) — title: % chip when score < 85%
const COLOR_TITLE_TAG_GOOD: RGB = rgb(0.0, 0.588, 0.533)  // teal — title: % chip when score >= 85%
// Decision-tag palette (Valid / Citation / Fabricated) — sourced from constants/colors.ts.
const COLOR_VALID_BORDER: RGB      = tup(DECISION_RGB.validBorder)
const COLOR_VALID_TEXT: RGB        = tup(DECISION_RGB.validText)
const COLOR_CITATION_BORDER: RGB   = tup(DECISION_RGB.citationBorder)
const COLOR_CITATION_TEXT: RGB     = tup(DECISION_RGB.citationText)
const COLOR_FABRICATED_BORDER: RGB = tup(DECISION_RGB.fabricatedBorder)
const COLOR_FABRICATED_TEXT: RGB   = tup(DECISION_RGB.fabricatedText)

const statusColor = (s: string): RGB => tup(verifyStatusRgbTuple(s))
const dbScoreColor = (s: number): RGB => tup(dbScoreRgbTuple(s))

// --- Public interfaces ---

export interface ReportBestMatch {
  title: string; authors: string[]; year?: number; journal?: string
  doi?: string; url?: string; database: string; score: number
  titleSimilarity: number; authorMatch: number; yearMatch: number
  // Bibliographic extras — populated when the source database returns them.
  // Rendered as a compact "Bibliographic details" block below the URL.
  volume?: string | null; issue?: string | null; pages?: string | null
  publisher?: string; editor?: string[]; documentType?: string
  language?: string; issn?: string[]; isbn?: string[]
}
export interface ReportSource {
  refNumber: number; text: string; status: string
  problemTags: string[]; bestMatch?: ReportBestMatch
  decisionTag?: 'valid' | 'citation' | 'fabricated'
  decisionTagOverride?: 'valid' | 'citation' | 'fabricated' | null
  tagOverrides?: Record<string, boolean>
  scholarUrl?: string; googleUrl?: string
}
export interface ReportData {
  pdfName: string
  summary: { high: number; medium: number; low: number; total: number; valid?: number; citation?: number; fabricated?: number }
  sources: ReportSource[]
  labels: {
    header: string; high: string; medium: string; low: string
    problems: string; noMatch: string
    sourcesLabel: string
    titleTag: string; validTag: string; citationTag: string; fabricatedTag: string
    tagLabel: (tag: string) => string
    bibliographic: string
    volume: string; issue: string; pages: string; publisher: string
    editor: string; documentType: string; language: string
    issn: string; isbn: string
    highestMatch: string
    problemDesc: { authors: string; title: string; year: string; journal: string; doi: string }
    total: string; matchGroup: string; decisionGroup: string
    reviewNotice: string
  }
  // When false, the "Bibliographic details" block under each best-match card
  // is omitted from the PDF. Default true.
  includeBibliographic?: boolean
}

interface ExtraField { key: string; label: string; value: string }

function buildExtras(m: ReportBestMatch, labels: ReportData['labels']): ExtraField[] {
  const out: ExtraField[] = []
  if (m.volume) out.push({ key: 'volume', label: labels.volume, value: m.volume })
  if (m.issue) out.push({ key: 'issue', label: labels.issue, value: m.issue })
  if (m.pages) out.push({ key: 'pages', label: labels.pages, value: m.pages })
  if (m.publisher) out.push({ key: 'publisher', label: labels.publisher, value: m.publisher })
  if (m.editor && m.editor.length > 0) out.push({ key: 'editor', label: labels.editor, value: m.editor.join(', ') })
  if (m.documentType) out.push({ key: 'documentType', label: labels.documentType, value: m.documentType })
  if (m.language) out.push({ key: 'language', label: labels.language, value: m.language })
  if (m.issn && m.issn.length > 0) out.push({ key: 'issn', label: labels.issn, value: m.issn.join(', ') })
  if (m.isbn && m.isbn.length > 0) out.push({ key: 'isbn', label: labels.isbn, value: m.isbn.join(', ') })
  return out
}

function effectiveDecision(src: ReportSource): 'valid' | 'citation' | 'fabricated' {
  if (src.decisionTagOverride) return src.decisionTagOverride
  if (!src.bestMatch) return 'fabricated'
  // Mirror classifyDecisionFromTags in tagState.ts — compute pill live from
  // the current chip states so export matches what the user sees on screen.
  const authorsOn = effectiveTagOnPdf(src, 'authors')
  const titleOn   = effectiveTagOnPdf(src, 'title')
  const journalOn = effectiveTagOnPdf(src, 'journal')
  const doiOn     = effectiveTagOnPdf(src, 'doi/arXiv')
  const authorMatches = !authorsOn, titleMatches = !titleOn
  const journalMatches = !journalOn, doiMatches = !doiOn
  const journalSupportsCitation = journalMatches && Boolean(src.bestMatch.journal?.trim())
  const doiSupportsCitation =
    doiMatches && Boolean(src.bestMatch.doi?.trim())
  // Keep the year issue visible in the report, without letting it change the
  // decision tag or the decision summary counts.
  if (authorMatches && titleMatches && journalMatches) return 'valid'
  if (titleMatches || (authorMatches && (journalSupportsCitation || doiSupportsCitation))) return 'citation'
  return 'fabricated'
}

// --- Helpers ---

function wrapText(text: string, font: PDFFont, sz: number, maxW: number): string[] {
  const lines: string[] = []
  for (const para of text.split('\n')) {
    const words = para.split(/\s+/).filter(Boolean)
    if (!words.length) { lines.push(''); continue }
    let cur = words[0]
    for (let i = 1; i < words.length; i++) {
      const t = cur + ' ' + words[i]
      if (font.widthOfTextAtSize(t, sz) <= maxW) cur = t
      else { lines.push(cur); cur = words[i] }
    }
    lines.push(cur)
  }
  return lines
}

/** Truncate a single-line string with an ellipsis so it fits within maxW. */
function truncateToWidth(text: string, font: PDFFont, sz: number, maxW: number): string {
  if (font.widthOfTextAtSize(text, sz) <= maxW) return text
  const ellipsis = '…'
  let lo = 0, hi = text.length
  while (lo < hi) {
    const mid = Math.floor((lo + hi + 1) / 2)
    if (font.widthOfTextAtSize(text.slice(0, mid) + ellipsis, sz) <= maxW) lo = mid
    else hi = mid - 1
  }
  return text.slice(0, lo) + ellipsis
}

function san(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F]/g, '')
}

// --- PageWriter ---

class PW {
  private doc: PDFDocument
  pg: PDFPage
  y: number
  rf: PDFFont
  bf: PDFFont

  constructor(doc: PDFDocument, rf: PDFFont, bf: PDFFont) {
    this.doc = doc; this.rf = rf; this.bf = bf
    this.pg = doc.addPage([PAGE_W, PAGE_H])
    this.y = PAGE_H - MARGIN_T
  }

  newPage() { this.pg = this.doc.addPage([PAGE_W, PAGE_H]); this.y = PAGE_H - MARGIN_T }
  ensure(h: number) { if (this.y - h < MARGIN_B) this.newPage() }

  text(s: string, x: number, sz: number, font: PDFFont, color: RGB) {
    const lh = sz * LH
    this.ensure(lh)
    this.pg.drawText(san(s), { x, y: this.y - sz, size: sz, font, color })
    this.y -= lh
  }

  skip(n: number) { this.y -= n }

  rect(x: number, y: number, w: number, h: number, opts: { fill?: RGB; border?: RGB; bw?: number }) {
    if (opts.fill) this.pg.drawRectangle({ x, y, width: w, height: h, color: opts.fill })
    if (opts.border) this.pg.drawRectangle({ x, y, width: w, height: h, borderColor: opts.border, borderWidth: opts.bw ?? 0.75 })
  }

  /** Add a clickable URI link annotation over a text region. */
  addLink(url: string, x: number, y: number, w: number, h: number) {
    const ctx = this.doc.context

    // Build the /A (action) dictionary manually with proper PDF types
    const actionDict = PDFDict.withContext(ctx)
    actionDict.set(PDFName.of('Type'), PDFName.of('Action'))
    actionDict.set(PDFName.of('S'), PDFName.of('URI'))
    actionDict.set(PDFName.of('URI'), PDFString.of(url))

    const rect = PDFArray.withContext(ctx)
    rect.push(PDFNumber.of(x))
    rect.push(PDFNumber.of(y))
    rect.push(PDFNumber.of(x + w))
    rect.push(PDFNumber.of(y + h))

    const border = PDFArray.withContext(ctx)
    border.push(PDFNumber.of(0))
    border.push(PDFNumber.of(0))
    border.push(PDFNumber.of(0))

    const annotDict = PDFDict.withContext(ctx)
    annotDict.set(PDFName.of('Type'), PDFName.of('Annot'))
    annotDict.set(PDFName.of('Subtype'), PDFName.of('Link'))
    annotDict.set(PDFName.of('Rect'), rect)
    annotDict.set(PDFName.of('Border'), border)
    annotDict.set(PDFName.of('A'), actionDict)

    const ref = ctx.register(annotDict)
    const existing = this.pg.node.Annots()
    if (existing) existing.push(ref)
    else this.pg.node.set(PDFName.of('Annots'), ctx.obj([ref]))
  }
}

// --- Pre-measure helpers ---

interface TopMeasure {
  badgeW: number; badgeH: number; badgeGap: number
  refLines: string[]  // first line is shorter (badge takes space), rest full-width
  height: number
}

function measureTop(src: ReportSource, rf: PDFFont, bf: PDFFont): TopMeasure {
  const refLabel = san(`[${src.refNumber}]`)
  const badgeW = bf.widthOfTextAtSize(refLabel, BODY_SIZE)
  const badgeH = BODY_SIZE * LH  // just text height, no padding
  const badgeGap = 6

  // Both first and subsequent lines use the indented width so wrapped text
  // stays aligned with the first letter rather than flowing back under the
  // [N] badge.
  const textW = BOX_INNER_W - badgeW - badgeGap
  const refLines = src.text
    ? wrapText(san(src.text), rf, SMALL_SIZE, textW)
    : []

  const firstRowH = Math.max(badgeH, SMALL_SIZE * LH)
  const restH = refLines.length > 1 ? (refLines.length - 1) * SMALL_SIZE * LH : 0
  const googleLinksH = (src.scholarUrl || src.googleUrl) ? 2 + (TINY_SIZE * LH) : 0

  return { badgeW, badgeH, badgeGap, refLines, height: firstRowH + restH + googleLinksH }
}

interface TagItem {
  text: string
  color: RGB
  width: number
  align?: 'right'
  // When set, the chip renders with the original db-badge style:
  // "<prefix>" in COLOR_DARK, then a yellow pill containing the db name
  // in COLOR_DB_TEXT, then the percentage in dbScoreColor(score).
  highestMatch?: { prefix: string; db: string; score: number; scoreText: string; scoreColor: RGB }
}

type PdfTagKey = 'authors' | 'year' | 'title' | 'journal' | 'doi/arXiv'
const PDF_TAG_ORDER: PdfTagKey[] = ['authors', 'year', 'title', 'journal', 'doi/arXiv']

function defaultTagOnPdf(src: ReportSource, tag: PdfTagKey): boolean {
  const bm = src.bestMatch
  const probs = src.problemTags ?? []
  switch (tag) {
    case 'authors':  return probs.includes('!authors')   && !!bm
    case 'year':     return probs.includes('!year')      && !!bm
    case 'journal':  return probs.includes('!journal')   && !!bm
    case 'doi/arXiv':return probs.includes('!doi/arXiv') && !!bm
    case 'title':    return probs.includes('!title')     && !!bm
  }
}

function effectiveTagOnPdf(src: ReportSource, tag: PdfTagKey): boolean {
  const ov = src.tagOverrides?.[tag]
  if (ov !== undefined) return ov
  return defaultTagOnPdf(src, tag)
}

function measureTags(src: ReportSource, labels: ReportData['labels'], bf: PDFFont): TagItem[] {
  const tags: TagItem[] = []

  const pushChip = (text: string, onColor: RGB, on: boolean) => {
    const color = on ? onColor : COLOR_MUTED
    tags.push({ text, color, width: bf.widthOfTextAtSize(san(text), TAG_SIZE) })
  }

  // Leading "highest match" chip — db + score (or no-match label) on the left.
  if (src.bestMatch) {
    const m = src.bestMatch
    const prefix = `${labels.highestMatch}: `
    const db = san(m.database)
    const scoreText = `${Math.round(m.score * 100)}%`
    const prefixW = bf.widthOfTextAtSize(san(prefix), TAG_SIZE)
    const dbW = bf.widthOfTextAtSize(db, TAG_SIZE) + 8 // pill horizontal padding
    const scoreW = bf.widthOfTextAtSize(scoreText, TAG_SIZE)
    const totalW = prefixW + dbW + 6 + scoreW // 6 = gap between pill and score
    tags.push({
      text: prefix + m.database + ' ' + scoreText,
      color: COLOR_DARK,
      width: totalW,
      highestMatch: { prefix, db, score: m.score, scoreText, scoreColor: dbScoreColor(m.score) },
    })
  } else {
    const t = labels.noMatch
    tags.push({ text: t, color: COLOR_LOW, width: bf.widthOfTextAtSize(san(t), TAG_SIZE) })
  }

  const leftChipsStart = tags.length
  for (const tag of PDF_TAG_ORDER) {
    const on = effectiveTagOnPdf(src, tag)
    if (!on) continue
    if (tag === 'title') {
      const pct = src.bestMatch ? Math.round(src.bestMatch.titleSimilarity * 100) : null
      const text = pct != null ? `${labels.titleTag}: ${pct}%` : `${labels.titleTag}: —`
      const titleColor = src.bestMatch && src.bestMatch.titleSimilarity >= 0.85
        ? COLOR_TITLE_TAG_GOOD
        : COLOR_TITLE_TAG
      pushChip(text, titleColor, true)
    } else {
      const text = labels.tagLabel(`!${tag}`)
      pushChip(text, COLOR_MEDIUM, true)
    }
  }
  // "Sorunlar: " prefix — only when at least one chip was added.
  if (tags.length > leftChipsStart) {
    const prefix = `${labels.problems}: `
    tags.splice(leftChipsStart, 0, {
      text: prefix,
      color: COLOR_DARK,
      width: bf.widthOfTextAtSize(san(prefix), TAG_SIZE),
    })
  }

  // Decision tag — always rendered (cycles Valid/Citation/Fabricated), override wins.
  const decision = effectiveDecision(src)
  if (decision === 'valid') {
    const t = labels.validTag
    tags.push({ text: t, color: COLOR_VALID_TEXT, width: bf.widthOfTextAtSize(san(t), TAG_SIZE), align: 'right' })
  } else if (decision === 'citation') {
    const t = labels.citationTag
    tags.push({ text: t, color: COLOR_CITATION_TEXT, width: bf.widthOfTextAtSize(san(t), TAG_SIZE), align: 'right' })
  } else {
    const t = labels.fabricatedTag
    tags.push({ text: t, color: COLOR_FABRICATED_TEXT, width: bf.widthOfTextAtSize(san(t), TAG_SIZE), align: 'right' })
  }

  return tags
}

/** Measure the height of the tags row (single line if they fit, or wrapping). */
function tagsRowHeight(tags: TagItem[]): number {
  if (!tags.length) return 0
  const tagLh = TAG_SIZE * LH
  // For simplicity, compute how many rows the tags need
  let x = 0
  let rows = 1
  const gap = 8
  for (let i = 0; i < tags.length; i++) {
    const needed = tags[i].width + (i > 0 ? gap : 0)
    if (x + needed > BOX_INNER_W && x > 0) { rows++; x = tags[i].width }
    else { x += needed }
  }
  return rows * tagLh
}

interface CardMeasure {
  titleLines: string[]; authorLines: string[]; journalLines: string[]
  metaLine: string; urlLine: string; totalHeight: number
  extras: ExtraField[]; extraLines: string[][]
}

function measureCard(m: ReportBestMatch, src: ReportSource, labels: ReportData['labels'], rf: PDFFont, bf: PDFFont, includeBibliographic: boolean): CardMeasure {
  const lh = SMALL_SIZE * LH, tlh = TINY_SIZE * LH
  const titleLines = wrapText(san(m.title), bf, SMALL_SIZE, CARD_INNER_W)
  const authorLines = m.authors.length ? wrapText(san(m.authors.join(', ')), rf, SMALL_SIZE, CARD_INNER_W) : []
  const journalLines = m.journal ? wrapText(san(m.journal), rf, SMALL_SIZE, CARD_INNER_W) : []
  const metaParts: string[] = []
  if (m.year) metaParts.push(`${m.year}`)
  if (m.doi) metaParts.push(`DOI: ${m.doi}`)
  const metaLine = metaParts.join('   ')
  const urlLine = m.url ?? ''

  // Bibliographic extras: each "Label: value" wrapped to fit the card.
  const extras = includeBibliographic ? buildExtras(m, labels) : []
  const extraLines = extras.map(ex => wrapText(san(`${ex.label}: ${ex.value}`), rf, TINY_SIZE, CARD_INNER_W))

  let h = CARD_PAD
  h += titleLines.length * lh
  if (authorLines.length) h += 2 + authorLines.length * lh
  if (journalLines.length) h += 2 + journalLines.length * lh
  if (metaLine) h += 2 + lh
  // Bibliographic details block: one wrapped row per extra (no header).
  if (extras.length > 0) {
    h += 4
    for (const lines of extraLines) h += lines.length * tlh
  }
  if (urlLine) h += 4 + tlh
  h += CARD_PAD

  return { titleLines, authorLines, journalLines, metaLine, urlLine, totalHeight: h, extras, extraLines }
}

function measureBottom(src: ReportSource, labels: ReportData['labels'], rf: PDFFont, bf: PDFFont, includeBibliographic: boolean): { tags: TagItem[]; card: CardMeasure | null; height: number } {
  const tags = measureTags(src, labels, bf)
  const card = src.bestMatch ? measureCard(src.bestMatch, src, labels, rf, bf, includeBibliographic) : null

  let h = 0
  const th = tagsRowHeight(tags)
  if (th > 0) h += th + 4
  if (card) h += card.totalHeight + 2

  return { tags, card, height: h }
}

// --- Main export ---

export async function generateVerificationReport(data: ReportData): Promise<Uint8Array> {
  const { regular, bold } = await getLiberationFontBytes()
  const doc = await PDFDocument.create()
  doc.registerFontkit(fontkit)
  const rf = await doc.embedFont(regular, { subset: true })
  const bf = await doc.embedFont(bold, { subset: true })

  const w = new PW(doc, rf, bf)
  const { labels, summary, pdfName } = data
  const includeBibliographic = data.includeBibliographic ?? true

  // --- Page header (multi-line, each line centered) ---
  for (const line of labels.header.split('\n')) {
    const trimmed = san(line.trim())
    if (!trimmed) continue
    const lineW = bf.widthOfTextAtSize(trimmed, TITLE_SIZE)
    const lineX = MARGIN_L + (CONTENT_W - lineW) / 2
    w.text(trimmed, lineX, TITLE_SIZE, bf, COLOR_DARK)
  }
  w.skip(4)
  // Strip .pdf extension from displayed name; center horizontally.
  const displayName = pdfName.replace(/\.pdf$/i, '')
  {
    const nameW = rf.widthOfTextAtSize(san(displayName), SUBTITLE_SIZE)
    const nameX = MARGIN_L + (CONTENT_W - nameW) / 2
    w.text(displayName, nameX, SUBTITLE_SIZE, rf, COLOR_MUTED)
  }
  w.skip(8)

  // --- Header section: counts table (left) + tag legend table (right), side by side ---
  {
    const cellPadX = 6
    const cellPadY = 2
    const dotR = 2.5
    const dotGap = 4

    interface CountPart { l: string; c: number; clr: RGB }
    const matchParts: CountPart[] = [
      { l: labels.high, c: summary.high, clr: COLOR_HIGH },
      { l: labels.medium, c: summary.medium, clr: COLOR_MEDIUM },
      { l: labels.low, c: summary.low, clr: COLOR_LOW },
    ]
    const decisionParts: CountPart[] = []
    if (summary.valid != null) decisionParts.push({ l: labels.validTag, c: summary.valid, clr: COLOR_VALID_BORDER })
    if (summary.citation != null) decisionParts.push({ l: labels.citationTag, c: summary.citation, clr: COLOR_CITATION_BORDER })
    if (summary.fabricated != null) decisionParts.push({ l: labels.fabricatedTag, c: summary.fabricated, clr: COLOR_FABRICATED_BORDER })

    // Count cell — split into 3 visual columns that align vertically across all rows:
    //   [dot] [name] | [count number]
    // The "|" is a real vertical divider line spanning the whole body.
    const allCountParts = [...matchParts, ...decisionParts]
    const widestNameW = allCountParts.reduce((m, p) =>
      Math.max(m, rf.widthOfTextAtSize(san(p.l), TAG_SIZE)), 0)
    const widestNumW = allCountParts.reduce((m, p) =>
      Math.max(m, rf.widthOfTextAtSize(san(`${p.c}`), TAG_SIZE)), 0)
    const nameNumGap = 8 // gap that contains the divider line (4pt + line + 4pt)
    // Lane-block width = the natural width needed to render dot + name + divider + number.
    const laneBlockW = dotR * 2 + dotGap + widestNameW + nameNumGap + widestNumW
    const minCountCellW = laneBlockW + cellPadX * 2

    // Group-label column: horizontal text "Eşleşme" / "Karar", spanning the 3 stacked count sub-rows.
    const matchLabelW = bf.widthOfTextAtSize(san(labels.matchGroup), TAG_SIZE)
    const decisionLabelW = bf.widthOfTextAtSize(san(labels.decisionGroup), TAG_SIZE)
    const labelColW = Math.max(matchLabelW, decisionLabelW) + cellPadX * 2

    // Counts: each count occupies its own sub-row, stacked vertically. 3 per group.
    const subRowsPerGroup = 3
    const minSubRowH = TAG_SIZE * 1.25 + cellPadY * 2
    const groupCount = decisionParts.length ? 2 : 1

    // Legend dimensions
    const entries: { chip: string; chipColor: RGB; hint: string }[] = [
      { chip: labels.tagLabel('!authors'),  chipColor: COLOR_MEDIUM, hint: labels.problemDesc.authors },
      { chip: labels.tagLabel('!year'),     chipColor: COLOR_MEDIUM, hint: labels.problemDesc.year },
      { chip: `${labels.titleTag}: %`,      chipColor: COLOR_TITLE_TAG,   hint: labels.problemDesc.title },
      { chip: labels.tagLabel('!journal'),  chipColor: COLOR_MEDIUM, hint: labels.problemDesc.journal },
      { chip: labels.tagLabel('!doi/arXiv'),chipColor: COLOR_MEDIUM, hint: labels.problemDesc.doi },
    ]
    const widestChipW = entries.reduce((m, e) => Math.max(m, bf.widthOfTextAtSize(san(e.chip), TAG_SIZE)), 0)
    const widestHintW = entries.reduce((m, e) => Math.max(m, rf.widthOfTextAtSize(san(e.hint), TINY_SIZE)), 0)
    const chipColW = widestChipW + cellPadX * 2
    const hintColW = widestHintW + cellPadX * 2
    const legendBodyW = chipColW + hintColW
    const sorunlarText = labels.problems
    const sorunlarTextW = bf.widthOfTextAtSize(san(sorunlarText), TAG_SIZE)
    const legendW = Math.max(legendBodyW, sorunlarTextW + cellPadX * 2)
    const headerRowH = TAG_SIZE * 1.25 + cellPadY * 2
    const minLegendBodyRowH = Math.max(TAG_SIZE, TINY_SIZE) * 1.25 + cellPadY * 2

    // Match heights: pick whichever table's natural size is taller, then grow
    // the other's body rows to match. Count table body = groupCount × subRowsPerGroup
    // stacked sub-rows (one per count).
    const totalCountSubRows = subRowsPerGroup * groupCount
    const naturalCountH = headerRowH + minSubRowH * totalCountSubRows
    const naturalLegendH = headerRowH + minLegendBodyRowH * entries.length
    const tableH = Math.max(naturalCountH, naturalLegendH)
    const countSubRowH = (tableH - headerRowH) / totalCountSubRows
    const legendBodyRowH = (tableH - headerRowH) / entries.length

    const totalText = `${labels.total}: ${summary.total} ${labels.sourcesLabel}`
    const totalTextW = bf.widthOfTextAtSize(san(totalText), TAG_SIZE)
    const finalCountTableH = tableH
    const legendTableH = tableH

    const startY = w.y
    const tableGap = 16
    // Expand the count table so the two tables together cover the full content width.
    const countTableW = Math.max(labelColW + minCountCellW, CONTENT_W - tableGap - legendW)
    const countCellW = countTableW - labelColW
    const countX = MARGIN_L
    const legendX = countX + countTableW + tableGap

    // ==================== Count table ====================
    const countTop = startY
    const countBottom = countTop - finalCountTableH
    w.rect(countX, countBottom, countTableW, finalCountTableH, { border: COLOR_BORDER, bw: 0.5 })

    // Header row "Toplam: <n> kaynak" — spans all 4 columns
    {
      const headerBottomY = countTop - headerRowH
      const headerTextX = countX + (countTableW - totalTextW) / 2
      const headerTextY = headerBottomY + cellPadY + (headerRowH - cellPadY * 2 - TAG_SIZE) / 2
      w.pg.drawText(san(totalText), {
        x: headerTextX, y: headerTextY,
        size: TAG_SIZE, font: bf, color: COLOR_DARK,
      })
      w.pg.drawLine({
        start: { x: countX,                y: headerBottomY },
        end:   { x: countX + countTableW,  y: headerBottomY },
        thickness: 0.5, color: COLOR_BORDER,
      })
    }
    const countBodyTop = countTop - headerRowH

    // Split the count cell into 2 equal compartments: [dot + name] | [number].
    const halfCompW = countCellW / 2
    const leftCompStartX = countX + labelColW
    const dividerX = leftCompStartX + halfCompW
    const numCenterX = dividerX + halfCompW / 2
    // Dot + name rendered as a group, centered horizontally in the left compartment.
    // Names left-align at nameStartX so starts line up vertically across rows.
    const dotNameBlockW = dotR * 2 + dotGap + widestNameW
    const dotNameBlockStartX = leftCompStartX + (halfCompW - dotNameBlockW) / 2
    const dotCenterX = dotNameBlockStartX + dotR
    const nameStartX = dotNameBlockStartX + dotR * 2 + dotGap

    // Vertical divider between label col and count col (body only)
    w.pg.drawLine({
      start: { x: countX + labelColW, y: countBodyTop },
      end:   { x: countX + labelColW, y: countBottom },
      thickness: 0.5, color: COLOR_BORDER,
    })
    // Single inner divider between [dot+name] and [number] compartments.
    w.pg.drawLine({
      start: { x: dividerX, y: countBodyTop },
      end:   { x: dividerX, y: countBottom },
      thickness: 0.5, color: COLOR_BORDER,
    })
    // Horizontal divider between groups (Eşleşme group → Karar group)
    if (groupCount === 2) {
      const midY = countBodyTop - countSubRowH * subRowsPerGroup
      w.pg.drawLine({
        start: { x: countX,                y: midY },
        end:   { x: countX + countTableW,  y: midY },
        thickness: 0.5, color: COLOR_BORDER,
      })
    }

    const renderCountGroup = (groupIdx: number, label: string, parts: CountPart[]) => {
      const groupHeight = countSubRowH * subRowsPerGroup
      const groupTop = countBodyTop - groupIdx * groupHeight
      const groupBottom = groupTop - groupHeight

      // Horizontal label centered in the label cell, vertically centered over its 3 sub-rows.
      const labelTextW = bf.widthOfTextAtSize(san(label), TAG_SIZE)
      const labelX = countX + (labelColW - labelTextW) / 2
      const labelY = groupBottom + (groupHeight - TAG_SIZE) / 2
      w.pg.drawText(san(label), {
        x: labelX, y: labelY, size: TAG_SIZE, font: bf, color: COLOR_DARK,
      })

      // 3 count sub-rows stacked vertically inside the count column.
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i]
        const subTop = groupTop - i * countSubRowH
        const subBottom = subTop - countSubRowH

        // Sub-row divider (only across the count column, so the label cell looks merged)
        if (i > 0) {
          w.pg.drawLine({
            start: { x: countX + labelColW,  y: subTop },
            end:   { x: countX + countTableW, y: subTop },
            thickness: 0.5, color: COLOR_BORDER,
          })
        }

        const cellTextY = subBottom + cellPadY + (countSubRowH - cellPadY * 2 - TAG_SIZE) / 2
        const numText = `${p.c}`
        const numTextW = rf.widthOfTextAtSize(san(numText), TAG_SIZE)
        // Dot — centered in its compartment.
        w.pg.drawCircle({ x: dotCenterX, y: cellTextY + TAG_SIZE * 0.35, size: dotR, color: p.clr })
        // Name — left-aligned at a fixed x so starts align vertically across rows.
        w.pg.drawText(san(p.l), {
          x: nameStartX, y: cellTextY,
          size: TAG_SIZE, font: rf, color: COLOR_TEXT,
        })
        // Number — centered in its compartment.
        w.pg.drawText(san(numText), {
          x: numCenterX - numTextW / 2, y: cellTextY,
          size: TAG_SIZE, font: rf, color: COLOR_TEXT,
        })
      }
    }
    renderCountGroup(0, labels.matchGroup, matchParts)
    if (decisionParts.length) renderCountGroup(1, labels.decisionGroup, decisionParts)

    // ==================== Legend table (with Sorunlar header) ====================
    const legendTop = startY
    const legendBottom = legendTop - legendTableH
    w.rect(legendX, legendBottom, legendW, legendTableH, { border: COLOR_BORDER, bw: 0.5 })

    // Header row: "Sorunlar" centered, spans both columns
    const headerBottomY = legendTop - headerRowH
    const headerTextX = legendX + (legendW - sorunlarTextW) / 2
    const headerTextY = headerBottomY + cellPadY + (headerRowH - cellPadY * 2 - TAG_SIZE) / 2
    w.pg.drawText(san(sorunlarText), {
      x: headerTextX, y: headerTextY,
      size: TAG_SIZE, font: bf, color: COLOR_DARK,
    })
    // Divider below header
    w.pg.drawLine({
      start: { x: legendX,            y: headerBottomY },
      end:   { x: legendX + legendW,  y: headerBottomY },
      thickness: 0.5, color: COLOR_BORDER,
    })

    // Vertical divider between chip and hint columns (body only)
    w.pg.drawLine({
      start: { x: legendX + chipColW, y: headerBottomY },
      end:   { x: legendX + chipColW, y: legendBottom },
      thickness: 0.5, color: COLOR_BORDER,
    })

    for (let i = 0; i < entries.length; i++) {
      const ent = entries[i]
      const rowTop = headerBottomY - i * legendBodyRowH
      const rowBottom = rowTop - legendBodyRowH
      if (i > 0) {
        w.pg.drawLine({
          start: { x: legendX,            y: rowTop },
          end:   { x: legendX + legendW,  y: rowTop },
          thickness: 0.5, color: COLOR_BORDER,
        })
      }
      const textY = rowBottom + cellPadY + (legendBodyRowH - cellPadY * 2 - TAG_SIZE) / 2
      w.pg.drawText(san(ent.chip), {
        x: legendX + cellPadX, y: textY,
        size: TAG_SIZE, font: bf, color: ent.chipColor,
      })
      w.pg.drawText(san(ent.hint), {
        x: legendX + chipColW + cellPadX, y: textY,
        size: TINY_SIZE, font: rf, color: COLOR_MUTED,
      })
    }

    // Advance cursor past whichever table ends lower on the page.
    w.y = Math.min(countBottom, legendBottom)
  }
  w.skip(8)
  // --- Review notice ---
  {
    const padX = 8
    const padY = 7
    const iconR = 6
    const iconGap = 7
    const noticeSize = 8.5
    const noticeLineH = noticeSize * LH
    const noticeTextX = MARGIN_L + padX + iconR * 2 + iconGap
    const noticeTextW = PAGE_W - MARGIN_R - padX - noticeTextX
    const decisionStyles = [
      { term: labels.validTag, color: COLOR_VALID_TEXT },
      { term: labels.citationTag, color: COLOR_CITATION_TEXT },
      { term: labels.fabricatedTag, color: COLOR_FABRICATED_TEXT },
    ]
    const escapedTerms = decisionStyles.map(({ term }) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')
    const decisionPattern = new RegExp(`(${escapedTerms})`, 'gi')
    const styledChunks = san(labels.reviewNotice).split(decisionPattern).filter(Boolean).map(chunk => {
      const style = decisionStyles.find(({ term }) => term.toLocaleLowerCase() === chunk.toLocaleLowerCase())
      return { text: chunk, font: style ? bf : rf, color: style?.color ?? COLOR_DARK }
    })
    const noticeLines: Array<Array<{ text: string; font: PDFFont; color: RGB }>> = [[]]
    let lineW = 0
    let pendingSpace = ''
    for (const chunk of styledChunks) {
      for (const token of chunk.text.match(/\S+|\s+/g) ?? []) {
        if (token.includes('\n')) {
          if (lineW > 0) noticeLines.push([])
          lineW = 0
          pendingSpace = ''
          continue
        }
        if (/^\s+$/.test(token)) { pendingSpace = ' '; continue }
        const text = pendingSpace + token
        const tokenW = chunk.font.widthOfTextAtSize(text, noticeSize)
        if (lineW > 0 && lineW + tokenW > noticeTextW) {
          noticeLines.push([])
          lineW = 0
          pendingSpace = ''
        }
        const lineText = (lineW === 0 ? token : text)
        noticeLines[noticeLines.length - 1].push({ text: lineText, font: chunk.font, color: chunk.color })
        lineW += chunk.font.widthOfTextAtSize(lineText, noticeSize)
        pendingSpace = ''
      }
    }
    const noticeH = Math.max(iconR * 2, noticeLines.length * noticeLineH) + padY * 2
    w.ensure(noticeH)

    const noticeTop = w.y
    const noticeBottom = noticeTop - noticeH
    w.rect(MARGIN_L, noticeBottom, CONTENT_W, noticeH, { fill: COLOR_NOTICE_BG, border: COLOR_NOTICE_BORDER, bw: 0.5 })

    const iconX = MARGIN_L + padX + iconR
    const iconY = noticeTop - noticeH / 2
    w.pg.drawCircle({ x: iconX, y: iconY, size: iconR, borderColor: COLOR_NOTICE_ICON, borderWidth: 1 })
    const infoW = bf.widthOfTextAtSize('i', 8)
    w.pg.drawText('i', { x: iconX - infoW / 2, y: iconY - 3, size: 8, font: bf, color: COLOR_NOTICE_ICON })

    const textTop = noticeTop - (noticeH - noticeLines.length * noticeLineH) / 2
    for (let i = 0; i < noticeLines.length; i++) {
      const lineWidth = noticeLines[i].reduce((total, run) => total + run.font.widthOfTextAtSize(run.text, noticeSize), 0)
      let x = noticeTextX + (noticeTextW - lineWidth) / 2
      for (const run of noticeLines[i]) {
        w.pg.drawText(run.text, {
          x,
          y: textTop - noticeSize - i * noticeLineH,
          size: noticeSize,
          font: run.font,
          color: run.color,
        })
        x += run.font.widthOfTextAtSize(run.text, noticeSize)
      }
    }
    w.y = noticeBottom
  }
  w.skip(8)
  w.pg.drawLine({ start: { x: MARGIN_L, y: w.y }, end: { x: PAGE_W - MARGIN_R, y: w.y }, thickness: 0.5, color: COLOR_BORDER })
  w.skip(10)

  // --- Sources --- preserve caller-provided order (matches middle-pane sort).
  const sorted = data.sources

  for (const src of sorted) {
    const top = measureTop(src, rf, bf)
    const bot = measureBottom(src, labels, rf, bf, includeBibliographic)
    const totalBoxH = BOX_PAD + top.height + 4 + 0.5 + 4 + bot.height + BOX_PAD
    const boxX = MARGIN_L
    const boxW = CONTENT_W

    // Never split a source box across pages
    if (w.y - totalBoxH < MARGIN_B) w.newPage()

    const boxTopY = w.y

    // Outer border — driven by the (possibly overridden) decision classification.
    const decision = effectiveDecision(src)
    let borderColor: RGB
    let borderWidth: number
    if (decision === 'citation') {
      borderColor = COLOR_CITATION_BORDER; borderWidth = 1.2
    } else if (decision === 'fabricated') {
      borderColor = COLOR_FABRICATED_BORDER; borderWidth = 1.2
    } else if (decision === 'valid') {
      borderColor = COLOR_VALID_BORDER; borderWidth = 1.0
    } else {
      borderColor = COLOR_BORDER; borderWidth = 0.75
    }
    w.rect(boxX, boxTopY - totalBoxH, boxW, totalBoxH, { border: borderColor, bw: borderWidth })

    const cx = boxX + BOX_PAD
    w.skip(BOX_PAD)

    // === TOP SECTION: [N] colored text + ref text on same row ===
    {
      const refLabel = san(`[${src.refNumber}]`)
      const baseY = w.y - BODY_SIZE

      // Colored ref number
      w.pg.drawText(refLabel, { x: cx, y: baseY, size: BODY_SIZE, font: bf, color: statusColor(src.status) })

      // First line of ref text on same baseline
      const textX = cx + top.badgeW + top.badgeGap
      if (top.refLines.length > 0) {
        w.pg.drawText(san(top.refLines[0]), { x: textX, y: baseY, size: SMALL_SIZE, font: rf, color: COLOR_TEXT })
      }

      // Advance past first row
      w.skip(BODY_SIZE * LH)

      // Remaining lines indented so they line up with the first letter
      // of the source text (past the [N] badge) instead of sliding
      // back under the badge.
      for (let i = 1; i < top.refLines.length; i++) {
        w.text(top.refLines[i], textX, SMALL_SIZE, rf, COLOR_TEXT)
      }

      // Google Scholar + Google Search links sit at the end of the citation
      // block so they are visually above the divider and the match section.
      {
        const gLinks = [
          { label: 'Google Scholar', url: src.scholarUrl },
          { label: 'Google Search', url: src.googleUrl },
        ].filter(l => l.url)
        if (gLinks.length) {
          w.skip(2)
          const uy = w.y - TINY_SIZE
          const gap = 12
          let gx = textX
          for (const link of gLinks) {
            const lt = san(link.label)
            const lw = bf.widthOfTextAtSize(lt, TINY_SIZE)
            w.pg.drawText(lt, { x: gx, y: uy, size: TINY_SIZE, font: bf, color: COLOR_LINK })
            w.pg.drawLine({ start: { x: gx, y: uy - 1 }, end: { x: gx + lw, y: uy - 1 }, thickness: 0.4, color: COLOR_LINK })
            w.addLink(link.url!, gx, uy - 1, lw, TINY_SIZE + 2)
            gx += lw + gap
          }
          w.skip(TINY_SIZE * LH)
        }
      }
    }

    // Advance to divider
    const topDrawn = boxTopY - w.y - BOX_PAD
    if (topDrawn < top.height) w.skip(top.height - topDrawn)
    w.skip(4)

    // Divider line
    w.pg.drawLine({
      start: { x: boxX + 1, y: w.y },
      end: { x: boxX + boxW - 1, y: w.y },
      thickness: 0.5, color: COLOR_BORDER,
    })
    w.skip(4)

    // === BOTTOM SECTION: tags (inline) + best match card ===

    // Tags: left-aligned tags on the left, right-aligned tag at right end, same row
    if (bot.tags.length) {
      const tagGap = 8
      const tagLh = TAG_SIZE * LH
      const leftTags = bot.tags.filter(t => t.align !== 'right')
      const rightTag = bot.tags.find(t => t.align === 'right')
      const baseY = w.y - TAG_SIZE

      // Draw left-aligned tags
      let tx = cx
      for (let i = 0; i < leftTags.length; i++) {
        const tag = leftTags[i]
        const needed = tag.width + (tx > cx ? tagGap : 0)
        if (tx + needed > cx + BOX_INNER_W && tx > cx) {
          w.skip(tagLh)
          tx = cx
        }
        if (tx > cx) tx += tagGap
        if (tag.highestMatch) {
          // Custom render: prefix + yellow pill (db name) + score in score color.
          const hm = tag.highestMatch
          const prefixW = bf.widthOfTextAtSize(san(hm.prefix), TAG_SIZE)
          const dbW = bf.widthOfTextAtSize(san(hm.db), TAG_SIZE) + 8
          const baseY2 = w.y - TAG_SIZE
          // Prefix
          w.pg.drawText(san(hm.prefix), { x: tx, y: baseY2, size: TAG_SIZE, font: bf, color: COLOR_DARK })
          // Pill
          const pillX = tx + prefixW
          const pillY = baseY2 - 2
          const pillH = TAG_SIZE + 4
          w.rect(pillX, pillY, dbW, pillH, { fill: COLOR_DB_BG })
          w.pg.drawText(san(hm.db), { x: pillX + 4, y: baseY2, size: TAG_SIZE, font: bf, color: COLOR_DB_TEXT })
          // Score
          const scoreX = pillX + dbW + 6
          w.pg.drawText(san(hm.scoreText), { x: scoreX, y: baseY2, size: TAG_SIZE, font: bf, color: hm.scoreColor })
        } else {
          w.pg.drawText(san(tag.text), { x: tx, y: w.y - TAG_SIZE, size: TAG_SIZE, font: bf, color: tag.color })
        }
        tx += tag.width
      }

      // Draw right-aligned tag at the right end of the row
      if (rightTag) {
        const rx = cx + BOX_INNER_W - rightTag.width
        w.pg.drawText(san(rightTag.text), { x: rx, y: baseY, size: TAG_SIZE, font: bf, color: rightTag.color })
      }

      w.skip(tagLh + 4)
    }

    // Best match card — left-aligned with cx (same as badge)
    if (bot.card && src.bestMatch) {
      const m = src.bestMatch
      const card = bot.card
      const cardX = cx
      const cardTopY = w.y

      // Card bg + border
      w.rect(cardX, cardTopY - card.totalHeight, CARD_W, card.totalHeight, {
        fill: COLOR_CARD_BG, border: COLOR_CARD_BORDER,
      })

      const ccx = cardX + CARD_PAD
      w.skip(CARD_PAD)

      // Title
      for (const line of card.titleLines)
        w.text(line, ccx, SMALL_SIZE, bf, COLOR_DARK)

      // Authors
      if (card.authorLines.length) {
        w.skip(2)
        for (const line of card.authorLines) w.text(line, ccx, SMALL_SIZE, rf, COLOR_MUTED)
      }

      // Journal
      if (card.journalLines.length) {
        w.skip(2)
        for (const line of card.journalLines) w.text(line, ccx, SMALL_SIZE, rf, COLOR_SOURCE)
      }

      // Year | DOI
      if (card.metaLine) {
        w.skip(2)
        if (m.doi) {
          const yearPart = m.year ? `${m.year}   ` : ''
          const doiPart = `DOI: ${m.doi}`
          const yw = yearPart ? rf.widthOfTextAtSize(san(yearPart), SMALL_SIZE) : 0
          if (yearPart) w.pg.drawText(san(yearPart), { x: ccx, y: w.y - SMALL_SIZE, size: SMALL_SIZE, font: rf, color: COLOR_MUTED })
          w.pg.drawText(san(doiPart), { x: ccx + yw, y: w.y - SMALL_SIZE, size: SMALL_SIZE, font: rf, color: COLOR_LINK })
          w.skip(SMALL_SIZE * LH)
        } else {
          w.text(card.metaLine, ccx, SMALL_SIZE, rf, COLOR_MUTED)
        }
      }

      // Bibliographic details — compact second block listing every populated
      // extra field (volume, issue, pages, publisher, editor, document type,
      // language, ISSN, ISBN). Header omitted; missing fields skipped.
      if (card.extras.length > 0) {
        w.skip(4)
        for (const lines of card.extraLines) {
          for (const line of lines) {
            w.text(line, ccx, TINY_SIZE, rf, COLOR_SOURCE)
          }
        }
      }

      // URL (underlined, clickable) — truncated to a single line in the
      // lower metadata area of the card.
      if (m.url) {
        w.skip(4)
        const displayUrl = truncateToWidth(san(m.url), rf, TINY_SIZE, CARD_INNER_W)
        const uy = w.y - TINY_SIZE
        w.pg.drawText(displayUrl, { x: ccx, y: uy, size: TINY_SIZE, font: rf, color: COLOR_LINK })
        const uw = rf.widthOfTextAtSize(displayUrl, TINY_SIZE)
        w.pg.drawLine({ start: { x: ccx, y: uy - 1 }, end: { x: ccx + uw, y: uy - 1 }, thickness: 0.4, color: COLOR_LINK })
        w.skip(TINY_SIZE * LH)
        w.addLink(m.url, ccx, w.y, uw, TINY_SIZE * LH)
      }

      // Ensure cursor past card bottom
      const cardDrawn = cardTopY - w.y
      if (cardDrawn < card.totalHeight) w.skip(card.totalHeight - cardDrawn)
    }

    // Ensure cursor past box bottom
    const boxDrawn = boxTopY - w.y
    if (boxDrawn < totalBoxH) w.skip(totalBoxH - boxDrawn)

    w.skip(8)
  }

  return doc.save()
}

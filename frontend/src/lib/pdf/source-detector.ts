// TypeScript port of backend/services/source_detector.py.
//
// The port is deliberately function-for-function so parity against the Python
// implementation is easy to audit. The behavior — including regex semantics,
// gap thresholds, and fallback ordering — matches the Python version; any
// intentional divergence should be called out in a comment.

import type { BoundingBox, SourceRectangle } from '../api/types'
import type { ParsedPdf, TextBlock } from './types'
import { makeSourceId } from '../utils/source-id'

// -------------------------------------------------------------------------
// Patterns (transliterated from source_detector.py)
// -------------------------------------------------------------------------

// Strict header patterns — must match the whole line.
const HEADER_PATTERNS_STRICT: RegExp[] = [
  /^\s*(?:EK[-\s]?\d*\s*[A-Z]?[:.\s]*)?\s*(?:KAYNAKLAR|KAYNAK[CÇ]A)\s*$/i,
  /^\s*(?:APPENDIX\s+[\dA-Z]+\s*[:.\-–—]?\s*)?(?:REFERENCES?|SOURCES?|BIBLIOGRAPHY|WORKS?\s+CITED)\s*$/i,
  /^\s*(?:[IVXLC]+|\d+)\s*[.):\-–—]\s*(?:REFERENCES?|SOURCES?|BIBLIOGRAPHY|WORKS?\s+CITED)\s*$/i,
  /^\s*LITERAT[UÜ]R\s*$/i,
]

// Looser patterns for short lines that might have trailing punctuation.
const HEADER_PATTERNS_LOOSE: RegExp[] = [
  /(?:EK[-\s]?\d*\s*[A-Z]?[:.\s]*)?\s*(?:KAYNAKLAR|KAYNAK[CÇ]A)/i,
  /(?:APPENDIX\s+[\dA-Z]+\s*[:.\-–—]?\s*)?(?:REFERENCES?|SOURCES?|BIBLIOGRAPHY)/i,
]

// Individual source-number patterns, matched at start-of-line.
const REF_NUMBER_PATTERNS: RegExp[] = [
  /^\s*\[(\d{1,3})\]\s*/, // [1] Text...
  /^\s*(\d{1,3})\.\s+/, // 1. Text...
  /^\s*(\d{1,3})\)\s+/, // 1) Text...
  /^\s*(\d{1,3})-(?!\d)\s*/, // 1- Text... (but not 014-1315-y)
]

// Instruction text — blocks matching these are dropped entirely.
const INSTRUCTION_PATTERNS: RegExp[] = [
  /^\d{4}BF[-\s]?\d+/,
  /^bu\s+b[oö]l[uü]mde/i,
  /^proje\s+[oö]nerisinde/i,
  /sayfas[ıi]ndaki\s+a[cç][ıi]klamalara/i,
  /verilmeli\s+ve\s+bu\s+kaynaklara/i,
  /sonuna\s+DOI\s+numaras[ıi]/i,
  /i[cç]erisinde\s+(?:ilgili\s+yerlerde|at[ıi]f)/i,
  /^g[uü]ncelleme\s+tarihi/i,
  /^zorunludur\s*[.\s]*$/i,
  /yap[ıi]lmal[ıi]d[ıi]r/i,
  /kaynaklar[ıi]n\s+listesi/i,
  /bibliyografik/i,
  /verilerin.duzenlenmesi/i,
  /eklenmesi\s*$/i,
  /Kurum\sİçi\sSınırsız\sKullanım\s\/\sKişisel\sVeri\sDeğil/i,
  /^\s*\d{4}\s*[-–—]{1,2}\s*BF\s+G[uü]ncelleme\s+Tarihi\s*:\s*\d{2}\/\d{2}\/\d{4}\s*$/i,
]

// Strict author-start patterns. Each must match at start-of-string.
const AUTHOR_START_PATTERNS: RegExp[] = [
  // LastName, A. or LastName, A.B. (initials with periods)
  /^[A-ZÀ-Ž\u00C0-\u024F][A-Za-z0-9_à-ž\u00C0-\u024F'\u2019\-]+,\s+[A-Z]\./u,
  // LastName, FirstName (multi-letter first name)
  /^[A-ZÀ-Ž\u00C0-\u024F][A-Za-z0-9_à-ž\u00C0-\u024F'\u2019\-]+,\s+[A-Z][a-zà-ž\u00C0-\u024F]/u,
  // LastName A. (Vancouver: no comma, initials with period)
  /^[A-ZÀ-Ž\u00C0-\u024F][a-zà-ž\u00C0-\u024F]+\s+[A-Z]\./u,
  // LastName &
  /^[A-ZÀ-Ž\u00C0-\u024F][a-zà-ž\u00C0-\u024F]+\s+&/u,
  // All-caps organization followed by year, opening paren, or Title-case word
  /^[A-Z]{2,},?\s+(?:\d{4}|\(|[A-Z][a-z])/,
  // Quoted title at the start of a citation
  /^"[A-Z]/,
  /^\u201C[A-Z]/,
  // Last name (no comma) followed by year in parens: "Smith (2020)"
  /^[A-ZÀ-Ž\u00C0-\u024F][a-zà-ž\u00C0-\u024F]+\s+\(\d{4}/u,
  // Title-only citations followed by a URL
  /^[A-Z][A-Za-z0-9_'\u2019\-]+(?:\s+[A-Z][A-Za-z0-9_'\u2019\-]+){1,5}\.\s+https?:\/\//,
]

const CONTINUATION_PATTERNS: RegExp[] = [
  /^\(\d{4}[a-z]?\)\.?\s/,
  /^\d+(?:[\-–—]\d+)?\./,
  // Journal name + volume(/issue): page — e.g. "EFSA Journal, 7(11): 1331".
  // Strong continuation signal because no real citation header has this shape.
  /^[A-Z][\w&'\-\s.]*,\s+\d+(?:\(\d+\))?\s*:\s*\d/,
]

const CITATION_SIGNAL_PATTERNS: RegExp[] = [
  /(?:19|20)\d{2}/, // year 1900-2099
  /\bet\s+al\b/i,
  /\b10\.\d{4,9}\//, // DOI prefix
  /https?:\/\//,
  /"[^"]{10,}"/,
  /\u201C[^\u201D]{10,}\u201D/,
  /\bpp\.\s*\d/,
  /\bvol\.\s*\d/i,
]

// Patterns that mark the previous block as still mid-citation, so the next
// line should NOT be treated as a new reference start. Beyond the obvious
// trailing separators (`,`, `;`, `&`, `ve`, `and`, `eds.`) we also recognise:
//   `:`        — `Editör:`, `Eds.:` immediately preceding a multi-author list
//   trailing `-` — mid-word break (e.g. `re-` continuing into next line)
//   single capital initial like `J.`, `C.` — author initial, title follows
//   year + period like `2024.` — author/year header, title follows
const PREV_CONTINUES_AUTHORS_RE =
  /(?:[,;&:]|\bve|\band|\beds?\.|-|\b[A-Z]\.|\b(?:19|20)\d{2}[a-z]?\.)\s*$/i

// Visual terminators that suggest the previous block actually ended a
// citation. Required for `authorBoundary` so that wrapped title lines like
// `... Holter EKG Analizinde\nNormal, Anormal, ...` don't trigger a split:
// the prev block ends with the bare word `Analizinde` — not a terminator —
// so we keep accumulating instead of starting a new ref.
//
// We also accept URLs and DOIs as terminators because many citations end
// with a bare URL/DOI (no trailing period) — without this, refs that end
// with `https://doi.org/...` would block the next legitimate ref start.
const PREV_TERMINATED_RE =
  /(?:[.”’")\]]|https?:\/\/\S+|\b10\.\d{4,9}\/\S+)\s*$/i

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

type PageBlock = [number, TextBlock]

function testAny(patterns: RegExp[], text: string): boolean {
  for (const p of patterns) if (p.test(text)) return true
  return false
}

function isDigits(text: string): boolean {
  return /^\d+$/.test(text)
}

function isInstructionText(text: string): boolean {
  return testAny(INSTRUCTION_PATTERNS, text)
}

function hasRefNumber(text: string): boolean {
  return testAny(REF_NUMBER_PATTERNS, text)
}

function looksLikeCitation(text: string): boolean {
  return testAny(CITATION_SIGNAL_PATTERNS, text)
}

function startsWithAuthorPattern(text: string): boolean {
  return testAny(AUTHOR_START_PATTERNS, text)
}

function looksLikeContinuation(text: string): boolean {
  return testAny(CONTINUATION_PATTERNS, text)
}

function extractRefNumber(text: string): number | null {
  for (const pattern of REF_NUMBER_PATTERNS) {
    const m = text.match(pattern)
    if (m && m[1]) {
      const n = parseInt(m[1], 10)
      if (!Number.isNaN(n)) return n
    }
  }
  return null
}

// -------------------------------------------------------------------------
// _merge_line_fragments
// -------------------------------------------------------------------------

function mergeLineFragments(blocks: TextBlock[]): TextBlock[] {
  if (blocks.length === 0) return blocks

  const sorted = [...blocks].sort((a, b) => {
    const ya = a.bbox[1] + a.bbox[3]
    const yb = b.bbox[1] + b.bbox[3]
    if (ya !== yb) return ya - yb
    return a.bbox[0] - b.bbox[0]
  })

  const merged: TextBlock[] = []
  let current: TextBlock = sorted[0]

  for (let i = 1; i < sorted.length; i++) {
    const block = sorted[i]
    const curYMid = (current.bbox[1] + current.bbox[3]) / 2
    const blkYMid = (block.bbox[1] + block.bbox[3]) / 2
    const curHeight = current.bbox[3] - current.bbox[1]
    const blkHeight = block.bbox[3] - block.bbox[1]
    const lineHeight = Math.max(curHeight, blkHeight, 5)

    const sameLine = Math.abs(curYMid - blkYMid) < lineHeight * 0.6
    const hGap = block.bbox[0] - current.bbox[2]
    const closeH = hGap < lineHeight * 3

    if (sameLine && closeH) {
      // pdfjs sometimes returns adjacent text items with no horizontal gap
      // (e.g. `133` and `:` arrive as separate items, `Wireless` arrives as
      // `W` + `ireless`). Always inserting a space produced `133 : 103550`
      // and `W ireless`. Suppress the space when fragments visually touch.
      const sep =
        current.text.endsWith('-')
          ? ''
          : hGap < lineHeight * 0.25
            ? ''
            : ' '
      current = {
        text: current.text + sep + block.text,
        bbox: [
          Math.min(current.bbox[0], block.bbox[0]),
          Math.min(current.bbox[1], block.bbox[1]),
          Math.max(current.bbox[2], block.bbox[2]),
          Math.max(current.bbox[3], block.bbox[3]),
        ],
        page: current.page,
        font_size: Math.max(current.font_size, block.font_size),
        font_name: current.font_name,
        is_bold: current.is_bold || block.is_bold,
      }
    } else {
      merged.push(current)
      current = block
    }
  }

  merged.push(current)
  return merged
}

// -------------------------------------------------------------------------
// _find_source_header
// -------------------------------------------------------------------------

function findSourceHeader(blocks: PageBlock[]): number | null {
  const candidates: number[] = []
  for (let idx = 0; idx < blocks.length; idx++) {
    const block = blocks[idx][1]
    const text = block.text.trim()

    // Strict match: exact header line.
    if (testAny(HEADER_PATTERNS_STRICT, text)) {
      candidates.push(idx)
      continue
    }

    // Loose match: short line with header keywords.
    if (text.length < 50) {
      if (block.is_bold && testAny(HEADER_PATTERNS_LOOSE, text)) {
        candidates.push(idx)
        continue
      }
      if (testAny(HEADER_PATTERNS_LOOSE, text)) {
        candidates.push(idx)
        continue
      }
    }
  }

  if (candidates.length === 0) return null
  if (candidates.length === 1) return candidates[0]

  // Multiple candidates: prefer one followed by real source content.
  for (const candidateIdx of candidates) {
    const blocksAfter = blocks.slice(candidateIdx + 1, candidateIdx + 10)
    let nonInstructionCount = 0
    for (const [, b] of blocksAfter) {
      const t = b.text.trim()
      if (!t || t.length < 5) continue
      if (isInstructionText(t)) continue
      nonInstructionCount += 1
    }
    if (nonInstructionCount >= 3) return candidateIdx
  }

  return candidates[0]
}

// -------------------------------------------------------------------------
// _filter_source_blocks
// -------------------------------------------------------------------------

function filterSourceBlocks(blocks: PageBlock[]): PageBlock[] {
  const filtered: PageBlock[] = []
  for (const entry of blocks) {
    const text = entry[1].text.trim()
    if (!text) continue
    // Skip page numbers (1-3 digit standalone). 4+ digit numbers can appear
    // legitimately as article/page IDs at the end of a citation.
    if (isDigits(text) && text.length <= 3) continue
    if (isInstructionText(text)) continue
    // Drop any block whose text is itself a section header (KAYNAKÇA,
    // KAYNAKLAR, EK-X: KAYNAKLAR, REFERENCES, BIBLIOGRAPHY, …). The chosen
    // refStartIdx is already excluded by the slice in detectSources, but a
    // second header block — duplicated heading, reprinted continuation
    // header, or appendix divider — survives that slice and would otherwise
    // land inside a detected rectangle. Length gate on the loose pattern
    // mirrors findSourceHeader so it doesn't over-match long lines.
    if (testAny(HEADER_PATTERNS_STRICT, text)) continue
    if (text.length < 50 && testAny(HEADER_PATTERNS_LOOSE, text)) continue
    if (text.toLowerCase().includes('tubitak.gov.tr') && !hasRefNumber(text)) continue
    filtered.push(entry)
  }
  return filtered
}

// -------------------------------------------------------------------------
// _validate_numbered_sources
// -------------------------------------------------------------------------

function validateNumberedSources(sources: SourceRectangle[]): boolean {
  if (sources.length === 0) return false
  const nums: number[] = []
  for (const s of sources) {
    if (s.ref_number != null) nums.push(s.ref_number)
  }
  if (nums.length === 0) return false
  if (nums[0] > 10) return false
  let ascending = 0
  for (let i = 1; i < nums.length; i++) if (nums[i] > nums[i - 1]) ascending += 1
  if (nums.length > 2 && ascending < Math.floor(nums.length / 3)) return false
  return true
}

// -------------------------------------------------------------------------
// _split_numbered_sources
// -------------------------------------------------------------------------

function splitNumberedSources(blocks: PageBlock[], pdfId: string): SourceRectangle[] {
  const sources: SourceRectangle[] = []
  let currentText = ''
  let currentNum: number | null = null
  let currentBlocks: PageBlock[] = []

  const isValidNext = (candidate: number): boolean => {
    if (currentNum === null) return candidate <= 3
    return currentNum < candidate && candidate <= currentNum + 2
  }

  for (const entry of blocks) {
    const [pageNum, block] = entry
    const text = block.text.trim()
    if (!text) continue

    const candidate = extractRefNumber(text)
    const isNewRef = candidate !== null && isValidNext(candidate)

    if (isNewRef) {
      if (currentBlocks.length > 0) {
        const src = createSourceRectangle(currentBlocks, currentText, currentNum, pdfId)
        if (src) sources.push(src)
      }
      currentText = text
      currentNum = candidate
      currentBlocks = [[pageNum, block]]
    } else if (currentBlocks.length > 0) {
      if (isContinuation(currentBlocks, pageNum, block)) {
        currentText += ' ' + text
        currentBlocks.push([pageNum, block])
      }
    }
  }

  if (currentBlocks.length > 0) {
    const src = createSourceRectangle(currentBlocks, currentText, currentNum, pdfId)
    if (src) sources.push(src)
  }

  return sources
}

// -------------------------------------------------------------------------
// _split_by_empty_lines
// -------------------------------------------------------------------------

function splitByEmptyLines(blocks: PageBlock[], pdfId: string): SourceRectangle[] {
  if (blocks.length < 2) return []

  // Step 1: baseline gaps per page
  const gapsPerPage = new Map<number, number[]>()
  for (let i = 1; i < blocks.length; i++) {
    const [prevPage, prevBlock] = blocks[i - 1]
    const [currPage, currBlock] = blocks[i]
    if (currPage !== prevPage) continue
    const gap = currBlock.bbox[1] - prevBlock.bbox[3]
    if (gap >= -2) {
      const list = gapsPerPage.get(currPage) ?? []
      list.push(Math.max(gap, 0))
      gapsPerPage.set(currPage, list)
    }
  }
  if (gapsPerPage.size === 0) return []

  const baselineGap = new Map<number, number>()
  for (const [page, gaps] of gapsPerPage) {
    baselineGap.set(page, gaps.length > 0 ? Math.min(...gaps) : 0)
  }

  // Average line height per page
  const lineHeights = new Map<number, number[]>()
  for (const [pageNum, block] of blocks) {
    const h = block.bbox[3] - block.bbox[1]
    if (h > 0) {
      const list = lineHeights.get(pageNum) ?? []
      list.push(h)
      lineHeights.set(pageNum, list)
    }
  }
  const lineHeightPerPage = new Map<number, number>()
  for (const [page, heights] of lineHeights) {
    const sum = heights.reduce((a, b) => a + b, 0)
    lineHeightPerPage.set(page, sum / heights.length)
  }

  // Step 2: hanging-indent edge (smallest x0 per page)
  const minX0PerPage = new Map<number, number>()
  for (const [pageNum, block] of blocks) {
    const x0 = block.bbox[0]
    const cur = minX0PerPage.get(pageNum)
    if (cur === undefined || x0 < cur) minX0PerPage.set(pageNum, x0)
  }

  // Step 3: boundary detection
  const boundaryIndices: number[] = [0]
  for (let i = 1; i < blocks.length; i++) {
    const [prevPage, prevBlock] = blocks[i - 1]
    const [currPage, currBlock] = blocks[i]
    const text = currBlock.text.trim()

    if (currPage !== prevPage) {
      const pageMinX0 = minX0PerPage.get(currPage) ?? 0
      const isFlushLeft = currBlock.bbox[0] <= pageMinX0 + 2.0
      const looksLikeAuthor = !!text && startsWithAuthorPattern(text)
      if ((isFlushLeft || looksLikeAuthor) && text.length >= 15) {
        boundaryIndices.push(i)
      }
      continue
    }

    const gap = currBlock.bbox[1] - prevBlock.bbox[3]
    const baseline = baselineGap.get(currPage) ?? 0
    const lh = lineHeightPerPage.get(currPage) ?? 12

    const gapIsBlankLine = gap > Math.max(baseline + lh * 0.4, baseline * 3 + 1)

    const pageMinX0 = minX0PerPage.get(currPage) ?? 0
    const currIsFlushLeft = currBlock.bbox[0] <= pageMinX0 + 2.0
    const prevIsIndented = prevBlock.bbox[0] > pageMinX0 + 3.0
    const indentBoundary = currIsFlushLeft && prevIsIndented && text.length >= 15

    const prevText = prevBlock.text.trim()
    const prevContinuesAuthors = PREV_CONTINUES_AUTHORS_RE.test(prevText)
    const prevLooksTerminated = PREV_TERMINATED_RE.test(prevText)
    const authorBoundary =
      text.length >= 15 &&
      !prevContinuesAuthors &&
      prevLooksTerminated &&
      startsWithAuthorPattern(text) &&
      !looksLikeContinuation(text)

    if (gapIsBlankLine || indentBoundary || authorBoundary) {
      boundaryIndices.push(i)
    }
  }

  if (boundaryIndices.length < 4) return []

  // Step 4: group blocks into sources. If a candidate group doesn't look like
  // its own citation (too short or no citation signals), reattach it to the
  // previous source instead of dropping it — those fragments are almost
  // always wrap-around tails (publisher+city, edition info) that the
  // boundary detector falsely cleaved off. Driving case: 126E156 ref 9
  // where `Springer, New York.` was split off and dropped.
  const sources: SourceRectangle[] = []
  const sourceBlocks: PageBlock[][] = []
  const sourceTexts: string[] = []
  let refCounter = 0

  for (let b = 0; b < boundaryIndices.length; b++) {
    const startIdx = boundaryIndices[b]
    const endIdx = b + 1 < boundaryIndices.length ? boundaryIndices[b + 1] : blocks.length

    const refBlocks = blocks.slice(startIdx, endIdx)
    const refText = refBlocks
      .map(([, blk]) => blk.text.trim())
      .filter(t => t.length > 0)
      .join(' ')

    if (!refText) continue

    // Very short fragments are almost always wrap-around tails the boundary
    // detector cleaved off (publisher+city, edition info, ISBN). Reattach
    // them to the previous source instead of dropping. Length cap kept low
    // so legitimate short refs like ISO standards stay independent.
    if (refText.length < 30 && sources.length > 0) {
      const lastIdx = sources.length - 1
      const mergedBlocks = [...sourceBlocks[lastIdx], ...refBlocks]
      const mergedText = sourceTexts[lastIdx] + ' ' + refText
      const replacement = createSourceRectangle(
        mergedBlocks,
        mergedText,
        sources[lastIdx].ref_number ?? null,
        pdfId
      )
      if (replacement) {
        sources[lastIdx] = replacement
        sourceBlocks[lastIdx] = mergedBlocks
        sourceTexts[lastIdx] = mergedText
      }
      continue
    }

    refCounter += 1
    const src = createSourceRectangle(refBlocks, refText, refCounter, pdfId)
    if (src) {
      sources.push(src)
      sourceBlocks.push(refBlocks)
      sourceTexts.push(refText)
    }
  }

  return sources
}

// -------------------------------------------------------------------------
// _split_unnumbered_sources
// -------------------------------------------------------------------------

function splitUnnumberedSources(blocks: PageBlock[], pdfId: string): SourceRectangle[] {
  const sources: SourceRectangle[] = []
  let currentText = ''
  let currentBlocks: PageBlock[] = []
  let refCounter = 0

  for (const entry of blocks) {
    const [pageNum, block] = entry
    const text = block.text.trim()
    if (!text) continue

    // Very short fragments are always continuations
    if (text.length < 10 && currentBlocks.length > 0) {
      if (isContinuation(currentBlocks, pageNum, block)) {
        currentText += ' ' + text
        currentBlocks.push([pageNum, block])
        continue
      }
    }

    // "(YEAR). Title..." is a continuation
    if (looksLikeContinuation(text) && currentBlocks.length > 0) {
      if (isContinuation(currentBlocks, pageNum, block)) {
        currentText += ' ' + text
        currentBlocks.push([pageNum, block])
        continue
      }
    }

    const isNewRef = isUnnumberedRefStart(text, currentBlocks, pageNum, block)

    if (isNewRef) {
      if (currentBlocks.length > 0) {
        refCounter += 1
        const src = createSourceRectangle(currentBlocks, currentText, refCounter, pdfId)
        if (src) sources.push(src)
      }
      currentText = text
      currentBlocks = [[pageNum, block]]
    } else if (currentBlocks.length > 0) {
      if (isContinuation(currentBlocks, pageNum, block)) {
        currentText += ' ' + text
        currentBlocks.push([pageNum, block])
      } else {
        refCounter += 1
        const src = createSourceRectangle(currentBlocks, currentText, refCounter, pdfId)
        if (src) sources.push(src)
        currentText = text
        currentBlocks = [[pageNum, block]]
      }
    } else {
      currentText = text
      currentBlocks = [[pageNum, block]]
    }
  }

  if (currentBlocks.length > 0) {
    refCounter += 1
    const src = createSourceRectangle(currentBlocks, currentText, refCounter, pdfId)
    if (src) sources.push(src)
  }

  return sources
}

function isUnnumberedRefStart(
  text: string,
  currentBlocks: PageBlock[],
  pageNum: number,
  block: TextBlock
): boolean {
  if (!text || text.length < 15) return false

  // Hanging indent detection
  if (currentBlocks.length > 0) {
    const [lastPage, lastBlock] = currentBlocks[currentBlocks.length - 1]
    if (pageNum === lastPage) {
      const indentDiff = lastBlock.bbox[0] - block.bbox[0]
      if (indentDiff > 3.0) return true
    }
  }

  // Must start with uppercase letter, quote, or opening paren
  const first = text[0]
  const isUpper = first !== first.toLowerCase() && first === first.toUpperCase()
  if (!(isUpper || first === '"' || first === '(' || first === '\u201C')) return false

  if (!startsWithAuthorPattern(text)) return false

  return true
}

function isContinuation(currentBlocks: PageBlock[], pageNum: number, block: TextBlock): boolean {
  if (currentBlocks.length === 0) return false
  const [lastPage, lastBlock] = currentBlocks[currentBlocks.length - 1]

  if (pageNum === lastPage) {
    const lastY1 = lastBlock.bbox[3]
    const currY0 = block.bbox[1]
    const lineHeight = Math.max(lastBlock.bbox[3] - lastBlock.bbox[1], 10)
    return currY0 - lastY1 < lineHeight * 5.0
  } else if (pageNum === lastPage + 1) {
    return true
  }
  return false
}

// -------------------------------------------------------------------------
// _create_source_rectangle
// -------------------------------------------------------------------------

function createSourceRectangle(
  entries: PageBlock[],
  text: string,
  refNum: number | null,
  pdfId: string
): SourceRectangle | null {
  if (entries.length === 0 || !text.trim()) return null

  const pagesBlocks = new Map<number, TextBlock[]>()
  for (const [pageNum, block] of entries) {
    const list = pagesBlocks.get(pageNum) ?? []
    list.push(block)
    pagesBlocks.set(pageNum, list)
  }

  const padding = 3.0
  const bboxes: BoundingBox[] = []
  const sortedPages = [...pagesBlocks.keys()].sort((a, b) => a - b)
  for (const pageNum of sortedPages) {
    const pageBlocks = pagesBlocks.get(pageNum)!
    const x0 = Math.max(0, Math.min(...pageBlocks.map(b => b.bbox[0])) - padding)
    const y0 = Math.max(0, Math.min(...pageBlocks.map(b => b.bbox[1])) - padding)
    const x1 = Math.max(...pageBlocks.map(b => b.bbox[2])) + padding
    const y1 = Math.max(...pageBlocks.map(b => b.bbox[3])) + padding
    bboxes.push({ x0, y0, x1, y1, page: pageNum })
  }

  // Collapse any whitespace runs introduced by the merge (or by pdfjs items
  // separated by spaces of varying width) so detector text matches what
  // `extractTextInBbox` produces — that's the format the approved cache
  // stores and the verification pipeline expects.
  const trimmed = text.replace(/\s+/g, ' ').trim()
  return {
    id: makeSourceId(pdfId, trimmed),
    pdf_id: pdfId,
    bbox: bboxes[0],
    bboxes: bboxes.length > 1 ? bboxes : [],
    text: trimmed,
    ref_number: refNum ?? undefined,
    status: 'detected',
  }
}

// -------------------------------------------------------------------------
// Top-level entry point
// -------------------------------------------------------------------------

export interface DetectResult {
  sources: SourceRectangle[]
  numbered: boolean
}

export function detectSources(document: ParsedPdf): DetectResult {
  const allBlocks: PageBlock[] = []
  for (const page of document.pages) {
    const merged = mergeLineFragments(page.text_blocks)
    for (const block of merged) {
      allBlocks.push([page.page_num, block])
    }
  }

  const refStartIdx = findSourceHeader(allBlocks)
  if (refStartIdx === null) return { sources: [], numbered: false }

  let refBlocks = allBlocks.slice(refStartIdx + 1)
  if (refBlocks.length === 0) return { sources: [], numbered: false }

  refBlocks = filterSourceBlocks(refBlocks)

  // Try numbered sources first
  let sources = splitNumberedSources(refBlocks, document.id)
  if (sources.length > 0 && !validateNumberedSources(sources)) sources = []

  if (sources.length > 0) return { sources, numbered: true }

  // Unnumbered: empty-line gap detection first, APA fallback otherwise
  const gapSources = splitByEmptyLines(refBlocks, document.id)
  if (gapSources.length > 0) return { sources: gapSources, numbered: false }

  return { sources: splitUnnumberedSources(refBlocks, document.id), numbered: false }
}

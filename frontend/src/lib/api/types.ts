// PDF and parsing types
export interface PdfDocument {
  id: string
  name: string
  path: string
  status: 'pending' | 'parsing' | 'parsed' | 'approved' | 'error'
  source_count: number
  /**
   * Client-only — set by the renderer's parseAndDetect() based on which
   * detection strategy fired (numbered list vs heuristic). The backend
   * persists this inside the parse-cache JSON (backend/api/parsing.py) but
   * never sets it on the PdfDocument Pydantic model. Always `false` on
   * objects that come straight from a REST endpoint returning PdfDocument.
   */
  numbered: boolean
  error?: string
}

export interface BoundingBox {
  x0: number
  y0: number
  x1: number
  y1: number
  page: number
}

export interface SourceRectangle {
  id: string
  pdf_id: string
  bbox: BoundingBox
  bboxes?: BoundingBox[]  // multi-page: one bbox per page
  text: string
  ref_number?: number
  status: 'detected' | 'edited' | 'approved'
}

export interface PageData {
  page_num: number
  width: number
  height: number
}

// Parsed source fields (NER extraction result)
export interface ParsedSource {
  raw_text: string
  title: string
  authors: string[]
  year?: number
  url?: string
  journal?: string
  citation_format?: string
  extraction_method: string
  parse_confidence: number
}

// Verification types
export type VerifyStatus = 'high' | 'medium' | 'low' | 'pending' | 'in_progress'

export type ProblemTag = '!authors' | '!doi/arXiv' | '!year' | '!journal' | '!title'

export type DecisionTag = 'valid' | 'citation' | 'fabricated'

export type TagKey = 'authors' | 'year' | 'title' | 'journal' | 'doi/arXiv'

export interface MatchResult {
  database: string
  title: string
  authors: string[]
  year?: number
  doi?: string
  journal?: string
  url: string
  search_url: string
  score: number
  // Unclamped composite (base + bonus). Same as ``score`` when the sum
  // stays in [0, 1]; only diverges (>1.0) when bonuses push it past the
  // cap. Used for "best match" ranking so two saturated 1.00 candidates
  // can still be ordered by signal strength.
  raw_score?: number
  match_details: {
    title_similarity: number
    author_match: number
    year_match: number
    url_match: boolean
  }
  // Bibliographic extras — populated when the underlying database returns
  // them. All optional; missing/empty fields mean "field unavailable".
  volume?: string | null
  issue?: string | null
  pages?: string | null
  publisher?: string
  editor?: string[]
  document_type?: string
  language?: string
  issn?: string[]
  isbn?: string[]
}

export interface VerificationResult {
  source_id: string
  status: VerifyStatus
  problem_tags: string[]
  decision_tag?: DecisionTag
  // Three-state user override for the decision pill. null = use decision_tag.
  decision_tag_override?: DecisionTag | null
  tag_overrides?: Record<string, boolean>
  url_liveness: Record<string, boolean>
  best_match?: MatchResult
  all_results: MatchResult[]
  databases_searched: string[]
  // Pre-built Google Scholar / Google Search URLs using the NER-extracted title
  scholar_url?: string
  google_url?: string
}

export interface PdfVerificationSummary {
  pdf_id: string
  high: number
  medium: number
  low: number
  in_progress: number
  total: number
  completed: boolean
}

// Settings types
export interface DatabaseConfig {
  id: string
  name: string
  enabled: boolean
}

export interface AppSettings {
  exported_pdf_dir?: string
  exclusion_list_url?: string
  exclusion_edit_url?: string
  databases: DatabaseConfig[]
  api_keys?: Record<string, string>
  polite_pool_email?: string
  // ISO-8601 date the user's OpenAIRE refresh token was last saved. The UI
  // derives "connected" state from the presence of api_keys.openaire and
  // uses this date to warn about the 1-month expiry window.
  openaire_token_saved_at?: string
  search_timeout: number
  max_concurrent_apis: number
  max_concurrent_sources_per_pdf: number
  auto_scholar_after_verify?: boolean
  strong_match_enabled?: boolean
  report_include_bibliographic?: boolean
  language?: 'tr' | 'en'
  auto_callout_text_fabricated?: string
  auto_callout_text_citation?: string
}

// WebSocket event types
export interface WsEvent {
  type: string
  data: Record<string, unknown>
}

export interface LogEntry {
  id: string
  timestamp: string
  level: 'info' | 'success' | 'warning' | 'error'
  message: string
  pdf_id?: string
  source_id?: string
  database?: string
  group_key?: string
}

// Verification progress tracking
export type DbCheckStatus = 'checking' | 'high' | 'medium' | 'low' | 'no_match' | 'timeout' | 'error' | 'rate_limited' | 'unauthorized' | 'skipped'

export interface DbCheckEntry {
  name: string
  status: DbCheckStatus
  searchUrl?: string
  errorMessage?: string
}

export interface SourceVerifyProgress {
  currentDb: string | null
  checkedDbs: DbCheckEntry[]
}

// Parse response types
export interface ParseResponse {
  job_id: string
}

export interface ParseStatusResponse {
  pdfs: PdfDocument[]
}

export interface VerifyResponse {
  job_id: string
}

// Google Scholar scan types
export interface ScholarCandidate {
  title: string
  authors: string[]
  year?: number
  doi?: string
  url: string
  snippet?: string
  apa_citation?: string
  scraped_truncated?: boolean
  cid?: string
}

export interface ScoreScholarResponse {
  updated: boolean
  result: VerificationResult | null
}

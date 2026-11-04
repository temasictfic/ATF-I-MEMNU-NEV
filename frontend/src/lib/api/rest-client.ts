import { getBackendBaseUrl } from './backend-endpoint'

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const baseUrl = await getBackendBaseUrl()
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' }
  }
  if (body !== undefined) {
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(`${baseUrl}${path}`, opts)
  if (!res.ok) {
    const errorText = await res.text()
    throw new Error(`API error ${res.status}: ${errorText}`)
  }
  return res.json()
}

export const api = {
  // Health
  health: () => request<{ status: string }>('GET', '/api/health'),

  // Source cache persistence — the backend no longer parses PDFs; these
  // endpoints read and write the `{output_dir}/cache/{pdf_id}.json` files
  // that the client-side orchestrator populates after parsing.
  getSources: (pdfId: string) =>
    request<{
      sources: Array<import('./types').SourceRectangle>
      cached: boolean
      numbered?: boolean
      approved?: boolean
      // Persisted alongside sources so a cached re-import can skip a full
      // PDF parse (the renderer otherwise reads bytes + re-runs pdfjs just
      // to learn the page count).
      page_count?: number
    }>('GET', `/api/parse/sources/${pdfId}`),

  updateSources: (
    pdfId: string,
    sources: Array<import('./types').SourceRectangle>,
    numbered?: boolean,
    pageCount?: number,
  ) =>
    request<{ success: boolean }>('PUT', `/api/parse/sources/${pdfId}`, {
      sources,
      ...(numbered !== undefined ? { numbered } : {}),
      ...(pageCount !== undefined ? { page_count: pageCount } : {}),
    }),

  approvePdf: (pdfId: string) =>
    request<{ success: boolean }>('POST', `/api/parse/approve/${pdfId}`),

  unapprovePdf: (pdfId: string) =>
    request<{ success: boolean }>('POST', `/api/parse/unapprove/${pdfId}`),

  removePdf: (pdfId: string) =>
    request<{ success: boolean }>('DELETE', `/api/parse/pdf/${pdfId}`),

  // NER field extraction — the only remaining backend-side PDF-ish work.
  extractFields: (text: string) =>
    request<import('./types').ParsedSource>('POST', '/api/parse/extract-fields', { text }),

  // Verification
  verify: (pdfIds: string[]) =>
    request<{ job_id: string }>('POST', '/api/verify', { pdf_ids: pdfIds }),

  verifyBatch: (pdfIds: string[], texts: Record<string, string>, excludedIds: string[]) =>
    request<{ job_id: string }>('POST', '/api/verify/batch', { pdf_ids: pdfIds, texts, excluded_ids: excludedIds }),

  verifyPdf: (pdfId: string) =>
    request<{ job_id: string }>('POST', `/api/verify/pdf/${pdfId}`),

  verifySource: (pdfId: string, sourceId: string, text?: string) =>
    request<{ job_id: string }>('POST', `/api/verify/source/${pdfId}/${sourceId}`, { text }),

  cancelVerification: () =>
    request<{ success: boolean }>('POST', '/api/verify/cancel'),

  cancelPdfVerification: (pdfId: string) =>
    request<{ success: boolean }>('POST', `/api/verify/cancel/pdf/${pdfId}`),

  cancelSourceVerification: (sourceId: string) =>
    request<{ success: boolean }>('POST', `/api/verify/cancel/source/${sourceId}`),

  verifyStatus: (jobId: string) =>
    request<{ pdfs: Array<import('./types').PdfVerificationSummary> }>('GET', `/api/verify/status/${jobId}`),

  verifyResults: (pdfId: string) =>
    request<{ results: Record<string, import('./types').VerificationResult> }>('GET', `/api/verify/results/${pdfId}`),

  overrideStatus: (pdfId: string, sourceId: string, status: 'high' | 'medium' | 'low') =>
    request<{ success: boolean }>('PUT', `/api/verify/override/${pdfId}/${sourceId}`, { status }),

  setTagOverride: (pdfId: string, sourceId: string, tag: string, state: boolean | null) =>
    request<{ success: boolean }>('POST', `/api/verify/tag-override/${pdfId}/${sourceId}`, { tag, state }),

  setDecisionOverride: (pdfId: string, sourceId: string, decision: import('./types').DecisionTag | null) =>
    request<{ success: boolean }>('POST', `/api/verify/decision-override/${pdfId}/${sourceId}`, { decision }),

  scoreScholar: (
    pdfId: string,
    sourceId: string,
    sourceText: string,
    candidates: import('./types').ScholarCandidate[],
    fullSourceText?: string,
  ) =>
    request<import('./types').ScoreScholarResponse>('POST', '/api/verify/score-scholar', {
      pdf_id: pdfId,
      source_id: sourceId,
      source_text: sourceText,
      candidates,
      full_source_text: fullSourceText,
    }),

  // Settings
  getSettings: () =>
    request<import('./types').AppSettings>('GET', '/api/settings'),

  // Partial PUT: send only the scalar fields that changed. Backend
  // applies the patch on top of its current state and returns the full
  // result. The `databases` field is not accepted here — callers must
  // use the granular endpoints below so a stale-renderer save can never
  // replace the on-disk list with the seed.
  updateSettings: (patch: Partial<import('./types').AppSettings>) =>
    request<import('./types').AppSettings>('PUT', '/api/settings', patch),

  // Granular database endpoints. Each is a single-row operation that the
  // backend applies to its current list — the renderer never sends the
  // whole list, so it can't accidentally replace it.
  setDatabaseEnabled: (dbId: string, enabled: boolean) =>
    request<import('./types').AppSettings>('PUT', `/api/settings/databases/${dbId}`, { enabled }),

  reorderDatabase: (id: string, afterId: string | null) =>
    request<import('./types').AppSettings>('POST', '/api/settings/databases/reorder', {
      id,
      after_id: afterId,
    }),

  addDatabase: (db: import('./types').DatabaseConfig) =>
    request<import('./types').AppSettings>('POST', '/api/settings/databases', db),

  removeDatabase: (dbId: string) =>
    request<import('./types').AppSettings>('DELETE', `/api/settings/databases/${dbId}`),

  // OpenAIRE auth — validates by exchanging the refresh token against
  // OpenAIRE; on success the backend persists it and returns the updated
  // settings so the store can sync without a second GET.
  validateOpenaireToken: (refreshToken: string) =>
    request<{ valid: boolean; error?: string; settings?: import('./types').AppSettings }>(
      'POST',
      '/api/settings/openaire/validate',
      { refresh_token: refreshToken },
    ),

  disconnectOpenaire: () =>
    request<import('./types').AppSettings>('POST', '/api/settings/openaire/disconnect'),
}

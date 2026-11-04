/**
 * Google Scholar automated scanner.
 *
 * Uses a hidden Electron <webview> to sequentially search Google Scholar
 * for non-High sources, extract results from the DOM, and send them
 * to the backend for scoring.
 */

import type { ScholarCandidate } from '../api/types'
import { api } from '../api/rest-client'

// --- Types ---

export interface ScholarQueueItem {
  pdfId: string
  sourceId: string
  searchText: string
  fullSourceText: string
}

export type ScholarScanStatus = 'idle' | 'scanning' | 'captcha' | 'done' | 'cancelled'

export interface ScholarScanCallbacks {
  onStatusChange: (status: ScholarScanStatus) => void
  onProgress: (current: number, total: number, foundCount: number) => void
  onCaptcha: (url: string) => void
  onCaptchaResolved: () => void  // close overlay + cleanup after CAPTCHA
  onError: (sourceId: string, error: string) => void
  onSourceDone: (sourceId: string, updated: boolean) => void
}

// --- Shared in-page detection fragments ---
// These JS source strings are injected into the webview as part of larger
// scripts. Keeping them in one place means the renderer's resume probes and
// the scanner's extraction loop use the *same* CAPTCHA/result definitions —
// previously they had drifted, with the renderer missing localized markers
// (e.g. Turkish "alışılmadık trafik") that the scanner already detected.
const CAPTCHA_DETECTION_FN_JS = `
function detectCaptcha() {
  var CAPTCHA_SELECTORS = '#gs_captcha_f, #gs_captcha_ccl, #captcha-form, #recaptcha, iframe[src*="recaptcha"], iframe[src*="captcha"]';
  if (document.querySelector(CAPTCHA_SELECTORS)) return true;
  if (window.location.hostname.indexOf('sorry.google.com') !== -1) return true;
  var title = (document.title || '').toLowerCase();
  if (title.indexOf('sorry') !== -1 || title.indexOf('unusual traffic') !== -1) return true;
  if (title.indexOf('\\u00fczg\\u00fcn\\u00fcz') !== -1) return true; // "Üzgünüz"
  var bodyText = (document.body && document.body.innerText) || '';
  if (bodyText.indexOf('unusual traffic') !== -1 || bodyText.indexOf('not a robot') !== -1) return true;
  if (bodyText.indexOf('al\\u0131\\u015f\\u0131lmad\\u0131k trafik') !== -1) return true; // "alışılmadık trafik"
  if (bodyText.indexOf('robot olmad\\u0131\\u011f\\u0131n\\u0131z') !== -1) return true; // "robot olmadığınız"
  return false;
}
`

const RESULT_DETECTION_FN_JS = `
var RESULT_SELECTORS = ['.gs_r.gs_or.gs_scl', '.gs_r.gs_or', '.gs_ri', '[data-cid]'];
function findItems() {
  for (var i = 0; i < RESULT_SELECTORS.length; i++) {
    var items = document.querySelectorAll(RESULT_SELECTORS[i]);
    if (items.length) return items;
  }
  return [];
}
function hasResultsContainer() {
  return !!document.querySelector('#gs_res_ccl, #gs_res_ccl_mid, .gs_r, .gs_ri');
}
`

// Lightweight one-shot probe used by the renderer to decide whether the
// overlay is on a real Scholar results page (CAPTCHA solved) or still
// showing the challenge. Returns immediately — no polling — so it is safe
// to call from a tight interval.
export const PROBE_PAGE_STATE_SCRIPT = `
(function() {
  ${CAPTCHA_DETECTION_FN_JS}
  ${RESULT_DETECTION_FN_JS}
  return {
    hasCaptcha: detectCaptcha(),
    hasResults: hasResultsContainer(),
    ready: document.readyState
  };
})()
`

// --- Extraction script injected into the webview ---
// Single async IIFE that polls up to 2500ms for either CAPTCHA markers or
// result items, extracts in the same pass, and short-circuits as soon as
// either is found. This replaces the old pair of separate DETECT+EXTRACT
// scripts and the hardcoded 2500ms post-load sleep — pages that render in
// 200-500ms (most of them) now complete extraction in well under a second
// instead of waiting out the full delay.
const POLL_AND_EXTRACT_SCRIPT = `
(async function() {
  ${CAPTCHA_DETECTION_FN_JS}
  ${RESULT_DETECTION_FN_JS}

  function extract(items) {
    var results = [];
    for (var i = 0; i < items.length; i++) {
      var el = items[i];
      // Prefer the anchor (has href for URL extraction). Fall back to the
      // .gs_rt heading itself for [ALINTI]/[CITATION]-only entries that
      // Scholar lists without a clickable link (no DOI, no PDF, just a
      // cluster record). Skipping these lost correct matches for papers
      // indexed only via Scholar's citation graph.
      var titleEl = el.querySelector('.gs_rt a')
        || el.querySelector('h3 a')
        || el.querySelector('a[data-clk]')
        || el.querySelector('.gs_rt')
        || el.querySelector('h3');
      if (!titleEl) continue;
      // Drop the .gs_ctu type markers ("[ALINTI]", "[C]", "[PDF]", "[BOOK]",
      // etc.) before reading text — they live in a child span inside .gs_rt.
      var titleText = '';
      if (titleEl.cloneNode) {
        var clone = titleEl.cloneNode(true);
        var markers = clone.querySelectorAll('.gs_ctu, .gs_ct1, .gs_ct2');
        for (var mk = 0; mk < markers.length; mk++) {
          markers[mk].parentNode && markers[mk].parentNode.removeChild(markers[mk]);
        }
        titleText = clone.textContent || '';
      } else {
        titleText = titleEl.textContent || '';
      }
      // Also strip any leading "[...]" prefixes left over (multiple possible).
      var title = titleText.replace(/^\\s*(?:\\[[^\\]]*\\]\\s*)+/, '').trim();
      if (!title) continue;
      var url = titleEl.href || '';
      var metaEl = el.querySelector('.gs_a');
      var metaText = metaEl ? (metaEl.textContent || '') : '';
      var parts = metaText.split(' - ');
      var authorsPart = (parts[0] || '').trim();
      var authors = authorsPart.split(',').map(function(a) { return a.trim(); })
        .filter(function(a) { return a.length > 0 && a !== '\\u2026'; });
      var year = null;
      var yearMatch = metaText.match(/\\b(19|20)\\d{2}\\b/);
      if (yearMatch) year = parseInt(yearMatch[0]);
      var doi = null;
      var links = el.querySelectorAll('a');
      for (var j = 0; j < links.length; j++) {
        var href = links[j].href || '';
        var doiMatch = href.match(/doi\\.org\\/(10\\.\\d{4,}\\/.+?)(?:[?&#]|$)/);
        if (doiMatch) { doi = decodeURIComponent(doiMatch[1]); break; }
      }
      var snippetEl = el.querySelector('.gs_rs');
      var snippet = snippetEl ? (snippetEl.textContent || '').trim() : '';
      // "…" (U+2026) in any scraped field signals Scholar truncation —
      // title can be cut, .gs_a authors/journal line often is. TS-side
      // uses this to decide whether to fetch APA for this one candidate.
      var scrapedTruncated =
        title.indexOf('\\u2026') !== -1 ||
        metaText.indexOf('\\u2026') !== -1;
      var cidEl = el.closest ? (el.closest('[data-cid]') || el) : el;
      var cid = (cidEl && cidEl.getAttribute) ? (cidEl.getAttribute('data-cid') || '') : '';
      results.push({
        title: title, authors: authors, year: year, doi: doi,
        url: url, snippet: snippet, apa_citation: '',
        scraped_truncated: scrapedTruncated, cid: cid
      });
    }
    return results;
  }

  // Poll at 100ms intervals up to 2500ms. CAPTCHA check runs every tick so
  // we short-circuit immediately instead of waiting out the rest of the
  // deadline on a page that will never render results.
  var DEADLINE = Date.now() + 2500;
  while (Date.now() < DEADLINE) {
    if (detectCaptcha()) return { captcha: true, results: [] };
    var items = findItems();
    if (items.length > 0) return { captcha: false, results: extract(items) };
    await new Promise(function(r) { setTimeout(r, 100); });
  }
  // Timed out. Re-check CAPTCHA once more (some Scholar variants render
  // the CAPTCHA iframe after a delay) and otherwise return empty results.
  return { captcha: detectCaptcha(), results: [] };
})()
`

// --- Rate limiter ---

class ScholarRateLimiter {
  private baseDelay = 4000
  private lastRequest = 0
  private captchaCount = 0
  private requestsSinceCaptcha = Infinity

  async waitForSlot(): Promise<void> {
    const jitter = Math.random() * 3000
    let delay = this.baseDelay + jitter

    if (this.requestsSinceCaptcha < 5) {
      delay = 8000 + Math.random() * 4000
    }
    if (this.captchaCount >= 2) {
      delay = Math.max(delay, 10000 + Math.random() * 5000)
    }

    const elapsed = Date.now() - this.lastRequest
    if (elapsed < delay) {
      await sleep(delay - elapsed)
    }
    this.lastRequest = Date.now()
    this.requestsSinceCaptcha++
  }

  onCaptcha(): void {
    this.captchaCount++
    this.requestsSinceCaptcha = 0
  }

  reset(): void {
    this.lastRequest = 0
    this.captchaCount = 0
    this.requestsSinceCaptcha = Infinity
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

// `lookup=0` forces the full results page; without it Scholar may show a
// single "best result" view that breaks our scrape. Truncated to 300 chars
// because Scholar silently rejects very long queries.
export function buildSearchUrl(searchText: string): string {
  const query = encodeURIComponent(searchText.slice(0, 300))
  return `https://scholar.google.com/scholar?lookup=0&q=${query}`
}

// Cheap token-overlap similarity used to pick the single best candidate
// to enrich with APA. This is not the authoritative score — the backend
// does proper scoring — but it's a good-enough ranker to decide which
// cid (if any) is worth paying the extra Scholar request for.
function quickTitleScore(candTitle: string, refText: string): number {
  const norm = (s: string): string[] =>
    s
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, ' ')
      .split(/\s+/)
      .filter(Boolean)
  const a = norm(candTitle)
  const b = norm(refText)
  if (!a.length || !b.length) return 0
  const refSet = new Set(b)
  let overlap = 0
  for (const tok of a) if (refSet.has(tok)) overlap++
  return overlap / Math.max(a.length, b.length)
}

// Standalone script that fetches the APA citation for a single cid via
// Scholar's "Cite" dialog. Runs as a same-origin fetch inside the webview
// so cookies attach automatically. Returns '' on CAPTCHA / parse failure;
// callers always fall back to the scraped fields in that case.
function buildFetchApaScript(cid: string): string {
  const safeCid = cid.replace(/[^A-Za-z0-9_-]/g, '')
  return `
(async function() {
  try {
    var citeUrl = '/scholar?q=info:' + ${JSON.stringify(safeCid)} + ':scholar.google.com/&output=cite&hl=en';
    var resp = await fetch(citeUrl, { credentials: 'same-origin' });
    if (!resp.ok) return '';
    var html = await resp.text();
    var lower = html.toLowerCase();
    if (lower.indexOf('unusual traffic') !== -1 || lower.indexOf('captcha') !== -1) return '';
    var doc = new DOMParser().parseFromString(html, 'text/html');
    var labels = doc.querySelectorAll('th, .gs_cith, dt');
    for (var i = 0; i < labels.length; i++) {
      var txt = (labels[i].textContent || '').trim();
      if (txt !== 'APA') continue;
      var tr = labels[i].closest && labels[i].closest('tr');
      if (tr) {
        var td = tr.querySelector('td');
        var cell = td ? (td.textContent || '').trim() : '';
        if (cell.length > 20) return cell;
      }
      var sib = labels[i].nextElementSibling;
      if (sib) {
        var sibText = (sib.textContent || '').trim();
        if (sibText.length > 20) return sibText;
      }
    }
    var rows = doc.querySelectorAll('.gs_citr');
    if (rows.length >= 2) {
      var apa = (rows[1].textContent || '').trim();
      if (apa.length > 20) return apa;
    }
    return '';
  } catch (e) {
    return '';
  }
})()`
}

// --- Scanner ---

export class ScholarScanner {
  private webview: any = null
  private overlayWebview: any = null
  private queue: ScholarQueueItem[] = []
  private currentIndex = 0
  private foundCount = 0
  private status: ScholarScanStatus = 'idle'
  private callbacks: ScholarScanCallbacks | null = null
  private rateLimiter = new ScholarRateLimiter()
  private cancelRequested = false
  // Bumped on every loadAndExtract call. The load closure captures its own
  // generation and checks after each await — if the counter has moved
  // forward, a newer call (or a cancellation) owns the webview now, so the
  // stale closure must not execute JS against it or resolve/reject.
  private loadGeneration = 0

  setWebview(webview: any): void {
    this.webview = webview
    console.log(`[Scholar] Webview ${webview ? 'connected' : 'disconnected'}`)
  }

  setOverlayWebview(webview: any): void {
    this.overlayWebview = webview
  }

  setCallbacks(callbacks: ScholarScanCallbacks): void {
    this.callbacks = callbacks
  }

  getStatus(): ScholarScanStatus {
    return this.status
  }

  async startScan(queue: ScholarQueueItem[]): Promise<void> {
    if (this.status === 'scanning') return
    this.queue = queue
    this.currentIndex = 0
    this.foundCount = 0
    this.cancelRequested = false
    this.rateLimiter.reset()
    this.setStatus('scanning')
    this.callbacks?.onProgress(0, queue.length, 0)
    await this.processNext()
  }

  cancel(): void {
    this.cancelRequested = true
    // Bump the generation so any in-flight loadAndExtract closure sees
    // `isStale() === true` on its next await and bails without touching
    // the webview or resolving its promise.
    this.loadGeneration++
    this.setStatus('cancelled')
  }

  async resumeAfterCaptcha(): Promise<void> {
    console.log(`[Scholar] resumeAfterCaptcha called (status=${this.status})`)
    if (this.status !== 'captcha') {
      console.warn('[Scholar] Ignoring resume — not in captcha state')
      return
    }

    const item = this.queue[this.currentIndex]
    if (!item) {
      console.warn('[Scholar] No current item, marking done')
      this.setStatus('done')
      return
    }

    // Prevent re-entry
    this.setStatus('scanning')

    // Try to extract results from the overlay webview (user solved CAPTCHA there).
    // `null` means "extraction unavailable" (overlay missing/closed/threw/wrong
    // shape) and triggers the hidden-webview fallback below. An empty array
    // means "extraction succeeded, Scholar returned 0 hits" — a legitimate
    // signal we MUST NOT retry, otherwise we burn an extra request per
    // genuine no-result source.
    const overlay = this.overlayWebview
    let candidates: ScholarCandidate[] | null = null
    console.log(`[Scholar] Overlay webview available: ${!!overlay}`)
    if (overlay) {
      try {
        await sleep(500)
        const extraction = await overlay.executeJavaScript(POLL_AND_EXTRACT_SCRIPT)
        console.log('[Scholar] Overlay extraction:', JSON.stringify(extraction).substring(0, 300))
        if (extraction && Array.isArray(extraction.results)) {
          candidates = extraction.results
        }
      } catch (err) {
        console.warn('[Scholar] Overlay extraction failed (overlay may be closed):', err)
      }
    }

    // Always close the overlay (no-op if already closed)
    try {
      this.callbacks?.onCaptchaResolved()
    } catch (err) {
      console.warn('[Scholar] onCaptchaResolved failed:', err)
    }

    // Fallback: overlay extraction was unavailable, so retry the search via
    // the hidden webview. Cookies for the scholar partition are now valid
    // post-CAPTCHA, so this typically succeeds without a fresh challenge.
    if (candidates === null) {
      const url = buildSearchUrl(item.searchText)
      console.log('[Scholar] Overlay extraction unavailable; retrying via hidden webview')
      try {
        const result = await this.loadAndExtract(url)
        if (this.cancelRequested) return
        if (result === 'captcha') {
          // Hit another CAPTCHA — re-enter captcha state without advancing.
          this.rateLimiter.onCaptcha()
          this.setStatus('captcha')
          this.callbacks?.onCaptcha(url)
          return
        }
        candidates = result
      } catch (err) {
        console.warn('[Scholar] Hidden-webview fallback failed:', err)
        candidates = []
      }
    }

    // Always score, even with an empty candidate list, so the backend records
    // "Google Scholar" in databases_searched and the UI gets its GS link.
    let updated = false
    try {
      const response = await api.scoreScholar(
        item.pdfId,
        item.sourceId,
        item.searchText,
        candidates,
        item.fullSourceText,
      )
      updated = response.updated
      if (updated) this.foundCount++
    } catch (err) {
      this.callbacks?.onError(item.sourceId, `Scoring error: ${err}`)
    }
    this.callbacks?.onSourceDone(item.sourceId, updated)

    // Advance to next item
    this.currentIndex++
    this.callbacks?.onProgress(this.currentIndex, this.queue.length, this.foundCount)

    console.log(`[Scholar] Continuing to next (${this.currentIndex}/${this.queue.length})`)
    await this.processNext()
  }

  private setStatus(status: ScholarScanStatus): void {
    this.status = status
    this.callbacks?.onStatusChange(status)
  }

  private async processNext(): Promise<void> {
    // One rate-limit slot is "pre-reserved" by the previous iteration during
    // its scoring POST, so the wait runs concurrently with the backend call
    // instead of serially after it. null means "no pre-reserved slot, call
    // waitForSlot fresh" — used on the very first iteration and when resuming
    // after a CAPTCHA (where the pre-reserved slot was never created).
    let nextSlotWait: Promise<void> | null = null

    while (!this.cancelRequested && this.currentIndex < this.queue.length) {
      const item = this.queue[this.currentIndex]

      if (nextSlotWait !== null) {
        await nextSlotWait
        nextSlotWait = null
      } else {
        await this.rateLimiter.waitForSlot()
      }
      if (this.cancelRequested) return

      const url = buildSearchUrl(item.searchText)
      console.log(`[Scholar] Searching: ${url.substring(0, 120)}...`)

      let candidates: ScholarCandidate[] | 'captcha'
      try {
        candidates = await this.loadAndExtract(url)
      } catch (err) {
        this.callbacks?.onError(item.sourceId, `${err}`)
        this.currentIndex++
        this.callbacks?.onProgress(this.currentIndex, this.queue.length, this.foundCount)
        continue
      }

      if (candidates === 'captcha') {
        console.log('[Scholar] Got CAPTCHA')
        this.rateLimiter.onCaptcha()
        this.setStatus('captcha')
        this.callbacks?.onCaptcha(url)
        return // Will resume from resumeAfterCaptcha()
      }

      // Pick the single best candidate via cheap token-overlap similarity
      // and, only if its scraped data is truncated ("…" in title/authors/
      // journal), pay one extra Scholar request to fetch its APA citation.
      // Previous behaviour enriched top 5 — that multiplied Scholar traffic
      // by ~6x and drove CAPTCHA frequency up; this targets the 0-1 fetches
      // that actually matter for scoring accuracy.
      let enrichedCount = 0
      if (candidates.length > 0) {
        let topIdx = 0
        let topScore = -1
        for (let i = 0; i < candidates.length; i++) {
          const s = quickTitleScore(candidates[i].title, item.searchText)
          if (s > topScore) {
            topScore = s
            topIdx = i
          }
        }
        const top = candidates[topIdx]
        if (top.scraped_truncated && top.cid) {
          const apa = await this.fetchApaForCid(top.cid)
          if (apa) {
            top.apa_citation = apa
            enrichedCount = 1
          }
        }
      }
      console.log(`[Scholar] Got ${candidates.length} candidates (${enrichedCount} with APA)`)

      // Kick off scoring in the background. Crucially, we also pre-reserve
      // the next iteration's rate-limit slot right now so that its 4–7 s
      // sleep runs in parallel with the ~100–500 ms scoring POST, instead
      // of serially after it. Only pre-reserve when a next iteration will
      // actually exist — otherwise the reservation is wasted and leaves
      // `lastRequest` stale at shutdown.
      // Always call scoreScholar — even with an empty candidate list — so the
      // backend records "Google Scholar" in databases_searched and broadcasts
      // it to the UI. Without this, a scanned-but-empty source has no GS link.
      const scorePromise: Promise<{ updated: boolean } | null> = api
        .scoreScholar(
          item.pdfId,
          item.sourceId,
          item.searchText,
          candidates,
          item.fullSourceText,
        )
        .catch((err) => {
          this.callbacks?.onError(item.sourceId, `Scoring error: ${err}`)
          return null
        })

      if (this.currentIndex + 1 < this.queue.length && !this.cancelRequested) {
        nextSlotWait = this.rateLimiter.waitForSlot()
      }

      const response = await scorePromise
      const updated = response?.updated ?? false
      if (updated) this.foundCount++
      this.callbacks?.onSourceDone(item.sourceId, updated)

      this.currentIndex++
      this.callbacks?.onProgress(this.currentIndex, this.queue.length, this.foundCount)
    }

    if (!this.cancelRequested) this.setStatus('done')
  }

  // Fetch APA citation for one cid via a short same-origin request inside
  // the active webview. Returns '' on timeout / CAPTCHA / parse failure —
  // caller falls back to scraped fields. Budgeted to 10 s so a stalled
  // fetch doesn't block the next page load.
  private async fetchApaForCid(cid: string): Promise<string> {
    const view = this.webview
    if (!view || typeof view.executeJavaScript !== 'function') return ''
    try {
      const script = buildFetchApaScript(cid)
      const run = view.executeJavaScript(script) as Promise<string>
      const timeout = new Promise<string>((resolve) => setTimeout(() => resolve(''), 10000))
      const result = await Promise.race([run, timeout])
      return typeof result === 'string' ? result : ''
    } catch {
      return ''
    }
  }

  private loadAndExtract(url: string): Promise<ScholarCandidate[] | 'captcha'> {
    // Capture the generation at the start of this call; every async step
    // below re-checks it and bails silently if a newer call has taken over
    // the shared webview. This prevents the old closure from running JS
    // against a page that belongs to the *next* citation.
    const myGen = ++this.loadGeneration
    const isStale = (): boolean => this.loadGeneration !== myGen

    return new Promise((resolve, reject) => {
      const view = this.webview
      if (!view) {
        reject(new Error('No webview available'))
        return
      }

      let settled = false
      const timeoutId = setTimeout(() => {
        if (!settled) {
          settled = true
          cleanup()
          reject(new Error('Webview load timeout'))
        }
      }, 30000)

      const cleanup = (): void => {
        try {
          view.removeEventListener('did-stop-loading', onLoaded)
          view.removeEventListener('did-fail-load', onFailed)
        } catch {
          // ignore
        }
      }

      const onFailed = (e: any): void => {
        if (settled) return
        settled = true
        clearTimeout(timeoutId)
        cleanup()
        reject(new Error(`Load failed: ${e?.errorDescription || 'unknown'}`))
      }

      const onLoaded = async (): Promise<void> => {
        if (settled) return
        settled = true
        clearTimeout(timeoutId)
        cleanup()

        try {
          // Single async call: polls the DOM up to 2500ms for either a
          // CAPTCHA marker or result items, extracts in the same pass,
          // and returns as soon as either is found. Most Scholar result
          // pages resolve here in 200-500ms — the full 2.5s is only spent
          // on pages that never render (soft blocks, empty results).
          const extraction = await view.executeJavaScript(POLL_AND_EXTRACT_SCRIPT)
          // Stale paths must still resolve — silently returning leaves the
          // caller's promise pending forever. Empty array is the safest
          // signal: callers either check cancelRequested or score against
          // [] (which costs one no-op POST at most).
          if (isStale()) {
            console.warn('[Scholar] stale onLoaded after extraction — discarding results')
            resolve([])
            return
          }
          if (extraction && extraction.captcha) {
            resolve('captcha')
            return
          }
          if (extraction && Array.isArray(extraction.results)) {
            resolve(extraction.results)
            return
          }
          // Unexpected shape — dump the page and return empty
          const debugInfo = await view.executeJavaScript(`({
            url: window.location.href,
            title: document.title,
            bodyLen: document.body?.innerText?.length || 0,
            html: document.documentElement.outerHTML.substring(0, 500)
          })`)
          if (isStale()) {
            resolve([])
            return
          }
          console.warn('[Scholar] Extraction returned unexpected shape. Page info:', debugInfo)
          resolve([])
        } catch (err) {
          if (isStale()) {
            resolve([])
            return
          }
          reject(new Error(`Extraction failed: ${err}`))
        }
      }

      view.addEventListener('did-stop-loading', onLoaded)
      view.addEventListener('did-fail-load', onFailed)

      // Navigate by setting src attribute (works reliably with Electron webview)
      try {
        if (typeof view.loadURL === 'function') {
          view.loadURL(url)
        } else {
          view.src = url
        }
      } catch (err) {
        if (!settled) {
          settled = true
          clearTimeout(timeoutId)
          cleanup()
          reject(new Error(`Navigation failed: ${err}`))
        }
      }
    })
  }
}

// Singleton instance
export const scholarScanner = new ScholarScanner()

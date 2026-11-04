import type { ExclusionEntry } from '../utils/exclusion-list'

export type ExclusionInfo =
  | { excluded: true; reason: string; kind: 'user' | 'word' | 'non-doi-url' | 'year-before' }
  | { excluded: false }

const URL_RE = /(?:https?:\/\/|www\.)[^\s<>()]+/gi
const DOI_RE = /\b10\.\d{4,9}\/\S+/gi
const YEAR_RE = /(?:19|20)\d{2}/

/**
 * Find a publication-year-shaped value without invoking the citation NER
 * service. We intentionally accept only conventional bibliography positions
 * (parenthesized, Vancouver `YYYY;volume`, or punctuation-delimited years),
 * and mask URLs/DOIs first so identifier digits cannot become a year.
 */
export function extractCitationYear(sourceText: string): number | undefined {
  const masked = sourceText
    .replace(URL_RE, match => ' '.repeat(match.length))
    .replace(DOI_RE, match => ' '.repeat(match.length))
  const patterns = [
    /\((?:[A-Za-zÀ-ɏ]+\.?\s+)?((?:19|20)\d{2})[a-z]?\s*(?:[,)]|$)/i,
    /\b((?:19|20)\d{2})[a-z]?\s*;\s*\d{1,4}(?:\s*\(|\s*:|\b)/,
    /(?:^|[.,]\s*)((?:19|20)\d{2})[a-z]?(?=\s*(?:[,.;)]|$))/,
    // IEEE-style: ", May 1997." — month name (abbreviated or full) before year
    /(?:^|[.,]\s*)(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s*\.?\s*((?:19|20)\d{2})[a-z]?(?=\s*(?:[,.;)]|$))/i,
  ]
  for (const pattern of patterns) {
    const match = masked.match(pattern)
    if (!match) continue
    const year = Number(match[1])
    if (YEAR_RE.test(match[1]) && year >= 1900 && year <= 2099) return year
  }
  return undefined
}

interface Reasons {
  userDisabled: string
  nonDoiUrl: string
  yearBefore: string
}

function urlHostname(rawUrl: string): string | null {
  try {
    // Citation punctuation often immediately follows a URL.
    let cleaned = rawUrl.replace(/[.,;:)}\]"']+$/, '')
    // Handle URLs without a protocol prefix (e.g. www.example.org).
    if (!/^https?:\/\//i.test(cleaned)) {
      cleaned = 'https://' + cleaned
    }
    return new URL(cleaned).hostname.toLowerCase()
  } catch {
    return null
  }
}

function isDoiHostname(hostname: string | null): boolean {
  return hostname === 'doi.org' || hostname === 'dx.doi.org'
}

function isAllowedHostname(hostname: string | null, allowedDomains: string[]): boolean {
  return hostname !== null && allowedDomains.some(domain => hostname === domain || hostname.endsWith(`.${domain}`))
}

/**
 * Decide whether a card is Muaf (excluded from verification) and why.
 *
 * `override` is the renderer's `enabledSources[id]` value, raw — pass
 * `undefined` for "no user choice yet", `true` for explicit force-enable,
 * `false` for explicit user-disable.
 *
 * Priority (first match wins):
 *   1. `override === true` → user override beats every other rule. Not Muaf.
 *   2. Word from the exclusion file appears in the text → file's reason.
 *   3. A citation-style publication year is below the configured `@year-before`
 *      threshold → directive's reason.
 *   4. The exclusion file declares `@non-doi-url` AND one of the text's
 *      non-DOI URLs is not a bare-domain allowlist entry.
 *   5. `override === false` → "User excluded it".
 *   6. Otherwise → not Muaf.
 *
 * The "user excluded it" reason only surfaces when no automatic rule
 * applies — clicking the ref-badge on an auto-Muaf card and then clicking
 * again returns the card to its automatic reason, not the user reason.
 */
export function computeExclusion(
  sourceText: string,
  override: boolean | undefined,
  exclusionEntries: ExclusionEntry[],
  reasons: Reasons,
): ExclusionInfo {
  if (override === true) return { excluded: false }

  const lower = sourceText.toLowerCase()
  let nonDoiUrlEntry: ExclusionEntry | null = null
  let yearBeforeEntry: ExclusionEntry | null = null
  const allowedDomains: string[] = []
  for (const entry of exclusionEntries) {
    if (entry.kind === 'non-doi-url') {
      // Remember it so step 3 can consult it after the word loop, but
      // don't try to substring-match the literal `@non-doi-url` token.
      nonDoiUrlEntry = entry
      continue
    }
    if (entry.kind === 'non-doi-domain') {
      if (entry.domain) allowedDomains.push(entry.domain)
      continue
    }
    if (entry.kind === 'year-before') {
      yearBeforeEntry ??= entry
      continue
    }
    if (entry.word && lower.includes(entry.word)) {
      return { excluded: true, reason: entry.reason || entry.word, kind: 'word' }
    }
  }
  const citationYear = extractCitationYear(sourceText)
  if (
    yearBeforeEntry?.yearBefore !== undefined
    && citationYear !== undefined
    && citationYear < yearBeforeEntry.yearBefore
  ) {
    return {
      excluded: true,
      reason: yearBeforeEntry.reason || reasons.yearBefore,
      kind: 'year-before',
    }
  }
  const nonDoiUrls = (sourceText.match(URL_RE) ?? [])
    .filter(url => !isDoiHostname(urlHostname(url)))
  if (
    nonDoiUrlEntry !== null
    && nonDoiUrls.some(url => !isAllowedHostname(urlHostname(url), allowedDomains))
  ) {
    return {
      excluded: true,
      reason: nonDoiUrlEntry.reason || reasons.nonDoiUrl,
      kind: 'non-doi-url',
    }
  }
  if (override === false) {
    return { excluded: true, reason: reasons.userDisabled, kind: 'user' }
  }
  return { excluded: false }
}

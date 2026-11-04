import type { TagKey, DecisionTag, VerificationResult } from '../api/types'

export const TAG_ORDER: TagKey[] = ['authors', 'year', 'title', 'journal', 'doi/arXiv']

export interface TagStateResultLike {
  problem_tags?: string[]
  best_match?: {
    authors?: string[]
    year?: number | null
    journal?: string
    doi?: string | null
  }
  tag_overrides?: Record<string, boolean>
  decision_tag?: DecisionTag
  decision_tag_override?: DecisionTag | null
}

export function defaultTagOn(result: TagStateResultLike | undefined, tag: TagKey): boolean {
  if (!result) return false
  const bm = result.best_match
  const probs = result.problem_tags ?? []
  // Pure pass-through from the backend's problem_tags. The backend is
  // authoritative: it already fires a tag when the source has the field
  // but the candidate is missing it or disagrees, which is exactly the
  // per-field issue signal the user needs to see.
  switch (tag) {
    case 'authors':  return probs.includes('!authors') && !!bm
    case 'year':     return probs.includes('!year') && !!bm
    case 'journal':  return probs.includes('!journal') && !!bm
    case 'doi/arXiv':return probs.includes('!doi/arXiv') && !!bm
    case 'title':    return probs.includes('!title') && !!bm
  }
}

export function effectiveTagOn(result: TagStateResultLike | undefined, tag: TagKey): boolean {
  const ov = result?.tag_overrides?.[tag]
  if (ov !== undefined) return ov
  return defaultTagOn(result, tag)
}

export function anyTagOn(result: TagStateResultLike | undefined): boolean {
  return TAG_ORDER.some(t => effectiveTagOn(result, t))
}

/** Classify the decision from the currently-effective chip states, using the
 *  rule mirrored from the backend's classify_decision(). A chip OFF means the
 *  signal matches; a chip ON means it disagrees or is missing on one side. */
export function classifyDecisionFromTags(result: TagStateResultLike | undefined): DecisionTag {
  if (!result || !result.best_match) return 'fabricated'
  const authorsOn = effectiveTagOn(result, 'authors')
  const titleOn   = effectiveTagOn(result, 'title')
  const journalOn = effectiveTagOn(result, 'journal')
  const doiOn     = effectiveTagOn(result, 'doi/arXiv')

  // OFF = matches
  const authorMatches  = !authorsOn
  const titleMatches   = !titleOn
  const journalMatches = !journalOn
  const doiMatches     = !doiOn
  // For Citation/Fabricated, "journal/doi both missing" should be neutral:
  // chip OFF in that case means "no disagreement", not "positive evidence".
  const journalSupportsCitation = journalMatches && Boolean(result.best_match.journal?.trim())
  const doiSupportsCitation =
    doiMatches && Boolean(result.best_match.doi?.trim())

  // Year remains a visible, toggleable problem chip but does not affect the
  // Valid / Citation / Fabricated decision.
  if (authorMatches && titleMatches && journalMatches) return 'valid'
  if (titleMatches || (authorMatches && (journalSupportsCitation || doiSupportsCitation))) return 'citation'
  return 'fabricated'
}

export function effectiveDecisionTag(result: TagStateResultLike | undefined): DecisionTag {
  if (!result) return 'valid'
  // A manual pill cycle (decision_tag_override) always wins. Otherwise the pill
  // reflects the current chip states — so toggling a chip immediately
  // re-classifies the source.
  if (result.decision_tag_override) return result.decision_tag_override
  return classifyDecisionFromTags(result)
}

export type { VerificationResult }

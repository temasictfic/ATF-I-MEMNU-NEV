// Shared mappings between verification/parse status values and UI presentation
// (color, icon, label). Single source of truth so ParsingPage, VerificationPage,
// and the PDF report writer can't drift apart.

import i18n from '../i18n'
import type { VerificationResult } from '../api/types'
import { DB_SCORE_HEX, DB_SCORE_RGB, STATUS_HEX, STATUS_RGB } from '../constants/colors'
import {
  STATUS_HIGH_THRESHOLD,
  STATUS_MEDIUM_THRESHOLD,
} from '../constants/scoring'

type RgbTuple = readonly [number, number, number]

// --- Parsing-page status (PdfDocument.status) ---

export function parseStatusIcon(status: string): string {
  switch (status) {
    case 'approved': return '✓'
    case 'parsed':   return '?'
    case 'parsing':  return '◌'
    case 'error':    return '✕'
    default:         return '◌'
  }
}

export function parseStatusColor(status: string): string {
  switch (status) {
    case 'approved': return DB_SCORE_HEX.high
    case 'parsed':   return DB_SCORE_HEX.medium
    case 'error':    return DB_SCORE_HEX.low
    default:         return STATUS_HEX.neutral
  }
}

// --- Verification-page status (VerificationResult.status) ---

export function verifyStatusColor(result: VerificationResult | undefined): string {
  if (!result) return STATUS_HEX.pending
  switch (result.status) {
    case 'high':        return STATUS_HEX.high
    case 'medium':      return STATUS_HEX.medium
    case 'low':         return STATUS_HEX.low
    case 'in_progress': return STATUS_HEX.in_progress
    default:            return STATUS_HEX.neutral
  }
}

export function verifyStatusLabel(result: VerificationResult | undefined): string {
  if (!result) return i18n.t('verification.status.pending')
  switch (result.status) {
    case 'high':        return i18n.t('verification.status.high')
    case 'medium':      return i18n.t('verification.status.medium')
    case 'low':         return i18n.t('verification.status.low')
    case 'in_progress': return i18n.t('verification.status.in_progress')
    default:            return i18n.t('verification.status.pending')
  }
}

// --- Per-database score → UI presentation ---

export function dbScoreIcon(score: number): string {
  if (score >= STATUS_HIGH_THRESHOLD) return '✓'
  if (score >= STATUS_MEDIUM_THRESHOLD) return '~'
  return '✕'
}

export function dbScoreColor(score: number): string {
  if (score >= STATUS_HIGH_THRESHOLD) return DB_SCORE_HEX.high
  if (score >= STATUS_MEDIUM_THRESHOLD) return DB_SCORE_HEX.medium
  return DB_SCORE_HEX.low
}

// --- RGB tuple variants (for pdf-lib export) ---
// Same threshold/mapping logic as the hex variants above, returning [r, g, b]
// floats so the report writer can wrap them with pdf-lib's `rgb()` without
// re-implementing the threshold checks.

export function verifyStatusRgbTuple(status: string): RgbTuple {
  switch (status) {
    case 'high':        return STATUS_RGB.high
    case 'medium':      return STATUS_RGB.medium
    case 'low':         return STATUS_RGB.low
    case 'in_progress': return STATUS_RGB.in_progress
    case 'pending':     return STATUS_RGB.pending
    default:            return STATUS_RGB.neutral
  }
}

export function dbScoreRgbTuple(score: number): RgbTuple {
  if (score >= STATUS_HIGH_THRESHOLD) return DB_SCORE_RGB.high
  if (score >= STATUS_MEDIUM_THRESHOLD) return DB_SCORE_RGB.medium
  return DB_SCORE_RGB.low
}

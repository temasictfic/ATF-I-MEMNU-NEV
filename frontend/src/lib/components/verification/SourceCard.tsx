// One source card in the Verification page's center list.
//
// Memoized: a verification run updates one source at a time, and with the
// cards inlined in VerificationPage every WebSocket event re-rendered all of
// them. Props are the card's own data plus a stable `actions` object, so a
// card re-renders only when something it shows changes.

import { memo } from 'react'
import type React from 'react'
import { useTranslation } from 'react-i18next'
import i18n from '../../i18n'
import type { DbCheckEntry, SourceRectangle, SourceVerifyProgress, TagKey, VerificationResult } from '../../api/types'
import { TAG_ORDER, effectiveTagOn, effectiveDecisionTag } from '../../verification/tagState'
import { verifyStatusColor as statusColor } from '../../utils/status-helpers'
import { STATUS_HEX, DECISION_HEX } from '../../constants/colors'
import { problemTagDescription, problemTagLabel } from './problem-tags'
import styles from './VerificationPage.module.css'

export interface SourceCardActions {
  registerRef(sourceId: string, el: HTMLDivElement | null): void
  select(sourceId: string | null): void
  toggleEnabled(sourceId: string): void
  resetText(sourceId: string): void
  setText(sourceId: string, text: string): void
  toggleTag(sourceId: string, tag: TagKey): void
  cycleDecision(sourceId: string): void
  dragStart(e: React.DragEvent, sourceId: string): void
  dragOver(e: React.DragEvent, idx: number): void
  drop(e: React.DragEvent, sourceId: string): void
  dragEnd(): void
  autoResize(el: HTMLTextAreaElement): void
}

export interface SourceCardProps {
  source: SourceRectangle
  result: VerificationResult | undefined
  progress: SourceVerifyProgress | undefined
  idx: number
  isSelected: boolean
  isMuaf: boolean
  enabled: boolean
  exclusionReason: string | undefined
  isDropTarget: boolean
  /** Text shown and verified (edited text, or the cleaned source text). */
  text: string
  showReset: boolean
  enabledDatabases: string[]
  dragDisabled: boolean
  actions: SourceCardActions
}

export const SourceCard = memo(function SourceCard({
  source,
  result,
  progress,
  idx,
  isSelected,
  isMuaf,
  enabled,
  exclusionReason,
  isDropTarget,
  text,
  showReset,
  enabledDatabases,
  dragDisabled,
  actions,
}: SourceCardProps) {
  const { t } = useTranslation()
  const cardClass = [
    styles['source-card'],
    isSelected ? styles['card-selected'] : '',
    isMuaf ? styles['card-disabled'] : '',
    isDropTarget ? styles['card-dragover'] : '',
  ].filter(Boolean).join(' ')

  // Tint the selected-border per decision state (Valid/Citation/Fabricated).
  const decision = result ? effectiveDecisionTag(result) : null
  const decisionBorderColor =
    decision === 'valid' ? DECISION_HEX.validBorder
    : decision === 'citation' ? DECISION_HEX.citationBorder
    : decision === 'fabricated' ? DECISION_HEX.fabricatedBorder
    : undefined
  const decisionGlow =
    decision === 'valid' ? 'rgba(134, 239, 172, 0.35)'
    : decision === 'citation' ? 'rgba(148, 163, 184, 0.35)'
    : decision === 'fabricated' ? 'rgba(232, 121, 249, 0.35)'
    : undefined

  return (
    <div
      ref={(el) => actions.registerRef(source.id, el)}
      className={cardClass}
      style={decisionBorderColor ? {
        ['--card-decision-border' as string]: decisionBorderColor,
        ['--card-decision-glow' as string]: decisionGlow,
      } as React.CSSProperties : undefined}
      role="button"
      tabIndex={0}
      aria-pressed={isSelected}
      onClick={() => actions.select(isSelected ? null : source.id)}
      onKeyDown={(e) => {
        if (e.currentTarget !== e.target) return
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          actions.select(isSelected ? null : source.id)
        }
      }}
      onDragOver={(e) => actions.dragOver(e, idx)}
      onDrop={(e) => actions.drop(e, source.id)}
    >
      {/* Status bar */}
      <div className={styles['status-bar']} style={{ background: statusColor(result) }} />

      <div className={styles['card-body']}>
        {/* Header row */}
        <div className={styles['card-header']}>
          <button
            type="button"
            className={`${styles['ref-badge']} ${isMuaf ? styles['ref-badge-disabled'] : ''}`}
            aria-pressed={!isMuaf}
            title={enabled ? t('verification.disableRef') : t('verification.enableRef')}
            onClick={(e) => { e.stopPropagation(); actions.toggleEnabled(source.id) }}
          >
            [{source.ref_number ?? '?'}]
          </button>
          {isMuaf ? (
            <span
              className={styles['status-badge']}
              style={{ background: '#9ca3af', color: 'white' }}
              title={exclusionReason}
            >
              {t('verification.muaf')}
            </span>
          ) : result?.status === 'in_progress' ? (
            <span className={styles['status-badge']} style={{ background: STATUS_HEX.in_progress, color: 'white' }}>
              {i18n.t('verification.status.in_progress')}
            </span>
          ) : null}
          <span className={styles['card-actions']}>
            <button
              className={styles['copy-btn']}
              onClick={(e) => {
                e.stopPropagation()
                actions.select(source.id)
                navigator.clipboard.writeText(text)
              }}
              title={t('verification.copyText')}
            >
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="5.5" y="5.5" width="9" height="9" rx="1.5" />
                <path d="M10.5 5.5V3a1.5 1.5 0 00-1.5-1.5H3A1.5 1.5 0 001.5 3v6A1.5 1.5 0 003 10.5h2.5" />
              </svg>
            </button>
            {showReset && (
              <button
                className={styles['reset-btn']}
                onClick={(e) => {
                  e.stopPropagation()
                  actions.resetText(source.id)
                }}
                title={t('verification.resetToOriginal')}
              >&#x21BA;</button>
            )}
          </span>
          <span className={styles['card-status-group']}>
            {result && (
              <span className={styles['problem-tags']}>
                {TAG_ORDER.map((tag: TagKey) => {
                  const on = effectiveTagOn(result, tag)
                  const isTitle = tag === 'title'
                  const titleSim = result?.best_match?.match_details?.title_similarity ?? 0
                  const titleGood = isTitle && titleSim >= 0.85
                  const baseClass = isTitle
                    ? (titleGood ? styles['title-tag-good'] : styles['title-tag'])
                    : styles['problem-tag']
                  const offClass = isTitle ? styles['title-tag-off'] : styles['problem-tag-off']
                  const className = on ? baseClass : offClass
                  const onToggle = (e: React.MouseEvent) => {
                    e.stopPropagation()
                    actions.toggleTag(source.id, tag)
                  }
                  if (isTitle) {
                    const bm = result?.best_match
                    const pct = bm ? Math.round(bm.match_details.title_similarity * 100) : null
                    const label = pct != null
                      ? `${t('verification.titleShort')}: ${pct}%`
                      : `${t('verification.titleShort')}: —`
                    const tip = on ? problemTagDescription('!title') : undefined
                    return (
                      <button key={tag} type="button" className={className} onClick={onToggle} title={tip}>
                        {label}
                      </button>
                    )
                  }
                  const bangKey = `!${tag}`
                  const tip = on ? problemTagDescription(bangKey) : undefined
                  return (
                    <button key={tag} type="button" className={className} onClick={onToggle} title={tip}>
                      {problemTagLabel(bangKey)}
                    </button>
                  )
                })}
              </span>
            )}
            {result && (() => {
              const decision = effectiveDecisionTag(result)
              const cls = decision === 'valid'
                ? styles['valid-tag']
                : decision === 'citation'
                  ? styles['citation-tag']
                  : styles['fabricated-tag']
              const label = decision === 'valid'
                ? t('verification.validTag')
                : decision === 'citation'
                  ? t('verification.citationTag')
                  : t('verification.fabricatedTag')
              const tip = decision === 'valid'
                ? t('verification.validTagTooltip')
                : decision === 'citation'
                  ? t('verification.citationTagTooltip')
                  : t('verification.fabricatedTagTooltip')
              return (
                <button
                  type="button"
                  className={cls}
                  title={tip}
                  onClick={(e) => {
                    e.stopPropagation()
                    actions.cycleDecision(source.id)
                  }}
                >
                  {label}
                </button>
              )
            })()}
          </span>
        </div>

        {/* Textarea row — drag handle on the left, vertically centered */}
        <div className={styles['card-body-row']}>
          <span
            className={[
              styles['drag-handle'],
              dragDisabled ? styles['drag-handle-disabled'] : '',
            ].filter(Boolean).join(' ')}
            draggable={!dragDisabled}
            onDragStart={(e) => {
              if (dragDisabled) { e.preventDefault(); return }
              e.stopPropagation(); actions.dragStart(e, source.id)
            }}
            onDragEnd={actions.dragEnd}
            onPointerDown={(e) => e.stopPropagation()}
            title={t('verification.dragToReorder')}
            aria-hidden="true"
          >&#x2807;</span>
          <textarea
            className={styles['card-textarea']}
            value={text}
            onInput={(e) => {
              const el = e.target as HTMLTextAreaElement
              actions.setText(source.id, el.value)
              actions.autoResize(el)
            }}
            onFocus={(e) => { actions.select(source.id); actions.autoResize(e.target as HTMLTextAreaElement) }}
            onClick={(e) => { e.stopPropagation(); actions.select(source.id) }}
            rows={2}
            disabled={isMuaf}
          />
        </div>

        {/* Progress indicator */}
        {progress && (result?.status === 'in_progress' || progress.checkedDbs.length > 0) && (
          <div className={styles['card-progress']}>
            {result?.status === 'in_progress' && (
              <span className={styles['progress-spinner']}>&#x25CC;</span>
            )}
            <span className={styles['progress-text']}>
              {progress.currentDb
                ? t('verification.status.searchingDb', { db: progress.currentDb })
                : result?.status === 'in_progress'
                  ? t('verification.status.waiting')
                  : (() => {
                      const actuallySearched = progress!.checkedDbs.filter((d: DbCheckEntry) => d.status !== 'skipped').length
                      const skippedCount = progress!.checkedDbs.filter((d: DbCheckEntry) => d.status === 'skipped').length
                      return skippedCount > 0
                        ? t('verification.status.searchedCountWithSkipped', { count: actuallySearched, skipped: skippedCount })
                        : t('verification.status.searchedCount', { count: actuallySearched })
                    })()}
            </span>
            <div className={styles['progress-dots']}>
              {enabledDatabases.map(db => {
                const check = progress?.checkedDbs.find((d: DbCheckEntry) => d.name === db)
                const isCurrent = progress?.currentDb === db
                const dotClass = [
                  styles['progress-dot'],
                  check ? styles[`dot-${check.status}`] : '',
                  isCurrent ? styles['dot-current'] : '',
                ].filter(Boolean).join(' ')
                return (
                  <span
                    key={db}
                    className={dotClass}
                    title={`${db}${check ? `: ${check.status}${check.errorMessage ? ` — ${check.errorMessage}` : ''}` : ''}`}
                  />
                )
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  )
})

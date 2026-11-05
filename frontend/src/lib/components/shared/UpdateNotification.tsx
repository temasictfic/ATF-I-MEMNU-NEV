import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import styles from './UpdateNotification.module.css'
import { hostAPI } from '../../host/bridge'
import type { ModelStatus, UpdateAvailablePayload, UpdateProgressPayload } from '../../host/bridge'

type UpdateStage = 'hidden' | 'available' | 'downloading' | 'ready' | 'error'

/**
 * Header status pill. It shows the app-update flow (driven by the Python
 * updater, formerly electron-updater). When no update is pending, it also
 * shows the one-time download of the citation NER model, which is no longer
 * bundled with each release.
 */
export default function UpdateNotification() {
  const { t } = useTranslation()
  const [stage, setStage] = useState<UpdateStage>('hidden')
  const [version, setVersion] = useState('')
  const [progressPercent, setProgressPercent] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [model, setModel] = useState<ModelStatus>({ state: 'ready' })
  // After the user dismisses an in-flight download, late progress/downloaded
  // events must not pop the card back up (they used to). A new
  // "update available" announcement re-arms it.
  const dismissedRef = useRef(false)

  useEffect(() => {
    const offAvailable = hostAPI.onUpdateAvailable((info: UpdateAvailablePayload) => {
      dismissedRef.current = false
      setVersion(info.version)
      setProgressPercent(0)
      setErrorMessage('')
      setStage('available')
    })

    const offProgress = hostAPI.onUpdateProgress((progress: UpdateProgressPayload) => {
      if (dismissedRef.current) return
      setProgressPercent(Math.max(0, Math.min(100, Number(progress.percent || 0))))
      setErrorMessage('')
      setStage('downloading')
    })

    const offDownloaded = hostAPI.onUpdateDownloaded(() => {
      if (dismissedRef.current) return
      setStage('ready')
    })

    const offError = hostAPI.onUpdateError((message: string) => {
      if (dismissedRef.current) return
      setErrorMessage(message || '')
      setStage('error')
    })

    // Clear a stale error banner if a later check confirms we're up to date.
    // Don't disrupt an active 'available'/'downloading'/'ready' flow.
    const offNotAvailable = hostAPI.onUpdateNotAvailable(() => {
      setStage((prev) => (prev === 'error' ? 'hidden' : prev))
    })

    const offModel = hostAPI.onModelStatus(setModel)

    // Events can predate this component (the startup check may fire before
    // the WebSocket connects), so replay the host's current state once.
    void hostAPI
      .getUpdateState()
      .then((state) => {
        if (!state || dismissedRef.current) return
        if (state.version) setVersion(state.version)
        if (typeof state.percent === 'number') setProgressPercent(state.percent)
        if (state.error) setErrorMessage(state.error)
        if (['available', 'downloading', 'ready', 'error'].includes(state.stage)) {
          setStage(state.stage as UpdateStage)
        }
      })
      .catch(() => {})
    void hostAPI.getModelStatus().then(setModel).catch(() => {})

    return () => {
      offAvailable()
      offProgress()
      offDownloaded()
      offError()
      offNotAvailable()
      offModel()
    }
  }, [])

  const hide = () => setStage('hidden')

  if (stage === 'hidden') {
    return <ModelStatusCard status={model} />
  }

  const baseLabel = (() => {
    if (stage === 'available') return version ? t('update.availableWithVersion', { version }) : t('update.availableGeneric')
    if (stage === 'downloading') return `${progressPercent.toFixed(1)}%`
    if (stage === 'ready') return t('update.ready')
    if (stage === 'error') return errorMessage || t('update.failed')
    return ''
  })()

  const hoverAction = (() => {
    if (stage === 'available') {
      return (
        <button
          type="button"
          className={styles['primary-btn']}
          onClick={() => {
            setStage('downloading')
            hostAPI.downloadUpdate()
          }}
        >
          {t('update.download')}
        </button>
      )
    }
    if (stage === 'downloading') {
      return (
        <button
          type="button"
          className={styles['secondary-btn']}
          onClick={() => {
            dismissedRef.current = true
            hostAPI.cancelUpdate()
            hide()
          }}
        >
          {t('update.dismiss')}
        </button>
      )
    }
    if (stage === 'ready') {
      return (
        <button type="button" className={styles['primary-btn']} onClick={() => hostAPI.installUpdate()}>
          {t('update.restart')}
        </button>
      )
    }
    if (stage === 'error') {
      return (
        <button
          type="button"
          className={styles['secondary-btn']}
          onClick={() => {
            setErrorMessage('')
            hide()
          }}
        >
          {t('update.dismiss')}
        </button>
      )
    }
    return null
  })()

  return (
    <aside className={styles['update-card']} role="status" aria-live="polite">
      <span className={`${styles['status-dot']} ${styles[`dot-${stage}`]}`} aria-hidden="true" />
      <div className={styles['hover-area']}>
        <span className={styles['base-label']}>{baseLabel}</span>
        {hoverAction && <div className={styles['hover-action']}>{hoverAction}</div>}
      </div>

      {stage === 'available' && (
        <button type="button" className={styles['close-btn']} aria-label={t('update.dismissAria')} onClick={hide}>
          ×
        </button>
      )}
    </aside>
  )
}

function ModelStatusCard({ status }: { status: ModelStatus }) {
  const { t } = useTranslation()
  if (status.state === 'ready' || status.state === 'missing') return null

  const label =
    status.state === 'downloading'
      ? t('model.downloading', { percent: status.percent.toFixed(0) })
      : status.state === 'verifying'
        ? t('model.verifying')
        : t('model.failed')

  return (
    <aside
      className={styles['update-card']}
      role="status"
      aria-live="polite"
      title={status.state === 'error' ? status.message : t('model.tooltip')}
    >
      <span
        className={`${styles['status-dot']} ${styles[status.state === 'error' ? 'dot-error' : 'dot-model']}`}
        aria-hidden="true"
      />
      <div className={styles['hover-area']}>
        <span className={styles['base-label']}>{label}</span>
        {status.state === 'error' && (
          <div className={styles['hover-action']}>
            <button type="button" className={styles['primary-btn']} onClick={() => hostAPI.retryModelDownload()}>
              {t('model.retry')}
            </button>
          </div>
        )}
      </div>
    </aside>
  )
}

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import styles from './UpdateNotification.module.css'

type UpdateStage = 'hidden' | 'available' | 'downloading' | 'ready' | 'error'

interface UpdateAvailablePayload {
  version: string
}

interface UpdateProgressPayload {
  percent: number
}

export default function UpdateNotification() {
  const { t } = useTranslation()
  const [stage, setStage] = useState<UpdateStage>('hidden')
  const [visible, setVisible] = useState(false)
  const [version, setVersion] = useState('')
  const [progressPercent, setProgressPercent] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')

  useEffect(() => {
    const offAvailable = window.electronAPI.onUpdateAvailable((info: UpdateAvailablePayload) => {
      setVersion(info.version)
      setProgressPercent(0)
      setErrorMessage('')
      setStage('available')
      setVisible(true)
    })

    const offProgress = window.electronAPI.onUpdateProgress((progress: UpdateProgressPayload) => {
      setProgressPercent(Math.max(0, Math.min(100, Number(progress.percent || 0))))
      setErrorMessage('')
      setStage('downloading')
      setVisible(true)
    })

    const offDownloaded = window.electronAPI.onUpdateDownloaded(() => {
      setStage('ready')
      setVisible(true)
    })

    const offError = window.electronAPI.onUpdateError((message: string) => {
      setErrorMessage(message || '')
      setStage('error')
      setVisible(true)
    })

    const offNotAvailable = window.electronAPI.onUpdateNotAvailable(() => {
      // Clear a stale error banner if a subsequent check confirms we're up to
      // date. Don't disrupt an active 'available'/'downloading'/'ready' flow.
      setStage(prev => {
        if (prev === 'error') {
          setVisible(false)
          setErrorMessage('')
          return 'hidden'
        }
        return prev
      })
    })

    return () => {
      offAvailable()
      offProgress()
      offDownloaded()
      offError()
      offNotAvailable()
    }
  }, [])

  if (!visible || stage === 'hidden') {
    return null
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
            window.electronAPI.downloadUpdate()
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
            window.electronAPI.cancelUpdate()
            setVisible(false)
            setStage('hidden')
          }}
        >
          {t('update.dismiss')}
        </button>
      )
    }
    if (stage === 'ready') {
      return (
        <button
          type="button"
          className={styles['primary-btn']}
          onClick={() => window.electronAPI.installUpdate()}
        >
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
            setVisible(false)
            setStage('hidden')
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
        <button
          type="button"
          className={styles['close-btn']}
          aria-label={t('update.dismissAria')}
          onClick={() => {
            setVisible(false)
            setStage('hidden')
          }}
        >
          ×
        </button>
      )}
    </aside>
  )
}

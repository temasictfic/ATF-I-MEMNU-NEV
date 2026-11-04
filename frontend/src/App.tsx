import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { wsClient } from './lib/api/ws-client'
import { initPdfListeners } from './lib/stores/pdf-store'
import { initVerificationListeners } from './lib/stores/verification-store'
import { useSettingsStore } from './lib/stores/settings-store'
import ParsingPage from './lib/components/parsing/ParsingPage'
import VerificationPage from './lib/components/verification/VerificationPage'
import SettingsPage from './lib/components/settings/SettingsPage'
import UpdateNotification from './lib/components/shared/UpdateNotification'
import iconUrl from './assets/icon.png'
import tubitakLogoUrl from './assets/tubitak-yatay.png'
import styles from './App.module.css'

type TabId = 'parsing' | 'verification' | 'settings'

interface Tab {
  id: TabId
  icon: string
}

const tabs: Tab[] = [
  { id: 'parsing', icon: '\u2B12' },
  { id: 'verification', icon: '\u25C9' },
]

export default function App() {
  const { t, i18n } = useTranslation()
  const [activeTab, setActiveTab] = useState<TabId>('parsing')
  const tabNavRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    document.title = t('app.windowTitle')
  }, [t, i18n.language])

  useEffect(() => {
    const el = tabNavRef.current
    if (!el) return
    const root = document.documentElement
    const update = () => {
      const rect = el.getBoundingClientRect()
      root.style.setProperty('--tab-left-vp', `${rect.left}px`)
      root.style.setProperty('--tab-right-vp', `${rect.right}px`)
      root.style.setProperty('--tab-center-vp', `${(rect.left + rect.right) / 2}px`)
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    window.addEventListener('resize', update)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', update)
    }
  }, [])

  // Initialize WebSocket, listeners, and settings on mount.
  // loadSettings is idempotent (no-op once successfully loaded). It runs
  // once at mount AND on every WebSocket (re)connect — the backend's
  // FastAPI HTTP server is up at the same time as its WS, so a successful
  // ws:connected event is a reliable signal that GET /settings is now
  // ready to answer. This is how we recover from a cold-start race
  // without an explicit retry loop in the store.
  useEffect(() => {
    wsClient.connect()
    const cleanupPdf = initPdfListeners()
    const cleanupVerify = initVerificationListeners()
    const reloadSettings = () => {
      useSettingsStore.getState().loadSettings()
    }
    reloadSettings()
    const cleanupWsConnect = wsClient.on('ws:connected', reloadSettings)
    return () => {
      cleanupPdf()
      cleanupVerify()
      cleanupWsConnect()
      wsClient.disconnect()
    }
  }, [])

  return (
    <div className={styles['app-shell']}>
      {/* Header */}
      <header className={styles['header-bar']}>
        <div className={styles['header-inner']}>
          {/* Brand */}
          <div className={styles['brand']}>
            <img src={tubitakLogoUrl} alt="TÜBİTAK" className={styles['brand-logo']} />
          </div>

          {/* Tabs */}
          <nav className={styles['tab-nav']} ref={tabNavRef}>
            {tabs.map(tab => (
              <button
                key={tab.id}
                className={`${styles['tab-btn']} ${activeTab === tab.id ? styles['tab-active'] : ''}`}
                onClick={() => setActiveTab(tab.id)}
              >
                <span className={styles['tab-icon']}>{tab.icon}</span>
                <span>{t(`app.tabs.${tab.id}`)}</span>
                {activeTab === tab.id && <div className={styles['tab-indicator']} />}
              </button>
            ))}
          </nav>

          {/* Status */}
          <div className={styles['header-status']}>
            <UpdateNotification />
            <button
              type="button"
              className={styles['status-icon-ring']}
              onClick={() => setActiveTab('settings')}
              title={t('app.tabs.settings')}
              aria-label={t('app.tabs.settings')}
            >
              <img src={iconUrl} alt="App icon" className={styles['status-icon']} />
            </button>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className={styles['main-content']}>
        <div style={{ display: activeTab === 'parsing' ? 'contents' : 'none' }}>
          <ParsingPage />
        </div>
        <div style={{ display: activeTab === 'verification' ? 'contents' : 'none' }}>
          <VerificationPage />
        </div>
        {activeTab === 'settings' && <SettingsPage />}
      </main>

      <footer className={styles['footer-bar']}>
        <button
          className={styles['scroll-top-btn']}
          onClick={() => {
            document.querySelectorAll('[data-scrollable]').forEach(el => {
              el.scrollTo({ top: 0, behavior: 'smooth' })
            })
          }}
          title={t('app.scrollToTop')}
          aria-label={t('app.scrollToTop')}
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 12V4" />
            <path d="M4 7l4-4 4 4" />
          </svg>
        </button>
      </footer>
    </div>
  )
}

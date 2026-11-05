// Boot information the desktop host injects into index.html before any of
// our code runs (see backend/main.py `_index_response` and
// atfi_app/app.py `boot_info`). It replaces Electron's preload-time
// globals and the old "poll backend:getPort 120×500 ms" discovery.

export interface SessionPdfEntry {
  path: string
  name: string
  importedAt: number
}

export type HostMode = 'webview2' | 'dev-browser'

export interface BootInfo {
  /** Backend/UI port. The UI is served from http://127.0.0.1:<port>/. */
  port: number
  /** Per-launch API token (X-Atfi-Token header / `token` WS query param). */
  token: string | null
  /** App version shown in Settings (single source: atfi_app/version.py). */
  version: string
  /** Saved UI language, so the first paint is already in the right language. */
  language?: 'tr' | 'en'
  /** 'webview2' inside the desktop app; 'dev-browser' in a normal browser (dev/tests). */
  mode: HostMode
  /** Previous run's imported PDFs (persisted by the host, not localStorage). */
  lastSessionPdfs?: SessionPdfEntry[]
  /** Updater state is unavailable in dev runs and portable builds. */
  updatesEnabled?: boolean
}

declare global {
  interface Window {
    __ATFI__?: Partial<BootInfo>
  }
}

function readBootInfo(): BootInfo {
  const injected = window.__ATFI__ ?? {}
  // `npm run dev` (Vite on :5173) has no injected object: point it at a
  // backend started with `python -m atfi_app --dev-browser --port 8765`.
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {}
  const port = Number(injected.port ?? env.VITE_ATFI_PORT ?? window.location.port ?? 0)
  return {
    port,
    token: injected.token ?? env.VITE_ATFI_TOKEN ?? null,
    version: injected.version ?? '0.0.0-dev',
    language: injected.language,
    mode: injected.mode ?? 'dev-browser',
    lastSessionPdfs: Array.isArray(injected.lastSessionPdfs) ? injected.lastSessionPdfs : [],
    updatesEnabled: injected.updatesEnabled ?? false,
  }
}

export const boot: BootInfo = readBootInfo()

/**
 * Origin of the backend. When the UI is served by the backend itself (the
 * normal case) this is the page's own origin, so every request is
 * same-origin: no CORS preflights and no mixed-content / local-network
 * checks.
 */
export const backendOrigin: string = (() => {
  const sameOrigin =
    window.location.protocol === 'http:' &&
    (window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost') &&
    Number(window.location.port) === boot.port
  return sameOrigin ? window.location.origin : `http://127.0.0.1:${boot.port}`
})()

export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  return boot.token ? { 'X-Atfi-Token': boot.token, ...extra } : { ...extra }
}

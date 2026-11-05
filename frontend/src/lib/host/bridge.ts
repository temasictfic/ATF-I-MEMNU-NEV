// Desktop-host bridge (replaces Electron's preload `window.electronAPI`).
//
// Every host operation is a token-authenticated, same-origin HTTP call to
// /__host/rpc/<method>, served by the Python host (atfi_app/host_api.py).
// Host → page events (updater, model download, native webview events)
// arrive on the backend WebSocket as `host_event` messages.
// The transport is the same inside WebView2 and in a plain browser
// (`--dev-browser`, used for development and the automated UI tests).
// Only dropped-file paths need WebView2 itself.

import { wsClient } from '../api/ws-client'
import { authHeaders, backendOrigin, boot } from './boot'
import type { SessionPdfEntry } from './boot'

export interface UpdateAvailablePayload {
  version: string
  releaseNotes?: unknown
}

export interface UpdateProgressPayload {
  percent: number
}

export type ModelStatus =
  | { state: 'ready' }
  | { state: 'missing' }
  | { state: 'downloading'; percent: number; receivedBytes: number; totalBytes: number }
  | { state: 'verifying' }
  | { state: 'error'; message: string }

export interface SaveDialogOptions {
  title?: string
  defaultPath?: string
  buttonLabel?: string
  filters?: Array<{ name: string; extensions: string[] }>
}

export class HostError extends Error {
  constructor(message: string, public readonly status: number) {
    super(message)
    this.name = 'HostError'
  }
}

async function rpc<T>(method: string, ...args: unknown[]): Promise<T> {
  const res = await fetch(`${backendOrigin}/__host/rpc/${method}`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ args }),
  })
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      message = body?.detail ?? message
    } catch {
      // not JSON
    }
    throw new HostError(`host.${method} failed: ${message}`, res.status)
  }
  const body = (await res.json()) as { result: T }
  return body.result
}

/** Fire-and-forget variant for high-frequency calls (e.g. native view bounds). */
function notify(method: string, ...args: unknown[]): void {
  void rpc(method, ...args).catch((err) => console.warn(`[host] ${method}:`, err))
}

type Listener = (payload: any) => void
const listeners = new Map<string, Set<Listener>>()
let wsSubscribed = false

function ensureHostEventSubscription(): void {
  if (wsSubscribed) return
  wsSubscribed = true
  wsClient.on('host_event', (data) => {
    const channel = String((data as { channel?: unknown }).channel ?? '')
    const payload = (data as { payload?: unknown }).payload
    listeners.get(channel)?.forEach((cb) => {
      try {
        cb(payload)
      } catch (err) {
        console.error(`[host] listener for ${channel} failed:`, err)
      }
    })
  })
}

export function onHostEvent(channel: string, cb: Listener): () => void {
  ensureHostEventSubscription()
  let set = listeners.get(channel)
  if (!set) {
    set = new Set()
    listeners.set(channel, set)
  }
  set.add(cb)
  return () => {
    set!.delete(cb)
  }
}

function pdfUrl(filePath: string): string {
  return `${backendOrigin}/__host/pdf?path=${encodeURIComponent(filePath)}`
}

interface ChromeWebview {
  postMessageWithAdditionalObjects?: (message: unknown, objects: ArrayLike<unknown>) => void
}

function chromeWebview(): ChromeWebview | undefined {
  return (window as unknown as { chrome?: { webview?: ChromeWebview } }).chrome?.webview
}

export const hostAPI = {
  mode: boot.mode,
  version: boot.version,
  updatesEnabled: boot.updatesEnabled ?? false,

  // ---- dialogs -----------------------------------------------------------
  selectDirectory: (defaultPath?: string): Promise<string | null> => rpc('selectDirectory', defaultPath ?? null),
  selectPdfs: (): Promise<string[]> => rpc('selectPdfs'),
  showSaveAs: (options: SaveDialogOptions = {}): Promise<string | null> => rpc('showSaveAs', options),

  // ---- files -------------------------------------------------------------
  readPdfFile: async (filePath: string): Promise<Uint8Array> => {
    const res = await fetch(pdfUrl(filePath), { headers: authHeaders() })
    if (!res.ok) throw new HostError(`Cannot read ${filePath}: ${await res.text()}`, res.status)
    return new Uint8Array(await res.arrayBuffer())
  },
  writePdfFile: async (filePath: string, bytes: Uint8Array): Promise<void> => {
    const res = await fetch(pdfUrl(filePath), {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/pdf' }),
      body: bytes as unknown as BodyInit,
    })
    if (!res.ok) throw new HostError(`Cannot write ${filePath}: ${await res.text()}`, res.status)
  },
  listPdfsInDirectory: (directory: string): Promise<string[]> => rpc('listPdfsInDirectory', directory),
  /**
   * Absolute paths of files dropped onto the page. WebView2 hands the real
   * paths to the host through `postMessageWithAdditionalObjects`. In a
   * plain browser (dev mode) paths are unavailable and this returns [].
   */
  getPathsForFiles: async (files: File[]): Promise<string[]> => {
    const webview = chromeWebview()
    if (files.length === 0 || !webview?.postMessageWithAdditionalObjects) return []
    webview.postMessageWithAdditionalObjects('FilesDropped', files)
    return rpc<string[]>('takeDroppedPaths', files.length)
  },

  // ---- shell -------------------------------------------------------------
  openExternal: (url: string): Promise<void> => rpc('openExternal', url),
  openCacheFolder: (): Promise<{ ok: boolean; path: string; error: string | null }> => rpc('openCacheFolder'),
  fetchExclusionList: (url: string): Promise<{ text: string | null; fromCache: boolean; error?: string }> =>
    rpc('fetchExclusionList', url),
  openDevTools: (): void => notify('openDevTools'),

  // ---- Scholar browsing session -----------------------------------------
  clearScholarSession: (): Promise<{ ok: boolean }> => rpc('clearScholarSession'),

  // ---- session list (was localStorage, which is per-origin and the origin
  //      now carries a per-launch port) --------------------------------------
  saveSessionPdfs: (entries: SessionPdfEntry[]): void => notify('saveSessionPdfs', entries),

  // ---- updater -----------------------------------------------------------
  onUpdateAvailable: (cb: (info: UpdateAvailablePayload) => void) => onHostEvent('update:available', cb),
  onUpdateProgress: (cb: (progress: UpdateProgressPayload) => void) => onHostEvent('update:progress', cb),
  onUpdateDownloaded: (cb: () => void) => onHostEvent('update:downloaded', () => cb()),
  onUpdateError: (cb: (message: string) => void) => onHostEvent('update:error', cb),
  onUpdateNotAvailable: (cb: () => void) => onHostEvent('update:notAvailable', () => cb()),
  downloadUpdate: (): void => notify('downloadUpdate'),
  cancelUpdate: (): void => notify('cancelUpdate'),
  installUpdate: (): void => notify('installUpdate'),
  /** Replays the current updater state (events may predate the WS connection). */
  getUpdateState: (): Promise<{ stage: string; version?: string; percent?: number; error?: string } | null> =>
    rpc('getUpdateState'),

  // ---- NER model (downloaded once per machine, not shipped per release) --
  getModelStatus: (): Promise<ModelStatus> => rpc('getModelStatus'),
  onModelStatus: (cb: (status: ModelStatus) => void) => onHostEvent('model:status', cb),
  retryModelDownload: (): void => notify('retryModelDownload'),
}

export type HostAPI = typeof hostAPI

/** Internal RPC surface used by <atfi-webview>. */
export const nativeViews = {
  create: (id: string, options: Record<string, unknown>): Promise<void> => rpc('webview.create', id, options),
  destroy: (id: string): void => notify('webview.destroy', id),
  setBounds: (id: string, bounds: Record<string, unknown>): void => notify('webview.setBounds', id, bounds),
  call: <T = unknown>(id: string, method: string, ...args: unknown[]): Promise<T> =>
    rpc<T>('webview.call', id, method, args),
}

// DevTools: F12 / Ctrl+Shift+I, as in the Electron build (the native
// browser accelerators are disabled on the main view, so the page sees them).
if (boot.mode === 'webview2') {
  window.addEventListener(
    'keydown',
    (e) => {
      const isF12 = e.key === 'F12'
      const isCtrlShiftI = e.ctrlKey && e.shiftKey && (e.key === 'I' || e.key === 'i')
      if (isF12 || isCtrlShiftI) {
        e.preventDefault()
        hostAPI.openDevTools()
      }
    },
    true,
  )
}

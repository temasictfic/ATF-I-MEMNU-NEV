// <atfi-webview>: drop-in replacement for Electron's <webview> tag.
//
// In the desktop app each element is backed by a *native* WebView2 control
// that the Python host (atfi_app/native/guest_view.py) places over the page
// at the element's on-screen rectangle. The element keeps the subset of the
// Electron <webview> API that VerificationPage and the Scholar scanner use,
// so those call sites only had to swap the tag name:
//
//   methods: executeJavaScript, loadURL, getURL, canGoBack, canGoForward,
//            goBack, goForward, reload, getZoomFactor, setZoomFactor,
//            findInPage, stopFindInPage
//   events:  did-start-loading, did-stop-loading, did-navigate,
//            did-navigate-in-page, dom-ready, did-fail-load, found-in-page,
//            zoom-changed, guest-shortcut (Ctrl+F / Esc / F5 / Ctrl±0 pressed
//            while the guest page has focus; replaces the old console.log
//            sentinel channel)
//   attributes: src, partition, allowpopups, background (keeps the view
//            alive off-screen at 1280×800, for the hidden Scholar scanner)
//
// Native child windows always paint above the HTML page. Elements marked
// `data-webview-occluder` (e.g. the find bar) are cut out of the native
// view's window region, so the HTML underneath shows through.
//
// In a plain browser (dev mode) there are no native views and the element
// falls back to an <iframe>. Most sites, including Google Scholar, refuse to
// be framed, so dev mode is only good for exercising the surrounding UI.

import { boot } from './boot'
import { nativeViews, onHostEvent } from './bridge'

interface NavState {
  url: string
  canGoBack: boolean
  canGoForward: boolean
  zoomFactor: number
}

interface HostWebviewEvent {
  id: string
  type: string
  state?: Partial<NavState>
  [key: string]: unknown
}

const registry = new Map<string, AtfiWebviewElement>()
let hostEventsHooked = false

function hookHostEvents(): void {
  if (hostEventsHooked) return
  hostEventsHooked = true
  onHostEvent('webview', (payload: HostWebviewEvent) => {
    registry.get(payload?.id)?.handleHostEvent(payload)
  })
}

interface Rect {
  x: number
  y: number
  width: number
  height: number
}

function round(r: DOMRect | Rect): Rect {
  return { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) }
}

function intersect(a: Rect, b: Rect): Rect | null {
  const x = Math.max(a.x, b.x)
  const y = Math.max(a.y, b.y)
  const right = Math.min(a.x + a.width, b.x + b.width)
  const bottom = Math.min(a.y + a.height, b.y + b.height)
  if (right <= x || bottom <= y) return null
  return { x, y, width: right - x, height: bottom - y }
}

const BOUNDS_POLL_MS = 120

export class AtfiWebviewElement extends HTMLElement {
  private readonly viewId = (crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`).toString()
  private created: Promise<void> | null = null
  private destroyed = false
  private pendingSrc: string | null = null
  private state: NavState = { url: '', canGoBack: false, canGoForward: false, zoomFactor: 1 }
  private lastSent = ''
  private resizeObserver: ResizeObserver | null = null
  private pollTimer: number | null = null
  private trackingCleanup: (() => void) | null = null
  private findRequestId = 0
  private iframe: HTMLIFrameElement | null = null
  private readonly native = boot.mode === 'webview2'

  // ---- lifecycle ---------------------------------------------------------

  connectedCallback(): void {
    if (!this.style.display) this.style.display = 'flex'
    this.destroyed = false
    if (!this.native) {
      this.mountIframeFallback()
      return
    }
    hookHostEvents()
    registry.set(this.viewId, this)
    const src = this.pendingSrc ?? this.getAttribute('src') ?? 'about:blank'
    this.pendingSrc = null
    this.state.url = src
    this.created = nativeViews
      .create(this.viewId, {
        src,
        partition: this.getAttribute('partition') ?? '',
        allowPopups: this.hasAttribute('allowpopups') && this.getAttribute('allowpopups') !== 'false',
        background: this.hasAttribute('background'),
      })
      .catch((err) => {
        console.error('[atfi-webview] create failed:', err)
        throw err
      })
    this.startTracking()
  }

  disconnectedCallback(): void {
    this.stopTracking()
    if (!this.native) return
    registry.delete(this.viewId)
    this.destroyed = true
    const created = this.created
    this.created = null
    this.lastSent = ''
    void created?.finally(() => nativeViews.destroy(this.viewId)).catch(() => {})
  }

  // `src` mirrors Electron: assigning it navigates.
  get src(): string {
    return this.state.url || this.getAttribute('src') || ''
  }

  set src(value: string) {
    if (!value) return
    if (!this.isConnected || !this.created) {
      this.pendingSrc = value
      this.state.url = value
      return
    }
    void this.loadURL(value).catch(() => {})
  }

  // ---- Electron <webview> API ---------------------------------------------

  async executeJavaScript(code: string): Promise<any> {
    if (!this.native) throw new Error('executeJavaScript is unavailable without the desktop host')
    await this.ready()
    return nativeViews.call(this.viewId, 'executeJavaScript', code)
  }

  async loadURL(url: string): Promise<void> {
    this.state.url = url
    if (!this.native) {
      if (this.iframe) this.iframe.src = url
      return
    }
    await this.ready()
    await nativeViews.call(this.viewId, 'loadURL', url)
  }

  getURL(): string {
    return this.state.url
  }

  canGoBack(): boolean {
    return this.state.canGoBack
  }

  canGoForward(): boolean {
    return this.state.canGoForward
  }

  goBack(): void {
    this.fire('goBack')
  }

  goForward(): void {
    this.fire('goForward')
  }

  reload(): void {
    if (!this.native) {
      if (this.iframe) this.iframe.src = this.iframe.src
      return
    }
    this.fire('reload')
  }

  getZoomFactor(): number {
    return this.state.zoomFactor
  }

  setZoomFactor(factor: number): void {
    this.state.zoomFactor = factor
    this.fire('setZoomFactor', factor)
  }

  findInPage(text: string, options: { forward?: boolean; findNext?: boolean; matchCase?: boolean } = {}): number {
    const requestId = ++this.findRequestId
    this.fire('findInPage', text, { forward: options.forward ?? true, findNext: options.findNext ?? false, matchCase: !!options.matchCase, requestId })
    return requestId
  }

  stopFindInPage(_action?: 'clearSelection' | 'keepSelection' | 'activateSelection'): void {
    this.fire('stopFindInPage')
  }

  // ---- host plumbing -------------------------------------------------------

  handleHostEvent(payload: HostWebviewEvent): void {
    if (payload.state) this.state = { ...this.state, ...payload.state }
    const { id: _id, type, state: _state, ...fields } = payload
    const event = new Event(type)
    Object.assign(event, fields)
    this.dispatchEvent(event)
  }

  private ready(): Promise<void> {
    if (!this.created) return Promise.reject(new Error('webview is not attached'))
    return this.created
  }

  private fire(method: string, ...args: unknown[]): void {
    if (!this.native || !this.created) return
    void this.created.then(() => nativeViews.call(this.viewId, method, ...args)).catch((err) => {
      console.warn(`[atfi-webview] ${method} failed:`, err)
    })
  }

  private startTracking(): void {
    if (this.hasAttribute('background')) return // host keeps it off-screen
    const update = () => this.sendBounds()
    this.resizeObserver = new ResizeObserver(update)
    this.resizeObserver.observe(this)
    window.addEventListener('resize', update)
    window.addEventListener('scroll', update, true)
    document.addEventListener('visibilitychange', update)
    // Position can change without a size change (layout shifts above us);
    // a cheap poll catches those. Only changes are sent to the host.
    this.pollTimer = window.setInterval(update, BOUNDS_POLL_MS)
    this.trackingCleanup = () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
      document.removeEventListener('visibilitychange', update)
    }
    update()
  }

  private stopTracking(): void {
    this.resizeObserver?.disconnect()
    this.resizeObserver = null
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer)
    this.pollTimer = null
    this.trackingCleanup?.()
    this.trackingCleanup = null
  }

  private sendBounds(): void {
    if (this.destroyed || !this.isConnected) return
    // display:none anywhere up the tree (inactive app tab) => no client rects.
    const visible = this.getClientRects().length > 0 && document.visibilityState === 'visible'
    const rect = visible ? round(this.getBoundingClientRect()) : null
    const holes: Rect[] = []
    if (rect) {
      document.querySelectorAll<HTMLElement>('[data-webview-occluder]').forEach((el) => {
        if (el.getClientRects().length === 0) return
        const hit = intersect(rect, round(el.getBoundingClientRect()))
        if (hit) holes.push({ x: hit.x - rect.x, y: hit.y - rect.y, width: hit.width, height: hit.height })
      })
    }
    const payload = {
      visible: !!rect && rect.width > 0 && rect.height > 0,
      rect,
      holes,
      dpr: window.devicePixelRatio || 1,
    }
    const key = JSON.stringify(payload)
    if (key === this.lastSent) return
    this.lastSent = key
    void this.created?.then(() => nativeViews.setBounds(this.viewId, payload)).catch(() => {})
  }

  private mountIframeFallback(): void {
    if (this.iframe) return
    const root = this.shadowRoot ?? this.attachShadow({ mode: 'open' })
    const frame = document.createElement('iframe')
    frame.style.cssText = 'border:0;width:100%;height:100%;flex:1;background:#fff'
    frame.src = this.pendingSrc ?? this.getAttribute('src') ?? 'about:blank'
    root.appendChild(frame)
    this.iframe = frame
  }
}

if (!customElements.get('atfi-webview')) {
  customElements.define('atfi-webview', AtfiWebviewElement)
}

declare global {
  interface HTMLElementTagNameMap {
    'atfi-webview': AtfiWebviewElement
  }
}

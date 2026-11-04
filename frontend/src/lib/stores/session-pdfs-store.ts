import { create } from 'zustand'

export interface SessionPdfEntry {
  path: string
  name: string
  importedAt: number
}

interface SessionPdfsState {
  lastSessionPdfs: SessionPdfEntry[]
  recordImport: (paths: string[]) => void
  removeFromLastSession: (path: string) => void
}

const CURRENT_KEY = 'atfimemnu.parsing.currentSession'

function nameFromPath(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return fallback
    const parsed = JSON.parse(raw) as T
    return parsed ?? fallback
  } catch {
    return fallback
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // localStorage may be unavailable (e.g. quota exceeded) — fail silently;
    // missing recent-list persistence is non-critical.
  }
}

// Promote on app start: whatever was in CURRENT_KEY is the previous run's
// import list — surface it as lastSessionPdfs, then clear CURRENT_KEY so the
// new run starts fresh. Promotion runs once per renderer load.
const previousRun = readJson<SessionPdfEntry[]>(CURRENT_KEY, [])
writeJson(CURRENT_KEY, [])

let currentSession: SessionPdfEntry[] = []

export const useSessionPdfsStore = create<SessionPdfsState>((set) => ({
  lastSessionPdfs: previousRun,

  recordImport: (paths) => {
    if (paths.length === 0) return
    const now = Date.now()
    const seen = new Set(currentSession.map(e => e.path))
    for (const path of paths) {
      if (seen.has(path)) continue
      currentSession.push({ path, name: nameFromPath(path), importedAt: now })
      seen.add(path)
    }
    writeJson(CURRENT_KEY, currentSession)
  },

  removeFromLastSession: (path) =>
    set(state => ({
      lastSessionPdfs: state.lastSessionPdfs.filter(e => e.path !== path),
    })),
}))

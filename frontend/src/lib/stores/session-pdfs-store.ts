import { create } from 'zustand'
import { boot } from '../host/boot'
import type { SessionPdfEntry } from '../host/boot'
import { hostAPI } from '../host/bridge'

export type { SessionPdfEntry }

interface SessionPdfsState {
  lastSessionPdfs: SessionPdfEntry[]
  recordImport: (paths: string[]) => void
  removeFromLastSession: (path: string) => void
}

function nameFromPath(filePath: string): string {
  return filePath.split(/[/\\]/).pop() ?? filePath
}

// Persistence moved from localStorage to the host: the UI origin now
// includes a per-launch port (http://127.0.0.1:<port>), and localStorage is
// per-origin. The host injects the previous run's list into
// window.__ATFI__.lastSessionPdfs and stores the current run's list.
const previousRun: SessionPdfEntry[] = boot.lastSessionPdfs ?? []

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
    hostAPI.saveSessionPdfs(currentSession)
  },

  removeFromLastSession: (path) =>
    set(state => ({
      lastSessionPdfs: state.lastSessionPdfs.filter(e => e.path !== path),
    })),
}))

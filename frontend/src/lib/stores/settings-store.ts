import { create } from 'zustand'
import type { AppSettings, DatabaseConfig } from '../api/types'
import { api } from '../api/rest-client'
import {
  SETTINGS_ERROR_FLASH_MS,
  SETTINGS_SAVED_FLASH_MS,
} from '../constants/timings'
import i18n from '../i18n'
import { parseExclusionList, type ExclusionEntry } from '../utils/exclusion-list'

export const DEFAULT_EXCLUSION_LIST_URL =
  'https://docs.google.com/spreadsheets/d/e/2PACX-1vS-DWUY4jAnn2b-DSVeLw7-kVT3pE77ZP-lnP47yt73WojeJJuUTmUPDoxLQQ3lIc0kvdUV8c87fWAY/pub?gid=355872624&single=true&output=tsv'
export const DEFAULT_EXCLUSION_EDIT_URL =
  'https://docs.google.com/spreadsheets/d/1-d9yoAdZJAD64XZOIPy2vMc314s9jsUGVwZV5uqnx1E/edit?usp=sharing'

// Must mirror backend `AppSettings.default().databases` in
// `backend/models/settings.py`. The backend is authoritative — these are
// only the seed values shown until the initial /settings GET resolves.
export const defaultDatabases: DatabaseConfig[] = [
  { id: 'crossref', name: 'Crossref', enabled: true },
  { id: 'openalex', name: 'OpenAlex', enabled: true },
  { id: 'openaire', name: 'OpenAIRE', enabled: true },
  { id: 'europe_pmc', name: 'Europe PMC', enabled: true },
  { id: 'arxiv', name: 'arXiv', enabled: true },
  { id: 'pubmed', name: 'PubMed', enabled: true },
  { id: 'semantic_scholar', name: 'Semantic Scholar', enabled: true },
  { id: 'trdizin', name: 'TRDizin', enabled: true },
  { id: 'open_library', name: 'Open Library', enabled: true },
  { id: 'base', name: 'BASE', enabled: false },
  { id: 'wos', name: 'Web of Science', enabled: false },
]

export const defaultDatabaseIds: ReadonlySet<string> = new Set(
  defaultDatabases.map(db => db.id),
)

interface SettingsState {
  settings: AppSettings
  // True once loadSettings has finished (success or failure). Mutators
  // gate the autosave on this flag so user edits made before the initial
  // load resolves don't get persisted as partial-defaults; the load's
  // set() will overwrite with server values, and from then on edits
  // autosave normally.
  settingsLoaded: boolean
  saveStatus: 'idle' | 'saving' | 'saved' | 'error'
  // Parsed contents of the exclusion list (renderer-only — not persisted
  // to backend). Fetched from the configured URL on startup and on
  // explicit update from the settings page. Falls back to local cache
  // when the URL is unreachable.
  exclusionEntries: ExclusionEntry[]
  exclusionLoadError: string | null
  exclusionFromCache: boolean
  exclusionLastUpdated: string | null
  loadSettings: () => Promise<void>
  updateSetting: <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => void
  toggleDatabase: (dbId: string) => void
  addDatabase: (db: DatabaseConfig) => void
  removeDatabase: (dbId: string) => void
  reorderDatabases: (fromIdx: number, toIdx: number) => void
  updateApiKey: (key: string, value: string) => void
  connectOpenaire: (refreshToken: string) => Promise<{ ok: boolean; error?: string }>
  disconnectOpenaire: () => Promise<void>
  reloadExclusionList: () => Promise<void>
  resetExclusionDefaults: () => void
}

// Auto-save debounce. Two durations: a short one for cheap, idempotent
// settings (database toggles, dropdowns) where the user wants quick
// feedback, and a longer one for api_keys / secrets where each keystroke
// would otherwise persist a half-typed token to disk + trigger an OpenAIRE
// cache invalidation. Timer is single-shot — the most recent caller's
// requested delay wins, which is the behaviour we want (a secret edit
// resets the cooldown to the longer interval).
const SETTINGS_AUTOSAVE_DEBOUNCE_MS = 500
const SECRETS_AUTOSAVE_DEBOUNCE_MS = 1500

let _saveTimer: ReturnType<typeof setTimeout> | null = null
let _savedTimer: ReturnType<typeof setTimeout> | null = null
// Fields that have changed since the last successful save. The debounced
// save reads the current value of each pending field from the store and
// PATCHes only those — never a full settings dump. This is the safety
// invariant: the backend only ever overwrites fields the renderer
// deliberately touched.
const _pendingFields = new Set<keyof AppSettings>()
function _debouncedSave(
  get: () => SettingsState,
  field: keyof AppSettings,
  delayMs: number = SETTINGS_AUTOSAVE_DEBOUNCE_MS,
) {
  // Skip autosave entirely until the initial load has resolved. Without
  // this, a user edit made during the load's in-flight window would race
  // with the load's set({ settings: s }) and the server's response could
  // either overwrite the user's edit (if it lands second) or persist a
  // mostly-default settings object (if the save fires first).
  if (!get().settingsLoaded) return

  _pendingFields.add(field)

  if (_saveTimer) clearTimeout(_saveTimer)
  if (_savedTimer) clearTimeout(_savedTimer)
  useSettingsStore.setState({ saveStatus: 'saving' })
  _saveTimer = setTimeout(async () => {
    if (_pendingFields.size === 0) return
    const fields = Array.from(_pendingFields)
    _pendingFields.clear()
    const settings = get().settings
    const patch: Partial<AppSettings> = {}
    for (const f of fields) {
      ;(patch as Record<string, unknown>)[f] = settings[f]
    }
    try {
      const s = await api.updateSettings(patch)
      useSettingsStore.setState({ settings: s, saveStatus: 'saved' })
      _savedTimer = setTimeout(() => useSettingsStore.setState({ saveStatus: 'idle' }), SETTINGS_SAVED_FLASH_MS)
    } catch (e) {
      console.error('Failed to auto-save settings:', e)
      // Re-queue the fields so the next edit (or retry) tries again.
      for (const f of fields) _pendingFields.add(f)
      useSettingsStore.setState({ saveStatus: 'error' })
      _savedTimer = setTimeout(() => useSettingsStore.setState({ saveStatus: 'idle' }), SETTINGS_ERROR_FLASH_MS)
    }
  }, delayMs)
}

// Runs a granular database operation: surfaces save status flashes and
// syncs the renderer state to the backend's authoritative response. The
// backend applies each operation to its own current list, so a stale
// renderer can't replace the on-disk order with the seed.
async function _runDatabaseOp(call: () => Promise<AppSettings>) {
  if (_savedTimer) clearTimeout(_savedTimer)
  useSettingsStore.setState({ saveStatus: 'saving' })
  try {
    const s = await call()
    useSettingsStore.setState({ settings: s, saveStatus: 'saved' })
    _savedTimer = setTimeout(
      () => useSettingsStore.setState({ saveStatus: 'idle' }),
      SETTINGS_SAVED_FLASH_MS,
    )
  } catch (e) {
    console.error('Failed database settings op:', e)
    useSettingsStore.setState({ saveStatus: 'error' })
    _savedTimer = setTimeout(
      () => useSettingsStore.setState({ saveStatus: 'idle' }),
      SETTINGS_ERROR_FLASH_MS,
    )
  }
}

export const useSettingsStore = create<SettingsState>()((set, get) => ({
  saveStatus: 'idle' as const,
  settingsLoaded: false,
  exclusionEntries: [],
  exclusionLoadError: null,
  exclusionFromCache: false,
  exclusionLastUpdated: null,
  settings: {
    exported_pdf_dir: '',
    exclusion_list_url: DEFAULT_EXCLUSION_LIST_URL,
    exclusion_edit_url: DEFAULT_EXCLUSION_EDIT_URL,
    databases: defaultDatabases,
    api_keys: {},
    polite_pool_email: '',
    search_timeout: 30,
    max_concurrent_apis: 5,
    max_concurrent_sources_per_pdf: 3,
    strong_match_enabled: false,
    language: 'tr',
    auto_callout_text_fabricated: 'Literatürde bulunmamaktadır.',
    auto_callout_text_citation:
      'Künye bilgilerinde eksik/hatalı bilgiler bulunmaktadır.',
  },

  loadSettings: async () => {
    // Idempotent: subsequent calls after the first successful load are
    // no-ops. Stops a remount or duplicate caller from clobbering live
    // user edits with a stale server snapshot.
    if (get().settingsLoaded) return
    try {
      const s = await api.getSettings()
      set({ settings: s, settingsLoaded: true })
      if (s.language && i18n.language !== s.language) {
        i18n.changeLanguage(s.language)
      }
      // Fetch the exclusion list from the configured URL (or default).
      // Any failure is non-fatal — the renderer just treats the list as
      // empty and surfaces the error in the settings page.
      void get().reloadExclusionList()
    } catch {
      // Don't flip settingsLoaded on failure. While the renderer doesn't
      // know the on-disk state we MUST NOT autosave — a save with the
      // seed defaults would overwrite the file. App.tsx re-invokes this
      // whenever the WebSocket (re)connects, so a backend cold-start or
      // mid-session hiccup eventually resolves into a successful load.
    }
  },

  updateSetting: (key, value) => {
    set(state => ({ settings: { ...state.settings, [key]: value } }))
    if (key === 'language' && typeof value === 'string') {
      i18n.changeLanguage(value)
    }
    _debouncedSave(get, key)
    // Re-fetch the exclusion list whenever its URL changes so word/reason
    // matches stay in sync without needing an explicit Update click.
    if (key === 'exclusion_list_url') {
      void get().reloadExclusionList()
    }
  },

  reloadExclusionList: async () => {
    const url = get().settings.exclusion_list_url
    if (!url) {
      set({ exclusionEntries: [], exclusionLoadError: null, exclusionFromCache: false })
      return
    }
    try {
      const result = await window.electronAPI.fetchExclusionList(url)
      if (result.text !== null) {
        set({
          exclusionEntries: parseExclusionList(result.text),
          exclusionLoadError: result.error || null,
          exclusionFromCache: result.fromCache,
          exclusionLastUpdated: result.fromCache
            ? get().exclusionLastUpdated
            : new Date().toISOString(),
        })
      } else {
        set({
          exclusionEntries: [],
          exclusionLoadError: result.error || 'fetch_failed',
          exclusionFromCache: false,
        })
      }
    } catch (err) {
      console.error('Failed to fetch exclusion list:', err)
      set({ exclusionEntries: [], exclusionLoadError: 'fetch_failed', exclusionFromCache: false })
    }
  },

  toggleDatabase: (dbId: string) => {
    if (!get().settingsLoaded) return
    const current = get().settings.databases.find(d => d.id === dbId)
    if (!current) return
    const newEnabled = !current.enabled
    set(state => ({
      settings: {
        ...state.settings,
        databases: state.settings.databases.map(db =>
          db.id === dbId ? { ...db, enabled: newEnabled } : db
        ),
      },
    }))
    _runDatabaseOp(() => api.setDatabaseEnabled(dbId, newEnabled))
  },

  addDatabase: (db: DatabaseConfig) => {
    if (!get().settingsLoaded) return
    set(state => ({
      settings: {
        ...state.settings,
        databases: [...state.settings.databases, db],
      },
    }))
    _runDatabaseOp(() => api.addDatabase(db))
  },

  removeDatabase: (dbId: string) => {
    if (!get().settingsLoaded) return
    set(state => ({
      settings: {
        ...state.settings,
        databases: state.settings.databases.filter(db => db.id !== dbId),
      },
    }))
    _runDatabaseOp(() => api.removeDatabase(dbId))
  },

  reorderDatabases: (fromIdx: number, toIdx: number) => {
    if (!get().settingsLoaded) return
    let movedId: string | null = null
    let afterId: string | null = null
    set(state => {
      const dbs = [...state.settings.databases]
      if (
        fromIdx < 0 ||
        toIdx < 0 ||
        fromIdx >= dbs.length ||
        toIdx >= dbs.length ||
        fromIdx === toIdx
      ) {
        return state
      }
      const [moved] = dbs.splice(fromIdx, 1)
      dbs.splice(toIdx, 0, moved)
      movedId = moved.id
      afterId = toIdx > 0 ? dbs[toIdx - 1].id : null
      return { settings: { ...state.settings, databases: dbs } }
    })
    if (!movedId) return
    const id = movedId
    const after = afterId
    _runDatabaseOp(() => api.reorderDatabase(id, after))
  },

  updateApiKey: (key: string, value: string) => {
    set(state => ({
      settings: {
        ...state.settings,
        api_keys: { ...(state.settings.api_keys || {}), [key]: value },
      },
    }))
    // Longer debounce on secrets so a typing burst doesn't persist a
    // dozen partial-key states to disk (and, for OpenAIRE, doesn't keep
    // re-invalidating the access-token cache between keystrokes).
    _debouncedSave(get, 'api_keys', SECRETS_AUTOSAVE_DEBOUNCE_MS)
  },

  connectOpenaire: async (refreshToken: string) => {
    // The OpenAIRE exchange is authoritative: we only persist a token that
    // already traded for an access token, so a successful return means the
    // user is truly connected and the backend has the rotated value.
    try {
      const res = await api.validateOpenaireToken(refreshToken)
      if (res.valid && res.settings) {
        set({ settings: res.settings })
        return { ok: true }
      }
      return { ok: false, error: res.error ?? 'Validation failed.' }
    } catch (err) {
      return {
        ok: false,
        error: err instanceof Error ? err.message : 'Network error.',
      }
    }
  },

  disconnectOpenaire: async () => {
    try {
      const updated = await api.disconnectOpenaire()
      set({ settings: updated })
    } catch (err) {
      console.error('Failed to disconnect OpenAIRE:', err)
    }
  },

  resetExclusionDefaults: () => {
    set(state => ({
      settings: {
        ...state.settings,
        exclusion_list_url: DEFAULT_EXCLUSION_LIST_URL,
        exclusion_edit_url: DEFAULT_EXCLUSION_EDIT_URL,
      },
    }))
    _debouncedSave(get, 'exclusion_list_url')
    _debouncedSave(get, 'exclusion_edit_url')
    void get().reloadExclusionList()
  },
}))

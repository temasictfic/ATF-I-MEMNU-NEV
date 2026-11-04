// Centralized timing constants. Renderer-side only (Electron main has its
// own timings). Update here, not at the call site, when tuning.

// Verification poll interval (verify_status REST poll, while a job is running).
export const POLL_INTERVAL_MS = 5000

// Toast that fades out after a verification action.
export const VERIFY_TOAST_DURATION_MS = 3000

// Settings auto-save: how long the "saved" indicator lingers, and how long
// the "save failed" indicator lingers before clearing.
export const SETTINGS_SAVED_FLASH_MS = 2000
export const SETTINGS_ERROR_FLASH_MS = 3000

// Browser webview zoom (Verification page).
export const MIN_BROWSER_ZOOM = 0.5
export const MAX_BROWSER_ZOOM = 3
export const BROWSER_ZOOM_STEP = 1.1

// Debounce on the parsing-page field-extraction request. Selecting a source
// fires off a backend extract_fields call; the debounce keeps rapid
// arrow-key cycling through cards from spawning a request per card.
export const PARSE_FIELD_DEBOUNCE_MS = 300

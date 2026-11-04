let backendPort: number | null = null
let backendPortPromise: Promise<number> | null = null

const BACKEND_PORT_DISCOVERY_MAX_ATTEMPTS = 120
const BACKEND_PORT_DISCOVERY_DELAY_MS = 500

function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function discoverBackendPortWithRetry(): Promise<number> {
  let lastError: unknown = null

  for (let attempt = 1; attempt <= BACKEND_PORT_DISCOVERY_MAX_ATTEMPTS; attempt++) {
    try {
      if (!window.electronAPI?.getBackendPort) {
        throw new Error('Backend port is not discoverable in this renderer context')
      }

      return await window.electronAPI.getBackendPort()
    } catch (error) {
      lastError = error
      if (attempt < BACKEND_PORT_DISCOVERY_MAX_ATTEMPTS) {
        await sleep(BACKEND_PORT_DISCOVERY_DELAY_MS)
      }
    }
  }

  const message = lastError instanceof Error ? lastError.message : String(lastError ?? 'Unknown error')
  throw new Error(`Backend port discovery timed out: ${message}`)
}

export async function getBackendPort(): Promise<number> {
  if (backendPort !== null) {
    return backendPort
  }

  if (!backendPortPromise) {
    backendPortPromise = discoverBackendPortWithRetry()
      .then(discoveredPort => {
        backendPort = discoveredPort
        return discoveredPort
      })
      .catch((error) => {
        // Allow future calls to retry discovery if startup timing changes.
        backendPortPromise = null
        throw error
      })
  }

  return backendPortPromise
}

export function getBackendPortSync(): number | null {
  return backendPort
}

export async function getBackendBaseUrl(): Promise<string> {
  const port = await getBackendPort()
  return `http://localhost:${port}`
}

export function getBackendBaseUrlSync(): string | null {
  const port = getBackendPortSync()
  if (port === null) return null
  return `http://localhost:${port}`
}

export async function getBackendWsUrl(): Promise<string> {
  const port = await getBackendPort()
  return `ws://localhost:${port}/api/ws`
}

export async function initializeBackendEndpoint(): Promise<void> {
  await getBackendPort()
}

import { backendOrigin, boot } from '../host/boot'

// The desktop host binds the backend socket before the window is created
// and injects the port and token into index.html, so the endpoint is known
// synchronously. Electron exposed the port before uvicorn was listening and
// the renderer polled `backend:getPort` up to 120 × 500 ms.

export function getBackendBaseUrl(): string {
  return backendOrigin
}

export function getBackendWsUrl(): string {
  const wsOrigin = backendOrigin.replace(/^http/, 'ws')
  const query = boot.token ? `?token=${encodeURIComponent(boot.token)}` : ''
  return `${wsOrigin}/api/ws${query}`
}

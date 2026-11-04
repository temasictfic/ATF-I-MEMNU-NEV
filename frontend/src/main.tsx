import { createRoot } from 'react-dom/client'
import App from './App'
import './app.css'
import './lib/i18n'
// pdfjs-dist's text layer and annotation layer CSS — required for the
// transparent selectable text overlay to position glyph spans correctly.
import 'pdfjs-dist/web/pdf_viewer.css'
import { initializeBackendEndpoint } from './lib/api/backend-endpoint'

createRoot(document.getElementById('app')!).render(<App />)

void initializeBackendEndpoint().catch((error) => {
  console.error('[Startup] Backend initialization failed:', error)
})

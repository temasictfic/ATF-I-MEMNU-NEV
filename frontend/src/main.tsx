import { createRoot } from 'react-dom/client'
import App from './App'
import './app.css'
import './lib/i18n'
// pdfjs-dist's text layer and annotation layer CSS — required for the
// transparent selectable text overlay to position glyph spans correctly.
import 'pdfjs-dist/web/pdf_viewer.css'
// Registers <atfi-webview> (native WebView2 views managed by the host).
import './lib/host/webview-element'

createRoot(document.getElementById('app')!).render(<App />)

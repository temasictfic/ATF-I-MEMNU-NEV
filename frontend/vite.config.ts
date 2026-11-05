import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The built UI is served by the in-process backend at http://127.0.0.1:<port>/
// and loaded into the WebView2 main view by the Python host (atfi_app).
// `base: './'` keeps every asset URL relative so the same dist works under
// any port.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: './',
  worker: {
    format: 'es',
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    // Desktop app served from localhost: big chunks (pdf.js, fontkit) cost
    // nothing over the network, so don't warn about them.
    chunkSizeWarningLimit: 1500,
  },
  server: {
    // `npm run dev`: the dev UI talks to a backend started with
    // `python -m atfi_app --dev-browser --port 8765` (see README).
    port: 5173,
    strictPort: true,
  },
})

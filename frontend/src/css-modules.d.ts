declare module '*.module.css' {
  const classes: { readonly [key: string]: string }
  export default classes
}

// Plain CSS files — imported for side effects only (Vite handles them).
declare module '*.css'
declare module 'pdfjs-dist/web/pdf_viewer.css'

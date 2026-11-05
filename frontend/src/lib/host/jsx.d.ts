import type React from 'react'

// JSX typing for the <atfi-webview> custom element (replaces Electron's
// <webview> typing from webview.d.ts).
declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'atfi-webview': React.DetailedHTMLProps<React.HTMLAttributes<HTMLElement>, HTMLElement> & {
        src?: string
        partition?: string
        allowpopups?: string
        background?: string
      }
    }
  }
}

export {}

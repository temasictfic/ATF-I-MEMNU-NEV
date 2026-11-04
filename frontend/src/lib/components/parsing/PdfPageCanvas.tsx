// Renders a single PDF page using pdfjs-dist — canvas underneath, transparent
// selectable text layer on top. The page is rendered at the PDF_DPI-matching
// `SCALE` factor so the canvas coordinate space matches the pixel coordinates
// stored in SourceRectangle bboxes. The user `zoom` factor is applied on top
// via CSS `transform: scale()` to keep the underlying canvas crisp.

import React, { useEffect, useRef } from 'react'
import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist/types/src/display/api'
import { getPdfjs } from '../../pdf/pdfjs-setup'
import { SCALE } from '../../pdf/types'

interface Props {
  doc: PDFDocumentProxy
  pageNum: number // 1-indexed, matching pdfjs-dist's getPage
  zoom: number // user zoom factor, applied as CSS transform
}

export const PdfPageCanvas = React.memo(function PdfPageCanvas({ doc, pageNum, zoom }: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const textLayerRef = useRef<HTMLDivElement | null>(null)
  const wrapperRef = useRef<HTMLDivElement | null>(null)
  const cleanupListenersRef = useRef<(() => void) | null>(null)

  useEffect(() => {
    let cancelled = false
    let pageProxy: PDFPageProxy | null = null
    let renderTask: { cancel: () => void; promise: Promise<void> } | null = null
    let textLayer: { cancel: () => void } | null = null

    ;(async () => {
      const pdfjsLib = getPdfjs()
      pageProxy = await doc.getPage(pageNum)
      if (cancelled || !pageProxy) return

      const viewport = pageProxy.getViewport({ scale: SCALE })

      const canvas = canvasRef.current
      const textContainer = textLayerRef.current
      const wrapper = wrapperRef.current
      if (!canvas || !textContainer || !wrapper) return

      // Size the canvas to the viewport's natural pixel resolution; the user
      // zoom is applied via CSS so glyphs stay crisp.
      canvas.width = Math.floor(viewport.width)
      canvas.height = Math.floor(viewport.height)
      canvas.style.width = `${viewport.width}px`
      canvas.style.height = `${viewport.height}px`

      // pdfjs-dist's text layer CSS uses `calc(var(--total-scale-factor) * N)`
      // for both the container's width/height AND each span's font-size.
      // The derivation rule `.pdfViewer .page { --total-scale-factor: ... }`
      // is scoped to class names we don't use, so we set the variable
      // directly. Without this, span font sizes fall back to browser defaults,
      // individual glyphs end up mis-sized, and text selection skips letters
      // at the right end of lines.
      textContainer.style.setProperty('--scale-factor', String(viewport.scale))
      textContainer.style.setProperty('--total-scale-factor', String(viewport.scale))
      pdfjsLib.setLayerDimensions(textContainer, viewport)
      textContainer.replaceChildren()

      wrapper.style.width = `${viewport.width}px`
      wrapper.style.height = `${viewport.height}px`

      const ctx = canvas.getContext('2d')
      if (!ctx) return

      renderTask = pageProxy.render({ canvasContext: ctx, viewport, canvas }) as unknown as {
        cancel: () => void
        promise: Promise<void>
      }
      try {
        await renderTask.promise
      } catch (err) {
        if ((err as { name?: string }).name === 'RenderingCancelledException') return
        throw err
      }
      if (cancelled) return

      const textContentSource = pageProxy.streamTextContent
        ? pageProxy.streamTextContent()
        : await pageProxy.getTextContent()
      const tl = new pdfjsLib.TextLayer({
        textContentSource: textContentSource as never,
        container: textContainer,
        viewport,
      })
      textLayer = tl
      try {
        await tl.render()
      } catch {
        // text layer rendering can race with unmounts — ignore.
      }
      if (cancelled) return

      // endOfContent boundary sentinel: pdfjs-dist's text-layer CSS clamps
      // selection to the text area via a `.endOfContent` div that has
      // `user-select: none` and, when the container has `.selecting`, expands
      // to cover the whole layer. The viewer's TextLayerBuilder wires this
      // up; the low-level TextLayer class we use does not. Replicate the
      // minimum: append the div + toggle `.selecting` on pointerdown/pointerup
      // so dragging past the end of a line doesn't select the whole page.
      const endOfContent = document.createElement('div')
      endOfContent.className = 'endOfContent'
      textContainer.append(endOfContent)

      const onTextLayerDown = () => {
        textContainer.classList.add('selecting')
      }
      textContainer.addEventListener('mousedown', onTextLayerDown)
      const onDocPointerUp = () => {
        textContainer.classList.remove('selecting')
      }
      document.addEventListener('pointerup', onDocPointerUp)
      document.addEventListener('pointercancel', onDocPointerUp)

      cleanupListenersRef.current = () => {
        textContainer.removeEventListener('mousedown', onTextLayerDown)
        document.removeEventListener('pointerup', onDocPointerUp)
        document.removeEventListener('pointercancel', onDocPointerUp)
      }
    })().catch((err: unknown) => {
      if (!cancelled) console.error('[PdfPageCanvas] render failed', err)
    })

    return () => {
      cancelled = true
      try {
        renderTask?.cancel()
      } catch {
        // ignore
      }
      try {
        textLayer?.cancel()
      } catch {
        // ignore
      }
      cleanupListenersRef.current?.()
      cleanupListenersRef.current = null
      pageProxy?.cleanup()
    }
  }, [doc, pageNum])

  return (
    <div
      ref={wrapperRef}
      className="pdf-page-wrapper"
      style={{
        position: 'relative',
        transform: `scale(${zoom})`,
        transformOrigin: 'top left',
      }}
    >
      <canvas ref={canvasRef} style={{ display: 'block' }} />
      {/*
        .textLayer class pulls in pdfjs-dist's shipped rules (imported in
        main.tsx) that set child spans to `color: transparent`, `position:
        absolute`, `white-space: pre`, etc. Do NOT set opacity here — that
        would make the spans visible as ghost text over the canvas.
      */}
      <div
        ref={textLayerRef}
        className="textLayer"
        style={{ position: 'absolute', inset: 0, overflow: 'hidden' }}
      />
    </div>
  )
})

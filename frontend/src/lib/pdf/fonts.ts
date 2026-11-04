// Shared Liberation Sans font loading for PDF export and canvas text
// measurement. The URLs still come from Vite's asset pipeline, but callers
// consume cached bytes so browser FontFace registration does not trigger
// slow-network web-font fallback messages for the dev-server `@fs` URL.

// @ts-expect-error Vite ?url import returns a string
import regularFontUrl from 'pdfjs-dist/standard_fonts/LiberationSans-Regular.ttf?url'
// @ts-expect-error Vite ?url import returns a string
import boldFontUrl from 'pdfjs-dist/standard_fonts/LiberationSans-Bold.ttf?url'

let regularFontBytesPromise: Promise<Uint8Array> | null = null
let boldFontBytesPromise: Promise<Uint8Array> | null = null

async function loadFontBytes(url: string): Promise<Uint8Array> {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`Failed to load font from ${url}: ${res.status}`)
  return new Uint8Array(await res.arrayBuffer())
}

function getRegularFontBytes(): Promise<Uint8Array> {
  regularFontBytesPromise ??= loadFontBytes(regularFontUrl as string)
  return regularFontBytesPromise
}

function getBoldFontBytes(): Promise<Uint8Array> {
  boldFontBytesPromise ??= loadFontBytes(boldFontUrl as string)
  return boldFontBytesPromise
}

function copyToArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength)
  copy.set(bytes)
  return copy.buffer
}

export async function getLiberationFontBytes(): Promise<{
  regular: Uint8Array
  bold: Uint8Array
}> {
  const [regular, bold] = await Promise.all([
    getRegularFontBytes(),
    getBoldFontBytes(),
  ])
  return { regular, bold }
}

export async function getLiberationFontFaceSources(): Promise<{
  regular: ArrayBuffer
  bold: ArrayBuffer
}> {
  const { regular, bold } = await getLiberationFontBytes()
  return {
    regular: copyToArrayBuffer(regular),
    bold: copyToArrayBuffer(bold),
  }
}

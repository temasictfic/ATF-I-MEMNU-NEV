import { sanitizeSourceText } from './source-text'

// FNV-1a 64-bit. Synchronous, no deps, ~10 lines. 48-bit truncation
// gives ~10⁻⁹ birthday-collision probability at 1000 refs per PDF —
// collisions inside a single PDF's source list are then handled
// by the deterministic `_2` / `_3` disambiguator in renumberSources.
function fnv1a64Hex(s: string): string {
  const bytes = new TextEncoder().encode(s)
  let hash = 0xcbf29ce484222325n
  const prime = 0x100000001b3n
  const mask = 0xffffffffffffffffn
  for (let i = 0; i < bytes.length; i++) {
    hash ^= BigInt(bytes[i])
    hash = (hash * prime) & mask
  }
  return hash.toString(16).padStart(16, '0')
}

export function makeSourceId(pdfId: string, text: string): string {
  const canonical = sanitizeSourceText(text)
  return `${pdfId}_${fnv1a64Hex(canonical).slice(0, 12)}`
}

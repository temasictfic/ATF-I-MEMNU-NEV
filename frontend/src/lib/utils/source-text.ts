const REF_NUMBER_PREFIX_PATTERNS: RegExp[] = [
  /^\s*\[(\d{1,3})\]\s*/,  // [1] Text...
  /^\s*(\d{1,3})\.\s+/,    // 1. Text...
  /^\s*(\d{1,3})\)\s+/,    // 1) Text...
  /^\s*(\d{1,3})-\s*/,      // 1- Text...
]

const ACCESS_DATE_PATTERNS: RegExp[] = [
  /[,;]?\s*son\s+eri[şs]im\s+tarihi\s*:?\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{4}\.?/gi,
  /[,;]?\s*eri[şs]im\s+tarihi\s*:?\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\.?/gi,
]

export function sanitizeSourceText(text: string): string {
  let cleaned = text ?? ''

  for (const pattern of REF_NUMBER_PREFIX_PATTERNS) {
    const next = cleaned.replace(pattern, '')
    if (next !== cleaned) {
      cleaned = next
      break
    }
  }

  for (const pattern of ACCESS_DATE_PATTERNS) {
    cleaned = cleaned.replace(pattern, ' ')
  }

  return cleaned.replace(/\s+/g, ' ').trim()
}

export function sanitizeSourceTextForSearch(text: string): string {
  let cleaned = sanitizeSourceText(text)
  cleaned = cleaned.replace(/https?:\/\/\S+/gi, ' ')
  cleaned = cleaned.replace(/doi[:\s]*10\.\S+/gi, ' ')
  cleaned = cleaned.replace(/10\.\d{4,9}\/\S+/gi, ' ')
  return cleaned.replace(/\s+/g, ' ').trim()
}

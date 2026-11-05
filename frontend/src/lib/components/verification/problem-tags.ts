import i18n from '../../i18n'

export function problemTagDescription(tag: string): string {
  switch (tag) {
    case '!authors': return i18n.t('verification.problemDesc.authors')
    case '!doi/arXiv': return i18n.t('verification.problemDesc.doi')
    case '!year': return i18n.t('verification.problemDesc.year')
    case '!journal': return i18n.t('verification.problemDesc.journal')
    case '!title': return i18n.t('verification.problemDesc.title')
    default: return tag
  }
}

export function problemTagLabel(tag: string): string {
  const key = `verification.problemTag.${tag}`
  const translated = i18n.t(key)
  return translated === key ? tag : translated
}

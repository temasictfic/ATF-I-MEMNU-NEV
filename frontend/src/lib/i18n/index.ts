import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import tr from './locales/tr.json'
import en from './locales/en.json'
import { boot } from '../host/boot'

void i18n.use(initReactI18next).init({
  resources: {
    tr: { translation: tr },
    en: { translation: en },
  },
  // The host injects the saved language so the first paint is already in
  // it (Electron always started in Turkish until GET /settings returned).
  lng: boot.language ?? 'tr',
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
  returnNull: false,
})

export default i18n

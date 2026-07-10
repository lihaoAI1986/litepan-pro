const THEME_STORAGE_KEY = 'litepan_theme'
const VALID_THEMES = new Set(['light', 'dark', 'auto'])

let mediaQuery = null
let mediaQueryHandler = null
let currentThemePreference = 'light'

const normalizeTheme = (theme) => {
  return VALID_THEMES.has(theme) ? theme : 'light'
}

const getSystemTheme = () => {
  if (typeof window === 'undefined' || !window.matchMedia) {
    return 'light'
  }
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

const resolveTheme = (theme) => {
  const normalized = normalizeTheme(theme)
  return normalized === 'auto' ? getSystemTheme() : normalized
}

const setStoredTheme = (theme) => {
  try {
    window.localStorage?.setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // localStorage 不可用时只在当前页面生效。
  }
}

const getStoredTheme = () => {
  try {
    return window.localStorage?.getItem(THEME_STORAGE_KEY) || 'light'
  } catch {
    return 'light'
  }
}

const bindSystemThemeListener = () => {
  if (typeof window === 'undefined' || !window.matchMedia) {
    return
  }
  mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  if (mediaQueryHandler) {
    return
  }
  mediaQueryHandler = () => {
    if (currentThemePreference === 'auto') {
      applyTheme('auto', { persist: false })
    }
  }
  mediaQuery.addEventListener?.('change', mediaQueryHandler)
}

export const applyTheme = (theme, options = {}) => {
  const { persist = true } = options
  const normalized = normalizeTheme(theme)
  const resolved = resolveTheme(normalized)

  currentThemePreference = normalized
  document.documentElement.dataset.themePreference = normalized
  document.documentElement.dataset.theme = resolved
  document.documentElement.style.colorScheme = resolved

  if (persist) {
    setStoredTheme(normalized)
  }
  bindSystemThemeListener()
  return resolved
}

export const initTheme = () => {
  applyTheme(normalizeTheme(getStoredTheme()), { persist: false })
}

export const isValidTheme = (theme) => VALID_THEMES.has(theme)

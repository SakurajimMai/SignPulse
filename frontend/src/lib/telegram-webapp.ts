export interface TelegramWebAppBackButton {
  show: () => void
  hide: () => void
  onClick: (handler: () => void) => void
  offClick: (handler: () => void) => void
}

export interface TelegramWebAppHapticFeedback {
  notificationOccurred?: (type: 'error' | 'success' | 'warning') => void
  impactOccurred?: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => void
}

export interface TelegramWebApp {
  initData: string
  colorScheme?: 'light' | 'dark'
  viewportStableHeight?: number
  ready: () => void
  expand: () => void
  BackButton?: TelegramWebAppBackButton
  HapticFeedback?: TelegramWebAppHapticFeedback
  onEvent?: (event: 'themeChanged' | 'viewportChanged', handler: () => void) => void
  offEvent?: (event: 'themeChanged' | 'viewportChanged', handler: () => void) => void
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

const SDK_ID = 'telegram-web-app-sdk'
const SDK_SRC = 'https://telegram.org/js/telegram-web-app.js'
const SDK_LOAD_TIMEOUT_MS = 10_000
let sdkPromise: Promise<TelegramWebApp | null> | null = null

export const getTelegramWebApp = (): TelegramWebApp | null =>
  window.Telegram?.WebApp || null

export function loadTelegramWebApp(): Promise<TelegramWebApp | null> {
  const existing = getTelegramWebApp()
  if (existing) return Promise.resolve(existing)
  if (sdkPromise) return sdkPromise

  const loading = new Promise<TelegramWebApp | null>((resolve) => {
    const oldScript = document.getElementById(SDK_ID) as HTMLScriptElement | null
    // A previous failed/timed-out attempt leaves a script element whose load
    // event will never fire again. Replace it so the retry can actually load.
    oldScript?.remove()

    const script = document.createElement('script')
    script.id = SDK_ID
    script.src = SDK_SRC
    script.async = true
    let settled = false
    let timeout: ReturnType<typeof setTimeout> | undefined
    const finish = (value: TelegramWebApp | null) => {
      if (settled) return
      settled = true
      if (timeout) clearTimeout(timeout)
      script.removeEventListener('load', onLoad)
      script.removeEventListener('error', onError)
      if (!value) script.remove()
      resolve(value)
    }
    const onLoad = () => finish(getTelegramWebApp())
    const onError = () => finish(null)
    script.addEventListener('load', onLoad, { once: true })
    script.addEventListener('error', onError, { once: true })
    document.head.appendChild(script)
    timeout = setTimeout(() => finish(null), SDK_LOAD_TIMEOUT_MS)
  })
  // Deduplicate only concurrent loads. Later calls must re-read window.Telegram
  // rather than retain an object from an earlier WebView/document lifecycle.
  sdkPromise = loading.finally(() => {
    sdkPromise = null
  })
  return sdkPromise
}

export async function prepareTelegramWebApp(): Promise<TelegramWebApp | null> {
  const webApp = await loadTelegramWebApp()
  if (!webApp) return null
  try {
    webApp.ready()
  } catch {
    // Older WebViews can expose a partial bridge; auth must still continue.
  }
  try {
    webApp.expand()
  } catch {
    // Expanding is an optional presentation enhancement.
  }
  return webApp
}

export const miniAppHaptic = (
  webApp: TelegramWebApp | null,
  type: 'error' | 'success' | 'warning',
) => {
  try {
    webApp?.HapticFeedback?.notificationOccurred?.(type)
  } catch {
    // Haptic feedback is optional and must never block an operation.
  }
}

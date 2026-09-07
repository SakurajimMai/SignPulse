import { createApp } from 'vue'
import { createPinia } from 'pinia'
import './style.css'
import App from './App.vue'
import router from './router'
import i18n from './i18n'

const isMiniApp = /^\/mini-app(?:\/|$)/.test(window.location.pathname)
const MINI_APP_SW_RESET_KEY = 'mini-app-sw-reset-v1'

async function prepareMiniAppCache(): Promise<boolean> {
  if (!isMiniApp || !('serviceWorker' in navigator)) return true

  const hadController = Boolean(navigator.serviceWorker.controller)
  try {
    const registrations = await navigator.serviceWorker.getRegistrations()
    await Promise.all(registrations.map((registration) => registration.unregister()))
    if ('caches' in window) {
      const cacheNames = await window.caches.keys()
      await Promise.all(cacheNames.map((name) => window.caches.delete(name)))
    }
  } catch {
    // Cache cleanup is best effort; the Mini App still needs to start.
  }

  if (hadController) {
    try {
      if (sessionStorage.getItem(MINI_APP_SW_RESET_KEY) !== '1') {
        sessionStorage.setItem(MINI_APP_SW_RESET_KEY, '1')
        window.location.reload()
        return false
      }
    } catch {
      // Some embedded browsers disable sessionStorage.
    }
  }
  try {
    sessionStorage.removeItem(MINI_APP_SW_RESET_KEY)
  } catch {
    // Some embedded browsers disable sessionStorage.
  }
  return true
}

async function startApp() {
  if (!(await prepareMiniAppCache())) return

  const app = createApp(App)
  app.use(createPinia())
  app.use(router)
  app.use(i18n)
  app.mount('#app')

  if (!isMiniApp && 'serviceWorker' in navigator) {
    void import('virtual:pwa-register').then(({ registerSW }) => {
      registerSW({ immediate: true })
    })
  }
}

void startApp()

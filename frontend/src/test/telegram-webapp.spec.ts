import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  getTelegramWebApp,
  loadTelegramWebApp,
  miniAppHaptic,
  prepareTelegramWebApp,
  type TelegramWebApp,
} from '../lib/telegram-webapp'

describe('Telegram WebApp adapter', () => {
  afterEach(() => {
    delete window.Telegram
    document.getElementById('telegram-web-app-sdk')?.remove()
    vi.useRealTimers()
  })

  it('prepares an existing Telegram WebApp instance', async () => {
    const ready = vi.fn()
    const expand = vi.fn()
    const webApp = { initData: 'signed', ready, expand } satisfies TelegramWebApp
    window.Telegram = { WebApp: webApp }

    expect(getTelegramWebApp()).toBe(webApp)
    await expect(prepareTelegramWebApp()).resolves.toBe(webApp)
    expect(ready).toHaveBeenCalledOnce()
    expect(expand).toHaveBeenCalledOnce()
  })

  it('continues when optional native bridge methods fail', async () => {
    const webApp = {
      initData: 'signed',
      ready: vi.fn(() => {
        throw new Error('bridge unavailable')
      }),
      expand: vi.fn(() => {
        throw new Error('expand unsupported')
      }),
    } satisfies TelegramWebApp
    window.Telegram = { WebApp: webApp }

    await expect(prepareTelegramWebApp()).resolves.toBe(webApp)
    expect(webApp.ready).toHaveBeenCalledOnce()
    expect(webApp.expand).toHaveBeenCalledOnce()
  })

  it('haptic feedback is optional and isolated from the operation', () => {
    const notificationOccurred = vi.fn(() => {
      throw new Error('unsupported')
    })
    const webApp = {
      initData: 'signed',
      ready: vi.fn(),
      expand: vi.fn(),
      HapticFeedback: { notificationOccurred },
    } satisfies TelegramWebApp

    expect(() => miniAppHaptic(webApp, 'success')).not.toThrow()
    expect(notificationOccurred).toHaveBeenCalledWith('success')
    expect(() => miniAppHaptic(null, 'error')).not.toThrow()
  })

  it('replaces a stale SDK element and resolves with the loaded WebApp', async () => {
    const staleScript = document.createElement('script')
    staleScript.id = 'telegram-web-app-sdk'
    document.head.appendChild(staleScript)

    const pending = loadTelegramWebApp()
    const script = document.getElementById('telegram-web-app-sdk') as HTMLScriptElement
    expect(script).not.toBe(staleScript)

    const webApp = {
      initData: 'signed',
      ready: vi.fn(),
      expand: vi.fn(),
    } satisfies TelegramWebApp
    window.Telegram = { WebApp: webApp }
    script.dispatchEvent(new Event('load'))

    await expect(pending).resolves.toBe(webApp)
  })

  it('clears a timed-out SDK load so a later attempt can retry', async () => {
    vi.useFakeTimers()
    const first = loadTelegramWebApp()
    const firstScript = document.getElementById('telegram-web-app-sdk')

    await vi.advanceTimersByTimeAsync(10_000)
    await expect(first).resolves.toBeNull()
    expect(firstScript?.isConnected).toBe(false)

    const second = loadTelegramWebApp()
    const secondScript = document.getElementById('telegram-web-app-sdk') as HTMLScriptElement
    expect(secondScript).not.toBe(firstScript)
    const webApp = {
      initData: 'signed-after-retry',
      ready: vi.fn(),
      expand: vi.fn(),
    } satisfies TelegramWebApp
    window.Telegram = { WebApp: webApp }
    secondScript.dispatchEvent(new Event('load'))

    await expect(second).resolves.toBe(webApp)
  })
})

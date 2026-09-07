import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('Telegram Mini App PWA isolation', () => {
  it('excludes Mini App requests and navigation from the global worker', () => {
    const configSource = readFileSync('vite.config.ts', 'utf8')
    const mainSource = readFileSync('src/main.ts', 'utf8')

    expect(configSource).toContain("!url.pathname.startsWith('/api/mini-app/')")
    expect(configSource).toContain('navigateFallbackDenylist')
    expect(configSource).toContain('injectRegister: false')
    expect(mainSource).toContain('registration.unregister()')
    expect(mainSource).toContain("if (!isMiniApp && 'serviceWorker' in navigator)")
  })
})

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  flushPromises,
  mockI18nPassthrough,
  mountComposable,
} from './composable-test-utils'

const toastSpy = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  show: vi.fn(),
}))
const confirmMock = vi.hoisted(() => ({
  confirm: vi.fn(async () => true),
}))

const routeLeaveGuard = vi.hoisted(() => ({
  handler: null as null | (() => boolean | Promise<boolean>),
}))

const api = vi.hoisted(() => ({
  getGlobalSettings: vi.fn(),
  getTelegramConfig: vi.fn(),
  getAIConfig: vi.fn(),
  getRuntimeStatus: vi.fn(),
  getMemoryStats: vi.fn(),
  getTelegramBotRuntimeStatus: vi.fn(),
  // save/backup/version deps used via nested composables
  saveGlobalSettings: vi.fn(),
  saveTelegramConfig: vi.fn(),
  resetTelegramConfig: vi.fn(),
  saveAIConfig: vi.fn(),
  testAIConnection: vi.fn(),
  runDeviceKeepalive: vi.fn(),
  testBotNotification: vi.fn(),
  testProxyConnection: vi.fn(),
  getBackupStatus: vi.fn(),
  exportAllConfigs: vi.fn(),
  importAllConfigs: vi.fn(),
  importConfigPreview: vi.fn(),
  exportBackupArchive: vi.fn(),
  testWebdavBackup: vi.fn(),
  listWebdavBackupFiles: vi.fn(),
  downloadWebdavBackup: vi.fn(),
  getAppVersion: vi.fn(),
  checkAppVersion: vi.fn(),
}))

vi.mock('../composables/useI18n', () => ({
  useI18n: () => mockI18nPassthrough(),
}))
vi.mock('../composables/useToast', () => ({
  useToast: () => toastSpy,
}))
vi.mock('../composables/useConfirm', () => ({
  useConfirm: () => confirmMock,
}))
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: (fn: () => boolean | Promise<boolean>) => {
    routeLeaveGuard.handler = fn
  },
  useRoute: () => ({ query: {}, name: 'settings' }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))
vi.mock('../lib/api', () => api)
vi.mock('../lib/version-utils', () => ({
  fetchGithubLatestRelease: vi.fn(),
  friendlyGithubError: vi.fn((e: unknown) => String(e)),
  isUpdateAvailable: vi.fn(() => false),
  loadCachedUpdateCheck: vi.fn(() => null),
  safeHttpUrl: vi.fn((u: string | null) => u),
  saveCachedUpdateCheck: vi.fn(),
}))

import { useSettingsPage } from '../composables/useSettingsPage'
import { useAuthStore } from '../stores/auth'

describe('useSettingsPage (mount + dirty)', () => {
  beforeEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
    routeLeaveGuard.handler = null
    confirmMock.confirm.mockResolvedValue(true)
    useAuthStore().setToken('tok')

    api.getGlobalSettings.mockResolvedValue({
      sign_interval: 45,
      log_retention_days: 14,
      telegram_bot_token_set: true,
      proxy_enabled: true,
      proxy_scheme: 'http',
      proxy_host: 'proxy.example.com',
      proxy_port: 7890,
      proxy_password_set: true,
      webdav_password_set: true,
      webdav_url: 'https://dav.example.com',
      webdav_username: 'u',
      timezone: 'UTC',
      telegram_bot_message_thread_id: 9,
    })
    api.getTelegramConfig.mockResolvedValue({
      is_custom: true,
      api_id: '123',
      api_hash: 'hash',
    })
    api.getAIConfig.mockResolvedValue({
      has_config: true,
      base_url: 'https://ai',
      model: 'm1',
      api_key_decrypt_failed: false,
    })
    api.getBackupStatus.mockResolvedValue({ last: 'ok' })
    api.getRuntimeStatus.mockResolvedValue({ uptime: 1 })
    api.getMemoryStats.mockResolvedValue({ rss: 1 })
    api.getTelegramBotRuntimeStatus.mockResolvedValue({
      state: 'disabled',
      configured: true,
      control_enabled: false,
      primary: true,
      running: false,
      webhook_conflict: false,
      polling_conflict: false,
      bot: { id: 42, username: 'ops_bot', first_name: 'Ops' },
      last_error: '',
      last_update_id: 18,
    })
    api.getAppVersion.mockResolvedValue({
      version: '1.0.0',
      update_check_enabled: false,
    })
    api.testProxyConnection.mockResolvedValue({ success: true, message: 'ok' })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('onMounted loads settings/tg/ai and marks clean', async () => {
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => {
      expect(result.pageLoading.value).toBe(false)
    })
    await flushPromises(10)

    expect(result.settings.value.checkInterval).toBe('45')
    expect(result.settings.value.logDays).toBe(14)
    expect(result.settings.value.timezone).toBe('UTC')
    expect(result.settings.value.botThreadId).toBe('9')
    expect(result.settings.value.proxyHost).toBe('proxy.example.com')
    expect(result.settings.value.proxyPort).toBe(7890)
    expect(result.settings.value.proxyPassword).toBe('')
    expect(result.proxyPasswordSet.value).toBe(true)
    expect(result.botTokenSet.value).toBe(true)
    expect(result.webdavPasswordSet.value).toBe(true)
    expect(result.tgConfig.value.api_id).toBe('123')
    expect(result.aiConfig.value.model).toBe('m1')
    expect(result.runtimeStatus.value).toEqual({ uptime: 1 })
    expect(result.memoryStats.value).toEqual({ rss: 1 })
    expect(result.telegramBotRuntimeStatus.value?.state).toBe('disabled')
    expect(result.backupStatus.value).toEqual({ last: 'ok' })
    expect(result.isDirty.value).toBe(false)
    expect(result.appVersion.value?.version).toBe('1.0.0')
    unmount()
  })

  it('without token skips load', async () => {
    useAuthStore().clearToken()
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await flushPromises(10)
    expect(result.pageLoading.value).toBe(false)
    expect(api.getGlobalSettings).not.toHaveBeenCalled()
    unmount()
  })

  it('starts independent runtime metadata loads without waiting for backup status', async () => {
    let releaseBackup!: (value: unknown) => void
    api.getBackupStatus.mockImplementationOnce(
      () => new Promise((resolve) => {
        releaseBackup = resolve
      }),
    )

    const { result, unmount } = mountComposable(() => useSettingsPage())
    try {
      await flushPromises(10)
      expect(api.getRuntimeStatus).toHaveBeenCalledWith('tok')
      expect(api.getMemoryStats).toHaveBeenCalledWith('tok')
      expect(api.getTelegramBotRuntimeStatus).toHaveBeenCalledWith('tok')
      expect(api.getAppVersion).toHaveBeenCalledWith('tok')
    } finally {
      releaseBackup({ last: 'parallel' })
      await flushPromises(10)
      unmount()
    }
    expect(result.runtimeStatus.value).toEqual({ uptime: 1 })
    expect(result.memoryStats.value).toEqual({ rss: 1 })
  })

  it('isDirty tracks field edits and dirtyLabels', async () => {
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))

    expect(result.isDirty.value).toBe(false)
    expect(result.proxyDirty.value).toBe(false)
    result.settings.value.proxyHost = 'proxy-2.example.com'
    expect(result.isDirty.value).toBe(true)
    expect(result.proxyDirty.value).toBe(true)
    expect(result.dirtyLabels.value.length).toBeGreaterThan(0)
    unmount()
  })

  it('toggleReveal flips secret visibility flags', async () => {
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    expect(result.revealSecrets.value.botToken).toBe(false)
    await result.toggleReveal('botToken')
    expect(result.revealSecrets.value.botToken).toBe(true)
    await result.toggleReveal('botToken')
    expect(result.revealSecrets.value.botToken).toBe(false)
    unmount()
  })

  it('beforeunload is blocked when dirty', async () => {
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    result.settings.value.proxyHost = 'proxy-2.example.com'
    const ev = new Event('beforeunload') as BeforeUnloadEvent
    Object.defineProperty(ev, 'returnValue', { writable: true, value: '' })
    const prevent = vi.spyOn(ev, 'preventDefault')
    window.dispatchEvent(ev)
    expect(prevent).toHaveBeenCalled()
    unmount()
  })

  it('route leave confirms when dirty', async () => {
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    expect(routeLeaveGuard.handler).toBeTruthy()

    // clean → allow
    await expect(routeLeaveGuard.handler!()).resolves.toBe(true)

    result.settings.value.proxyHost = 'proxy-2.example.com'
    confirmMock.confirm.mockResolvedValueOnce(false)
    await expect(routeLeaveGuard.handler!()).resolves.toBe(false)
    expect(confirmMock.confirm).toHaveBeenCalled()

    confirmMock.confirm.mockResolvedValueOnce(true)
    await expect(routeLeaveGuard.handler!()).resolves.toBe(true)
    unmount()
  })

  it('load failure toasts error', async () => {
    api.getGlobalSettings.mockRejectedValue(new Error('boom'))
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    expect(toastSpy.error).toHaveBeenCalled()
    unmount()
  })

  it('exposes save handlers from nested composables', async () => {
    api.saveGlobalSettings.mockResolvedValue({})
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    await result.saveSettings()
    expect(api.saveGlobalSettings).toHaveBeenCalled()
    expect(result.isDirty.value).toBe(false)
    unmount()
  })

  it('polls Bot runtime status at least twice after Bot settings are saved', async () => {
    api.saveGlobalSettings.mockResolvedValue({})
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    api.getTelegramBotRuntimeStatus.mockClear()
    vi.useFakeTimers()

    const pending = result.saveBotSettings()
    await flushPromises(10)
    expect(api.getTelegramBotRuntimeStatus).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(500)
    await pending

    expect(api.getTelegramBotRuntimeStatus).toHaveBeenCalledTimes(2)
    expect(api.getTelegramBotRuntimeStatus).toHaveBeenLastCalledWith('tok')
    expect(result.telegramBotRuntimeLoading.value).toBe(false)
    unmount()
  })

  it('does not accept the first stale Bot runtime state after enabling control', async () => {
    api.saveGlobalSettings.mockResolvedValue({})
    api.getTelegramBotRuntimeStatus.mockResolvedValueOnce({
      state: 'disabled',
      configured: true,
      control_enabled: false,
      primary: true,
      running: false,
      webhook_conflict: false,
      polling_conflict: false,
      bot: { id: null, username: '', first_name: '' },
      last_error: '',
      last_update_id: null,
    })
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    result.settings.value.botControlEnabled = true
    result.settings.value.botMiniAppUrl = 'https://panel.example.com/mini-app'
    result.settings.value.botAllowedUserIds = '42'
    api.getTelegramBotRuntimeStatus.mockReset()
    api.getTelegramBotRuntimeStatus
      .mockResolvedValueOnce({
        state: 'disabled',
        configured: true,
        control_enabled: false,
        primary: true,
        running: false,
        webhook_conflict: false,
        polling_conflict: false,
        bot: { id: null, username: '', first_name: '' },
        last_error: '',
        last_update_id: null,
      })
      .mockResolvedValueOnce({
        state: 'stopped',
        configured: true,
        control_enabled: true,
        primary: true,
        running: false,
        webhook_conflict: false,
        polling_conflict: false,
        bot: { id: null, username: '', first_name: '' },
        last_error: '',
        last_update_id: null,
      })
      .mockResolvedValue({
        state: 'running',
        configured: true,
        control_enabled: true,
        primary: true,
        running: true,
        webhook_conflict: false,
        polling_conflict: false,
        bot: { id: 42, username: 'ops_bot', first_name: 'Ops' },
        last_error: '',
        last_update_id: 18,
      })
    vi.useFakeTimers()

    const pending = result.saveBotSettings()
    await flushPromises(10)
    expect(result.telegramBotRuntimeStatus.value?.state).toBe('disabled')

    await vi.advanceTimersByTimeAsync(500)
    expect(result.telegramBotRuntimeStatus.value?.state).toBe('stopped')
    await vi.advanceTimersByTimeAsync(500)
    await pending

    expect(api.getTelegramBotRuntimeStatus).toHaveBeenCalledTimes(3)
    expect(result.telegramBotRuntimeStatus.value?.state).toBe('running')
    unmount()
  })

  it('bounds Bot runtime polling when no terminal state is reached', async () => {
    api.saveGlobalSettings.mockResolvedValue({})
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))
    api.getTelegramBotRuntimeStatus.mockReset()
    api.getTelegramBotRuntimeStatus.mockResolvedValue({
      state: 'stopped',
      configured: true,
      control_enabled: true,
      primary: true,
      running: false,
      webhook_conflict: false,
      polling_conflict: false,
      bot: { id: null, username: '', first_name: '' },
      last_error: '',
      last_update_id: null,
    })
    vi.useFakeTimers()

    const pending = result.saveBotSettings()
    await flushPromises(10)
    await vi.advanceTimersByTimeAsync(2_500)
    await pending

    expect(api.getTelegramBotRuntimeStatus).toHaveBeenCalledTimes(6)
    expect(result.telegramBotRuntimeLoading.value).toBe(false)
    unmount()
  })

  it('saves proxy secrets without retaining them in browser state and tests saved config', async () => {
    api.saveGlobalSettings.mockResolvedValue({})
    const { result, unmount } = mountComposable(() => useSettingsPage())
    await vi.waitFor(() => expect(result.pageLoading.value).toBe(false))

    result.settings.value.proxyPassword = 'replacement-secret'
    await result.saveProxySettings()

    expect(api.saveGlobalSettings).toHaveBeenCalledWith(
      'tok',
      expect.objectContaining({
        proxy_enabled: true,
        proxy_host: 'proxy.example.com',
        proxy_password: 'replacement-secret',
        proxy_clear_credentials: false,
      }),
    )
    expect(result.settings.value.proxyPassword).toBe('')
    expect(result.proxyPasswordSet.value).toBe(true)

    await result.testProxy()
    expect(api.testProxyConnection).toHaveBeenCalledWith('tok')
    unmount()
  })
})

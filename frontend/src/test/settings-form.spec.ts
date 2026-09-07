import { describe, it, expect } from 'vitest'
import {
  emptyToNull,
  parseNumberInputValue,
  applyGlobalSettingsToForm,
  buildGeneralPayload,
  buildProxyPayload,
  buildBotPayload,
  buildSmtpPayload,
  emptySmtpForm,
  buildAdvancedPayload,
  buildAiRuntimePayload,
  buildBackupPayload,
  parseTelegramUserIds,
  validateBotControlSettings,
  validateProxySettings,
  normalizeNoProxy,
  snapAllSections,
  isAnySectionDirty,
  dirtySectionLabels,
  type SettingsFormState,
  type TgFormState,
  type AiFormState,
} from '../lib/settings-form'

const baseSettings = (): SettingsFormState => ({
  checkInterval: '30',
  logDays: 7,
  dataDir: '/data',
  concurrency: 2,
  deviceKeepaliveEnabled: true,
  deviceKeepaliveIntervalDays: 30,
  proxyEnabled: false,
  proxyScheme: 'http',
  proxyHost: '',
  proxyPort: '',
  proxyUsername: '',
  proxyPassword: '',
  proxyNoProxy: '',
  proxyClearCredentials: false,
  botEnabled: false,
  botLoginNotify: false,
  botTaskFailure: true,
  botTaskSuccess: false,
  quietEnabled: false,
  quietStart: '23:00',
  quietEnd: '07:00',
  botToken: '',
  botChatId: '',
  botThreadId: '',
  botControlEnabled: false,
  botMiniAppUrl: '',
  botAllowedUserIds: '',
  timezone: 'Asia/Hong_Kong',
  execTimeout: '',
  accountCooldown: '',
  flowRetry: '',
  historyMaxAge: '',
  aiVisionTimeout: '',
  aiVisionRetry: '',
  autoBackupEnabled: false,
  autoBackupInterval: 24,
  autoBackupKeep: 3,
  webdavUrl: '',
  webdavUsername: '',
  webdavPassword: '',
  webdavRemoteDir: 'tg-signpulse-backups',
  ...emptySmtpForm(),
})

describe('settings-form', () => {
  it('emptyToNull handles empty and invalid', () => {
    expect(emptyToNull('')).toBeNull()
    expect(emptyToNull('12')).toBe(12)
    expect(emptyToNull('x')).toBeNull()
    expect(emptyToNull(0)).toBe(0)
  })

  it('parseNumberInputValue keeps empty and drops NaN', () => {
    expect(parseNumberInputValue('')).toBe('')
    expect(parseNumberInputValue('3')).toBe(3)
    expect(parseNumberInputValue('1.5')).toBe(1.5)
    expect(parseNumberInputValue('abc')).toBe('')
    expect(parseNumberInputValue('NaN')).toBe('')
  })

  it('buildGeneralPayload maps fields', () => {
    const p = buildGeneralPayload(baseSettings())
    expect(p.sign_interval).toBe(30)
    expect(p.log_retention_days).toBe(7)
    expect(p.timezone).toBe('Asia/Hong_Kong')
  })

  it('buildGeneralPayload normalizes empty numeric fields to safe defaults', () => {
    const s = baseSettings()
    s.logDays = ''
    s.concurrency = ''
    s.deviceKeepaliveIntervalDays = ''

    const p = buildGeneralPayload(s)

    expect(p.log_retention_days).toBe(7)
    expect(p.tg_global_concurrency).toBe(1)
    expect(p.device_keepalive_interval_days).toBe(30)
  })

  it('validates enabled proxy host and port but allows disabling incomplete config', () => {
    const s = baseSettings()
    expect(validateProxySettings(s)).toEqual({})

    s.proxyEnabled = true
    expect(validateProxySettings(s)).toEqual({
      host: 'PROXY_HOST_REQUIRED',
      port: 'PROXY_PORT_REQUIRED',
    })

    s.proxyHost = 'http://proxy.example.com/path'
    s.proxyPort = 70000
    expect(validateProxySettings(s)).toEqual({
      host: 'INVALID_PROXY_HOST',
      port: 'INVALID_PROXY_PORT',
    })
  })

  it('buildProxyPayload keeps an empty password and normalizes NO_PROXY', () => {
    const s = baseSettings()
    Object.assign(s, {
      proxyEnabled: true,
      proxyScheme: 'socks5',
      proxyHost: ' proxy.example.com ',
      proxyPort: 1080,
      proxyUsername: ' alice ',
      proxyNoProxy: 'localhost, 127.0.0.1\nlocalhost',
    })

    const payload = buildProxyPayload(s) as Record<string, unknown>
    expect(payload).toMatchObject({
      proxy_enabled: true,
      proxy_scheme: 'socks5',
      proxy_host: 'proxy.example.com',
      proxy_port: 1080,
      proxy_username: 'alice',
      proxy_no_proxy: 'localhost,127.0.0.1',
      proxy_clear_credentials: false,
    })
    expect(payload).not.toHaveProperty('proxy_password')
    expect(normalizeNoProxy(' , \n')).toBeNull()

    s.proxyPassword = 'new-secret'
    expect(buildProxyPayload(s)).toHaveProperty('proxy_password', 'new-secret')
  })

  it('buildProxyPayload requires an explicit flag to clear credentials', () => {
    const s = baseSettings()
    Object.assign(s, {
      proxyUsername: 'alice',
      proxyPassword: 'secret',
      proxyClearCredentials: true,
    })

    const payload = buildProxyPayload(s) as Record<string, unknown>
    expect(payload.proxy_username).toBeNull()
    expect(payload.proxy_clear_credentials).toBe(true)
    expect(payload).not.toHaveProperty('proxy_password')
  })

  it('buildBotPayload parses thread id', () => {
    const s = baseSettings()
    s.botThreadId = '42'
    s.botEnabled = true
    const p = buildBotPayload(s)
    expect(p.telegram_bot_message_thread_id).toBe(42)
    expect(p.telegram_bot_notify_enabled).toBe(true)
  })

  it('buildBotPayload includes scoped Bot control settings', () => {
    const s = baseSettings()
    s.botControlEnabled = true
    s.botMiniAppUrl = 'https://panel.example.com/mini-app'
    s.botAllowedUserIds = '123, 456 123'
    const p = buildBotPayload(s)
    expect(p.telegram_bot_control_enabled).toBe(true)
    expect(p.telegram_bot_mini_app_url).toBe('https://panel.example.com/mini-app')
    expect(p.telegram_bot_allowed_user_ids).toEqual([123, 456])
  })

  it('allows discovery-only Bot control and validates configured values', () => {
    const s = baseSettings()
    s.botControlEnabled = true
    expect(validateBotControlSettings(s)).toEqual({})
    expect(buildBotPayload(s).telegram_bot_allowed_user_ids).toEqual([])
    s.botMiniAppUrl = 'http://panel.example.com/mini-app'
    s.botAllowedUserIds = '123, bad'
    expect(validateBotControlSettings(s)).toEqual({
      miniAppUrl: 'INVALID_MINI_APP_URL',
      allowedUserIds: 'INVALID_TELEGRAM_USER_IDS',
    })
    expect(() => buildBotPayload(s)).toThrow()
    expect(parseTelegramUserIds('9; 8 9')).toEqual([9, 8])
    s.botMiniAppUrl = ''
    s.botAllowedUserIds = '123'
    expect(validateBotControlSettings(s)).toEqual({})
  })

  it('buildBotPayload omits empty token (keep server value)', () => {
    const s = baseSettings()
    s.botToken = ''
    const p = buildBotPayload(s) as Record<string, unknown>
    expect('telegram_bot_token' in p).toBe(false)
    s.botToken = '123:ABC'
    const p2 = buildBotPayload(s) as Record<string, unknown>
    expect(p2.telegram_bot_token).toBe('123:ABC')
  })

  it('buildAdvancedPayload nulls empty numbers', () => {
    const p = buildAdvancedPayload(baseSettings())
    expect(p.sign_task_execution_timeout).toBeNull()
    expect(p.auto_backup_keep).toBe(3)
  })

  it('buildAiRuntimePayload only includes runtime fields', () => {
    const s = baseSettings()
    s.execTimeout = 120
    s.aiVisionTimeout = 20
    s.autoBackupEnabled = true
    const p = buildAiRuntimePayload(s) as Record<string, unknown>
    expect(p.sign_task_execution_timeout).toBe(120)
    expect(p.ai_vision_timeout).toBe(20)
    expect('auto_backup_enabled' in p).toBe(false)
    expect('webdav_url' in p).toBe(false)
  })

  it('buildBackupPayload only includes backup/webdav fields', () => {
    const s = baseSettings()
    s.execTimeout = 120
    s.webdavUrl = 'https://dav.example'
    s.webdavPassword = 'secret'
    const p = buildBackupPayload(s) as Record<string, unknown>
    expect(p.webdav_url).toBe('https://dav.example')
    expect(p.webdav_password).toBe('secret')
    expect('sign_task_execution_timeout' in p).toBe(false)
    expect('ai_vision_timeout' in p).toBe(false)
  })

  it('section dirty is independent', () => {
    const s = baseSettings()
    const tg: TgFormState = { api_id: '', api_hash: '' }
    const ai: AiFormState = { base_url: '', model: '', api_key: '' }
    const baseline = snapAllSections(s, tg, ai)

    const s2 = { ...s, botEnabled: true }
    const cur = snapAllSections(s2, tg, ai)
    expect(isAnySectionDirty(baseline, cur)).toBe(true)
    // 仅 bot 脏：general 快照应相同
    expect(cur.general).toBe(baseline.general)
    expect(cur.bot).not.toBe(baseline.bot)

    const labels = dirtySectionLabels(baseline, cur, {
      general: 'G',
      proxy: 'P',
      bot: 'B',
      smtp: 'S',
      advanced: 'A',
      tg: 'T',
      ai: 'I',
    })
    expect(labels).toEqual(['B'])
  })

  it('ai runtime fields dirty ai section; backup fields dirty advanced', () => {
    const s = baseSettings()
    const tg: TgFormState = { api_id: '', api_hash: '' }
    const ai: AiFormState = { base_url: '', model: '', api_key: '' }
    const baseline = snapAllSections(s, tg, ai)

    const sRuntime = { ...s, execTimeout: 90 }
    const curRuntime = snapAllSections(sRuntime, tg, ai)
    expect(curRuntime.ai).not.toBe(baseline.ai)
    expect(curRuntime.advanced).toBe(baseline.advanced)

    const sBackup = { ...s, autoBackupEnabled: true }
    const curBackup = snapAllSections(sBackup, tg, ai)
    expect(curBackup.advanced).not.toBe(baseline.advanced)
    expect(curBackup.ai).toBe(baseline.ai)
  })

  it('secret fields mask in snapshot (bot token / ai key)', () => {
    const s = baseSettings()
    s.botToken = '123:ABC'
    s.proxyPassword = 'proxy-secret'
    const tg: TgFormState = { api_id: '1', api_hash: 'h' }
    const ai: AiFormState = { base_url: 'u', model: 'm', api_key: 'sk' }
    const snap = snapAllSections(s, tg, ai)
    expect(snap.bot).toContain('***set***')
    expect(snap.bot).not.toContain('123:ABC')
    expect(snap.ai).toContain('***set***')
    expect(snap.ai).not.toContain('sk')
    expect(snap.proxy).toContain('***set***')
    expect(snap.proxy).not.toContain('proxy-secret')
  })
  it('buildBotPayload ignores invalid thread id', () => {
    const s = baseSettings()
    s.botThreadId = 'abc'
    const p = buildBotPayload(s)
    expect(p.telegram_bot_message_thread_id).toBeNull()
  })

  it('buildSmtpPayload omits empty password and defaults port', () => {
    const s = baseSettings()
    s.smtpEnabled = true
    s.smtpHost = 'smtp.example.com'
    s.smtpNotifyEmail = 'ops@example.com'
    s.smtpPort = ''
    const p = buildSmtpPayload(s)
    expect(p.smtp_enabled).toBe(true)
    expect(p.smtp_host).toBe('smtp.example.com')
    expect(p.smtp_port).toBe(465)
    expect(p).not.toHaveProperty('smtp_password')
    s.smtpPassword = 'secret'
    s.smtpEncryption = 'starttls'
    s.smtpPort = ''
    const withPwd = buildSmtpPayload(s)
    expect(withPwd.smtp_password).toBe('secret')
    expect(withPwd.smtp_port).toBe(587)
  })

})

describe('applyGlobalSettingsToForm', () => {
  it('maps server payload into form fields without secrets', () => {
    const s = baseSettings()
    const flags = applyGlobalSettingsToForm(s, {
      sign_interval: 45,
      log_retention_days: 14,
      telegram_bot_token_set: true,
      proxy_enabled: true,
      proxy_scheme: 'socks5',
      proxy_host: 'proxy.example.com',
      proxy_port: 1080,
      proxy_username: 'alice',
      proxy_password_set: true,
      proxy_no_proxy: 'localhost,127.0.0.1',
      webdav_password_set: true,
      telegram_bot_message_thread_id: 9,
      telegram_bot_control_enabled: true,
      telegram_bot_mini_app_url: 'https://panel.example.com/mini-app',
      telegram_bot_allowed_user_ids: [123, 456],
      timezone: 'UTC',
    })
    expect(s.checkInterval).toBe('45')
    expect(s.logDays).toBe(14)
    expect(s.botToken).toBe('')
    expect(s.proxyEnabled).toBe(true)
    expect(s.proxyScheme).toBe('socks5')
    expect(s.proxyHost).toBe('proxy.example.com')
    expect(s.proxyPort).toBe(1080)
    expect(s.proxyUsername).toBe('alice')
    expect(s.proxyPassword).toBe('')
    expect(s.proxyNoProxy).toBe('localhost,127.0.0.1')
    expect(s.webdavPassword).toBe('')
    expect(s.botThreadId).toBe('9')
    expect(s.botControlEnabled).toBe(true)
    expect(s.botMiniAppUrl).toBe('https://panel.example.com/mini-app')
    expect(s.botAllowedUserIds).toBe('123, 456')
    expect(s.timezone).toBe('UTC')
    expect(flags.botTokenSet).toBe(true)
    expect(flags.proxyPasswordSet).toBe(true)
    expect(flags.webdavPasswordSet).toBe(true)
    expect(flags.smtpPasswordSet).toBe(false)
  })

  it('migrates a legacy global_proxy without exposing its password', () => {
    const s = baseSettings()
    const flags = applyGlobalSettingsToForm(s, {
      global_proxy: 'socks5://alice:secret@127.0.0.1:1080',
    })

    expect(s.proxyEnabled).toBe(true)
    expect(s.proxyScheme).toBe('socks5')
    expect(s.proxyHost).toBe('127.0.0.1')
    expect(s.proxyPort).toBe(1080)
    expect(s.proxyUsername).toBe('alice')
    expect(s.proxyPassword).toBe('')
    expect(flags.proxyPasswordSet).toBe(true)
  })

  it('maps smtp fields without copying the password', () => {
    const s = baseSettings()
    s.smtpPassword = 'should-clear'
    const flags = applyGlobalSettingsToForm(s, {
      smtp_enabled: true,
      smtp_host: 'mail.example.com',
      smtp_port: 587,
      smtp_encryption: 'starttls',
      smtp_notify_email: 'a@b.c',
      smtp_password_set: true,
    })
    expect(s.smtpEnabled).toBe(true)
    expect(s.smtpHost).toBe('mail.example.com')
    expect(s.smtpPort).toBe(587)
    expect(s.smtpEncryption).toBe('starttls')
    expect(s.smtpPassword).toBe('')
    expect(flags.smtpPasswordSet).toBe(true)
  })
})

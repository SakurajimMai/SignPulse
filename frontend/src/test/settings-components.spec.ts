import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import AboutSettings from '../components/settings/AboutSettings.vue'
import GeneralSettings from '../components/settings/GeneralSettings.vue'
import ProxySettings from '../components/settings/ProxySettings.vue'
import SmtpSettings from '../components/settings/SmtpSettings.vue'
import BotNotifySettings from '../components/settings/BotNotifySettings.vue'
import { emptySmtpForm, type SettingsFormState } from '../lib/settings-form'
import type { TelegramBotRuntimeStatus } from '../lib/api'

const settingsState = (): SettingsFormState => ({
  checkInterval: '',
  logDays: 7,
  dataDir: '',
  concurrency: 1,
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

const botRuntimeStatus = (
  state: string,
  lastError = '',
): TelegramBotRuntimeStatus => ({
  state,
  configured: true,
  control_enabled: true,
  primary: state !== 'standby',
  running: state === 'running',
  webhook_conflict: state === 'webhook_conflict',
  polling_conflict: state === 'polling_conflict',
  bot: { id: 42, username: 'ops_bot', first_name: 'Ops' },
  last_error: lastError,
  last_update_id: 18,
})

describe('设置页拆分组件契约', () => {
  it('AboutSettings 展示后端 current_rss_mb 字段', () => {
    const wrapper = mount(AboutSettings, {
      props: {
        appVersion: null,
        runtimeStatus: {
          ready: true,
          scheduler_lock_held: true,
          legacy_tasks_writable: false,
          legacy_tasks_removed: true,
          database_is_sqlite: true,
          monitor_shard: '',
          monitor_allowlist: '',
        },
        memoryStats: { available: true, stats: { current_rss_mb: 128.456 } },
        versionBanner: null,
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('128.5 MB')
  })

  it('GeneralSettings 清空数字输入时保留空值而不是转换为 0', async () => {
    const wrapper = mount(GeneralSettings, {
      props: {
        modelValue: settingsState(),
        timezoneOptions: [],
      },
      global: { plugins: [i18n] },
    })

    await wrapper.find('input[type="number"]').setValue('')

    const updates = wrapper.emitted('update:modelValue')
    expect(updates).toBeTruthy()
    expect((updates?.at(-1)?.[0] as SettingsFormState).logDays).toBe('')
  })

  it('ProxySettings 启用后提供字段级校验并阻止无效保存', async () => {
    const wrapper = mount(ProxySettings, {
      props: {
        modelValue: { ...settingsState(), proxyEnabled: true },
        reveal: { proxyPassword: false },
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('系统代理')
    expect(wrapper.text()).toContain('启用代理时必须填写主机')
    expect(wrapper.get('#settings-proxy-host').attributes('aria-invalid')).toBe('true')
    expect(wrapper.get('.ui-btn-primary').attributes()).toHaveProperty('disabled')

    await wrapper.get('[role="switch"]').trigger('click')
    expect(
      (wrapper.emitted('update:modelValue')?.at(-1)?.[0] as SettingsFormState)
        .proxyEnabled,
    ).toBe(false)
  })

  it('ProxySettings 密码不回显，并以明确状态清除凭据', async () => {
    const wrapper = mount(ProxySettings, {
      props: {
        modelValue: {
          ...settingsState(),
          proxyEnabled: true,
          proxyHost: 'proxy.example.com',
          proxyPort: 1080,
          proxyUsername: 'alice',
        },
        passwordSet: true,
        reveal: { proxyPassword: false },
      },
      global: { plugins: [i18n] },
    })

    const password = wrapper.get('#settings-proxy-password')
    expect(password.attributes('type')).toBe('password')
    expect((password.element as HTMLInputElement).value).toBe('')

    const clear = wrapper.findAll('button').find((button) =>
      button.text().includes('清除代理凭据'),
    )
    expect(clear).toBeDefined()
    await clear!.trigger('click')
    const update = wrapper.emitted('update:modelValue')?.at(-1)?.[0] as SettingsFormState
    expect(update.proxyClearCredentials).toBe(true)
    expect(update.proxyUsername).toBe('')
    expect(update.proxyPassword).toBe('')
  })

  it('ProxySettings 有未保存草稿时不允许测试旧配置', () => {
    const wrapper = mount(ProxySettings, {
      props: {
        modelValue: {
          ...settingsState(),
          proxyEnabled: true,
          proxyHost: 'proxy.example.com',
          proxyPort: 1080,
        },
        reveal: { proxyPassword: false },
        testDisabled: true,
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.get('.ui-btn-secondary').attributes()).toHaveProperty('disabled')
  })

  it('SmtpSettings 切换开关并触发保存', async () => {
    const wrapper = mount(SmtpSettings, {
      props: {
        modelValue: settingsState(),
        smtpPasswordSet: false,
        reveal: { smtpPassword: false },
      },
      global: { plugins: [i18n] },
    })
    expect(wrapper.text()).toContain('SMTP')
    await wrapper.get('[role="switch"]').trigger('click')
    const updates = wrapper.emitted('update:modelValue')
    expect((updates?.at(-1)?.[0] as SettingsFormState).smtpEnabled).toBe(true)
    await wrapper.get('.ui-btn-primary').trigger('click')
    expect(wrapper.emitted('save')).toBeTruthy()
  })

  it('BotNotifySettings 在控制配置无效时阻止保存', async () => {
    const invalid = settingsState()
    invalid.botControlEnabled = true
    invalid.botAllowedUserIds = 'invalid-id'
    const wrapper = mount(BotNotifySettings, {
      props: {
        modelValue: invalid,
        reveal: { botToken: false },
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('Bot 控制与 Mini App')
    expect(wrapper.text()).toContain('操作者用户 ID 只能填写正整数')
    expect(wrapper.text()).toContain('通知、控制命令和 Mini App 共用这些用户 ID')
    expect(wrapper.get('.ui-btn-primary').attributes()).toHaveProperty('disabled')

    const controlSwitch = wrapper.findAll('[role="switch"]').find(
      (item) => item.attributes('aria-label') === '启用或停用 Bot 控制',
    )
    expect(controlSwitch).toBeDefined()
    await controlSwitch!.trigger('click')
    expect((wrapper.emitted('update:modelValue')?.at(-1)?.[0] as SettingsFormState).botControlEnabled).toBe(false)
  })

  it('BotNotifySettings 允许空白名单启动身份查询模式', async () => {
    const discoveryOnly = settingsState()
    discoveryOnly.botControlEnabled = true
    const wrapper = mount(BotNotifySettings, {
      props: {
        modelValue: discoveryOnly,
        reveal: { botToken: false },
      },
      global: { plugins: [i18n] },
    })

    expect(wrapper.text()).toContain('留空时 Bot 仅提供用户 ID 查询')
    expect(wrapper.get('.ui-btn-primary').attributes()).not.toHaveProperty('disabled')
    await wrapper.get('.ui-btn-primary').trigger('click')
    expect(wrapper.emitted('save')).toBeTruthy()
  })

  it('BotNotifySettings 展示并刷新 Bot runtime 状态与最近错误', async () => {
    const wrapper = mount(BotNotifySettings, {
      props: {
        modelValue: settingsState(),
        reveal: { botToken: false },
        runtimeStatus: botRuntimeStatus('running'),
      },
      global: { plugins: [i18n] },
    })

    const status = wrapper.get('[data-testid="bot-runtime-status"]')
    expect(status.attributes('role')).toBe('status')
    expect(status.attributes('aria-live')).toBe('polite')
    expect(status.text()).toContain('运行中')
    expect(status.text()).toContain('@ops_bot')

    for (const [state, label] of [
      ['standby', '当前副本待命'],
      ['webhook_conflict', 'Webhook 冲突'],
      ['polling_conflict', '轮询冲突'],
    ] as const) {
      await wrapper.setProps({
        runtimeStatus: botRuntimeStatus(
          state,
          state === 'polling_conflict' ? 'another_get_updates_consumer' : '',
        ),
      })
      expect(status.text()).toContain(label)
    }
    expect(status.text()).toContain('another_get_updates_consumer')

    await wrapper.setProps({ runtimeStatus: null })
    expect(status.text()).toContain('状态暂不可用')

    await wrapper.get('[aria-label="刷新 Bot 运行状态"]').trigger('click')
    expect(wrapper.emitted('refresh-status')).toBeTruthy()
  })
})

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import Alerts from '../views/Alerts.vue'

const { getAlertRules, saveAlertRules, testAlertMail, getAuthToken } = vi.hoisted(() => ({
  getAlertRules: vi.fn(),
  saveAlertRules: vi.fn(),
  testAlertMail: vi.fn(),
  getAuthToken: vi.fn(),
}))

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    getAlertRules,
    saveAlertRules,
    testAlertMail,
  }
})

vi.mock('../lib/api/core', () => ({
  getAuthToken,
}))

const baseRules = [
  {
    id: 'sign_task_timeout',
    group: 'core',
    title: '签到任务执行超时',
    severity: 'critical',
    enabled: true,
    email_enabled: true,
    telegram_enabled: true,
    cooldown_minutes: 30,
  },
  {
    id: 'manga_ingest_worker_fail',
    group: 'manga',
    title: '漫画频道采集进程失败',
    severity: 'warning',
    enabled: true,
    email_enabled: true,
    telegram_enabled: false,
    cooldown_minutes: 30,
  },
  {
    id: 'games_site_publish_fail',
    group: 'games',
    title: '游戏 WordPress 发布失败',
    severity: 'warning',
    enabled: true,
    email_enabled: true,
    telegram_enabled: false,
    cooldown_minutes: 15,
  },
  {
    id: 'cloud_auth_invalid',
    group: 'cloud',
    title: '网盘登录凭据失效',
    severity: 'critical',
    enabled: true,
    email_enabled: true,
    telegram_enabled: true,
    cooldown_minutes: 60,
  },
  {
    id: 'system_backup_archive_fail',
    group: 'system',
    title: '本地自动备份失败',
    severity: 'info',
    enabled: true,
    email_enabled: true,
    telegram_enabled: false,
    cooldown_minutes: 360,
  },
]

const canonicalRuleIds = [
  'sign_task_rate_limited',
  'sign_task_ai_fail',
  'sign_task_network_fail',
  'sign_task_timeout',
  'sign_task_flow_fail',
  'account_login_invalid',
  'manga_ingest_worker_fail',
  'manga_imgbed_upload_fail',
  'manga_channel_publish_fail',
  'manga_ehentai_worker_fail',
  'manga_ehentai_gallery_fail',
  'manga_hmw_source_fail',
  'manga_hmw_convert_fail',
  'manga_hmw_storage_fail',
  'manga_site_fail',
  'games_source_pull_fail',
  'games_archive_fail',
  'games_site_publish_fail',
  'games_cloud_fail',
  'cloud_auth_invalid',
  'cloud_keepalive_fail',
  'system_backup_archive_fail',
  'system_backup_webdav_fail',
  'system_backup_retention_fail',
  'system_device_keepalive_fail',
  'system_memory_high',
  'system_scheduler_job_fail',
]

const canonicalReasonCodes = [
  'disabled',
  'channel_disabled',
  'context_disabled',
  'email_not_ready',
  'telegram_not_ready',
  'no_deliverable_channel',
  'quiet_hours',
  'cooldown',
  'in_flight',
  'retry_backoff',
  'unknown_rule',
]

type RuleUpdate = {
  id: string
  enabled: boolean
  email_enabled: boolean
  telegram_enabled: boolean
  cooldown_minutes: number
}

const payload = (overrides: Record<string, unknown> = {}) => ({
  smtp_ready: true,
  bot_ready: true,
  smtp_enabled: true,
  quiet_hours: false,
  rules: baseRules.map((item) => ({ ...item })),
  recent: [],
  ...overrides,
})

describe('告警规则页', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    i18n.global.locale.value = 'zh-CN'
    getAuthToken.mockReturnValue('tok')
    getAlertRules.mockResolvedValue(payload())
    saveAlertRules.mockImplementation(async (_token: string, updates: RuleUpdate[]) =>
      payload({
        rules: baseRules.map((rule) => ({
          ...rule,
          ...updates.find((item) => item.id === rule.id),
        })),
      }),
    )
    testAlertMail.mockResolvedValue({ success: true, message: 'ok' })
  })

  it('中英文文案覆盖全部 canonical 规则和 dispatcher 原因码', () => {
    for (const locale of ['zh-CN', 'en-US']) {
      const message = i18n.global.getLocaleMessage(locale) as {
        alerts: {
          rules: Record<string, string>
          ruleHints: Record<string, string>
          reasons: Record<string, string>
        }
      }
      expect(Object.keys(message.alerts.rules).sort()).toEqual(
        [...canonicalRuleIds].sort(),
      )
      expect(Object.keys(message.alerts.ruleHints).sort()).toEqual(
        [...canonicalRuleIds].sort(),
      )
      for (const code of canonicalReasonCodes) {
        expect(message.alerts.reasons[code]).toBeTruthy()
      }
    }
  })

  it('动态展示完整分组，仅在修改后保存，并为开关提供可访问名称', async () => {
    const wrapper = mount(Alerts, {
      global: { plugins: [i18n] },
    })
    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('漫画采集')
    })

    expect(wrapper.text()).toContain('告警规则')
    expect(wrapper.text()).toContain('漫画采集')
    expect(wrapper.text()).toContain('游戏发布')
    expect(wrapper.text()).toContain('网盘登录')
    expect(wrapper.text()).toContain('签到与账号')
    expect(wrapper.text()).toContain('系统运行')

    const switches = wrapper.findAll('[role="switch"]')
    expect(switches).toHaveLength(baseRules.length * 3)
    const ruleSwitches = switches.filter((item) =>
      item.attributes('aria-label')?.startsWith('启用或停用“'),
    )
    expect(ruleSwitches).toHaveLength(baseRules.length)
    expect(ruleSwitches[0].attributes('aria-label')).toContain('签到任务执行超时')
    expect(wrapper.text()).toContain('严重')
    expect(wrapper.text()).toContain('Telegram Bot')

    const saveButton = wrapper.get('button.ui-btn-primary')
    expect(saveButton.attributes()).toHaveProperty('disabled')
    await ruleSwitches[0].trigger('click')
    expect(saveButton.attributes()).not.toHaveProperty('disabled')

    await saveButton.trigger('click')
    expect(saveAlertRules).toHaveBeenCalled()
    const updates = saveAlertRules.mock.calls[0][1] as RuleUpdate[]
    expect(updates.find((item) => item.id === 'sign_task_timeout')?.enabled).toBe(false)

    await vi.waitFor(() => {
      expect(saveButton.attributes()).toHaveProperty('disabled')
    })

    wrapper.unmount()
  })

  it('可独立切换邮件与 Telegram 通道并提交', async () => {
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))

    const telegramSwitch = wrapper.findAll('[role="switch"]').find((item) =>
      item.attributes('aria-label')?.includes('Telegram投递') &&
      item.attributes('aria-checked') === 'false',
    )
    expect(telegramSwitch).toBeDefined()
    expect(telegramSwitch?.attributes('aria-checked')).toBe('false')
    await telegramSwitch!.trigger('click')
    await wrapper.get('button.ui-btn-primary').trigger('click')

    const updates = saveAlertRules.mock.calls[0][1] as RuleUpdate[]
    const manga = updates.find((item) => item.id === 'manga_ingest_worker_fail')
    expect(manga).toMatchObject({ email_enabled: true, telegram_enabled: true })
  })

  it('未知分组仍可见，缺失翻译时回退 API 标题', async () => {
    getAlertRules.mockResolvedValue(
      payload({
        rules: [
          {
            id: 'future_rule',
            group: 'future',
            title: '未来告警',
            enabled: true,
            cooldown_minutes: 20,
          },
        ],
      }),
    )
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })

    await vi.waitFor(() => expect(wrapper.text()).toContain('未来告警'))
    expect(wrapper.text()).toContain('其他分组 / future')
    expect(wrapper.text()).not.toContain('alerts.rules.future_rule')
  })

  it('冷却时间显示行内错误且不会静默修正或提交', async () => {
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))

    const input = wrapper.get('#alert-cooldown-sign_task_timeout')
    const saveButton = wrapper.get('button.ui-btn-primary')
    await input.setValue('0')

    expect((input.element as HTMLInputElement).value).toBe('0')
    expect(input.attributes('aria-invalid')).toBe('true')
    expect(wrapper.text()).toContain('冷却时间须为 1 到 1440 分钟')
    expect(saveButton.attributes()).toHaveProperty('disabled')
    await saveButton.trigger('click')
    expect(saveAlertRules).not.toHaveBeenCalled()

    await input.setValue('45')
    expect(input.attributes('aria-invalid')).toBe('false')
    expect(saveButton.attributes()).not.toHaveProperty('disabled')
    await saveButton.trigger('click')
    const updates = saveAlertRules.mock.calls[0][1] as RuleUpdate[]
    expect(updates.find((item) => item.id === 'sign_task_timeout')?.cooldown_minutes).toBe(45)
  })

  it('加载失败可重试，无 token 时不会永久 loading', async () => {
    getAlertRules.mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(payload())
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })

    await vi.waitFor(() => expect(wrapper.text()).toContain('加载告警规则失败'))
    const retry = wrapper.findAll('button').find((button) => button.text().includes('重试'))
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))
    expect(getAlertRules).toHaveBeenCalledTimes(2)

    wrapper.unmount()
    vi.clearAllMocks()
    getAuthToken.mockReturnValue('')
    const withoutToken = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(withoutToken.text()).toContain('登录状态已失效'))
    expect(withoutToken.text()).not.toContain('加载中')
    expect(getAlertRules).not.toHaveBeenCalled()
  })

  it('展示 skipped 原因并可发送测试邮件', async () => {
    getAlertRules.mockResolvedValue(
      payload({
        recent: [
          {
            at: '2026-08-31T01:00:00Z',
            rule_id: 'sign_task_timeout',
            status: 'skipped',
            reason: 'cooldown',
            deliveries: [
              { channel: 'email', status: 'sent' },
              { channel: 'telegram', status: 'failed', reason: 'telegram_not_ready' },
            ],
          },
          {
            at: '2026-08-31T01:01:00Z',
            rule_id: 'removed_rule',
            status: 'ignored',
            reason: 'unknown_rule',
          },
        ],
      }),
    )
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('已跳过'))

    expect(wrapper.text()).toContain('仍在冷却期')
    expect(wrapper.text()).toContain('已忽略')
    expect(wrapper.text()).toContain('未知规则')
    expect(wrapper.text()).toContain('邮件 · 已发送')
    expect(wrapper.text()).toContain('Telegram · 发送失败')
    expect(wrapper.text()).toContain('Telegram Bot 未就绪')
    const testButton = wrapper.get('[data-testid="test-alert-mail"]')
    expect(testButton.attributes()).not.toHaveProperty('disabled')
    await testButton.trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('测试邮件已发送'))
    expect(testAlertMail).toHaveBeenCalledWith('tok')
  })

  it('测试邮件投递失败时显示服务端反馈', async () => {
    testAlertMail.mockResolvedValue({ success: false, message: 'SMTP rejected' })
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))

    await wrapper.get('[data-testid="test-alert-mail"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('SMTP rejected'))
  })

  it('SMTP 未就绪时禁用测试邮件，保存失败后保留脏状态', async () => {
    getAlertRules.mockResolvedValue(payload({ smtp_ready: false }))
    saveAlertRules.mockRejectedValue(new Error('save failed'))
    const wrapper = mount(Alerts, { global: { plugins: [i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))

    const testButton = wrapper.get('[data-testid="test-alert-mail"]')
    expect(testButton.attributes()).toHaveProperty('disabled')
    await wrapper.findAll('[role="switch"]')[0].trigger('click')
    const saveButton = wrapper.get('button.ui-btn-primary')
    await saveButton.trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('保存告警规则失败'))
    expect(wrapper.text()).toContain('告警规则有未保存的更改')
    expect(saveButton.attributes()).not.toHaveProperty('disabled')
  })
})

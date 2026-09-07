import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import MiniApp from '../views/MiniApp.vue'
import type { MiniAppBootstrap } from '../lib/api'

const mocks = vi.hoisted(() => {
  const telegramState = { backHandler: null as (() => void) | null }
  const webApp = {
    initData: 'signed-init-data',
    colorScheme: 'light' as const,
    viewportStableHeight: 720,
    ready: vi.fn(),
    expand: vi.fn(),
    onEvent: vi.fn(),
    offEvent: vi.fn(),
    BackButton: {
      show: vi.fn(),
      hide: vi.fn(),
      onClick: vi.fn((handler: () => void) => {
        telegramState.backHandler = handler
      }),
      offClick: vi.fn(),
    },
  }
  return {
    telegramState,
    webApp,
    prepareTelegramWebApp: vi.fn(),
    miniAppHaptic: vi.fn(),
    authenticateMiniApp: vi.fn(),
    getMiniAppBootstrap: vi.fn(),
    getMiniAppAlerts: vi.fn(),
    runMiniAppTask: vi.fn(),
    getMiniAppTaskRunStatus: vi.fn(),
    confirm: vi.fn(),
    toast: { success: vi.fn(), error: vi.fn() },
  }
})

vi.mock('../lib/telegram-webapp', () => ({
  prepareTelegramWebApp: mocks.prepareTelegramWebApp,
  miniAppHaptic: mocks.miniAppHaptic,
}))

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    authenticateMiniApp: mocks.authenticateMiniApp,
    getMiniAppBootstrap: mocks.getMiniAppBootstrap,
    getMiniAppAlerts: mocks.getMiniAppAlerts,
    runMiniAppTask: mocks.runMiniAppTask,
    getMiniAppTaskRunStatus: mocks.getMiniAppTaskRunStatus,
  }
})

vi.mock('../composables/useConfirm', () => ({
  useConfirm: () => ({ confirm: mocks.confirm }),
}))

vi.mock('../composables/useToast', () => ({
  useToast: () => mocks.toast,
}))

const bootstrap = (): MiniAppBootstrap => ({
  system: {
    status: 'ready',
    accounts_total: 2,
    accounts_connected: 1,
    tasks_total: 1,
    tasks_enabled: 1,
    active_runs_total: 0,
  },
  tasks: [
    {
      name: 'daily-sign',
      account_name: '*',
      account_names: ['acc-a', 'acc-b'],
      enabled: true,
      sign_at: '08:00',
      execution_mode: 'fixed',
      last_run: null,
      active_run: null,
    },
  ],
  active_runs: [],
  recent_alerts: [],
  capabilities: ['tasks:read', 'tasks:run', 'alerts:read'],
})

const createTestRouter = async (query = '') => {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/mini-app', name: 'mini-app', component: MiniApp }],
  })
  await router.push(`/mini-app${query}`)
  await router.isReady()
  return router
}

describe('Telegram Mini App page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.telegramState.backHandler = null
    mocks.webApp.initData = 'signed-init-data'
    mocks.prepareTelegramWebApp.mockResolvedValue(mocks.webApp)
    mocks.authenticateMiniApp.mockResolvedValue({
      access_token: 'mini-token',
      token_type: 'bearer',
      expires_in: 3600,
      user: { id: 123, first_name: 'Alice', username: 'alice' },
    })
    mocks.getMiniAppBootstrap.mockResolvedValue(bootstrap())
    mocks.getMiniAppAlerts.mockResolvedValue({
      total: 1,
      rules: [],
      items: [
        {
          at: '2026-08-31T01:00:00Z',
          rule_id: 'sign_task_timeout',
          title: '签到任务执行超时',
          detail: 'timeout',
          severity: 'critical',
          status: 'partial',
          reason: 'quiet_hours',
          deliveries: [
            { channel: 'email', status: 'sent' },
            { channel: 'telegram', status: 'failed' },
          ],
        },
      ],
    })
    mocks.confirm.mockResolvedValue(true)
    mocks.runMiniAppTask.mockResolvedValue({
      request_id: 'req',
      run_id: 'run-1',
      state: 'finished',
      success: true,
      account_name: 'acc-b',
      task_name: 'daily-sign',
    })
    i18n.global.locale.value = 'zh-CN'
  })

  it('authenticates with initData and runs an explicitly selected account', async () => {
    const router = await createTestRouter()
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })
    await vi.waitFor(() => expect(wrapper.text()).toContain('系统运行正常'))

    expect(mocks.authenticateMiniApp).toHaveBeenCalledWith('signed-init-data')
    expect(mocks.getMiniAppBootstrap).toHaveBeenCalledWith('mini-token')
    expect(wrapper.text()).toContain('Alice')

    const tasksTab = wrapper.findAll('button').find((item) => item.text() === '任务')
    await tasksTab!.trigger('click')
    await wrapper.get('.mini-select').setValue('acc-b')
    await wrapper.get('.mini-primary-button').trigger('click')

    await vi.waitFor(() => expect(mocks.runMiniAppTask).toHaveBeenCalled())
    expect(mocks.confirm).toHaveBeenCalled()
    expect(mocks.runMiniAppTask).toHaveBeenCalledWith(
      'mini-token',
      'daily-sign',
      'acc-b',
      expect.any(String),
    )
    expect(mocks.webApp.BackButton.show).toHaveBeenCalled()

    mocks.telegramState.backHandler?.()
    await vi.waitFor(() => expect(wrapper.find('[data-testid="mini-tab-status"]').exists()).toBe(true))
    wrapper.unmount()
  })

  it('keeps same-name tasks scoped to their own accounts', async () => {
    const payload = bootstrap()
    payload.system.tasks_total = 2
    payload.system.tasks_enabled = 2
    payload.tasks = [
      {
        ...payload.tasks[0],
        name: 'daily',
        account_name: 'acc-a',
        account_names: ['acc-a'],
      },
      {
        ...payload.tasks[0],
        name: 'daily',
        account_name: 'acc-b',
        account_names: ['acc-b'],
      },
    ]
    mocks.getMiniAppBootstrap.mockResolvedValue(payload)
    mocks.runMiniAppTask.mockResolvedValueOnce({
      request_id: 'req-a',
      run_id: 'run-a',
      state: 'finished',
      success: true,
      account_name: 'acc-a',
      task_name: 'daily',
    })
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.findAll('.mini-task')).toHaveLength(2))
    const cards = wrapper.findAll('.mini-task')
    expect((cards[0].get('.mini-select').element as HTMLSelectElement).value).toBe('acc-a')
    expect((cards[1].get('.mini-select').element as HTMLSelectElement).value).toBe('acc-b')

    await cards[0].get('.mini-primary-button').trigger('click')
    await vi.waitFor(() => expect(mocks.runMiniAppTask).toHaveBeenCalled())
    expect(mocks.runMiniAppTask).toHaveBeenCalledWith(
      'mini-token',
      'daily',
      'acc-a',
      expect.any(String),
    )
    wrapper.unmount()
  })

  it('shows recent per-channel deliveries in the alerts tab', async () => {
    const router = await createTestRouter('?tab=alerts')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.text()).toContain('签到任务执行超时'))
    expect(mocks.getMiniAppAlerts).toHaveBeenCalledWith('mini-token', 30)
    expect(wrapper.text()).toContain('邮件 · 已发送')
    expect(wrapper.text()).toContain('Telegram · 发送失败')
    expect(wrapper.text()).toContain('处于免打扰时段')
    expect(wrapper.text()).not.toContain('quiet_hours')
    wrapper.unmount()
  })

  it('treats an idle aggregate status as not running', async () => {
    const payload = bootstrap()
    payload.tasks[0].active_run = {
      run_id: '',
      state: 'idle',
      account_name: 'acc-a',
      task_name: 'daily-sign',
    }
    mocks.runMiniAppTask.mockResolvedValueOnce({
      run_id: 'idle-result',
      state: 'idle',
      success: null,
      account_name: 'acc-a',
      task_name: 'daily-sign',
    })
    mocks.getMiniAppBootstrap.mockResolvedValue(payload)
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-primary-button').exists()).toBe(true))
    expect(wrapper.get('.mini-select').attributes()).not.toHaveProperty('disabled')
    expect(wrapper.get('.mini-primary-button').attributes()).not.toHaveProperty('disabled')
    expect(wrapper.get('.mini-primary-button').text()).toContain('运行任务')
    await wrapper.get('.mini-primary-button').trigger('click')
    await vi.waitFor(() => expect(mocks.runMiniAppTask).toHaveBeenCalled())
    expect(mocks.miniAppHaptic).not.toHaveBeenCalled()
    expect(mocks.toast.success).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not apply another account aggregate run to the selected account', async () => {
    const payload = bootstrap()
    payload.tasks[0].active_run = {
      run_id: 'run-acc-a',
      state: 'running',
      account_name: 'acc-a',
      task_name: 'daily-sign',
    }
    payload.active_runs = [payload.tasks[0].active_run]
    payload.system.active_runs_total = 1
    mocks.getMiniAppBootstrap.mockResolvedValue(payload)
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-select').exists()).toBe(true))
    const select = wrapper.get('.mini-select')
    expect(select.attributes()).not.toHaveProperty('disabled')
    await select.setValue('acc-b')
    const runButton = wrapper.get('.mini-primary-button')
    expect(runButton.attributes()).not.toHaveProperty('disabled')
    expect(runButton.text()).toContain('运行任务')
    await runButton.trigger('click')

    await vi.waitFor(() => expect(mocks.runMiniAppTask).toHaveBeenCalled())
    expect(mocks.runMiniAppTask).toHaveBeenCalledWith(
      'mini-token',
      'daily-sign',
      'acc-b',
      expect.any(String),
    )
    wrapper.unmount()
  })

  it('blocks a duplicate run found in bootstrap active_runs for the selected account', async () => {
    const payload = bootstrap()
    payload.tasks[0].active_run = {
      run_id: 'run-acc-a',
      state: 'running',
      account_name: 'acc-a',
      task_name: 'daily-sign',
    }
    payload.active_runs = [
      payload.tasks[0].active_run,
      {
        run_id: 'run-acc-b',
        state: 'running',
        account_name: 'acc-b',
        task_name: 'daily-sign',
      },
    ]
    payload.system.active_runs_total = 2
    mocks.getMiniAppBootstrap.mockResolvedValue(payload)
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-select').exists()).toBe(true))
    await wrapper.get('.mini-select').setValue('acc-b')
    const runButton = wrapper.get('.mini-primary-button')
    expect(runButton.attributes()).toHaveProperty('disabled')
    expect(runButton.text()).toContain('运行中')
    await runButton.trigger('click')
    expect(mocks.runMiniAppTask).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('retries replica status polling and clears the transient error on recovery', async () => {
    mocks.runMiniAppTask.mockResolvedValueOnce({
      request_id: 'req',
      run_id: 'run-retry',
      state: 'running',
      success: null,
      account_name: 'acc-b',
      task_name: 'daily-sign',
    })
    const unavailable = Object.assign(new Error('MINI_APP_PRIMARY_UNAVAILABLE'), {
      code: 'MINI_APP_PRIMARY_UNAVAILABLE',
      status: 503,
    })
    mocks.getMiniAppTaskRunStatus
      .mockRejectedValueOnce(unavailable)
      .mockResolvedValueOnce({
        run_id: 'run-retry',
        state: 'idle',
        success: null,
        account_name: 'acc-b',
        task_name: 'daily-sign',
      })
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-select').exists()).toBe(true))
    await wrapper.get('.mini-select').setValue('acc-b')
    vi.useFakeTimers()
    try {
      await wrapper.get('.mini-primary-button').trigger('click')
      await flushPromises()
      const hapticsAfterStart = mocks.miniAppHaptic.mock.calls.length

      await vi.advanceTimersByTimeAsync(1800)
      await flushPromises()
      expect(mocks.getMiniAppTaskRunStatus).toHaveBeenCalledTimes(1)
      expect(wrapper.text()).toContain('主服务暂时不可用，请稍后重试')

      await vi.advanceTimersByTimeAsync(2999)
      expect(mocks.getMiniAppTaskRunStatus).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(1)
      await flushPromises()

      expect(mocks.getMiniAppTaskRunStatus).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).not.toContain('主服务暂时不可用，请稍后重试')
      expect(mocks.miniAppHaptic).toHaveBeenCalledTimes(hapticsAfterStart)
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('retries an origin HTML polling failure and reaches the final status', async () => {
    mocks.runMiniAppTask.mockResolvedValueOnce({
      request_id: 'req-html',
      run_id: 'run-html',
      state: 'running',
      success: null,
      account_name: 'acc-b',
      task_name: 'daily-sign',
    })
    const originHtml = Object.assign(new Error('ORIGIN_HTML_ERROR'), {
      code: 'ORIGIN_HTML_ERROR',
      status: 200,
    })
    mocks.getMiniAppTaskRunStatus
      .mockRejectedValueOnce(originHtml)
      .mockResolvedValueOnce({
        run_id: 'run-html',
        state: 'finished',
        success: true,
        account_name: 'acc-b',
        task_name: 'daily-sign',
      })
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-select').exists()).toBe(true))
    await wrapper.get('.mini-select').setValue('acc-b')
    vi.useFakeTimers()
    try {
      await wrapper.get('.mini-primary-button').trigger('click')
      await flushPromises()

      await vi.advanceTimersByTimeAsync(1800)
      await flushPromises()
      expect(mocks.getMiniAppTaskRunStatus).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(mocks.getMiniAppTaskRunStatus).toHaveBeenCalledTimes(2)
      expect(wrapper.get('.mini-primary-button').attributes()).not.toHaveProperty('disabled')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('localizes primary-unavailable errors instead of showing the code', async () => {
    const error = Object.assign(new Error('MINI_APP_PRIMARY_UNAVAILABLE'), {
      code: 'MINI_APP_PRIMARY_UNAVAILABLE',
      status: 503,
    })
    mocks.runMiniAppTask.mockRejectedValueOnce(error)
    const router = await createTestRouter('?tab=tasks')
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.find('.mini-select').exists()).toBe(true))
    await wrapper.get('.mini-select').setValue('acc-b')
    await wrapper.get('.mini-primary-button').trigger('click')

    await vi.waitFor(() => {
      expect(mocks.toast.error).toHaveBeenCalledWith('主服务暂时不可用，请稍后重试')
    })
    expect(wrapper.text()).not.toContain('MINI_APP_PRIMARY_UNAVAILABLE')
    wrapper.unmount()
  })

  it('explains when Mini App control is disabled instead of showing the code', async () => {
    const error = Object.assign(new Error('MINI_APP_DISABLED'), {
      code: 'MINI_APP_DISABLED',
      status: 403,
    })
    mocks.authenticateMiniApp.mockRejectedValueOnce(error)
    const router = await createTestRouter()
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请在系统设置中开启 Telegram Bot 控制')
    })
    expect(wrapper.text()).toContain('请先在系统设置中启用 Telegram Bot 控制')
    expect(wrapper.text()).not.toContain('请检查网络后重试')
    expect(wrapper.text()).not.toContain('MINI_APP_DISABLED')
    wrapper.unmount()
  })

  it('tells the operator to reopen Telegram when initData has expired', async () => {
    const error = Object.assign(new Error('MINI_APP_INIT_DATA_INVALID'), {
      code: 'MINI_APP_INIT_DATA_INVALID',
      status: 401,
    })
    mocks.authenticateMiniApp.mockRejectedValueOnce(error)
    const router = await createTestRouter()
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => {
      expect(wrapper.text()).toContain('请彻底关闭当前 Mini App，再从机器人菜单重新打开')
    })
    expect(wrapper.text()).not.toContain('请检查网络后重试')
    wrapper.unmount()
  })

  it('retries a transient initial request before showing an error', async () => {
    const error = Object.assign(new Error('MINI_APP_PRIMARY_UNAVAILABLE'), {
      code: 'MINI_APP_PRIMARY_UNAVAILABLE',
      status: 503,
    })
    mocks.authenticateMiniApp.mockRejectedValueOnce(error)
    vi.useFakeTimers()
    const router = await createTestRouter()
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    try {
      await flushPromises()
      expect(mocks.authenticateMiniApp).toHaveBeenCalledTimes(1)
      await vi.advanceTimersByTimeAsync(600)
      await flushPromises()

      expect(mocks.authenticateMiniApp).toHaveBeenCalledTimes(2)
      expect(wrapper.text()).toContain('系统运行正常')
    } finally {
      wrapper.unmount()
      vi.useRealTimers()
    }
  })

  it('does not call auth outside a Telegram launch context', async () => {
    mocks.webApp.initData = ''
    const router = await createTestRouter()
    const wrapper = mount(MiniApp, { global: { plugins: [router, i18n] } })

    await vi.waitFor(() => expect(wrapper.text()).toContain('请从 Telegram 机器人打开'))
    expect(mocks.authenticateMiniApp).not.toHaveBeenCalled()
    expect(wrapper.find('nav').exists()).toBe(false)
    wrapper.unmount()
  })
})

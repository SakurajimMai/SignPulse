import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import Settings from '../views/Settings.vue'

vi.mock('../composables/useSettingsPage', () => {
  const noop = vi.fn()
  return {
    useSettingsPage: () => ({
      t: (key: string) => key,
      settings: {},
      timezoneOptions: [],
      tgConfig: {},
      aiConfig: {},
      aiKeyDecryptFailed: false,
      loading: false,
      proxyLoading: false,
      proxyTestLoading: false,
      tgLoading: false,
      aiLoading: false,
      dataLoading: false,
      backupLoading: false,
      backupStatus: null,
      runtimeStatus: null,
      memoryStats: null,
      telegramBotRuntimeStatus: null,
      telegramBotRuntimeLoading: false,
      advancedLoading: false,
      botTestLoading: false,
      pageLoading: false,
      revealSecrets: {
        tgApiId: false,
        tgApiHash: false,
        aiKey: false,
        botToken: false,
        proxyPassword: false,
        smtpPassword: false,
        webdavPassword: false,
      },
      isDirty: false,
      proxyDirty: false,
      dirtyLabels: [],
      appVersion: null,
      versionLoading: false,
      checkLoading: false,
      versionBanner: null,
      botLoading: false,
      smtpLoading: false,
      smtpTestLoading: false,
      keepaliveLoading: false,
      saveAllLoading: false,
      webdavTestLoading: false,
      webdavListLoading: false,
      remoteWebdavFiles: [],
      remoteWebdavMessage: '',
      webdavPasswordSet: false,
      botTokenSet: false,
      proxyPasswordSet: false,
      smtpPasswordSet: false,
      remoteDownloadName: '',
      saveSettings: noop,
      saveProxySettings: noop,
      runKeepaliveNow: noop,
      saveBotSettings: noop,
      saveSmtpSettings: noop,
      saveAdvancedSettings: noop,
      saveAllSettings: noop,
      testBot: noop,
      testProxy: noop,
      testSmtp: noop,
      saveTgConfig: noop,
      resetTgConfig: noop,
      saveAiConfig: noop,
      testAi: noop,
      handleExport: noop,
      handleImportFile: noop,
      handleBackupExport: noop,
      handleWebdavTest: noop,
      handleListRemoteBackups: noop,
      handleDownloadRemoteBackup: noop,
      handleCheckUpdate: noop,
      refreshTelegramBotRuntimeStatus: noop,
      toggleReveal: noop,
    }),
  }
})

const sectionStub = (id: string) => ({
  name: id,
  template: `<div data-section="${id}" />`,
})

describe('设置页双列布局', () => {
  it('数据管理与 Telegram API 在同一左列，不会被右列撑出中间空白', () => {
    const wrapper = mount(Settings, {
      global: {
        plugins: [i18n],
        stubs: {
          GeneralSettings: sectionStub('general'),
          ProxySettings: sectionStub('proxy'),
          TelegramApiSettings: sectionStub('telegram-api'),
          DataManagementSettings: sectionStub('data-management'),
          AiSettings: sectionStub('ai'),
          BotNotifySettings: sectionStub('bot'),
          SmtpSettings: sectionStub('smtp'),
          AboutSettings: sectionStub('about'),
        },
      },
    })

    const columns = wrapper.findAll('.grid > .flex.flex-col')
    expect(columns).toHaveLength(2)

    const leftIds = columns[0]
      .findAll('[data-section]')
      .map((node) => node.attributes('data-section'))
    const rightIds = columns[1]
      .findAll('[data-section]')
      .map((node) => node.attributes('data-section'))

    expect(leftIds).toEqual(['general', 'proxy', 'telegram-api', 'data-management'])
    expect(rightIds).toEqual(['ai', 'bot', 'smtp', 'about'])
    expect(wrapper.find('.grid').classes()).toContain('items-start')

    wrapper.unmount()
  })
})

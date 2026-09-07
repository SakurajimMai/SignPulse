/**
 * 系统设置页业务逻辑：加载/保存/脏检查/备份/版本检查。
 * Settings.vue 仅负责布局与子组件接线。
 */
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  getGlobalSettings,
  getTelegramConfig,
  getAIConfig,
  getRuntimeStatus,
  getMemoryStats,
  getTelegramBotRuntimeStatus,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import type {
  RuntimeStatus,
  MemoryStatsResponse,
  TelegramBotRuntimeStatus,
} from '../lib/api'
import { useI18n } from './useI18n'
import { useToast } from './useToast'
import { useConfirm } from './useConfirm'
import { notifyApiError, resolveApiErrorMessage } from '../lib/notify'
import { devLog } from '../lib/devLog'
import {
  applyGlobalSettingsToForm,
  buildAdvancedPayload as buildAdvancedPayloadOf,
  buildAiRuntimePayload as buildAiRuntimePayloadOf,
  buildBackupPayload as buildBackupPayloadOf,
  buildBotPayload as buildBotPayloadOf,
  buildGeneralPayload as buildGeneralPayloadOf,
  buildProxyPayload as buildProxyPayloadOf,
  buildSmtpPayload as buildSmtpPayloadOf,
  emptySmtpForm,
  dirtySectionLabels,
  isAnySectionDirty,
  snapAllSections,
  type SettingsSection,
  type SettingsFormState,
  type TgFormState,
  type AiFormState,
} from '../lib/settings-form'
import { useSettingsVersionCheck } from './useSettingsVersionCheck'
import { useSettingsBackup } from './useSettingsBackup'
import { useSettingsSave } from './useSettingsSave'

const BOT_RUNTIME_POLL_ATTEMPTS = 6
const BOT_RUNTIME_POLL_INTERVAL_MS = 500
const BOT_RUNTIME_TERMINAL_STATES = new Set([
  'running',
  'disabled',
  'unconfigured',
  'standby',
  'webhook_conflict',
  'polling_conflict',
  'error',
])

export function useSettingsPage() {
  const { t } = useI18n()
  const toast = useToast()
  const { confirm } = useConfirm()

  const settings = ref<SettingsFormState>({
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
    botTaskFailure: false,
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
    execTimeout: '' as string | number,
    accountCooldown: '' as string | number,
    flowRetry: '' as string | number,
    historyMaxAge: '' as string | number,
    aiVisionTimeout: '' as string | number,
    aiVisionRetry: '' as string | number,
    autoBackupEnabled: false,
    autoBackupInterval: 24,
    autoBackupKeep: 3,
    webdavUrl: '',
    webdavUsername: '',
    webdavPassword: '',
    webdavRemoteDir: 'tg-signpulse-backups',
    ...emptySmtpForm(),
  })

  // 时区选项列表
  const timezoneOptions = [
    { label: 'Asia/Shanghai (UTC+8)', value: 'Asia/Shanghai' },
    { label: 'Asia/Hong_Kong (UTC+8)', value: 'Asia/Hong_Kong' },
    { label: 'Asia/Tokyo (UTC+9)', value: 'Asia/Tokyo' },
    { label: 'Asia/Seoul (UTC+9)', value: 'Asia/Seoul' },
    { label: 'Asia/Singapore (UTC+8)', value: 'Asia/Singapore' },
    { label: 'Asia/Taipei (UTC+8)', value: 'Asia/Taipei' },
    { label: 'Asia/Bangkok (UTC+7)', value: 'Asia/Bangkok' },
    { label: 'Asia/Dubai (UTC+4)', value: 'Asia/Dubai' },
    { label: 'Asia/Kolkata (UTC+5:30)', value: 'Asia/Kolkata' },
    { label: 'Australia/Sydney (UTC+10/+11)', value: 'Australia/Sydney' },
    { label: 'America/New_York (UTC-5/-4)', value: 'America/New_York' },
    { label: 'America/Chicago (UTC-6/-5)', value: 'America/Chicago' },
    { label: 'America/Denver (UTC-7/-6)', value: 'America/Denver' },
    { label: 'America/Los_Angeles (UTC-8/-7)', value: 'America/Los_Angeles' },
    { label: 'America/Sao_Paulo (UTC-3)', value: 'America/Sao_Paulo' },
    { label: 'Europe/London (UTC+0/+1)', value: 'Europe/London' },
    { label: 'Europe/Berlin (UTC+1/+2)', value: 'Europe/Berlin' },
    { label: 'Europe/Paris (UTC+1/+2)', value: 'Europe/Paris' },
    { label: 'Europe/Moscow (UTC+3)', value: 'Europe/Moscow' },
    { label: 'Africa/Cairo (UTC+2)', value: 'Africa/Cairo' },
    { label: 'Pacific/Auckland (UTC+12/+13)', value: 'Pacific/Auckland' },
    { label: 'UTC', value: 'UTC' },
  ]

  const tgConfig = ref<TgFormState>({
    api_id: '',
    api_hash: ''
  })

  const aiConfig = ref<AiFormState>({
    base_url: '',
    model: '',
    api_key: ''
  })
  /** 服务端 AI Key 解密失败标记（APP_SECRET_KEY 不匹配） */
  const aiKeyDecryptFailed = ref(false)

  const runtimeStatus = ref<RuntimeStatus | null>(null)
  const memoryStats = ref<MemoryStatsResponse | null>(null)
  const telegramBotRuntimeStatus = ref<TelegramBotRuntimeStatus | null>(null)
  const telegramBotRuntimeLoading = ref(false)
  const pageLoading = ref(true)
  // 卸载标记：异步加载期间离开页面时停止后续副作用
  let disposed = false
  const botTokenSet = ref(false)
  const proxyPasswordSet = ref(false)
  const smtpPasswordSet = ref(false)
  const afterBotTokenSaved = () => {
    if (settings.value.botToken) {
      botTokenSet.value = true
      settings.value.botToken = ''
    }
    revealSecrets.value = { ...revealSecrets.value, botToken: false }
    settingsSecretsLoaded.value = false
  }
  const afterSmtpPasswordSaved = () => {
    if (settings.value.smtpPassword) {
      smtpPasswordSet.value = true
      settings.value.smtpPassword = ''
    }
    revealSecrets.value = { ...revealSecrets.value, smtpPassword: false }
    settingsSecretsLoaded.value = false
  }
  const afterProxySettingsSaved = () => {
    if (settings.value.proxyClearCredentials) {
      proxyPasswordSet.value = false
    } else if (settings.value.proxyPassword) {
      proxyPasswordSet.value = true
    }
    settings.value.proxyPassword = ''
    settings.value.proxyClearCredentials = false
    revealSecrets.value = { ...revealSecrets.value, proxyPassword: false }
    settingsSecretsLoaded.value = false
  }
  /** 密钥字段显隐（默认隐藏） */
  const revealSecrets = ref({
    tgApiId: false,
    tgApiHash: false,
    aiKey: false,
    botToken: false,
    proxyPassword: false,
    smtpPassword: false,
    webdavPassword: false,
  })
  const settingsSecretsLoaded = ref(false)
  const aiSecretLoaded = ref(false)
  const secretsLoading = ref(false)

  /** 分段脏检查基线（分块保存只清对应段） */
  const sectionBaseline = ref<Record<SettingsSection, string> | null>(null)

  const currentSectionSnaps = () =>
    snapAllSections(settings.value, tgConfig.value, aiConfig.value)

  const markAllClean = () => {
    sectionBaseline.value = currentSectionSnaps()
  }

  const markSectionClean = (section: SettingsSection) => {
    if (!sectionBaseline.value) {
      markAllClean()
      return
    }
    sectionBaseline.value = {
      ...sectionBaseline.value,
      [section]: snapSectionFor(section),
    }
  }

  const snapSectionFor = (section: SettingsSection) =>
    currentSectionSnaps()[section]

  const isDirty = computed(() =>
    isAnySectionDirty(sectionBaseline.value, currentSectionSnaps()),
  )

  const proxyDirty = computed(() => {
    if (!sectionBaseline.value) return false
    return sectionBaseline.value.proxy !== snapSectionFor('proxy')
  })

  const dirtyLabels = computed(() =>
    dirtySectionLabels(sectionBaseline.value, currentSectionSnaps(), {
      general: t('settings.general'),
      proxy: t('settings.proxyTitle'),
      bot: t('settings.botNotify'),
      smtp: t('settings.smtpTitle'),
      // advanced 段仅含备份/WebDAV（数据管理）
      advanced: t('settings.dataManagement'),
      tg: t('settings.tgApi'),
      ai: t('settings.aiConfig'),
    }),
  )

  const onBeforeUnload = (e: BeforeUnloadEvent) => {
    if (!isDirty.value) return
    e.preventDefault()
    e.returnValue = ''
  }

  onBeforeRouteLeave(async () => {
    if (!isDirty.value) return true
    const ok = await confirm({
      title: t('settings.unsavedTitle'),
      message: t('settings.unsavedMessage'),
      confirmText: t('settings.leaveAnyway'),
      danger: true,
    })
    return ok
  })

  const {
    appVersion,
    versionLoading,
    checkLoading,
    versionBanner,
    loadVersion,
    handleCheckUpdate,
  } = useSettingsVersionCheck()

  const buildGeneralPayload = () => buildGeneralPayloadOf(settings.value)
  const buildProxyPayload = () => buildProxyPayloadOf(settings.value)
  const buildBotPayload = () => buildBotPayloadOf(settings.value)
  const buildAdvancedPayload = () => buildAdvancedPayloadOf(settings.value)
  const buildAiRuntimePayload = () => buildAiRuntimePayloadOf(settings.value)
  const buildBackupPayload = () => buildBackupPayloadOf(settings.value)
  const buildSmtpPayload = () => buildSmtpPayloadOf(settings.value)

  const loadTelegramBotRuntimeStatus = async (
    token: string,
    showFeedback: boolean,
  ): Promise<TelegramBotRuntimeStatus | null> => {
    try {
      const status = await getTelegramBotRuntimeStatus(token)
      if (!disposed) telegramBotRuntimeStatus.value = status
      return status
    } catch (error: unknown) {
      devLog.error('Failed to load Telegram Bot runtime status', error)
      if (showFeedback) {
        toast.error(resolveApiErrorMessage(error, 'settings.botRuntimeLoadFailed'))
      }
      return null
    }
  }

  const refreshTelegramBotRuntimeStatus = async (
    showFeedback = true,
    suppliedToken?: string,
  ) => {
    const token = suppliedToken || getAuthToken()
    if (!token || telegramBotRuntimeLoading.value) return
    telegramBotRuntimeLoading.value = true
    try {
      await loadTelegramBotRuntimeStatus(token, showFeedback)
    } finally {
      telegramBotRuntimeLoading.value = false
    }
  }

  const isTelegramBotRuntimeSettled = (status: TelegramBotRuntimeStatus) => {
    if (!BOT_RUNTIME_TERMINAL_STATES.has(status.state)) return false
    if (status.control_enabled !== settings.value.botControlEnabled) return false
    if (!settings.value.botControlEnabled) return status.state === 'disabled'
    if (status.configured !== botTokenSet.value) return false
    if (!botTokenSet.value) return status.state === 'unconfigured'
    return !['disabled', 'unconfigured'].includes(status.state)
  }

  const pollTelegramBotRuntimeStatusAfterSave = async (token: string) => {
    telegramBotRuntimeLoading.value = true
    try {
      for (let attempt = 0; attempt < BOT_RUNTIME_POLL_ATTEMPTS; attempt += 1) {
        if (attempt > 0) {
          await new Promise<void>((resolve) => {
            window.setTimeout(resolve, BOT_RUNTIME_POLL_INTERVAL_MS)
          })
        }
        if (disposed) return
        const status = await loadTelegramBotRuntimeStatus(token, false)
        // The first response may still be the pre-save snapshot. Always wait for
        // one subsequent reconciliation sample before accepting a terminal state.
        if (attempt > 0 && status && isTelegramBotRuntimeSettled(status)) return
      }
    } finally {
      telegramBotRuntimeLoading.value = false
    }
  }

  const {
    dataLoading,
    backupLoading,
    backupStatus,
    webdavTestLoading,
    webdavListLoading,
    remoteWebdavFiles,
    remoteWebdavMessage,
    webdavPasswordSet,
    remoteDownloadName,
    afterWebdavSettingsSaved,
    handleExport,
    handleListRemoteBackups,
    handleDownloadRemoteBackup,
    handleBackupExport,
    handleWebdavTest,
    handleImportFile,
    loadBackupStatus,
  } = useSettingsBackup({
    settings,
    buildBackupPayload,
    markSectionClean: (section) => markSectionClean(section),
  })

  const {
    loading,
    proxyLoading,
    proxyTestLoading,
    botLoading,
    advancedLoading,
    saveAllLoading,
    tgLoading,
    aiLoading,
    botTestLoading,
    smtpLoading,
    smtpTestLoading,
    keepaliveLoading,
    saveSettings,
    saveProxySettings,
    runKeepaliveNow,
    saveBotSettings,
    saveSmtpSettings,
    saveAdvancedSettings,
    saveAllSettings,
    testBot,
    testProxy,
    testSmtp,
    saveTgConfig,
    resetTgConfig,
    saveAiConfig,
    testAi,
  } = useSettingsSave({
    tgConfig,
    aiConfig,
    aiKeyDecryptFailed,
    buildGeneralPayload,
    buildProxyPayload,
    buildBotPayload,
    buildAdvancedPayload,
    buildAiRuntimePayload,
    buildBackupPayload,
    buildSmtpPayload,
    markSectionClean,
    afterBotTokenSaved,
    afterProxySettingsSaved,
    afterBotSettingsSaved: pollTelegramBotRuntimeStatusAfterSave,
    afterSmtpPasswordSaved,
    afterWebdavSettingsSaved,
    loadBackupStatus,
  })

  onMounted(async () => {
    // 同步注册 beforeunload：避免异步加载期间卸载导致监听器永久泄漏
    window.addEventListener('beforeunload', onBeforeUnload)
    const token = getAuthToken()
    if (!token) {
      pageLoading.value = false
      return
    }

    try {
      const [res, tgRes, aiRes] = await Promise.all([
        getGlobalSettings(token),
        getTelegramConfig(token).catch(() => null),
        getAIConfig(token).catch(() => null)
      ])
      const flags = applyGlobalSettingsToForm(settings.value, res)
      botTokenSet.value = flags.botTokenSet
      proxyPasswordSet.value = flags.proxyPasswordSet
      webdavPasswordSet.value = flags.webdavPasswordSet
      smtpPasswordSet.value = flags.smtpPasswordSet

      if (tgRes && tgRes.is_custom) {
        tgConfig.value.api_id = tgRes.api_id
        tgConfig.value.api_hash = tgRes.api_hash
      }

      if (aiRes && aiRes.has_config) {
        aiConfig.value.base_url = aiRes.base_url || ''
        aiConfig.value.model = aiRes.model || ''
        aiKeyDecryptFailed.value = !!aiRes.api_key_decrypt_failed
      } else {
        aiKeyDecryptFailed.value = false
      }

      // 运行信息互不依赖，并行请求可以减少设置页首屏等待；单项失败仍然降级。
      const [backupResult, runtimeResult, memoryResult, botRuntimeResult, versionResult] =
        await Promise.allSettled([
          loadBackupStatus(token),
          getRuntimeStatus(token),
          getMemoryStats(token),
          getTelegramBotRuntimeStatus(token),
          loadVersion(token),
        ])
      if (backupResult.status === 'rejected') {
        devLog.error('Failed to load backup status', backupResult.reason)
      }
      if (runtimeResult.status === 'fulfilled') {
        runtimeStatus.value = runtimeResult.value
      } else {
        devLog.error('Failed to load runtime status', runtimeResult.reason)
      }
      if (memoryResult.status === 'fulfilled') {
        memoryStats.value = memoryResult.value
      } else {
        devLog.error('Failed to load memory stats', memoryResult.reason)
      }
      if (botRuntimeResult.status === 'fulfilled') {
        telegramBotRuntimeStatus.value = botRuntimeResult.value
      } else {
        devLog.error(
          'Failed to load Telegram Bot runtime status',
          botRuntimeResult.reason,
        )
      }
      if (versionResult.status === 'rejected') {
        devLog.error('Failed to load app version', versionResult.reason)
      }
      if (disposed) return // 加载期间已卸载：不再标记干净或注册监听
      markAllClean()
    } catch (e: unknown) {
      devLog.error('Failed to load settings', e)
      toast.error(resolveApiErrorMessage(e, 'settings.loadFailed'))
    } finally {
      if (!disposed) pageLoading.value = false
    }
  })

  onUnmounted(() => {
    disposed = true
    window.removeEventListener('beforeunload', onBeforeUnload)
  })


  type RevealKey =
    | 'tgApiId'
    | 'tgApiHash'
    | 'aiKey'
    | 'botToken'
    | 'proxyPassword'
    | 'smtpPassword'
    | 'webdavPassword'

  const fillSettingsSecrets = (res: {
    telegram_bot_token?: string | null
    proxy_password?: string | null
    smtp_password?: string | null
    webdav_password?: string | null
  }) => {
    if (!settings.value.botToken && res.telegram_bot_token) {
      settings.value.botToken = res.telegram_bot_token
    }
    if (!settings.value.proxyPassword && res.proxy_password) {
      settings.value.proxyPassword = res.proxy_password
    }
    if (!settings.value.smtpPassword && res.smtp_password) {
      settings.value.smtpPassword = res.smtp_password
    }
    if (!settings.value.webdavPassword && res.webdav_password) {
      settings.value.webdavPassword = res.webdav_password
    }
    settingsSecretsLoaded.value = true
  }

  const toggleReveal = async (key: RevealKey) => {
    if (revealSecrets.value[key]) {
      revealSecrets.value = { ...revealSecrets.value, [key]: false }
      return
    }
    const token = getAuthToken()
    if (key === 'aiKey' && !aiConfig.value.api_key && token && !aiSecretLoaded.value) {
      secretsLoading.value = true
      try {
        const aiRes = await getAIConfig(token, true)
        if (aiRes.api_key) aiConfig.value.api_key = aiRes.api_key
        aiSecretLoaded.value = true
      } catch (error: unknown) {
        notifyApiError(error, 'settings.loadFailed')
        return
      } finally {
        secretsLoading.value = false
      }
    } else if (
      key !== 'tgApiId' &&
      key !== 'tgApiHash' &&
      key !== 'aiKey' &&
      token &&
      !settingsSecretsLoaded.value
    ) {
      const field = key as 'botToken' | 'proxyPassword' | 'smtpPassword' | 'webdavPassword'
      const current = {
        botToken: settings.value.botToken,
        proxyPassword: settings.value.proxyPassword,
        smtpPassword: settings.value.smtpPassword,
        webdavPassword: settings.value.webdavPassword,
      }
      const saved = {
        botToken: botTokenSet.value,
        proxyPassword: proxyPasswordSet.value,
        smtpPassword: smtpPasswordSet.value,
        webdavPassword: webdavPasswordSet.value,
      }
      if (!current[field] && saved[field]) {
        secretsLoading.value = true
        try {
          fillSettingsSecrets(await getGlobalSettings(token, true))
        } catch (error: unknown) {
          notifyApiError(error, 'settings.loadFailed')
          return
        } finally {
          secretsLoading.value = false
        }
      }
    }
    revealSecrets.value = { ...revealSecrets.value, [key]: true }
  }

  return {
    t,
    settings,
    timezoneOptions,
    tgConfig,
    aiConfig,
    aiKeyDecryptFailed,
    loading,
    proxyLoading,
    proxyTestLoading,
    tgLoading,
    aiLoading,
    dataLoading,
    backupLoading,
    backupStatus,
    runtimeStatus,
    memoryStats,
    telegramBotRuntimeStatus,
    telegramBotRuntimeLoading,
    advancedLoading,
    botTestLoading,
    smtpLoading,
    smtpTestLoading,
    pageLoading,
    revealSecrets,
    secretsLoading,
    isDirty,
    proxyDirty,
    dirtyLabels,
    appVersion,
    versionLoading,
    checkLoading,
    versionBanner,
    botLoading,
    keepaliveLoading,
    saveAllLoading,
    webdavTestLoading,
    webdavListLoading,
    remoteWebdavFiles,
    remoteWebdavMessage,
    webdavPasswordSet,
    botTokenSet,
    proxyPasswordSet,
    smtpPasswordSet,
    remoteDownloadName,
    saveSettings,
    saveProxySettings,
    runKeepaliveNow,
    saveBotSettings,
    saveSmtpSettings,
    saveAdvancedSettings,
    saveAllSettings,
    testBot,
    testProxy,
    testSmtp,
    saveTgConfig,
    resetTgConfig,
    saveAiConfig,
    testAi,
    handleExport,
    handleImportFile,
    handleBackupExport,
    handleWebdavTest,
    handleListRemoteBackups,
    handleDownloadRemoteBackup,
    handleCheckUpdate,
    refreshTelegramBotRuntimeStatus,
    toggleReveal,
  }
}

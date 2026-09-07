/**
 * 设置页：分块保存 / 全量保存 / TG·AI 保存与测试。
 */
import { ref, type Ref } from 'vue'
import {
  saveGlobalSettings,
  saveTelegramConfig,
  resetTelegramConfig,
  saveAIConfig,
  testAIConnection,
  runDeviceKeepalive,
  testBotNotification,
  testAlertMail,
  testProxyConnection,
} from '../lib/api'
import { withToken, getAuthToken } from '../lib/api/core'
import type { AiFormState, SettingsSection, TgFormState } from '../lib/settings-form'
import { resolveApiErrorMessage } from '../lib/notify'
import { devLog } from '../lib/devLog'
import { useI18n } from './useI18n'
import { useToast } from './useToast'
import { useConfirm } from './useConfirm'

export function useSettingsSave(options: {
  tgConfig: Ref<TgFormState>
  aiConfig: Ref<AiFormState>
  aiKeyDecryptFailed: Ref<boolean>
  buildGeneralPayload: () => Record<string, unknown>
  buildProxyPayload?: () => Record<string, unknown>
  buildBotPayload: () => Record<string, unknown>
  buildAdvancedPayload: () => Record<string, unknown>
  buildAiRuntimePayload: () => Record<string, unknown>
  buildBackupPayload: () => Record<string, unknown>
  buildSmtpPayload?: () => Record<string, unknown>
  markSectionClean: (section: SettingsSection) => void
  afterBotTokenSaved: () => void
  afterProxySettingsSaved?: () => void
  afterBotSettingsSaved?: (token: string) => Promise<void> | void
  afterSmtpPasswordSaved?: () => void
  afterWebdavSettingsSaved: () => void
  loadBackupStatus: (token: string) => Promise<void>
}) {
  const { t } = useI18n()
  const toast = useToast()
  const { confirm } = useConfirm()
  const notifySuccess = (msg: string) => toast.success(msg)
  const notifyError = (msg: string) => toast.error(msg)

  const loading = ref(false)
  const proxyLoading = ref(false)
  const proxyTestLoading = ref(false)
  const botLoading = ref(false)
  const advancedLoading = ref(false)
  const saveAllLoading = ref(false)
  const tgLoading = ref(false)
  const aiLoading = ref(false)
  const botTestLoading = ref(false)
  const smtpLoading = ref(false)
  const smtpTestLoading = ref(false)
  const keepaliveLoading = ref(false)

  const saveSettings = async () => {
    return withToken(async (token) => {
      loading.value = true
      try {
        await saveGlobalSettings(token, options.buildGeneralPayload())
        options.markSectionClean('general')
        notifySuccess(t('settings.saveSuccess'))
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        loading.value = false
      }
    })
  }

  const saveProxySettings = async () => {
    const buildProxyPayload = options.buildProxyPayload
    if (!buildProxyPayload) return
    return withToken(async (token) => {
      proxyLoading.value = true
      try {
        await saveGlobalSettings(token, buildProxyPayload())
        options.afterProxySettingsSaved?.()
        options.markSectionClean('proxy')
        notifySuccess(t('settings.proxySaveSuccess'))
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        proxyLoading.value = false
      }
    })
  }

  const testProxy = async () => {
    return withToken(async (token) => {
      proxyTestLoading.value = true
      try {
        const res = await testProxyConnection(token)
        if (res.success) {
          notifySuccess(res.message || t('settings.proxyTestSuccess'))
        } else {
          notifyError(res.message || t('settings.proxyTestFailed'))
        }
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.proxyTestFailed'))
      } finally {
        proxyTestLoading.value = false
      }
    })
  }

  const runKeepaliveNow = async () => {
    return withToken(async (token) => {
      keepaliveLoading.value = true
      try {
        const res = await runDeviceKeepalive(token)
        notifySuccess(
          `${t('settings.keepaliveDone')}：${res.kept_alive}/${res.checked}，${t('settings.failed')} ${res.failed}`,
        )
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.keepaliveFailed'))
      } finally {
        keepaliveLoading.value = false
      }
    })
  }

  const saveBotSettings = async () => {
    return withToken(async (token) => {
      botLoading.value = true
      try {
        await saveGlobalSettings(token, options.buildBotPayload())
        options.afterBotTokenSaved()
        options.markSectionClean('bot')
        await options.afterBotSettingsSaved?.(token)
        notifySuccess(t('settings.saveSuccess'))
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        botLoading.value = false
      }
    })
  }

  const saveSmtpSettings = async () => {
    const buildSmtpPayload = options.buildSmtpPayload
    if (!buildSmtpPayload) return
    return withToken(async (token) => {
      smtpLoading.value = true
      try {
        await saveGlobalSettings(token, buildSmtpPayload())
        options.afterSmtpPasswordSaved?.()
        options.markSectionClean('smtp')
        notifySuccess(t('settings.saveSuccess'))
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        smtpLoading.value = false
      }
    })
  }

  const saveAdvancedSettings = async () => {
    return withToken(async (token) => {
      advancedLoading.value = true
      try {
        await saveGlobalSettings(token, options.buildBackupPayload())
        options.afterWebdavSettingsSaved()
        options.markSectionClean('advanced')
        notifySuccess(t('settings.saveSuccess'))
        try {
          await options.loadBackupStatus(token)
        } catch {
          /* ignore */
        }
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        advancedLoading.value = false
      }
    })
  }

  const saveAllSettings = async () => {
    return withToken(async (token) => {
      saveAllLoading.value = true
      const partial: string[] = []
      try {
        await saveGlobalSettings(token, {
          ...options.buildGeneralPayload(),
          ...(options.buildProxyPayload ? options.buildProxyPayload() : {}),
          ...options.buildBotPayload(),
          ...(options.buildSmtpPayload ? options.buildSmtpPayload() : {}),
          ...options.buildAdvancedPayload(),
        })
        options.afterWebdavSettingsSaved()
        options.afterBotTokenSaved()
        options.afterProxySettingsSaved?.()
        options.afterSmtpPasswordSaved?.()
        options.markSectionClean('general')
        options.markSectionClean('proxy')
        options.markSectionClean('bot')
        options.markSectionClean('smtp')
        options.markSectionClean('advanced')
        await options.afterBotSettingsSaved?.(token)
        if (options.tgConfig.value.api_id && options.tgConfig.value.api_hash) {
          try {
            await saveTelegramConfig(token, {
              api_id: options.tgConfig.value.api_id,
              api_hash: options.tgConfig.value.api_hash,
            })
            options.markSectionClean('tg')
          } catch (e: unknown) {
            partial.push(t('settings.tgApi'))
            devLog.error('saveAll tg failed', e)
          }
        } else {
          options.markSectionClean('tg')
        }
        if (
          options.aiConfig.value.base_url ||
          options.aiConfig.value.model ||
          options.aiConfig.value.api_key
        ) {
          try {
            await saveAIConfig(token, {
              base_url: options.aiConfig.value.base_url || undefined,
              model: options.aiConfig.value.model || undefined,
              api_key: options.aiConfig.value.api_key || undefined,
            })
            options.aiConfig.value.api_key = ''
            options.markSectionClean('ai')
          } catch (e: unknown) {
            partial.push(t('settings.aiConfig'))
            devLog.error('saveAll ai failed', e)
          }
        } else {
          options.markSectionClean('ai')
        }
        if (partial.length) {
          notifyError(`${t('settings.saveAllPartial')}: ${partial.join(', ')}`)
        } else {
          notifySuccess(t('settings.saveAllSuccess'))
        }
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
      } finally {
        saveAllLoading.value = false
      }
    })
  }

  const testBot = async () => {
    return withToken(async (token) => {
      botTestLoading.value = true
      try {
        const res = await testBotNotification(token)
        if (res.success) notifySuccess(res.message)
        else notifyError(res.message)
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.testFailed'))
      } finally {
        botTestLoading.value = false
      }
    })
  }

  const testSmtp = async () => {
    return withToken(async (token) => {
      smtpTestLoading.value = true
      try {
        const res = await testAlertMail(token)
        if (res.success) notifySuccess(res.message)
        else notifyError(res.message)
      } catch (e: unknown) {
        notifyError(resolveApiErrorMessage(e, 'settings.testFailed'))
      } finally {
        smtpTestLoading.value = false
      }
    })
  }

  const saveTgConfig = async () => {
    const token = getAuthToken()
    tgLoading.value = true
    try {
      await saveTelegramConfig(token, {
        api_id: options.tgConfig.value.api_id,
        api_hash: options.tgConfig.value.api_hash,
      })
      options.markSectionClean('tg')
      notifySuccess(t('settings.tgConfigSaved'))
    } catch (e: unknown) {
      notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
    } finally {
      tgLoading.value = false
    }
  }

  const resetTgConfig = async () => {
    const token = getAuthToken()
    const ok = await confirm({
      title: t('settings.resetDefault'),
      message: t('settings.resetConfirm'),
      confirmText: t('common.continue'),
      danger: true,
    })
    if (!ok) return
    tgLoading.value = true
    try {
      await resetTelegramConfig(token)
      options.tgConfig.value.api_id = ''
      options.tgConfig.value.api_hash = ''
      options.markSectionClean('tg')
      notifySuccess(t('settings.resetSuccess'))
    } catch (e: unknown) {
      notifyError(resolveApiErrorMessage(e, 'settings.resetFailed'))
    } finally {
      tgLoading.value = false
    }
  }

  const saveAiConfig = async () => {
    const token = getAuthToken()
    aiLoading.value = true
    try {
      await saveGlobalSettings(token, options.buildAiRuntimePayload())
      const hasAiInput = !!(
        options.aiConfig.value.base_url ||
        options.aiConfig.value.model ||
        options.aiConfig.value.api_key
      )
      try {
        await saveAIConfig(token, {
          base_url: options.aiConfig.value.base_url || undefined,
          model: options.aiConfig.value.model || undefined,
          api_key: options.aiConfig.value.api_key || undefined,
        })
        if (options.aiConfig.value.api_key) {
          options.aiKeyDecryptFailed.value = false
        }
        options.aiConfig.value.api_key = ''
      } catch (e: unknown) {
        if (hasAiInput) throw e
        devLog.error('saveAi model skipped (runtime-only or no key yet)', e)
      }
      options.markSectionClean('ai')
      notifySuccess(t('settings.aiConfigSaved'))
    } catch (e: unknown) {
      notifyError(resolveApiErrorMessage(e, 'settings.saveFailed'))
    } finally {
      aiLoading.value = false
    }
  }

  const testAi = async () => {
    const token = getAuthToken()
    aiLoading.value = true
    try {
      const res = await testAIConnection(token)
      if (res.success) {
        notifySuccess(res.message || t('settings.testSuccess'))
      } else {
        notifyError(res.message || t('settings.testFailed'))
      }
    } catch (e: unknown) {
      notifyError(resolveApiErrorMessage(e, 'settings.testFailed'))
    } finally {
      aiLoading.value = false
    }
  }

  return {
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
  }
}

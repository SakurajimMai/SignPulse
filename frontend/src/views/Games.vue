<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { CheckCircle2, LoaderCircle, Radio, RefreshCw, Sparkles, XCircle } from 'lucide-vue-next'
import {
  getGamesSettings,
  getGamesStatus,
  refreshGamesClouds,
  retryGamesAutomation,
  saveGamesSettings,
  scanGamesAutomation,
  type GamesRuntimeStatus,
  type GamesSettings,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { getErrorMessage } from '../lib/types'
import { formatShortDateTime } from '../lib/datetime'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import { useAccountsStore } from '../stores/accounts'
import { useSecretReveal } from '../composables/useSecretReveal'
import GamesPanel from '../components/games/GamesPanel.vue'
import SecretInput from '../components/SecretInput.vue'

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const accountsStore = useAccountsStore()

const settings = ref<GamesSettings | null>(null)
const status = ref<GamesRuntimeStatus | null>(null)
const pageLoading = ref(true)
const saving = ref(false)
const automationBusy = ref('')
const keepaliveBusy = ref(false)
const errorMessage = ref('')
const {
  draft: secretDraft,
  reveal: revealSecrets,
  loading: secretsLoading,
  toggle: toggleSecret,
  reset: resetSecrets,
} = useSecretReveal(
  () => ({
    wp_app_password: '',
    baidu_cookie: '',
    pikpak_password: '',
    pikpak_refresh_token: '',
    terabox_cookie: '',
    quark_cookie: '',
    openlist_token: '',
  }),
  {
    isSaved: (key) => Boolean(settings.value?.[`${key}_set` as keyof GamesSettings]),
    fetchRevealed: () => getGamesSettings(token!, true),
    onError: (error) => notifyApiError(error, 'games.loadFailed'),
  },
)
let pollTimer: number | undefined

const applySettings = (value: GamesSettings, keepDrafts = false) => {
  settings.value = { ...value }
  if (!keepDrafts) resetSecrets()
}

const selectedAccountMissing = computed(() => {
  const name = settings.value?.telegram_account_name
  if (!name) return false
  return !accountsStore.accounts.some((item) => item.name === name)
})

const configured = computed(() => Boolean(status.value?.configured || status.value?.wp?.ok))
const wpOk = computed(() => Boolean(status.value?.wp?.ok))
const sevenOk = computed(() => Boolean(status.value?.sevenzip?.ok))
const apateOk = computed(() => Boolean(status.value?.apate_tool?.ok))
const jobRunning = computed(() => Boolean(status.value?.running))
const automation = computed(() => status.value?.automation)
const automationReady = computed(() => Boolean(automation.value?.requirements?.ready))
const automationLabel = computed(() => {
  if (automation.value?.listening) return t('games.automationListening')
  if (automation.value?.enabled) return t('games.automationStopped')
  return t('games.automationDisabled')
})

const loadPage = async () => {
  if (!token) return
  pageLoading.value = true
  errorMessage.value = ''
  try {
    const [settingsResult, statusResult] = await Promise.all([
      getGamesSettings(token),
      getGamesStatus(token),
    ])
    applySettings(settingsResult)
    status.value = statusResult
  } catch (error: unknown) {
    errorMessage.value = t('games.loadFailed')
    notifyApiError(error, 'games.loadFailed')
  } finally {
    pageLoading.value = false
  }
}

const pollStatus = async () => {
  if (!token) return
  try {
    const next = await getGamesStatus(token, false)
    status.value = { ...(status.value || {}), ...next }
  } catch {
    /* keep last */
  }
}

const update = <K extends keyof GamesSettings>(key: K, next: GamesSettings[K]) => {
  if (!settings.value) return
  settings.value = { ...settings.value, [key]: next }
}

const parseOptionalAmount = (raw: string): number | null => {
  const text = raw.trim()
  if (!text) return null
  const value = Number(text)
  return Number.isFinite(value) && value >= 0 ? value : null
}

const saveSettings = async () => {
  if (!token || !settings.value) return
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      wp_url: settings.value.wp_url || '',
      wp_user: settings.value.wp_user || '',
      wp_default_categories: settings.value.wp_default_categories || '',
      wp_default_tags: settings.value.wp_default_tags || '',
      wp_pay_enabled: Boolean(settings.value.wp_pay_enabled),
      wp_pay_modo: settings.value.wp_pay_modo || '0',
      wp_pay_price: Number(settings.value.wp_pay_price || 0),
      wp_points_price: Number(settings.value.wp_points_price || 0),
      wp_vip1_price: settings.value.wp_vip1_price ?? null,
      wp_vip2_price: settings.value.wp_vip2_price ?? null,
      wp_vip1_points: settings.value.wp_vip1_points ?? null,
      wp_vip2_points: settings.value.wp_vip2_points ?? null,
      wp_pay_extra_template: settings.value.wp_pay_extra_template || '',
      wp_apate_url: settings.value.wp_apate_url || '',
      wp_status: settings.value.wp_status || 'publish',
      telegram_account_name: settings.value.telegram_account_name || '',
      telegram_source_channels: settings.value.telegram_source_channels || '',
      auto_publish_enabled: Boolean(settings.value.auto_publish_enabled),
      telegram_poll_seconds: Number(settings.value.telegram_poll_seconds || 30),
      telegram_backfill_limit: Number(settings.value.telegram_backfill_limit || 0),
      auto_retry_limit: Number(settings.value.auto_retry_limit || 3),
      extract_passwords: settings.value.extract_passwords || '',
      pack_password: settings.value.pack_password || '',
      split_volume_mb: Number(settings.value.split_volume_mb || 4096),
      ad_keywords: settings.value.ad_keywords || '',
      apate_enabled: Boolean(settings.value.apate_enabled),
      apate_bin: settings.value.apate_bin || 'apate',
      baidu_enabled: Boolean(settings.value.baidu_enabled),
      baidu_remote_dir: settings.value.baidu_remote_dir || '',
      pikpak_username: settings.value.pikpak_username || '',
      pikpak_folder_id: settings.value.pikpak_folder_id || '',
      pikpak_remote_dir: settings.value.pikpak_remote_dir || '',
      terabox_remote_dir: settings.value.terabox_remote_dir || '',
      quark_folder_id: settings.value.quark_folder_id || '',
      quark_remote_dir: settings.value.quark_remote_dir || '',
      openlist_url: settings.value.openlist_url || '',
      openlist_baidu_path: settings.value.openlist_baidu_path || '',
      openlist_pikpak_path: settings.value.openlist_pikpak_path || '',
      openlist_terabox_path: settings.value.openlist_terabox_path || '',
      openlist_quark_path: settings.value.openlist_quark_path || '',
      cleanup_after_publish: Boolean(settings.value.cleanup_after_publish),
      ai_enabled: Boolean(settings.value.ai_enabled),
      ai_refine_prompt: settings.value.ai_refine_prompt || '',
      ai_skip_non_games: settings.value.ai_skip_non_games !== false,
    }
    const secrets = secretDraft.value
    ;(Object.keys(secrets) as Array<keyof typeof secrets>).forEach((key) => {
      const value = secrets[key].trim()
      if (value) payload[key] = value
    })
    const result = await saveGamesSettings(token, payload)
    applySettings(result.settings, true)
    toast.success(t('games.saved'))
    status.value = await getGamesStatus(token, true)
  } catch (error: unknown) {
    notifyApiError(error, 'games.saveFailed')
  } finally {
    saving.value = false
  }
}

const scanAutomation = async () => {
  if (!token) return
  automationBusy.value = 'scan'
  try {
    const next = await scanGamesAutomation(token)
    status.value = { ...(status.value || {}), automation: next }
    toast.success(t('games.automationScanRequested'))
  } catch (error: unknown) {
    notifyApiError(error, 'games.loadFailed')
  } finally {
    automationBusy.value = ''
  }
}

const keepalive = computed(() => status.value?.keepalive)
const cloudKeepaliveItems = computed(() => {
  const results = keepalive.value?.results || {}
  const labels = {
    baidu: t('games.linkBaidu'),
    pikpak: t('games.linkPikpak'),
    terabox: t('games.linkTerabox'),
    quark: t('games.linkQuark'),
  } as const
  return (Object.keys(labels) as Array<keyof typeof labels>).map((key) => ({
    key,
    label: labels[key],
    ...results[key],
  }))
})

const refreshKeepalive = async () => {
  if (!token) return
  keepaliveBusy.value = true
  try {
    const result = await refreshGamesClouds(token)
    status.value = { ...(status.value || {}), keepalive: result.keepalive }
    toast.success(result.message || t('games.cloudKeepaliveDone'))
  } catch (error: unknown) {
    notifyApiError(error, 'games.cloudKeepaliveFailed')
  } finally {
    keepaliveBusy.value = false
  }
}

const retryAutomation = async () => {
  if (!token) return
  automationBusy.value = 'retry'
  try {
    const next = await retryGamesAutomation(token)
    status.value = { ...(status.value || {}), automation: next }
    toast.success(t('games.automationRetryRequested'))
  } catch (error: unknown) {
    notifyApiError(error, 'games.loadFailed')
  } finally {
    automationBusy.value = ''
  }
}

onMounted(() => {
  void loadPage()
  void accountsStore.ensureAccounts().catch(() => undefined)
  pollTimer = window.setInterval(() => {
    if (status.value?.running || status.value?.automation?.enabled) void pollStatus()
  }, 2000)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="space-y-6">
    <div v-if="pageLoading" class="ui-card p-8 text-sm text-gray-500">{{ t('common.loading') }}</div>
    <template v-else>
      <div>
        <div class="ui-section-label mb-2">{{ t('games.eyebrow') }} / {{ t('nav.games') }}</div>
        <h2 class="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100">{{ t('games.title') }}</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2 max-w-3xl">{{ t('games.pageHint') }}</p>
      </div>

      <div v-if="errorMessage" class="border border-rose-200 dark:border-rose-800/40 bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-300 px-4 py-3 text-sm" role="alert">
        {{ errorMessage }}
      </div>

      <div class="grid grid-cols-2 xl:grid-cols-5 gap-3">
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('games.wp') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="wpOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'">
            <CheckCircle2 v-if="wpOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ wpOk ? t('games.connected') : (configured ? t('games.notConfigured') : t('games.notConfigured')) }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('games.sevenZip') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="sevenOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            <CheckCircle2 v-if="sevenOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ sevenOk ? t('games.connected') : t('games.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('games.apate') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="apateOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            <CheckCircle2 v-if="apateOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ apateOk ? t('games.connected') : t('games.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('games.job') }}</div>
          <div class="mt-2 text-sm font-medium text-gray-800 dark:text-gray-100">
            {{ jobRunning ? status?.stage : (status?.stage || t('games.notConfigured')) }}
          </div>
          <p v-if="status?.message" class="mt-1 text-[11px] text-gray-500 truncate">{{ status.message }}</p>
        </div>
        <div class="ui-card p-4 col-span-2 xl:col-span-1">
          <div class="ui-section-label">{{ t('games.autoPublish') }}</div>
          <div
            class="mt-2 flex items-center gap-2 text-sm font-medium"
            :class="automation?.listening
              ? 'text-emerald-600 dark:text-emerald-400'
              : automation?.enabled
                ? 'text-amber-600 dark:text-amber-400'
                : 'text-gray-500'"
          >
            <Radio class="w-4 h-4" aria-hidden="true" />
            {{ automationLabel }}
          </div>
          <p class="mt-1 text-[11px] text-gray-500 truncate">
            {{ t('games.automationQueue', { queued: automation?.queued || 0, failed: automation?.failed || 0 }) }}
          </p>
        </div>
      </div>

      <section v-if="settings" class="ui-card p-5 sm:p-6 space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.connection') }}</div>
            <p class="text-[10px] text-gray-500 mt-1">{{ t('games.configHint') }}</p>
          </div>
          <button type="button" class="ui-btn-primary !px-3 !py-2 !text-xs shrink-0" :disabled="saving" @click="saveSettings">
            <LoaderCircle v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            {{ t('common.save') }}
          </button>
        </div>
        <div class="rounded-lg border border-[var(--sp-border)] p-3 space-y-2">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.cloudKeepaliveTitle') }}</div>
              <p class="text-[11px] text-gray-500 mt-1">{{ t('games.cloudKeepaliveHint', { hours: keepalive?.interval_hours || 6 }) }}</p>
              <p v-if="keepalive?.last_run_at" class="text-[11px] text-gray-500 mt-1">
                {{ t('games.cloudKeepaliveLast', { time: formatShortDateTime(keepalive.last_run_at) }) }}
              </p>
            </div>
            <button type="button" class="ui-btn-secondary !px-3 !py-2 !text-xs shrink-0" :disabled="keepaliveBusy" @click="refreshKeepalive">
              <LoaderCircle v-if="keepaliveBusy" class="w-3.5 h-3.5 animate-spin" />
              {{ t('games.cloudKeepaliveNow') }}
            </button>
          </div>
          <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
            <div v-for="item in cloudKeepaliveItems" :key="item.key" class="text-[11px] rounded-md bg-gray-50 dark:bg-gray-900/40 px-3 py-2">
              <div class="font-medium text-gray-800 dark:text-gray-100">{{ item.label }}</div>
              <div
                class="mt-1"
                :class="item.skipped ? 'text-gray-500' : item.ok === false ? 'text-rose-600 dark:text-rose-400' : item.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'"
              >
                {{
                  item.skipped
                    ? t('games.cloudKeepaliveSkipped')
                    : item.ok === false
                      ? t('games.cloudKeepaliveFailed')
                      : item.ok
                        ? (item.refreshed ? t('games.cloudKeepaliveRefreshed') : t('games.cloudKeepaliveOk'))
                        : t('games.cloudKeepaliveIdle')
                }}
              </div>
              <p v-if="item.message && item.ok === false" class="mt-1 text-rose-600/80 dark:text-rose-400/80 line-clamp-2">{{ item.message }}</p>
            </div>
          </div>
        </div>
        <div class="border-y border-[var(--sp-border)] py-4 space-y-3">
          <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
            <div>
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.automationTitle') }}</div>
              <p class="text-[11px] leading-relaxed text-gray-500 mt-1">{{ t('games.automationHint') }}</p>
            </div>
            <label class="inline-flex min-h-11 items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer shrink-0">
              <input
                type="checkbox"
                class="rounded border-gray-300"
                :checked="settings.auto_publish_enabled"
                @change="update('auto_publish_enabled', ($event.target as HTMLInputElement).checked)"
              >
              {{ t('games.autoPublishEnabled') }}
            </label>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
            <div class="space-y-1 md:col-span-2">
              <label class="ui-label">{{ t('games.telegramChannels') }}</label>
              <input
                :value="settings.telegram_source_channels"
                class="ui-input"
                placeholder="@Zhzbzx"
                @input="update('telegram_source_channels', ($event.target as HTMLInputElement).value)"
              >
            </div>
            <div class="space-y-1">
              <label class="ui-label">{{ t('games.telegramPollSeconds') }}</label>
              <input
                :value="settings.telegram_poll_seconds || 30"
                type="number"
                min="10"
                max="3600"
                class="ui-input"
                @input="update('telegram_poll_seconds', Number(($event.target as HTMLInputElement).value) || 30)"
              >
            </div>
            <div class="space-y-1">
              <label class="ui-label">{{ t('games.autoRetryLimit') }}</label>
              <input
                :value="settings.auto_retry_limit || 3"
                type="number"
                min="1"
                max="10"
                class="ui-input"
                @input="update('auto_retry_limit', Number(($event.target as HTMLInputElement).value) || 3)"
              >
            </div>
            <div class="space-y-1 md:col-span-2">
              <label class="ui-label">{{ t('games.telegramBackfillLimit') }}</label>
              <input
                :value="settings.telegram_backfill_limit || 0"
                type="number"
                min="0"
                max="100"
                class="ui-input"
                @input="update('telegram_backfill_limit', Number(($event.target as HTMLInputElement).value) || 0)"
              >
              <p class="text-[10px] text-gray-500">{{ t('games.telegramBackfillHint') }}</p>
            </div>
            <!-- 用同结构的标签占位，让按钮与输入框对齐，而不是被提示文案顶到格子底部 -->
            <div class="space-y-1 md:col-span-2">
              <span class="ui-label hidden md:block invisible select-none" aria-hidden="true">&nbsp;</span>
              <div class="flex flex-wrap items-stretch gap-2">
                <button
                  type="button"
                  class="ui-btn-secondary !px-3 !text-xs min-h-[2.375rem] whitespace-nowrap"
                  :disabled="automationBusy !== '' || !automation?.enabled"
                  @click="scanAutomation"
                >
                  <LoaderCircle v-if="automationBusy === 'scan'" class="w-3.5 h-3.5 animate-spin" />
                  <RefreshCw v-else class="w-3.5 h-3.5" aria-hidden="true" />
                  {{ t('games.automationScanNow') }}
                </button>
                <button
                  type="button"
                  class="ui-btn-secondary !px-3 !text-xs min-h-[2.375rem] whitespace-nowrap"
                  :disabled="automationBusy !== '' || !automation?.failed"
                  @click="retryAutomation"
                >
                  <LoaderCircle v-if="automationBusy === 'retry'" class="w-3.5 h-3.5 animate-spin" />
                  <RefreshCw v-else class="w-3.5 h-3.5" aria-hidden="true" />
                  {{ t('games.automationRetryFailed') }}
                </button>
              </div>
            </div>
          </div>
          <div
            class="text-xs leading-relaxed"
            :class="automationReady ? 'text-gray-500' : 'text-amber-600 dark:text-amber-400'"
            role="status"
          >
            <template v-if="automationReady">
              {{ t('games.automationStatus', {
                status: automationLabel,
                processed: automation?.processed || 0,
                queued: automation?.queued || 0,
                failed: automation?.failed || 0,
              }) }}
            </template>
            <template v-else>
              {{ automation?.requirements?.message || t('games.automationNotReady') }}
            </template>
          </div>
          <p v-if="automation?.last_error" class="text-xs text-rose-600 dark:text-rose-400 break-words" role="alert">
            {{ getErrorMessage(automation.last_error) }}
          </p>
          <p v-if="automation?.disk" class="text-[11px] text-gray-500">
            {{ t('games.automationDiskFree', { free: automation.disk.free_gb }) }}
          </p>
          <div v-if="automation?.recent?.length" class="border-t border-[var(--sp-border)] pt-3 space-y-2">
            <div class="ui-label">{{ t('games.automationRecent') }}</div>
            <div
              v-for="item in automation.recent"
              :key="item.source_key || item.url"
              class="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3 text-xs"
            >
              <div class="min-w-0">
                <a
                  v-if="item.url"
                  :href="item.url"
                  target="_blank"
                  rel="noreferrer"
                  class="block truncate text-gray-700 hover:text-sky-600 dark:text-gray-300 dark:hover:text-sky-400"
                >
                  {{ item.caption || item.url }}
                </a>
                <p v-if="item.error" class="mt-0.5 truncate text-rose-600 dark:text-rose-400">{{ item.error }}</p>
              </div>
              <span class="whitespace-nowrap text-gray-500">
                {{ t(`games.automationStates.${item.status || 'pending'}`) }} · {{ item.attempts || 0 }}
              </span>
            </div>
          </div>
        </div>
        <div class="rounded-lg border border-[var(--sp-border)] p-3 space-y-3">
          <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
            <div class="min-w-0">
              <div class="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">
                <Sparkles class="w-4 h-4 text-sky-500" aria-hidden="true" />
                {{ t('games.aiTitle') }}
              </div>
              <p class="text-[11px] leading-relaxed text-gray-500 mt-1">{{ t('games.aiHint') }}</p>
            </div>
            <label class="inline-flex min-h-11 items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer shrink-0">
              <input
                type="checkbox"
                class="rounded border-gray-300"
                :checked="Boolean(settings.ai_enabled)"
                @change="update('ai_enabled', ($event.target as HTMLInputElement).checked)"
              >
              {{ t('games.aiEnabled') }}
            </label>
          </div>
          <p v-if="settings.ai_enabled && !settings.ai_model_configured" class="text-xs text-amber-600 dark:text-amber-400">
            {{ t('games.aiNeedModel') }}
            <RouterLink to="/settings" class="underline underline-offset-2">{{ t('games.aiGoSettings') }}</RouterLink>
          </p>
          <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
            <input
              type="checkbox"
              class="rounded border-gray-300"
              :checked="settings.ai_skip_non_games !== false"
              :disabled="!settings.ai_enabled"
              @change="update('ai_skip_non_games', ($event.target as HTMLInputElement).checked)"
            >
            {{ t('games.aiSkipNonGames') }}
          </label>
          <div class="space-y-1">
            <div class="flex items-center justify-between gap-2">
              <label class="ui-label">{{ t('games.aiPrompt') }}</label>
              <button
                type="button"
                class="text-[11px] text-sky-600 hover:underline disabled:text-gray-400"
                :disabled="!settings.ai_refine_prompt"
                @click="update('ai_refine_prompt', '')"
              >
                {{ t('games.aiPromptReset') }}
              </button>
            </div>
            <textarea
              :value="settings.ai_refine_prompt || ''"
              rows="8"
              class="ui-input min-h-[9rem] font-mono text-[12px] leading-relaxed"
              :placeholder="settings.ai_refine_prompt_default || t('games.aiPromptPlaceholder')"
              :disabled="!settings.ai_enabled"
              @input="update('ai_refine_prompt', ($event.target as HTMLTextAreaElement).value)"
            ></textarea>
            <p class="text-[10px] text-gray-500">{{ t('games.aiPromptHint') }}</p>
          </div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpUrl') }}</label><input :value="settings.wp_url" class="ui-input" @input="update('wp_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpUser') }}</label><input :value="settings.wp_user" class="ui-input" @input="update('wp_user', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.wpPassword') }}</label>
            <SecretInput v-model="secretDraft.wp_app_password" :revealed="revealSecrets.wp_app_password" :loading="secretsLoading" :placeholder="settings.wp_app_password_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('wp_app_password')" />
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.telegramAccount') }}</label>
            <select
              class="ui-input"
              :value="settings.telegram_account_name || ''"
              @change="update('telegram_account_name', ($event.target as HTMLSelectElement).value)"
            >
              <option value="">{{ t('games.selectAccount') }}</option>
              <option
                v-if="selectedAccountMissing && settings.telegram_account_name"
                :value="settings.telegram_account_name"
              >
                {{ settings.telegram_account_name }}
              </option>
              <option v-for="account in accountsStore.accounts" :key="account.name" :value="account.name">
                {{ account.name }}{{ account.remark ? ` · ${account.remark}` : '' }}
              </option>
            </select>
            <p v-if="!accountsStore.accounts.length" class="text-[10px] text-amber-600 dark:text-amber-400">
              {{ t('games.noAccounts') }}
            </p>
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpCategories') }}</label><input :value="settings.wp_default_categories" class="ui-input" @input="update('wp_default_categories', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpTags') }}</label><input :value="settings.wp_default_tags" class="ui-input" @input="update('wp_default_tags', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpPayModo') }}</label>
            <select :value="settings.wp_pay_modo || '0'" class="ui-input" @change="update('wp_pay_modo', ($event.target as HTMLSelectElement).value)">
              <option value="0">{{ t('games.wpPayBalance') }}</option>
              <option value="points">{{ t('games.wpPayPoints') }}</option>
            </select>
            <p class="text-[10px] text-gray-500">{{ t('games.payModoHint') }}</p>
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpPrice') }}</label><input :value="settings.wp_pay_price" type="number" min="0" step="0.01" class="ui-input" @input="update('wp_pay_price', Number(($event.target as HTMLInputElement).value) || 0)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpPointsPrice') }}</label><input :value="settings.wp_points_price" type="number" min="0" step="1" class="ui-input" @input="update('wp_points_price', Number(($event.target as HTMLInputElement).value) || 0)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpVip1Price') }}</label><input :value="settings.wp_vip1_price ?? ''" type="number" min="0" step="0.01" class="ui-input" :placeholder="t('games.wpVipEmpty')" @input="update('wp_vip1_price', parseOptionalAmount(($event.target as HTMLInputElement).value))"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpVip2Price') }}</label><input :value="settings.wp_vip2_price ?? ''" type="number" min="0" step="0.01" class="ui-input" :placeholder="t('games.wpVipEmpty')" @input="update('wp_vip2_price', parseOptionalAmount(($event.target as HTMLInputElement).value))"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpVip1Points') }}</label><input :value="settings.wp_vip1_points ?? ''" type="number" min="0" step="1" class="ui-input" :placeholder="t('games.wpVipEmpty')" @input="update('wp_vip1_points', parseOptionalAmount(($event.target as HTMLInputElement).value))"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpVip2Points') }}</label><input :value="settings.wp_vip2_points ?? ''" type="number" min="0" step="1" class="ui-input" :placeholder="t('games.wpVipEmpty')" @input="update('wp_vip2_points', parseOptionalAmount(($event.target as HTMLInputElement).value))"></div>
          <p class="text-[10px] text-gray-500 md:col-span-2">{{ t('games.wpVipHint') }}</p>
          <div class="space-y-1 md:col-span-2">
            <label class="ui-label">{{ t('games.wpPayExtra') }}</label>
            <textarea
              :value="settings.wp_pay_extra_template || ''"
              rows="6"
              class="ui-input min-h-[8rem] font-mono text-xs"
              :placeholder="t('games.wpPayExtraPlaceholder')"
              @input="update('wp_pay_extra_template', ($event.target as HTMLTextAreaElement).value)"
            />
            <p class="text-[10px] text-gray-500">{{ t('games.wpPayExtraHint') }}</p>
          </div>
          <div class="space-y-1 md:col-span-2">
            <label class="ui-label">{{ t('games.wpApateUrl') }}</label>
            <input :value="settings.wp_apate_url || ''" class="ui-input" placeholder="https://www.ixacg.top/16403.html" @input="update('wp_apate_url', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.wpStatus') }}</label>
            <select :value="settings.wp_status" class="ui-input" @change="update('wp_status', ($event.target as HTMLSelectElement).value)">
              <option value="publish">{{ t('games.statusPublish') }}</option>
              <option value="draft">{{ t('games.statusDraft') }}</option>
            </select>
          </div>
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 md:col-span-2">
            <input type="checkbox" class="rounded border-gray-300" :checked="settings.wp_pay_enabled" @change="update('wp_pay_enabled', ($event.target as HTMLInputElement).checked)">
            {{ t('games.wpPay') }}
          </label>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.extractPasswords') }}</label>
            <input
              :value="settings.extract_passwords || ''"
              type="text"
              autocomplete="off"
              spellcheck="false"
              class="ui-input"
              placeholder="sakuramai,ixacg.top"
              @input="update('extract_passwords', ($event.target as HTMLInputElement).value)"
            >
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.packPassword') }}</label>
            <input
              :value="settings.pack_password || ''"
              type="text"
              autocomplete="off"
              spellcheck="false"
              class="ui-input"
              placeholder="sakuramai"
              @input="update('pack_password', ($event.target as HTMLInputElement).value)"
            >
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.splitVolumeMb') }}</label>
            <input
              :value="settings.split_volume_mb || 4096"
              type="number"
              min="256"
              max="4096"
              step="1"
              class="ui-input"
              @input="update('split_volume_mb', Number(($event.target as HTMLInputElement).value) || 4096)"
            >
          </div>
          <div class="space-y-1 md:col-span-2"><label class="ui-label">{{ t('games.adKeywords') }}</label><input :value="settings.ad_keywords" class="ui-input" @input="update('ad_keywords', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.apateBin') }}</label><input :value="settings.apate_bin" class="ui-input" @input="update('apate_bin', ($event.target as HTMLInputElement).value)"></div>
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" class="rounded border-gray-300" :checked="settings.apate_enabled" @change="update('apate_enabled', ($event.target as HTMLInputElement).checked)">
            {{ t('games.apateEnabled') }}
          </label>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.baiduCookie') }}</label>
            <SecretInput v-model="secretDraft.baidu_cookie" :revealed="revealSecrets.baidu_cookie" :loading="secretsLoading" :placeholder="settings.baidu_cookie_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('baidu_cookie')" />
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.baiduDir') }}</label>
            <input :value="settings.baidu_remote_dir" class="ui-input" placeholder="/网站/ixacg/game" @input="update('baidu_remote_dir', ($event.target as HTMLInputElement).value)">
            <p class="text-[10px] text-gray-500">{{ t('games.baiduDirHint') }}</p>
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.pikpakUser') }}</label><input :value="settings.pikpak_username" class="ui-input" @input="update('pikpak_username', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.pikpakPassword') }}</label>
            <SecretInput v-model="secretDraft.pikpak_password" :revealed="revealSecrets.pikpak_password" :loading="secretsLoading" :placeholder="settings.pikpak_password_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('pikpak_password')" />
          </div>
          <div class="space-y-1 md:col-span-2">
            <label class="ui-label">{{ t('games.pikpakToken') }}</label>
            <SecretInput v-model="secretDraft.pikpak_refresh_token" :revealed="revealSecrets.pikpak_refresh_token" :loading="secretsLoading" :placeholder="settings.pikpak_refresh_token_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('pikpak_refresh_token')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.pikpakDir') }}</label><input :value="settings.pikpak_remote_dir" class="ui-input" @input="update('pikpak_remote_dir', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.pikpakFolderId') }}</label><input :value="settings.pikpak_folder_id" class="ui-input" @input="update('pikpak_folder_id', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.teraboxCookie') }}</label>
            <SecretInput v-model="secretDraft.terabox_cookie" :revealed="revealSecrets.terabox_cookie" :loading="secretsLoading" :placeholder="settings.terabox_cookie_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('terabox_cookie')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.teraboxDir') }}</label><input :value="settings.terabox_remote_dir" class="ui-input" @input="update('terabox_remote_dir', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.quarkCookie') }}</label>
            <SecretInput v-model="secretDraft.quark_cookie" :revealed="revealSecrets.quark_cookie" :loading="secretsLoading" :placeholder="settings.quark_cookie_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('quark_cookie')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.quarkDir') }}</label><input :value="settings.quark_remote_dir" class="ui-input" @input="update('quark_remote_dir', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1 md:col-span-2"><label class="ui-label">{{ t('games.quarkFolderId') }}</label><input :value="settings.quark_folder_id" class="ui-input" @input="update('quark_folder_id', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.openlistUrl') }}</label><input :value="settings.openlist_url" class="ui-input" @input="update('openlist_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('games.openlistToken') }}</label>
            <SecretInput v-model="secretDraft.openlist_token" :revealed="revealSecrets.openlist_token" :loading="secretsLoading" :placeholder="settings.openlist_token_set ? t('games.keepExisting') : t('games.enterSecret')" @toggle="toggleSecret('openlist_token')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.openlistBaidu') }}</label><input :value="settings.openlist_baidu_path" class="ui-input" @input="update('openlist_baidu_path', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.openlistPikpak') }}</label><input :value="settings.openlist_pikpak_path" class="ui-input" @input="update('openlist_pikpak_path', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.openlistTerabox') }}</label><input :value="settings.openlist_terabox_path" class="ui-input" @input="update('openlist_terabox_path', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('games.openlistQuark') }}</label><input :value="settings.openlist_quark_path" class="ui-input" @input="update('openlist_quark_path', ($event.target as HTMLInputElement).value)"></div>
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" class="rounded border-gray-300" :checked="settings.cleanup_after_publish" @change="update('cleanup_after_publish', ($event.target as HTMLInputElement).checked)">
            {{ t('games.cleanup') }}
          </label>
        </div>
      </section>

      <GamesPanel :settings="settings" :status="status" @refresh-status="pollStatus" />
    </template>
  </div>
</template>

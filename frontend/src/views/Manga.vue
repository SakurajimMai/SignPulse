<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  BookOpen,
  CheckCircle2,
  ExternalLink,
  Image,
  LoaderCircle,
  Radio,
  RefreshCw,
  Save,
  Search,
  Send,
  Sparkles,
  Square,
  Users,
  XCircle,
} from 'lucide-vue-next'
import {
  getMangaRuntimeStatus,
  getMangaSettings,
  listMangaCatalog,
  saveMangaSettings,
  startMangaWorker,
  stopMangaWorker,
  type MangaRuntimeStatus,
  type MangaSettings,
  type MangaSourceBinding,
  type MangaSummary,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { getErrorMessage } from '../lib/types'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import { formatShortDateTime } from '../lib/datetime'
import { useAccountsStore } from '../stores/accounts'
import { useSecretReveal } from '../composables/useSecretReveal'
import SecretInput from '../components/SecretInput.vue'

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const route = useRoute()
const router = useRouter()

const leaveIfEhentaiHash = () => {
  if (String(route.hash || '').toLowerCase().includes('ehentai')) {
    void router.replace({ name: 'manga-ehentai' })
  }
}

const settings = ref<MangaSettings | null>(null)
const status = ref<MangaRuntimeStatus | null>(null)
const catalog = ref<MangaSummary[]>([])
const catalogTotal = ref(0)
const catalogPage = ref(1)
const catalogPages = ref(0)
const searchQuery = ref('')
const pageLoading = ref(true)
const catalogLoading = ref(false)
const saving = ref(false)
const workerLoading = ref(false)
const errorMessage = ref('')
const sourceBindings = ref<MangaSourceBinding[]>([{ channel: '', discussion: '', ingest_mode: 'auto', forward_videos: true, ingest_enabled: true }])
const accountsStore = useAccountsStore()
let pollTimer: number | undefined

const {
  draft: secretDraft,
  reveal: revealSecrets,
  loading: secretsLoading,
  toggle: toggleSecret,
  reset: resetSecrets,
} = useSecretReveal(
  () => ({
    cfbed_auth_code: '',
    cfbed_api_token: '',
    site_publish_secret: '',
    outbound_bot_token: '',
  }),
  {
    isSaved: (key) => Boolean(settings.value?.[`${key}_set` as keyof MangaSettings]),
    fetchRevealed: () => getMangaSettings(token!, true),
    onError: (error) => notifyApiError(error, 'manga.loadFailed'),
  },
)

const workerRunning = computed(() => status.value?.worker_status === 'running' || status.value?.worker_status === 'starting')
const siteOrigin = computed(() => {
  const raw = settings.value?.site_publish_url || ''
  try {
    return raw ? new URL(raw).origin : ''
  } catch {
    return ''
  }
})
const mangaSiteUrl = (manga: MangaSummary) => {
  if (!siteOrigin.value || !manga.slug) return ''
  return `${siteOrigin.value}/manga/${encodeURIComponent(manga.slug)}`
}
const statusTone = computed(() => {
  if (workerRunning.value) return 'text-emerald-600 dark:text-emerald-400'
  if (status.value?.last_error) return 'text-rose-600 dark:text-rose-400'
  return 'text-gray-500 dark:text-gray-400'
})

const normalizeBindings = (value: MangaSettings): MangaSourceBinding[] => {
  if (value.source_bindings?.length) {
    return value.source_bindings.map((item) => ({
      channel: item.channel || '',
      discussion: item.discussion || '',
      ingest_mode: item.ingest_mode || 'auto',
      forward_videos: item.forward_videos !== false,
      ingest_enabled: item.ingest_enabled !== false,
    }))
  }
  const refs = (value.tg_source_chats || '').split(',').map((item) => item.trim()).filter(Boolean)
  if (!refs.length) return [{ channel: '', discussion: '', ingest_mode: 'auto', forward_videos: true, ingest_enabled: true }]
  return refs.map((channel) => ({ channel, discussion: '', ingest_mode: 'auto', forward_videos: true, ingest_enabled: true }))
}

const applySettings = (value: MangaSettings, keepDrafts = false) => {
  settings.value = { ...value }
  sourceBindings.value = normalizeBindings(value)
  if (!keepDrafts) resetSecrets()
}

const selectedAccountMissing = computed(() => {
  const name = settings.value?.telegram_account_name
  if (!name) return false
  return !accountsStore.accounts.some((item) => item.name === name)
})

const addBinding = () => {
  sourceBindings.value = [...sourceBindings.value, { channel: '', discussion: '', ingest_mode: 'auto', forward_videos: true, ingest_enabled: true }]
}

const removeBinding = (index: number) => {
  const next = sourceBindings.value.filter((_, i) => i !== index)
  sourceBindings.value = next.length ? next : [{ channel: '', discussion: '', ingest_mode: 'auto', forward_videos: true, ingest_enabled: true }]
}

const updateBinding = (index: number, key: keyof MangaSourceBinding, value: string | boolean) => {
  sourceBindings.value = sourceBindings.value.map((item, i) => (
    i === index ? { ...item, [key]: value } : item
  ))
}

const loadStatus = async () => {
  if (!token) return
  try {
    status.value = await getMangaRuntimeStatus(token)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.loadFailed')
  }
}

const loadCatalog = async (page = catalogPage.value) => {
  if (!token) return
  catalogLoading.value = true
  try {
    const result = await listMangaCatalog(token, page, 12, searchQuery.value)
    catalog.value = result.data
    catalogTotal.value = result.total
    catalogPage.value = result.page
    catalogPages.value = result.total_pages
  } catch (error: unknown) {
    notifyApiError(error, 'manga.loadFailed')
  } finally {
    catalogLoading.value = false
  }
}

const loadPage = async () => {
  if (!token) return
  pageLoading.value = true
  errorMessage.value = ''
  try {
    const [settingsResult, statusResult] = await Promise.all([
      getMangaSettings(token),
      getMangaRuntimeStatus(token),
      accountsStore.ensureAccounts().catch((error: unknown) => {
        notifyApiError(error, 'apiErrors.ACCOUNTS_LOAD_FAILED')
        return []
      }),
    ])
    applySettings(settingsResult)
    status.value = statusResult
    await loadCatalog(1)
  } catch (error: unknown) {
    errorMessage.value = t('manga.loadFailed')
    notifyApiError(error, 'manga.loadFailed')
  } finally {
    pageLoading.value = false
  }
}

const update = <K extends keyof MangaSettings>(key: K, next: MangaSettings[K]) => {
  if (!settings.value) return
  settings.value = { ...settings.value, [key]: next }
}

const saveSettings = async () => {
  if (!token || !settings.value) return
  saving.value = true
  try {
    const payload: Record<string, unknown> = {
      enabled: settings.value.enabled,
      telegram_account_name: settings.value.telegram_account_name || '',
      source_bindings: sourceBindings.value
        .map((item) => ({
          channel: item.channel.trim(),
          discussion: item.discussion.trim(),
          ingest_mode: item.ingest_mode || 'auto',
          forward_videos: item.forward_videos !== false,
          ingest_enabled: item.ingest_enabled !== false,
        }))
        .filter((item) => item.channel || item.discussion),
      tg_allowed_sender_ids: settings.value.tg_allowed_sender_ids,
      discussion_only: settings.value.discussion_only,
      ignore_user_comments: settings.value.ignore_user_comments,
      chapter_idle_seconds: settings.value.chapter_idle_seconds,
      chapter_reply_idle_seconds: settings.value.chapter_reply_idle_seconds,
      chapter_max_pages: settings.value.chapter_max_pages,
      accept_image_documents: settings.value.accept_image_documents,
      cfbed_upload_url: settings.value.cfbed_upload_url,
      cfbed_extra_query: settings.value.cfbed_extra_query,
      cfbed_public_base: settings.value.cfbed_public_base,
      cfbed_file_field: settings.value.cfbed_file_field,
      cfbed_retry_delay_seconds: settings.value.cfbed_retry_delay_seconds,
      site_publish_url: settings.value.site_publish_url,
      outbound_enabled: Boolean(settings.value.outbound_enabled),
      outbound_channel: settings.value.outbound_channel || '',
      outbound_preview_count: settings.value.outbound_preview_count || 4,
      outbound_button_text: settings.value.outbound_button_text || '点击阅读',
      outbound_site_base: settings.value.outbound_site_base || '',
      outbound_forward_videos: settings.value.outbound_forward_videos !== false,
      outbound_video_block_keywords: settings.value.outbound_video_block_keywords || '',
      outbound_video_allow_keywords: settings.value.outbound_video_allow_keywords || '',
      outbound_video_min_seconds: Number(settings.value.outbound_video_min_seconds || 0),
      outbound_video_max_seconds: Number(settings.value.outbound_video_max_seconds || 0),
      ai_enabled: Boolean(settings.value.ai_enabled),
      ai_refine_prompt: settings.value.ai_refine_prompt || '',
    }
    for (const [key, secret] of Object.entries(secretDraft.value)) {
      if (secret.trim()) payload[key] = secret.trim()
    }
    const result = await saveMangaSettings(token, payload)
    applySettings(result.settings, true)
    status.value = result.status
    toast.success(t('manga.saveSuccess'))
  } catch (error: unknown) {
    notifyApiError(error, 'manga.saveFailed')
  } finally {
    saving.value = false
  }
}

const toggleWorker = async () => {
  if (!token) return
  workerLoading.value = true
  try {
    const result = workerRunning.value
      ? await stopMangaWorker(token)
      : await startMangaWorker(token)
    status.value = result.status
    toast.success(result.message)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.workerFailed')
    await loadStatus()
  } finally {
    workerLoading.value = false
  }
}

const refresh = async () => {
  await Promise.all([loadStatus(), loadCatalog(catalogPage.value)])
}

const search = async () => {
  await loadCatalog(1)
}

const goPage = (page: number) => {
  if (page < 1 || (catalogPages.value > 0 && page > catalogPages.value)) return
  void loadCatalog(page)
}

const statusLabel = (value: string | undefined) => {
  const key = `manga.status.${value || 'stopped'}`
  const translated = t(key)
  return translated === key ? value || '-' : translated
}

onMounted(async () => {
  leaveIfEhentaiHash()
  await loadPage()
  pollTimer = window.setInterval(() => void loadStatus(), 10000)
})

watch(() => route.hash, leaveIfEhentaiHash)

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="mx-auto w-full space-y-6 pb-10">
    <div v-if="pageLoading" class="grid grid-cols-1 lg:grid-cols-3 gap-6" aria-busy="true">
      <div v-for="i in 3" :key="i" class="ui-card p-6 space-y-4">
        <div class="ui-skeleton h-5 w-28" />
        <div class="ui-skeleton h-3 w-48" />
        <div class="ui-skeleton h-10 w-full" />
        <div class="ui-skeleton h-10 w-full" />
      </div>
    </div>

    <template v-else>
      <div class="flex flex-col sm:flex-row sm:items-end justify-between gap-4">
        <div>
          <div class="ui-section-label mb-2">{{ t('manga.eyebrow') }}</div>
          <h2 class="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100">{{ t('manga.title') }}</h2>
          <p class="text-sm text-gray-500 dark:text-gray-400 mt-2 max-w-2xl">{{ t('manga.description') }}</p>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <button type="button" class="ui-btn-secondary !px-3 !py-2 !text-xs" :disabled="catalogLoading" @click="refresh">
            <RefreshCw class="w-3.5 h-3.5" :class="catalogLoading ? 'animate-spin' : ''" />
            {{ t('common.refresh') }}
          </button>
          <button type="button" class="ui-btn-primary !px-3 !py-2 !text-xs" :disabled="workerLoading || !settings" @click="toggleWorker">
            <LoaderCircle v-if="workerLoading" class="w-3.5 h-3.5 animate-spin" />
            <Square v-else-if="workerRunning" class="w-3.5 h-3.5" />
            <Radio v-else class="w-3.5 h-3.5" />
            {{ workerRunning ? t('manga.stopWorker') : t('manga.startWorker') }}
          </button>
        </div>
      </div>

      <div v-if="errorMessage" class="border border-rose-200 dark:border-rose-800/40 bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-300 px-4 py-3 text-sm" role="alert">
        {{ errorMessage }}
      </div>

      <div class="grid grid-cols-2 xl:grid-cols-6 gap-3">
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.workerStatus') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="statusTone">
            <CheckCircle2 v-if="workerRunning" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ statusLabel(status?.worker_status) }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.sourceChats') }}</div>
          <div class="mt-2 flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-gray-100 tabular-nums"><Users class="w-4 h-4 text-sky-500" />{{ status?.source_bindings ?? status?.source_chats ?? 0 }}</div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.catalogCount') }}</div>
          <div class="mt-2 flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-gray-100 tabular-nums"><BookOpen class="w-4 h-4 text-sky-500" />{{ catalogTotal }}</div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.imgbed') }}</div>
          <div class="mt-2 text-sm font-medium" :class="status?.imgbed_configured ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'">{{ status?.imgbed_configured ? t('manga.configured') : t('manga.notConfigured') }}</div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.sitePublish') }}</div>
          <div class="mt-2 text-sm font-medium" :class="status?.site_publish_enabled ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500 dark:text-gray-400'">{{ status?.site_publish_enabled ? t('manga.connected') : t('manga.notConfigured') }}</div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.outboundPublish') }}</div>
          <div class="mt-2 text-sm font-medium" :class="status?.outbound_publish_enabled ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500 dark:text-gray-400'">{{ status?.outbound_publish_enabled ? (status.outbound_bot_configured ? t('manga.outboundViaBot') : t('manga.connected')) : t('manga.notConfigured') }}</div>
        </div>
      </div>

      <div v-if="status?.last_error" class="flex gap-2 items-start text-xs text-rose-700 dark:text-rose-300 border border-rose-200 dark:border-rose-800/40 bg-rose-50/80 dark:bg-rose-500/10 p-3">
        <XCircle class="w-4 h-4 shrink-0 mt-0.5" />
        <span class="break-all">{{ getErrorMessage(status.last_error) }}</span>
      </div>

      <section v-if="settings" class="ui-card p-5 sm:p-6">
        <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-3 border-b border-gray-200 dark:border-gray-800/60 pb-4 mb-5">
          <div class="flex items-start gap-3">
            <span class="ui-section-icon"><Send class="w-3.5 h-3.5" /></span>
            <div>
              <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.configTitle') }}</h3>
              <p class="text-xs text-gray-500 mt-1">{{ t('manga.configHint') }}</p>
            </div>
          </div>
          <button type="button" class="ui-btn-primary !px-3 !py-2 !text-xs shrink-0" :disabled="saving" @click="saveSettings">
            <LoaderCircle v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            <Save v-else class="w-3.5 h-3.5" />
            {{ saving ? t('settings.saving') : t('common.save') }}
          </button>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-5">
          <div class="lg:col-span-2 flex items-center justify-between gap-4 p-3 border border-sky-200 dark:border-sky-800/40 bg-sky-50/70 dark:bg-sky-500/10">
            <div>
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.enable') }}</div>
              <p class="text-xs text-gray-500 mt-1">{{ t('manga.enableHint') }}</p>
            </div>
            <button type="button" class="ui-switch" role="switch" :aria-checked="settings.enabled" :class="settings.enabled ? 'ui-switch-on' : ''" @click="update('enabled', !settings.enabled)"><span class="ui-switch-knob" /></button>
          </div>

          <div class="lg:col-span-2 space-y-3 p-3 border border-gray-200 dark:border-gray-800/60">
            <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
              <div class="min-w-0">
                <div class="flex items-center gap-2 text-sm font-medium text-gray-900 dark:text-gray-100">
                  <Sparkles class="w-4 h-4 text-sky-500" aria-hidden="true" />
                  {{ t('manga.aiTitle') }}
                </div>
                <p class="text-xs text-gray-500 mt-1">{{ t('manga.aiHint') }}</p>
              </div>
              <button
                type="button"
                class="ui-switch"
                role="switch"
                :aria-checked="Boolean(settings.ai_enabled)"
                :class="settings.ai_enabled ? 'ui-switch-on' : ''"
                @click="update('ai_enabled', !settings.ai_enabled)"
              >
                <span class="ui-switch-knob" />
              </button>
            </div>
            <p v-if="settings.ai_enabled && !settings.ai_model_configured" class="text-xs text-amber-600 dark:text-amber-400">
              {{ t('manga.aiNeedModel') }}
              <RouterLink to="/settings" class="underline underline-offset-2">{{ t('manga.aiGoSettings') }}</RouterLink>
            </p>
            <div class="space-y-1.5">
              <div class="flex items-center justify-between gap-2">
                <label class="ui-label">{{ t('manga.aiPrompt') }}</label>
                <button
                  type="button"
                  class="text-[11px] text-sky-600 hover:underline disabled:text-gray-400"
                  :disabled="!settings.ai_refine_prompt"
                  @click="update('ai_refine_prompt', '')"
                >
                  {{ t('manga.aiPromptReset') }}
                </button>
              </div>
              <textarea
                :value="settings.ai_refine_prompt || ''"
                rows="8"
                class="ui-input min-h-[9rem] font-mono text-[12px] leading-relaxed"
                :placeholder="settings.ai_refine_prompt_default || t('manga.aiPromptPlaceholder')"
                :disabled="!settings.ai_enabled"
                @input="update('ai_refine_prompt', ($event.target as HTMLTextAreaElement).value)"
              ></textarea>
              <p class="text-[10px] text-gray-500">{{ t('manga.aiPromptHint') }}</p>
            </div>
          </div>

          <div class="lg:col-span-2 space-y-3">
            <div class="flex items-center justify-between gap-3">
              <div>
                <label class="ui-label">{{ t('manga.sourceBindingsLabel') }}</label>
                <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.sourceBindingsHint') }}</p>
              </div>
              <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="addBinding">{{ t('manga.addBinding') }}</button>
            </div>
            <div v-for="(binding, index) in sourceBindings" :key="index" class="space-y-2" :class="binding.ingest_enabled === false ? 'opacity-70' : ''">
              <div class="grid grid-cols-1 md:grid-cols-[1fr_1fr_10rem_auto] gap-2 items-end">
                <div class="space-y-1">
                  <label class="ui-label">{{ t('manga.channelLabel') }}</label>
                  <input :value="binding.channel" class="ui-input" :placeholder="t('manga.channelPlaceholder')" @input="updateBinding(index, 'channel', ($event.target as HTMLInputElement).value)">
                </div>
                <div class="space-y-1">
                  <label class="ui-label">{{ t('manga.discussionLabel') }}</label>
                  <input :value="binding.discussion" class="ui-input" :placeholder="t('manga.discussionPlaceholder')" :disabled="binding.ingest_mode === 'telegraph'" @input="updateBinding(index, 'discussion', ($event.target as HTMLInputElement).value)">
                </div>
                <div class="space-y-1">
                  <label class="ui-label">{{ t('manga.ingestModeLabel') }}</label>
                  <select class="ui-input" :value="binding.ingest_mode || 'auto'" @change="updateBinding(index, 'ingest_mode', ($event.target as HTMLSelectElement).value)">
                    <option value="auto">{{ t('manga.ingestModeAuto') }}</option>
                    <option value="discussion">{{ t('manga.ingestModeDiscussion') }}</option>
                    <option value="telegraph">{{ t('manga.ingestModeTelegraph') }}</option>
                  </select>
                </div>
                <button type="button" class="ui-btn-secondary !px-3 !py-2 !text-xs" @click="removeBinding(index)">{{ t('common.delete') }}</button>
              </div>
              <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
                <label class="inline-flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
                  <input :checked="binding.ingest_enabled === false" type="checkbox" class="accent-sky-500" @change="updateBinding(index, 'ingest_enabled', !($event.target as HTMLInputElement).checked)">
                  {{ t('manga.skipIngest') }}
                </label>
                <label class="inline-flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
                  <input :checked="binding.forward_videos !== false" type="checkbox" class="accent-sky-500" @change="updateBinding(index, 'forward_videos', ($event.target as HTMLInputElement).checked)">
                  {{ t('manga.bindingForwardVideos') }}
                </label>
              </div>
              <p v-if="binding.ingest_enabled === false" class="text-[10px] text-amber-600 dark:text-amber-400">{{ t('manga.skipIngestHint') }}</p>
            </div>
          </div>
          <div class="space-y-1.5">
            <label class="ui-label">{{ t('manga.allowedSenders') }}</label>
            <input :value="settings.tg_allowed_sender_ids" class="ui-input" placeholder="123456789,987654321" @input="update('tg_allowed_sender_ids', ($event.target as HTMLInputElement).value)">
            <p class="text-[10px] text-gray-500">{{ t('manga.allowedSendersHint') }}</p>
          </div>
          <div class="lg:col-span-2 space-y-3 p-3 border border-gray-200 dark:border-gray-800/60">
            <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-3">
              <div>
                <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.accountTitle') }}</div>
                <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.accountHint') }}</p>
              </div>
              <RouterLink to="/accounts" class="ui-btn-secondary !px-3 !py-1.5 !text-xs shrink-0">
                {{ t('manga.manageAccounts') }}
              </RouterLink>
            </div>
            <div class="space-y-1.5">
              <label class="ui-label">{{ t('manga.ingestAccount') }}</label>
              <select
                class="ui-input"
                :value="settings.telegram_account_name || ''"
                @change="update('telegram_account_name', ($event.target as HTMLSelectElement).value)"
              >
                <option value="">{{ t('manga.selectAccount') }}</option>
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
                {{ t('manga.noAccounts') }}
              </p>
            </div>
          </div>

          <div class="lg:col-span-2 flex flex-wrap gap-x-6 gap-y-3 pt-1">
            <label class="inline-flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300"><input :checked="settings.discussion_only" type="checkbox" class="accent-sky-500" @change="update('discussion_only', ($event.target as HTMLInputElement).checked)">{{ t('manga.discussionOnly') }}</label>
            <label class="inline-flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300"><input :checked="settings.ignore_user_comments" type="checkbox" class="accent-sky-500" @change="update('ignore_user_comments', ($event.target as HTMLInputElement).checked)">{{ t('manga.ignoreComments') }}</label>
            <label class="inline-flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300"><input :checked="settings.accept_image_documents" type="checkbox" class="accent-sky-500" @change="update('accept_image_documents', ($event.target as HTMLInputElement).checked)">{{ t('manga.acceptDocuments') }}</label>
          </div>

          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.idleSeconds') }}</label><input :value="settings.chapter_idle_seconds" type="number" min="1" step="1" class="ui-input" @input="update('chapter_idle_seconds', Number(($event.target as HTMLInputElement).value) || 8)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.maxPages') }}</label><input :value="settings.chapter_max_pages" type="number" min="1" max="10000" class="ui-input" @input="update('chapter_max_pages', Number(($event.target as HTMLInputElement).value) || 200)"></div>

          <div class="lg:col-span-2 pt-3 border-t border-gray-200 dark:border-gray-800/60"><div class="ui-section-label mb-3">{{ t('manga.deliveryTitle') }}</div></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.imgbedUrl') }}</label><input :value="settings.cfbed_upload_url" class="ui-input" placeholder="https://img.example.com/upload" @input="update('cfbed_upload_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.imgbedAuth') }}</label><SecretInput v-model="secretDraft.cfbed_auth_code" :revealed="revealSecrets.cfbed_auth_code" :loading="secretsLoading" :placeholder="settings.cfbed_auth_code_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('cfbed_auth_code')" /></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.imgbedToken') }}</label><SecretInput v-model="secretDraft.cfbed_api_token" :revealed="revealSecrets.cfbed_api_token" :loading="secretsLoading" :placeholder="settings.cfbed_api_token_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('cfbed_api_token')" /></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.imgbedPublicBase') }}</label><input :value="settings.cfbed_public_base" class="ui-input" placeholder="https://img.example.com" @input="update('cfbed_public_base', ($event.target as HTMLInputElement).value)"></div>
          <div class="lg:col-span-2 space-y-1.5"><label class="ui-label">{{ t('manga.imgbedExtraQuery') }}</label><input :value="settings.cfbed_extra_query" class="ui-input" placeholder="uploadChannel=cfr2" @input="update('cfbed_extra_query', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.siteUrl') }}</label><input :value="settings.site_publish_url" class="ui-input" placeholder="https://example.com/api/manga/publish" @input="update('site_publish_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.siteSecret') }}</label><SecretInput v-model="secretDraft.site_publish_secret" :revealed="revealSecrets.site_publish_secret" :loading="secretsLoading" :placeholder="settings.site_publish_secret_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('site_publish_secret')" /></div>

          <div class="lg:col-span-2 pt-3 border-t border-gray-200 dark:border-gray-800/60"><div class="ui-section-label mb-3">{{ t('manga.outboundTitle') }}</div></div>
          <div class="lg:col-span-2 flex items-center justify-between gap-4 p-3 border border-gray-200 dark:border-gray-800/60">
            <div>
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.outboundEnable') }}</div>
              <p class="text-xs text-gray-500 mt-1">{{ t('manga.outboundHint') }}</p>
            </div>
            <button type="button" class="ui-switch" role="switch" :aria-checked="Boolean(settings.outbound_enabled)" :class="settings.outbound_enabled ? 'ui-switch-on' : ''" @click="update('outbound_enabled', !settings.outbound_enabled)"><span class="ui-switch-knob" /></button>
          </div>
          <div class="lg:col-span-2 flex items-center justify-between gap-4 p-3 border border-gray-200 dark:border-gray-800/60">
            <div>
              <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.outboundForwardVideos') }}</div>
              <p class="text-xs text-gray-500 mt-1">{{ t('manga.outboundForwardVideosHint') }}</p>
            </div>
            <button type="button" class="ui-switch" role="switch" :aria-checked="settings.outbound_forward_videos !== false" :class="settings.outbound_forward_videos !== false ? 'ui-switch-on' : ''" @click="update('outbound_forward_videos', settings.outbound_forward_videos === false)"><span class="ui-switch-knob" /></button>
          </div>
          <div class="lg:col-span-2 space-y-1.5">
            <label class="ui-label">{{ t('manga.videoBlockKeywords') }}</label>
            <textarea :value="settings.outbound_video_block_keywords || ''" rows="3" class="ui-input min-h-[4.5rem]" :placeholder="t('manga.videoBlockKeywordsPlaceholder')" @input="update('outbound_video_block_keywords', ($event.target as HTMLTextAreaElement).value)"></textarea>
            <p class="text-[10px] text-gray-500">{{ t('manga.videoBlockKeywordsHint') }}</p>
          </div>
          <div class="lg:col-span-2 space-y-1.5">
            <label class="ui-label">{{ t('manga.videoAllowKeywords') }}</label>
            <textarea :value="settings.outbound_video_allow_keywords || ''" rows="2" class="ui-input min-h-[3.5rem]" :placeholder="t('manga.videoAllowKeywordsPlaceholder')" @input="update('outbound_video_allow_keywords', ($event.target as HTMLTextAreaElement).value)"></textarea>
            <p class="text-[10px] text-gray-500">{{ t('manga.videoAllowKeywordsHint') }}</p>
          </div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.videoMinSeconds') }}</label><input :value="settings.outbound_video_min_seconds ?? 0" type="number" min="0" max="86400" class="ui-input" @input="update('outbound_video_min_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.videoMaxSeconds') }}</label><input :value="settings.outbound_video_max_seconds ?? 0" type="number" min="0" max="86400" class="ui-input" @input="update('outbound_video_max_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.outboundChannel') }}</label><input :value="settings.outbound_channel" class="ui-input" placeholder="-1002085070183" @input="update('outbound_channel', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.outboundPreviewCount') }}</label><input :value="settings.outbound_preview_count ?? 4" type="number" min="1" max="10" class="ui-input" @input="update('outbound_preview_count', Math.min(10, Math.max(1, Number(($event.target as HTMLInputElement).value) || 4)))"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.outboundButtonText') }}</label><input :value="settings.outbound_button_text || t('manga.outboundButtonDefault')" class="ui-input" :placeholder="t('manga.outboundButtonDefault')" @input="update('outbound_button_text', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.outboundSiteBase') }}</label><input :value="settings.outbound_site_base" class="ui-input" placeholder="https://www.ixacg.de" @input="update('outbound_site_base', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1.5"><label class="ui-label">{{ t('manga.outboundBotToken') }}</label><SecretInput v-model="secretDraft.outbound_bot_token" :revealed="revealSecrets.outbound_bot_token" :loading="secretsLoading" :placeholder="settings.outbound_bot_token_set ? t('manga.keepExisting') : t('manga.outboundBotTokenPlaceholder')" @toggle="toggleSecret('outbound_bot_token')" /></div>
        </div>
      </section>

      <section class="space-y-4">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          <div><h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.catalogTitle') }}</h3><p class="text-xs text-gray-500 mt-1">{{ t('manga.catalogHint') }}</p></div>
          <form class="flex gap-2 w-full sm:w-auto" @submit.prevent="search"><div class="relative flex-1 sm:w-64"><Search class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" /><input v-model="searchQuery" class="ui-input !pl-9" :placeholder="t('manga.searchPlaceholder')"></div><button type="submit" class="ui-btn-secondary !px-3"><Search class="w-4 h-4" /><span class="sr-only">{{ t('common.search') }}</span></button></form>
        </div>
        <div v-if="catalogLoading" class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4" aria-busy="true"><div v-for="i in 6" :key="i" class="ui-card p-4 flex gap-4"><div class="ui-skeleton w-20 h-28 shrink-0" /><div class="flex-1 space-y-3"><div class="ui-skeleton h-4 w-3/4" /><div class="ui-skeleton h-3 w-1/2" /><div class="ui-skeleton h-3 w-full" /></div></div></div>
        <div v-else-if="!catalog.length" class="ui-card p-10 text-center text-sm text-gray-500">{{ t('manga.catalogEmpty') }}</div>
        <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
          <article v-for="manga in catalog" :key="manga.id" class="ui-card ui-card-hover overflow-hidden flex min-h-[156px]">
            <div class="w-24 sm:w-28 shrink-0 bg-gray-100 dark:bg-gray-900/60"><img v-if="manga.cover_url" :src="manga.cover_url" :alt="manga.title" loading="lazy" class="w-full h-full object-cover aspect-[3/4]"><div v-else class="w-full h-full min-h-[156px] flex items-center justify-center text-gray-400"><Image class="w-6 h-6" /></div></div>
            <div class="min-w-0 flex-1 p-4 flex flex-col"><div class="flex items-start justify-between gap-2"><h4 class="font-medium text-sm text-gray-900 dark:text-gray-100 line-clamp-2" :title="manga.title">{{ manga.title }}</h4><span class="text-[10px] font-mono text-gray-400 shrink-0">#{{ manga.id }}</span></div><p v-if="manga.author" class="text-xs text-gray-500 mt-2 line-clamp-1">{{ manga.author }}</p><p class="text-[11px] text-gray-500 mt-1 tabular-nums">{{ manga.page_count }} P · {{ manga.chapter_count }} {{ t('manga.chapters') }}</p><div class="mt-auto pt-3 flex items-center justify-between gap-2"><span class="text-[10px] text-gray-400 truncate">{{ formatShortDateTime(manga.updated_at) }}</span><a v-if="mangaSiteUrl(manga)" :href="mangaSiteUrl(manga)" target="_blank" rel="noopener" class="ui-row-action !p-1" :title="t('manga.openSite')"><ExternalLink class="w-3.5 h-3.5" /></a></div></div>
          </article>
        </div>
        <div v-if="catalogPages > 1" class="flex items-center justify-center gap-3 pt-2"><button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage <= 1 || catalogLoading" @click="goPage(catalogPage - 1)">{{ t('common.prev') }}</button><span class="text-xs text-gray-500 tabular-nums">{{ catalogPage }} / {{ catalogPages }}</span><button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage >= catalogPages || catalogLoading" @click="goPage(catalogPage + 1)">{{ t('common.next') }}</button></div>
      </section>
    </template>
  </div>
</template>

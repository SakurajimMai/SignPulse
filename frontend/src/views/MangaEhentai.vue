<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { CheckCircle2, LoaderCircle, XCircle } from 'lucide-vue-next'
import {
  getMangaRuntimeStatus,
  getMangaSettings,
  saveMangaSettings,
  type MangaRuntimeStatus,
  type MangaSettings,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import { useSecretReveal } from '../composables/useSecretReveal'
import EhentaiPanel from '../components/manga/EhentaiPanel.vue'

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()

const settings = ref<MangaSettings | null>(null)
const status = ref<MangaRuntimeStatus | null>(null)
const pageLoading = ref(true)
const saving = ref(false)
const errorMessage = ref('')
const {
  draft: cookieSecrets,
  reveal: revealSecrets,
  loading: secretsLoading,
  toggle: toggleSecret,
  reset: resetSecrets,
} = useSecretReveal(
  () => ({ ehentai_cookie: '' }),
  {
    isSaved: () => Boolean(settings.value?.ehentai_cookie_set),
    fetchRevealed: () => getMangaSettings(token!, true),
    onError: (error) => notifyApiError(error, 'manga.loadFailed'),
  },
)
const cookieDraft = computed({
  get: () => cookieSecrets.value.ehentai_cookie,
  set: (value: string) => {
    cookieSecrets.value = { ...cookieSecrets.value, ehentai_cookie: value }
  },
})
let pollTimer: number | undefined

const applySettings = (value: MangaSettings, keepDrafts = false) => {
  settings.value = { ...value }
  if (!keepDrafts) resetSecrets()
}

const loadPage = async () => {
  if (!token) return
  pageLoading.value = true
  errorMessage.value = ''
  try {
    const [settingsResult, statusResult] = await Promise.all([
      getMangaSettings(token),
      getMangaRuntimeStatus(token),
    ])
    applySettings(settingsResult)
    status.value = statusResult
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
      ehentai_enabled: Boolean(settings.value.ehentai_enabled),
      ehentai_exhentai: Boolean(settings.value.ehentai_exhentai),
      ehentai_search: settings.value.ehentai_search || '',
      ehentai_cats: settings.value.ehentai_cats || '704',
      ehentai_max_pages: Number(settings.value.ehentai_max_pages || 400),
      ehentai_search_pages: Number(settings.value.ehentai_search_pages || 1),
      ehentai_delay_seconds: Number(settings.value.ehentai_delay_seconds ?? 1.2),
      ehentai_gallery_delay_seconds: Number(settings.value.ehentai_gallery_delay_seconds ?? 5),
      ehentai_poll_seconds: Number(settings.value.ehentai_poll_seconds || 300),
      ehentai_translation_url: settings.value.ehentai_translation_url || '',
      ehentai_translation_auto: settings.value.ehentai_translation_auto !== false,
    }
    if (cookieDraft.value.trim()) payload.ehentai_cookie = cookieDraft.value.trim()
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

onMounted(async () => {
  await loadPage()
  pollTimer = window.setInterval(async () => {
    if (!token) return
    try {
      status.value = await getMangaRuntimeStatus(token)
    } catch {
      /* 轮询失败时保留当前状态 */
    }
  }, 10000)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})

const siteOk = computed(() => Boolean(status.value?.site_publish_enabled))
const imgbedOk = computed(() => Boolean(status.value?.imgbed_configured))
</script>

<template>
  <div class="mx-auto w-full space-y-6 pb-10">
    <div v-if="pageLoading" class="grid grid-cols-1 lg:grid-cols-3 gap-6" aria-busy="true">
      <div v-for="i in 3" :key="i" class="ui-card p-6 space-y-4">
        <div class="ui-skeleton h-5 w-28" />
        <div class="ui-skeleton h-3 w-48" />
        <div class="ui-skeleton h-10 w-full" />
      </div>
    </div>

    <template v-else>
      <div>
        <div class="ui-section-label mb-2">{{ t('manga.eyebrow') }} / {{ t('nav.mangaEhentai') }}</div>
        <h2 class="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiTitle') }}</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2 max-w-2xl">{{ t('manga.ehentaiPageHint') }}</p>
      </div>

      <div v-if="errorMessage" class="border border-rose-200 dark:border-rose-800/40 bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-300 px-4 py-3 text-sm" role="alert">
        {{ errorMessage }}
      </div>

      <div class="grid grid-cols-2 xl:grid-cols-4 gap-3">
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.imgbed') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="imgbedOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'">
            <CheckCircle2 v-if="imgbedOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ imgbedOk ? t('manga.configured') : t('manga.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.sitePublish') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="siteOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            <CheckCircle2 v-if="siteOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ siteOk ? t('manga.connected') : t('manga.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4 col-span-2">
          <p class="text-xs text-gray-500">{{ t('manga.ehentaiSharedConfigHint') }}</p>
          <RouterLink to="/manga" class="inline-flex mt-2 text-xs text-sky-600 dark:text-sky-400 hover:underline">
            {{ t('manga.ehentaiGoTelegram') }}
          </RouterLink>
        </div>
      </div>

      <EhentaiPanel
        v-if="settings"
        :settings="settings"
        :cookie-draft="cookieDraft"
        :cookie-revealed="revealSecrets.ehentai_cookie"
        :cookie-loading="secretsLoading"
        :saving="saving"
        @update="update"
        @update:cookie-draft="cookieDraft = $event"
        @toggle-cookie-reveal="toggleSecret('ehentai_cookie')"
        @save="saveSettings"
      />
      <div v-else-if="!pageLoading" class="ui-card p-10 text-center text-sm text-gray-500">
        <LoaderCircle class="w-5 h-5 animate-spin inline-block mr-2" />
        {{ t('common.loading') }}
      </div>
    </template>
  </div>
</template>

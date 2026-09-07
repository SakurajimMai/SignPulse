<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ExternalLink, LoaderCircle, Radio, RefreshCw, Save, Search } from 'lucide-vue-next'
import {
  getEhentaiStatus,
  listMangaCatalog,
  refreshEhentaiTranslations,
  runEhentaiPass,
  type EhentaiRuntimeStatus,
  type MangaSettings,
  type MangaSummary,
} from '../../lib/api'
import { formatTimeOnly } from '../../lib/datetime'
import HmwMangaCard from './HmwMangaCard.vue'
import SecretInput from '../SecretInput.vue'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { getErrorMessage } from '../../lib/types'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'

const props = defineProps<{
  settings: MangaSettings
  cookieDraft: string
  cookieRevealed?: boolean
  cookieLoading?: boolean
  saving: boolean
}>()

const emit = defineEmits<{
  update: [key: keyof MangaSettings, value: MangaSettings[keyof MangaSettings]]
  'update:cookieDraft': [value: string]
  'toggle-cookie-reveal': []
  save: []
}>()

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const status = ref<EhentaiRuntimeStatus | null>(null)
const catalog = ref<MangaSummary[]>([])
const catalogTotal = ref(0)
const catalogPage = ref(1)
const catalogPages = ref(0)
const catalogLoading = ref(false)
const searchQuery = ref('')
const workerLoading = ref(false)
const translating = ref(false)
let pollTimer: number | undefined

const running = computed(
  () => status.value?.worker_status === 'running' || status.value?.worker_status === 'starting',
)
const listeningOn = computed(() => Boolean(props.settings.ehentai_enabled) || running.value)
const nextPassLabel = computed(() => formatTimeOnly(status.value?.next_pass_at || '', ''))
const siteOrigin = computed(() => {
  const raw = props.settings.outbound_site_base || props.settings.site_publish_url || ''
  try {
    return raw ? new URL(raw).origin : ''
  } catch {
    return ''
  }
})

const mangaSiteUrl = (manga: MangaSummary) => {
  if (siteOrigin.value && manga.slug) {
    return `${siteOrigin.value}/manga/${encodeURIComponent(manga.slug)}`
  }
  return ''
}

const loadCatalog = async (page = catalogPage.value) => {
  if (!token) return
  catalogLoading.value = true
  try {
    const result = await listMangaCatalog(token, page, 12, searchQuery.value, 'ehentai')
    catalog.value = result.data || []
    catalogTotal.value = result.total
    catalogPage.value = result.page
    catalogPages.value = result.total_pages
  } catch (error: unknown) {
    notifyApiError(error, 'manga.loadFailed')
  } finally {
    catalogLoading.value = false
  }
}

const goPage = (page: number) => {
  if (page < 1 || (catalogPages.value > 0 && page > catalogPages.value)) return
  void loadCatalog(page)
}

const search = async () => {
  await loadCatalog(1)
}

const loadStatus = async () => {
  if (!token) return
  try {
    status.value = await getEhentaiStatus(token)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.ehentaiLoadFailed')
  }
}

const openManga = (manga: MangaSummary) => {
  const href = mangaSiteUrl(manga)
  if (href) window.open(href, '_blank', 'noopener')
}

const runOnce = async () => {
  if (!token) return
  workerLoading.value = true
  try {
    const result = await runEhentaiPass(token)
    status.value = result.status
    toast.success(result.message)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.ehentaiWorkerFailed')
    await loadStatus()
  } finally {
    workerLoading.value = false
  }
}

const translations = computed(() => status.value?.translations)
const shaShort = computed(() => (translations.value?.sha || '').slice(0, 12))

/** 原爬虫 config.ini 的 f_search，方便一次填回旧列表 */
const ORIGINAL_EH_SEARCH = [
  '3d language:Chinese',
  'gender change language:Chinese',
  'female:NTR language:Chinese',
  'corruption language:Chinese',
  'crotch tattoo language:Chinese',
  'milf language:Chinese',
  'tentacles language:Chinese',
  'female:mother language:Chinese',
  'female:impregnation language:Chinese',
].join('\n')

const searchQueries = computed(() =>
  String(props.settings.ehentai_search || '')
    .split(/[\n,]+/)
    .map((item) => item.trim())
    .filter(Boolean),
)

const toggleEnableListen = () => {
  emit('update', 'ehentai_enabled', !props.settings.ehentai_enabled)
  emit('save')
}

const restoreOriginalSearch = () => {
  emit('update', 'ehentai_search', ORIGINAL_EH_SEARCH)
  if (!props.settings.ehentai_cats) emit('update', 'ehentai_cats', '704')
}

const refreshTranslations = async () => {
  if (!token) return
  translating.value = true
  try {
    const result = await refreshEhentaiTranslations(token)
    status.value = result.status
    toast.success(result.message)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.ehentaiTranslationFailed')
    await loadStatus()
  } finally {
    translating.value = false
  }
}

const statusLabel = () => {
  const value = status.value?.worker_status
  const phase = status.value?.phase
  const key =
    value === 'running' && phase === 'waiting'
      ? 'manga.status.waiting'
      : value === 'running' || value === 'starting'
        ? 'manga.status.listening'
        : `manga.status.${value || 'stopped'}`
  const translated = t(key)
  return translated === key ? value || '-' : translated
}

watch(
  () => props.settings.ehentai_enabled,
  () => void loadStatus(),
)

watch(
  () => status.value?.processed,
  (processed, previous) => {
    if (processed !== previous) void loadCatalog(catalogPage.value)
  },
)

onMounted(async () => {
  await loadStatus()
  await loadCatalog(1)
  pollTimer = window.setInterval(() => void loadStatus(), 8000)
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="space-y-6">
  <section class="ui-card p-5 sm:p-6 space-y-5">
    <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-3 border-b border-gray-200 dark:border-gray-800/60 pb-4">
      <div>
        <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiTitle') }}</h3>
        <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiHint') }}</p>
      </div>
      <div class="flex items-center gap-2 shrink-0">
        <button type="button" class="ui-btn-secondary !px-3 !py-2 !text-xs" :disabled="saving" @click="emit('save')">
          <LoaderCircle v-if="saving" class="w-3.5 h-3.5 animate-spin" />
          <Save v-else class="w-3.5 h-3.5" />
          {{ saving ? t('settings.saving') : t('common.save') }}
        </button>
        <button
          type="button"
          class="ui-btn-primary !px-3 !py-2 !text-xs"
          :disabled="workerLoading || !listeningOn"
          :title="t('manga.ehentaiRunOnceHint')"
          @click="runOnce"
        >
          <LoaderCircle v-if="workerLoading" class="w-3.5 h-3.5 animate-spin" />
          <Radio v-else class="w-3.5 h-3.5" />
          {{ t('manga.ehentaiStart') }}
        </button>
      </div>
    </div>

    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <div class="p-3 border border-gray-200 dark:border-gray-800/60">
        <div class="ui-section-label">{{ t('manga.ehentaiStatus') }}</div>
        <div class="mt-1 text-sm font-medium" :class="running ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
          {{ statusLabel() }}
        </div>
      </div>
      <div class="p-3 border border-gray-200 dark:border-gray-800/60">
        <div class="ui-section-label">{{ t('manga.ehentaiCookie') }}</div>
        <div class="mt-1 text-sm font-medium" :class="status?.cookie_configured ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'">
          {{ status?.cookie_configured ? t('manga.configured') : t('manga.notConfigured') }}
        </div>
      </div>
      <div class="p-3 border border-gray-200 dark:border-gray-800/60">
        <div class="ui-section-label">{{ t('manga.ehentaiProcessed') }}</div>
        <div class="mt-1 text-sm font-semibold tabular-nums">{{ status?.processed ?? 0 }}</div>
      </div>
      <div class="p-3 border border-gray-200 dark:border-gray-800/60">
        <div class="ui-section-label">{{ t('manga.ehentaiSkipped') }}</div>
        <div class="mt-1 text-sm font-semibold tabular-nums">{{ status?.skipped ?? 0 }} / {{ status?.failed ?? 0 }}</div>
      </div>
    </div>

    <p v-if="status?.current?.title" class="text-xs text-gray-500">
      {{ t('manga.ehentaiCurrent') }}：{{ status.current.title }}
      <span v-if="status.current.total" class="tabular-nums">（{{ status.current.pages || 0 }}/{{ status.current.total }}）</span>
    </p>
    <p v-else-if="running && status?.phase === 'waiting' && nextPassLabel" class="text-xs text-gray-500">
      {{ t('manga.ehentaiNextPass') }}：{{ nextPassLabel }}
    </p>
    <p v-if="status?.last_error" class="text-xs text-rose-600 dark:text-rose-300 break-all">{{ getErrorMessage(status.last_error) }}</p>

    <div class="flex items-center justify-between gap-4 p-3 border border-sky-200 dark:border-sky-800/40 bg-sky-50/70 dark:bg-sky-500/10">
      <div>
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiEnable') }}</div>
        <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiEnableHint') }}</p>
      </div>
      <button
        type="button"
        class="ui-switch"
        role="switch"
        :aria-checked="Boolean(settings.ehentai_enabled)"
        :class="settings.ehentai_enabled ? 'ui-switch-on' : ''"
        :disabled="saving"
        @click="toggleEnableListen"
      >
        <span class="ui-switch-knob" />
      </button>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-4">
      <div class="lg:col-span-2 space-y-1.5">
        <div class="flex items-center justify-between gap-3">
          <label class="ui-label">{{ t('manga.ehentaiCookie') }}</label>
          <span
            class="text-[11px] font-medium"
            :class="settings.ehentai_cookie_set ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'"
          >
            {{ settings.ehentai_cookie_set ? t('manga.ehentaiCookieSaved') : t('manga.notConfigured') }}
          </span>
        </div>
        <SecretInput
          :model-value="cookieDraft"
          :revealed="Boolean(cookieRevealed)"
          :loading="cookieLoading"
          :placeholder="settings.ehentai_cookie_set ? t('manga.keepExisting') : t('manga.ehentaiCookiePlaceholder')"
          @update:model-value="emit('update:cookieDraft', $event)"
          @toggle="emit('toggle-cookie-reveal')"
        />
        <p class="text-[10px] text-gray-500">{{ t('manga.ehentaiCookieHint') }}</p>
      </div>
      <div class="lg:col-span-2 flex items-center justify-between gap-4 p-3 border border-gray-200 dark:border-gray-800/60">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiExhentai') }}</div>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiExhentaiHint') }}</p>
        </div>
        <button
          type="button"
          class="ui-switch"
          role="switch"
          :aria-checked="Boolean(settings.ehentai_exhentai)"
          :class="settings.ehentai_exhentai ? 'ui-switch-on' : ''"
          @click="emit('update', 'ehentai_exhentai', !settings.ehentai_exhentai)"
        >
          <span class="ui-switch-knob" />
        </button>
      </div>
      <div class="lg:col-span-2 space-y-1.5">
        <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2">
          <label class="ui-label">{{ t('manga.ehentaiSearch') }}</label>
          <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="restoreOriginalSearch">
            {{ t('manga.ehentaiRestoreSearch') }}
          </button>
        </div>
        <textarea
          :value="settings.ehentai_search || ''"
          rows="8"
          class="ui-input min-h-[10rem] font-mono text-sm"
          :placeholder="t('manga.ehentaiSearchPlaceholder')"
          @input="emit('update', 'ehentai_search', ($event.target as HTMLTextAreaElement).value)"
        />
        <div v-if="searchQueries.length" class="flex flex-wrap gap-1.5">
          <span
            v-for="query in searchQueries"
            :key="query"
            class="px-2 py-0.5 text-[11px] rounded-md bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-300 border border-sky-200/80 dark:border-sky-800/40"
          >{{ query }}</span>
        </div>
        <p class="text-[10px] text-gray-500">{{ t('manga.ehentaiSearchHint') }}</p>
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiCats') }}</label>
        <input
          :value="settings.ehentai_cats || '704'"
          class="ui-input"
          @input="emit('update', 'ehentai_cats', ($event.target as HTMLInputElement).value)"
        >
        <p class="text-[10px] text-gray-500">{{ t('manga.ehentaiCatsHint') }}</p>
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiMaxPages') }}</label>
        <input
          :value="settings.ehentai_max_pages ?? 400"
          type="number"
          min="1"
          max="2000"
          class="ui-input"
          @input="emit('update', 'ehentai_max_pages', Math.max(1, Math.min(2000, Number(($event.target as HTMLInputElement).value) || 400)))"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiSearchPages') }}</label>
        <input
          :value="settings.ehentai_search_pages ?? 1"
          type="number"
          min="1"
          max="10"
          class="ui-input"
          @input="emit('update', 'ehentai_search_pages', Math.max(1, Math.min(10, Number(($event.target as HTMLInputElement).value) || 1)))"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiDelay') }}</label>
        <input
          :value="settings.ehentai_delay_seconds ?? 1.2"
          type="number"
          min="0"
          step="0.1"
          class="ui-input"
          @input="emit('update', 'ehentai_delay_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiGalleryDelay') }}</label>
        <input
          :value="settings.ehentai_gallery_delay_seconds ?? 5"
          type="number"
          min="0"
          step="0.5"
          class="ui-input"
          @input="emit('update', 'ehentai_gallery_delay_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiPoll') }}</label>
        <input
          :value="settings.ehentai_poll_seconds ?? 300"
          type="number"
          min="60"
          class="ui-input"
          @input="emit('update', 'ehentai_poll_seconds', Math.max(60, Number(($event.target as HTMLInputElement).value) || 300))"
        >
      </div>
    </div>

    <div class="space-y-3 p-3 border border-gray-200 dark:border-gray-800/60">
      <div class="flex flex-col sm:flex-row sm:items-start justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiTranslationTitle') }}</div>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiTranslationHint') }}</p>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <a
            class="ui-btn-secondary !px-3 !py-1.5 !text-xs"
            :href="translations?.editor_url || 'https://ehtt.vercel.app/list/all'"
            target="_blank"
            rel="noopener"
          >
            <ExternalLink class="w-3.5 h-3.5" />
            {{ t('manga.ehentaiTranslationOpen') }}
          </a>
          <button type="button" class="ui-btn-primary !px-3 !py-1.5 !text-xs" :disabled="translating" @click="refreshTranslations">
            <LoaderCircle v-if="translating" class="w-3.5 h-3.5 animate-spin" />
            <RefreshCw v-else class="w-3.5 h-3.5" />
            {{ t('manga.ehentaiTranslationRefresh') }}
          </button>
        </div>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-4 gap-2 text-xs text-gray-600 dark:text-gray-300">
        <div>{{ t('manga.ehentaiTranslationTags') }}：<span class="tabular-nums font-medium">{{ translations?.tag_count ?? 0 }}</span></div>
        <div>{{ t('manga.ehentaiTranslationNamespaces') }}：<span class="tabular-nums font-medium">{{ translations?.namespaces ?? 0 }}</span></div>
        <div>SHA：<span class="font-mono">{{ shaShort || '-' }}</span></div>
        <div>{{ translations?.using_bundled ? t('manga.ehentaiTranslationBundled') : t('manga.ehentaiTranslationCached') }}</div>
      </div>
      <p v-if="translations?.updated_at" class="text-[10px] text-gray-400">{{ t('manga.ehentaiTranslationUpdated') }} {{ translations.updated_at }}</p>
      <div class="flex items-center justify-between gap-4 p-3 border border-gray-200 dark:border-gray-800/60">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiTranslationAuto') }}</div>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiTranslationAutoHint') }}</p>
        </div>
        <button
          type="button"
          class="ui-switch"
          role="switch"
          :aria-checked="settings.ehentai_translation_auto !== false"
          :class="settings.ehentai_translation_auto !== false ? 'ui-switch-on' : ''"
          @click="emit('update', 'ehentai_translation_auto', settings.ehentai_translation_auto === false)"
        >
          <span class="ui-switch-knob" />
        </button>
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.ehentaiTranslationUrl') }}</label>
        <input
          :value="settings.ehentai_translation_url || ''"
          class="ui-input"
          :placeholder="t('manga.ehentaiTranslationUrlPlaceholder')"
          @input="emit('update', 'ehentai_translation_url', ($event.target as HTMLInputElement).value)"
        >
        <p class="text-[10px] text-gray-500">{{ t('manga.ehentaiTranslationUrlHint') }}</p>
      </div>
    </div>

  </section>

    <section class="space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.ehentaiRecent') }}</h3>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.ehentaiRecentHint') }}</p>
        </div>
        <form class="flex gap-2 w-full sm:w-auto" @submit.prevent="search">
          <div class="relative flex-1 sm:w-64">
            <Search class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
            <input v-model="searchQuery" class="ui-input !pl-9" :placeholder="t('manga.searchPlaceholder')">
          </div>
          <button type="submit" class="ui-btn-secondary !px-3" :disabled="catalogLoading">
            <Search class="w-4 h-4" /><span class="sr-only">{{ t('common.search') }}</span>
          </button>
        </form>
      </div>
      <p class="text-[11px] text-gray-500 tabular-nums">{{ t('manga.ehentaiCatalogCount') }} · {{ catalogTotal }}</p>
      <div v-if="catalogLoading" class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4" aria-busy="true">
        <div v-for="i in 6" :key="i" class="ui-card p-4 flex gap-4">
          <div class="ui-skeleton w-20 h-28 shrink-0" />
          <div class="flex-1 space-y-3">
            <div class="ui-skeleton h-4 w-3/4" />
            <div class="ui-skeleton h-3 w-1/2" />
            <div class="ui-skeleton h-3 w-full" />
          </div>
        </div>
      </div>
      <div v-else-if="!catalog.length" class="ui-card p-10 text-center text-sm text-gray-500">{{ t('manga.ehentaiRecentEmpty') }}</div>
      <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
        <HmwMangaCard
          v-for="manga in catalog"
          :key="manga.id"
          :title="manga.title"
          :id-label="manga.id"
          :author="manga.author"
          :cover-url="manga.cover_url"
          :page-count="manga.page_count"
          :chapter-count="manga.chapter_count"
          :updated-at="manga.updated_at"
          :public-url="mangaSiteUrl(manga)"
          @click="openManga(manga)"
        />
      </div>
      <div v-if="catalogPages > 1" class="flex items-center justify-center gap-3 pt-2">
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage <= 1 || catalogLoading" @click="goPage(catalogPage - 1)">{{ t('common.prev') }}</button>
        <span class="text-xs text-gray-500 tabular-nums">{{ catalogPage }} / {{ catalogPages }}</span>
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage >= catalogPages || catalogLoading" @click="goPage(catalogPage + 1)">{{ t('common.next') }}</button>
      </div>
    </section>
  </div>
</template>

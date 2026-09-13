<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { LoaderCircle, Radio, Save, Search } from 'lucide-vue-next'
import {
  getWnacgStatus,
  listMangaCatalog,
  runWnacgPass,
  type MangaSettings,
  type MangaSummary,
  type WnacgCategory,
  type WnacgRuntimeStatus,
} from '../../lib/api'
import { formatTimeOnly } from '../../lib/datetime'
import HmwMangaCard from './HmwMangaCard.vue'
import UploadTargetSelect from './UploadTargetSelect.vue'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { getErrorMessage } from '../../lib/types'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'

const props = defineProps<{
  settings: MangaSettings
  saving: boolean
}>()

const emit = defineEmits<{
  update: [key: keyof MangaSettings, value: MangaSettings[keyof MangaSettings]]
  save: []
}>()

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const status = ref<WnacgRuntimeStatus | null>(null)
const catalog = ref<MangaSummary[]>([])
const catalogTotal = ref(0)
const catalogPage = ref(1)
const catalogPages = ref(0)
const catalogLoading = ref(false)
const searchQuery = ref('')
const workerLoading = ref(false)
let pollTimer: number | undefined

const running = computed(
  () => status.value?.worker_status === 'running' || status.value?.worker_status === 'starting',
)
const listeningOn = computed(() => Boolean(props.settings.wnacg_enabled) || running.value)
const nextPassLabel = computed(() => formatTimeOnly(status.value?.next_pass_at || '', ''))
const siteOrigin = computed(() => {
  const raw = props.settings.outbound_site_base || props.settings.site_publish_url || ''
  try {
    return raw ? new URL(raw).origin : ''
  } catch {
    return ''
  }
})

const categories = computed<WnacgCategory[]>(() => status.value?.categories || [])
const categoryGroups = computed(() => {
  const groups: { id: string; label: string; items: WnacgCategory[] }[] = []
  const index = new Map<string, { id: string; label: string; items: WnacgCategory[] }>()
  for (const item of categories.value) {
    let group = index.get(item.group)
    if (!group) {
      group = { id: item.group, label: item.group_label, items: [] }
      index.set(item.group, group)
      groups.push(group)
    }
    group.items.push(item)
  }
  return groups
})

const selectedIds = computed(() => {
  const raw = String(props.settings.wnacg_categories || '')
  return new Set(
    raw
      .split(/[\s,，、;；]+/)
      .map((item) => item.trim())
      .filter(Boolean),
  )
})

const selectedCount = computed(() => selectedIds.value.size)

const toggleCategory = (id: string) => {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  const ordered = categories.value.map((item) => item.id).filter((item) => next.has(item))
  emit('update', 'wnacg_categories', ordered.join(','))
}

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
    const result = await listMangaCatalog(token, page, 12, searchQuery.value, 'wnacg')
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
    status.value = await getWnacgStatus(token)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.wnacgLoadFailed')
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
    const result = await runWnacgPass(token)
    status.value = result.status
    toast.success(result.message)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.wnacgWorkerFailed')
    await loadStatus()
  } finally {
    workerLoading.value = false
  }
}

const toggleEnableListen = () => {
  emit('update', 'wnacg_enabled', !props.settings.wnacg_enabled)
  emit('save')
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
  () => props.settings.wnacg_enabled,
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
          <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.wnacgTitle') }}</h3>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.wnacgHint') }}</p>
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
            :title="t('manga.wnacgRunOnceHint')"
            @click="runOnce"
          >
            <LoaderCircle v-if="workerLoading" class="w-3.5 h-3.5 animate-spin" />
            <Radio v-else class="w-3.5 h-3.5" />
            {{ t('manga.wnacgStart') }}
          </button>
        </div>
      </div>

      <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div class="p-3 border border-gray-200 dark:border-gray-800/60">
          <div class="ui-section-label">{{ t('manga.wnacgStatus') }}</div>
          <div class="mt-1 text-sm font-medium" :class="running ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            {{ statusLabel() }}
          </div>
        </div>
        <div class="p-3 border border-gray-200 dark:border-gray-800/60">
          <div class="ui-section-label">{{ t('manga.wnacgCategories') }}</div>
          <div class="mt-1 text-sm font-semibold tabular-nums">{{ selectedCount }}</div>
        </div>
        <div class="p-3 border border-gray-200 dark:border-gray-800/60">
          <div class="ui-section-label">{{ t('manga.wnacgProcessed') }}</div>
          <div class="mt-1 text-sm font-semibold tabular-nums">{{ status?.processed ?? 0 }}</div>
        </div>
        <div class="p-3 border border-gray-200 dark:border-gray-800/60">
          <div class="ui-section-label">{{ t('manga.wnacgSkipped') }}</div>
          <div class="mt-1 text-sm font-semibold tabular-nums">{{ status?.skipped ?? 0 }} / {{ status?.failed ?? 0 }}</div>
        </div>
      </div>

      <p v-if="status?.current?.title" class="text-xs text-gray-500">
        {{ t('manga.wnacgCurrent') }}：{{ status.current.title }}
        <span v-if="status.current.total" class="tabular-nums">（{{ status.current.pages || 0 }}/{{ status.current.total }}）</span>
      </p>
      <p v-else-if="running && status?.phase === 'waiting' && nextPassLabel" class="text-xs text-gray-500">
        {{ t('manga.wnacgNextPass') }}：{{ nextPassLabel }}
      </p>
      <p v-if="status?.last_error" class="text-xs text-rose-600 dark:text-rose-300 break-all">{{ getErrorMessage(status.last_error) }}</p>

      <div class="flex items-center justify-between gap-4 p-3 border border-sky-200 dark:border-sky-800/40 bg-sky-50/70 dark:bg-sky-500/10">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.wnacgEnable') }}</div>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.wnacgEnableHint') }}</p>
        </div>
        <button
          type="button"
          class="ui-switch"
          role="switch"
          :aria-checked="Boolean(settings.wnacg_enabled)"
          :class="settings.wnacg_enabled ? 'ui-switch-on' : ''"
          :disabled="saving"
          @click="toggleEnableListen"
        >
          <span class="ui-switch-knob" />
        </button>
      </div>

      <div class="space-y-1.5">
        <label class="ui-label">{{ t('manga.uploadWnacg') }}</label>
        <UploadTargetSelect :model-value="settings.upload_wnacg || 'imgbed'" @update:model-value="emit('update', 'upload_wnacg', $event)" />
        <p class="text-[10px] text-gray-500">{{ t('manga.uploadSourceHint') }}</p>
      </div>

      <div class="space-y-3">
        <div>
          <label class="ui-label">{{ t('manga.wnacgCategories') }}</label>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.wnacgCategoriesHint') }}</p>
        </div>
        <div v-if="!categoryGroups.length" class="text-xs text-gray-500">{{ t('common.loading') }}</div>
        <div v-else class="space-y-4">
          <div v-for="group in categoryGroups" :key="group.id" class="space-y-2">
            <div class="text-[11px] font-medium tracking-wide text-gray-500 dark:text-gray-400">{{ group.label }}</div>
            <div class="flex flex-wrap gap-2">
              <button
                v-for="item in group.items"
                :key="item.id"
                type="button"
                class="px-2.5 py-1 text-xs border transition-colors"
                :class="selectedIds.has(item.id)
                  ? 'border-sky-400 bg-sky-50 text-sky-800 dark:border-sky-700 dark:bg-sky-500/15 dark:text-sky-200'
                  : 'border-gray-200 text-gray-600 hover:border-gray-300 dark:border-gray-800 dark:text-gray-300'"
                :aria-pressed="selectedIds.has(item.id)"
                @click="toggleCategory(item.id)"
              >
                {{ item.label }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="grid grid-cols-1 lg:grid-cols-2 gap-x-6 gap-y-4">
        <div class="lg:col-span-2 space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgBaseUrl') }}</label>
          <input
            :value="settings.wnacg_base_url || 'https://www.wnacg.com'"
            class="ui-input font-mono text-sm"
            :placeholder="t('manga.wnacgBaseUrlPlaceholder')"
            @input="emit('update', 'wnacg_base_url', ($event.target as HTMLInputElement).value)"
          >
          <p class="text-[10px] text-gray-500">{{ t('manga.wnacgBaseUrlHint') }}</p>
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgMaxPages') }}</label>
          <input
            :value="settings.wnacg_max_pages ?? 400"
            type="number"
            min="1"
            max="2000"
            class="ui-input"
            @input="emit('update', 'wnacg_max_pages', Math.max(1, Math.min(2000, Number(($event.target as HTMLInputElement).value) || 400)))"
          >
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgListPages') }}</label>
          <input
            :value="settings.wnacg_list_pages ?? 1"
            type="number"
            min="1"
            max="10"
            class="ui-input"
            @input="emit('update', 'wnacg_list_pages', Math.max(1, Math.min(10, Number(($event.target as HTMLInputElement).value) || 1)))"
          >
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgDelay') }}</label>
          <input
            :value="settings.wnacg_delay_seconds ?? 1"
            type="number"
            min="0"
            step="0.1"
            class="ui-input"
            @input="emit('update', 'wnacg_delay_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"
          >
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgGalleryDelay') }}</label>
          <input
            :value="settings.wnacg_gallery_delay_seconds ?? 3"
            type="number"
            min="0"
            step="0.5"
            class="ui-input"
            @input="emit('update', 'wnacg_gallery_delay_seconds', Math.max(0, Number(($event.target as HTMLInputElement).value) || 0))"
          >
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('manga.wnacgPoll') }}</label>
          <input
            :value="settings.wnacg_poll_seconds ?? 300"
            type="number"
            min="60"
            class="ui-input"
            @input="emit('update', 'wnacg_poll_seconds', Math.max(60, Number(($event.target as HTMLInputElement).value) || 300))"
          >
        </div>
      </div>
    </section>

    <section class="space-y-4">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.wnacgRecent') }}</h3>
          <p class="text-xs text-gray-500 mt-1">{{ t('manga.wnacgRecentHint') }}</p>
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
      <p class="text-[11px] text-gray-500 tabular-nums">{{ t('manga.wnacgCatalogCount') }} · {{ catalogTotal }}</p>
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
      <div v-else-if="!catalog.length" class="ui-card p-10 text-center text-sm text-gray-500">{{ t('manga.wnacgRecentEmpty') }}</div>
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

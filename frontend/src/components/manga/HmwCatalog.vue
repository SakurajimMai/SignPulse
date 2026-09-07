<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ExternalLink, LoaderCircle, Search } from 'lucide-vue-next'
import {
  deleteHmwChapter,
  deleteHmwManga,
  getHmwCatalogManga,
  listHmwCatalog,
  syncHmwCatalog,
  updateHmwChapter,
  updateHmwManga,
  type HmwCatalogEntry,
  type HmwLibraryManga,
} from '../../lib/api'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { useConfirm } from '../../composables/useConfirm'
import HmwMangaCard from './HmwMangaCard.vue'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'

const props = defineProps<{
  liveCount?: number
  running?: boolean
}>()

const emit = defineEmits<{
  'use-for-append': [manga: HmwLibraryManga]
}>()

const { t } = useI18n()
const toast = useToast()
const { confirm } = useConfirm()
const token = getAuthToken()

const catalog = ref<HmwCatalogEntry[]>([])
const catalogTotal = ref(0)
const catalogPage = ref(1)
const catalogPages = ref(0)
const catalogLoading = ref(false)
const searchQuery = ref('')
const busy = ref('')
const selected = ref<HmwLibraryManga | null>(null)
const draft = ref({
  title: '',
  slug: '',
  author: '',
  alternative_title: '',
  status: 'completed',
  description: '',
})
const chapterDrafts = ref<Array<{ id: number; number: number; title: string; page_count: number }>>([])

const applyDetail = (manga: HmwLibraryManga) => {
  selected.value = {
    ...manga,
    public_url: manga.public_url || selected.value?.public_url,
  }
  draft.value = {
    title: manga.title || '',
    slug: manga.slug || '',
    author: manga.author || '',
    alternative_title: manga.alternative_title || '',
    status: manga.status || 'completed',
    description: manga.description || '',
  }
  chapterDrafts.value = (manga.chapters || [])
    .filter((item) => item.id != null)
    .map((item) => ({
      id: Number(item.id),
      number: Number(item.number),
      title: item.title || '',
      page_count: Number(item.page_count || item.pages || 0),
    }))
    .sort((a, b) => a.number - b.number)
}

const loadCatalog = async (page = catalogPage.value) => {
  if (!token) return
  catalogLoading.value = true
  try {
    const result = await listHmwCatalog(token, page, 12, searchQuery.value)
    catalog.value = result.data || []
    catalogTotal.value = result.total
    catalogPage.value = result.page
    catalogPages.value = result.total_pages
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLoadFailed')
  } finally {
    catalogLoading.value = false
  }
}

const goPage = async (page: number) => {
  if (page < 1 || (catalogPages.value > 0 && page > catalogPages.value)) return
  await loadCatalog(page)
}

const search = async () => {
  await loadCatalog(1)
}

const syncFromSite = async () => {
  if (!token) return
  busy.value = 'sync'
  try {
    const result = await syncHmwCatalog(token, { query: searchQuery.value.trim(), language: 'zh' })
    toast.success(t('manga.hmwCatalogSynced', { count: result.imported }))
    await loadCatalog(1)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwSyncFailed')
  } finally {
    busy.value = ''
  }
}

const openManage = async (item: HmwCatalogEntry) => {
  if (!token) return
  busy.value = `open:${item.id}`
  try {
    const result = await getHmwCatalogManga(token, item.id)
    applyDetail({
      ...item,
      ...result.manga,
      id: result.manga.id || item.id,
      public_url: result.catalog?.public_url || result.manga.public_url || item.public_url,
      chapters: result.manga.chapters || item.chapters || [],
      version: result.manga.version || result.catalog?.version || item.version,
    })
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLibraryFailed')
  } finally {
    busy.value = ''
  }
}

const closeManage = () => {
  selected.value = null
  chapterDrafts.value = []
}

const saveManga = async () => {
  if (!token || !selected.value) return
  busy.value = 'save-manga'
  try {
    const result = await updateHmwManga(token, selected.value.id, {
      title: draft.value.title.trim(),
      slug: draft.value.slug.trim(),
      author: draft.value.author.trim(),
      alternative_title: draft.value.alternative_title.trim(),
      status: draft.value.status,
      description: draft.value.description,
      expected_version: selected.value.version || null,
    })
    applyDetail({
      ...selected.value,
      ...result.manga,
      public_url: result.catalog?.public_url || result.manga.public_url || selected.value.public_url,
      chapters: result.manga.chapters || selected.value.chapters,
    })
    toast.success(t('manga.hmwManageSaved'))
    await loadCatalog(catalogPage.value)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwManageFailed')
  } finally {
    busy.value = ''
  }
}

const removeManga = async () => {
  if (!token || !selected.value) return
  const ok = await confirm({
    title: t('manga.hmwDeleteManga'),
    message: t('manga.hmwDeleteMangaConfirm'),
    danger: true,
  })
  if (!ok) return
  busy.value = 'delete-manga'
  try {
    await deleteHmwManga(token, selected.value.id, selected.value.version)
    toast.success(t('manga.hmwMangaDeleted'))
    closeManage()
    await loadCatalog(catalogPage.value)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwManageFailed')
  } finally {
    busy.value = ''
  }
}

const saveChapter = async (index: number) => {
  if (!token || !selected.value) return
  const chapter = chapterDrafts.value[index]
  if (!chapter?.id) return
  busy.value = `save-ch:${chapter.id}`
  try {
    const result = await updateHmwChapter(token, chapter.id, {
      number: Number(chapter.number),
      title: chapter.title.trim(),
      expected_version: selected.value.version,
      manga_id: selected.value.id,
    })
    if (result.manga) applyDetail(result.manga)
    toast.success(t('manga.hmwChapterSaved'))
    await loadCatalog(catalogPage.value)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwManageFailed')
  } finally {
    busy.value = ''
  }
}

const removeChapter = async (index: number) => {
  if (!token || !selected.value) return
  const chapter = chapterDrafts.value[index]
  if (!chapter?.id) return
  const ok = await confirm({
    title: t('manga.hmwDeleteChapter'),
    message: t('manga.hmwDeleteChapterConfirm', { number: chapter.number }),
    danger: true,
  })
  if (!ok) return
  busy.value = `del-ch:${chapter.id}`
  try {
    const result = await deleteHmwChapter(token, chapter.id, {
      expected_version: selected.value.version,
      manga_id: selected.value.id,
    })
    if (result.manga) applyDetail(result.manga)
    toast.success(t('manga.hmwChapterDeleted'))
    await loadCatalog(catalogPage.value)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwManageFailed')
  } finally {
    busy.value = ''
  }
}

const appendChapters = () => {
  if (!selected.value) return
  emit('use-for-append', selected.value)
}

const updateChapterDraft = (index: number, key: 'number' | 'title', value: string | number) => {
  const next = [...chapterDrafts.value]
  next[index] = { ...next[index], [key]: value }
  chapterDrafts.value = next
}

watch(
  () => [props.liveCount, props.running] as const,
  ([count, running], previous) => {
    const prevCount = previous?.[0]
    const prevRunning = previous?.[1]
    if ((count && count !== prevCount) || (prevRunning && !running)) {
      void loadCatalog(catalogPage.value)
    }
  },
)

onMounted(() => {
  void loadCatalog(1)
})

defineExpose({ reload: () => loadCatalog(catalogPage.value) })
</script>

<template>
  <section class="space-y-4">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div>
        <h3 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwCatalogTitle') }}</h3>
        <p class="text-xs text-gray-500 mt-1">{{ t('manga.hmwCatalogHint') }}</p>
      </div>
      <form class="flex flex-wrap gap-2 w-full sm:w-auto" @submit.prevent="search">
        <div class="relative flex-1 sm:w-64">
          <Search class="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input v-model="searchQuery" class="ui-input !pl-9" :placeholder="t('manga.searchPlaceholder')">
        </div>
        <button type="submit" class="ui-btn-secondary !px-3" :disabled="catalogLoading">
          <Search class="w-4 h-4" /><span class="sr-only">{{ t('common.search') }}</span>
        </button>
        <button type="button" class="ui-btn-secondary !px-3 !text-xs" :disabled="busy === 'sync'" @click="syncFromSite">
          <LoaderCircle v-if="busy === 'sync'" class="w-3 h-3 animate-spin" />
          {{ t('manga.hmwCatalogSync') }}
        </button>
      </form>
    </div>

    <p class="text-[11px] text-gray-500 tabular-nums">{{ t('manga.hmwCatalogCount') }} · {{ catalogTotal }}</p>

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
    <div v-else-if="!catalog.length" class="ui-card p-10 text-center text-sm text-gray-500">{{ t('manga.hmwCatalogEmpty') }}</div>
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
        :updated-at="manga.last_published_at || manga.updated_at"
        :public-url="manga.public_url"
        :selected="selected?.id === manga.id"
        @click="openManage(manga)"
      />
    </div>
    <div v-if="catalogPages > 1" class="flex items-center justify-center gap-3 pt-2">
      <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage <= 1 || catalogLoading" @click="goPage(catalogPage - 1)">{{ t('common.prev') }}</button>
      <span class="text-xs text-gray-500 tabular-nums">{{ catalogPage }} / {{ catalogPages }}</span>
      <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage >= catalogPages || catalogLoading" @click="goPage(catalogPage + 1)">{{ t('common.next') }}</button>
    </div>

    <section v-if="selected" class="ui-card p-5 sm:p-6 space-y-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwManage') }} · #{{ selected.id }}</div>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.hmwManageHint') }}</p>
          <p class="text-[10px] text-sky-600 dark:text-sky-400 mt-1">{{ t('manga.hmwAppendHowTo') }}</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="appendChapters">{{ t('manga.hmwAppendFromCatalog') }}</button>
          <a v-if="selected.public_url || selected.slug" :href="selected.public_url || `https://www.hmw.app/${selected.language || 'zh'}/manga/${encodeURIComponent(selected.slug || '')}`" target="_blank" rel="noopener" class="ui-btn-secondary !px-3 !py-1.5 !text-xs inline-flex items-center gap-1">
            {{ t('manga.hmwOpenPublic') }} <ExternalLink class="w-3 h-3" />
          </a>
          <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="closeManage">{{ t('common.close') }}</button>
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwTitleField') }}</label><input v-model="draft.title" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwSlug') }}</label><input v-model="draft.slug" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwAuthor') }}</label><input v-model="draft.author" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwAltTitle') }}</label><input v-model="draft.alternative_title" class="ui-input"></div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('manga.hmwStatus') }}</label>
          <select v-model="draft.status" class="ui-input">
            <option value="completed">{{ t('manga.hmwStatusCompleted') }}</option>
            <option value="ongoing">{{ t('manga.hmwStatusOngoing') }}</option>
            <option value="hiatus">{{ t('manga.hmwStatusHiatus') }}</option>
          </select>
        </div>
        <div class="md:col-span-2 space-y-1"><label class="ui-label">{{ t('manga.hmwDescription') }}</label><textarea v-model="draft.description" rows="3" class="ui-input" /></div>
      </div>
      <div class="flex flex-wrap gap-2">
        <button type="button" class="ui-btn-primary !px-3 !py-1.5 !text-xs" :disabled="Boolean(busy)" @click="saveManga">
          <LoaderCircle v-if="busy === 'save-manga'" class="w-3 h-3 animate-spin" />
          {{ t('manga.hmwSaveManga') }}
        </button>
        <button type="button" class="ui-btn-danger !px-3 !py-1.5 !text-xs" :disabled="Boolean(busy)" @click="removeManga">{{ t('manga.hmwDeleteManga') }}</button>
      </div>

      <div class="text-sm font-medium text-gray-900 dark:text-gray-100 pt-2">{{ t('manga.hmwChapters') }} · {{ chapterDrafts.length }}</div>
      <div v-if="!chapterDrafts.length" class="text-xs text-gray-500">{{ t('common.noData') }}</div>
      <div v-else class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="text-[11px] text-gray-500">
            <tr>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterId') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterNumber') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterTitle') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterPages') }}</th>
              <th class="text-left py-1" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="(chapter, index) in chapterDrafts" :key="chapter.id" class="border-t border-gray-100 dark:border-gray-800">
              <td class="py-2 pr-3 font-mono text-[11px] text-gray-500">{{ chapter.id }}</td>
              <td class="py-2 pr-3 w-24"><input :value="chapter.number" type="number" step="0.1" class="ui-input !py-1" @input="updateChapterDraft(index, 'number', Number(($event.target as HTMLInputElement).value))"></td>
              <td class="py-2 pr-3"><input :value="chapter.title" class="ui-input !py-1" @input="updateChapterDraft(index, 'title', ($event.target as HTMLInputElement).value)"></td>
              <td class="py-2 pr-3 text-gray-500 tabular-nums">{{ chapter.page_count }}</td>
              <td class="py-2 whitespace-nowrap text-right">
                <button type="button" class="ui-btn-secondary !px-2 !py-1 !text-xs mr-2" :disabled="Boolean(busy)" @click="saveChapter(index)">{{ t('manga.hmwSaveChapter') }}</button>
                <button type="button" class="ui-btn-danger !px-2 !py-1 !text-xs" :disabled="Boolean(busy)" @click="removeChapter(index)">{{ t('manga.hmwDeleteChapter') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

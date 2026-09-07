<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ExternalLink, LoaderCircle } from 'lucide-vue-next'
import {
  cancelHmwJob,
  downloadHmwTelegramDoc,
  listHmwCatalog,
  listHmwJobs,
  listHmwSources,
  listHmwTelegramDocs,
  getHmwLibraryManga,
  listHmwLibrary,
  pullHmwTelegramAlbum,
  preflightHmw,
  resumeHmwJob,
  scanHmwSource,
  startHmwJob,
  unzipHmwSource,
  uploadHmwSource,
  type HmwChapterMap,
  type HmwCatalogEntry,
  type HmwJobStatus,
  type HmwPreflightPlan,
  type HmwRuntimeStatus,
  type HmwLibraryManga,
  type HmwSourceItem,
  type HmwTelegramDoc,
  type MangaSettings,
} from '../../lib/api'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'
import { useAccountsStore } from '../../stores/accounts'
import HmwCatalog from './HmwCatalog.vue'
import HmwMangaCard from './HmwMangaCard.vue'

const props = defineProps<{
  settings: MangaSettings | null
  status: HmwRuntimeStatus | null
}>()

const emit = defineEmits<{
  'refresh-status': []
}>()

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const accountsStore = useAccountsStore()

const sources = ref<HmwSourceItem[]>([])
const jobs = ref<HmwJobStatus[]>([])
const catalogIndex = ref<Map<string, HmwCatalogEntry>>(new Map())
const docs = ref<HmwTelegramDoc[]>([])
const selectedPath = ref('')
const zipPassword = ref('')
const telegramChat = ref('')
const telegramAccount = ref('')
const albumUrl = ref('')
const title = ref('')
const slug = ref('')
const author = ref('Ann')
const genres = ref('3D')
const language = ref('zh')
const statusValue = ref('completed')
const description = ref('')
const chapters = ref<HmwChapterMap[]>([])
const preflight = ref<HmwPreflightPlan | null>(null)
const busy = ref('')
const uploading = ref(false)
const libraryQuery = ref('')
const libraryHits = ref<HmwLibraryManga[]>([])
const existingManga = ref<HmwLibraryManga | null>(null)

const job = computed(() => props.status)
const running = computed(() => Boolean(job.value?.running))
const JOB_PAGE_SIZE = 12
const jobPage = ref(1)
const jobPages = computed(() => {
  const total = jobs.value.length
  return total ? Math.ceil(total / JOB_PAGE_SIZE) : 0
})
const pagedJobs = computed(() => {
  const start = (Math.max(1, jobPage.value) - 1) * JOB_PAGE_SIZE
  return jobs.value.slice(start, start + JOB_PAGE_SIZE)
})
const progressLabel = computed(() => {
  const current = job.value?.current || 0
  const total = job.value?.total || 0
  if (!total) return job.value?.message || ''
  return `${current}/${total} ${job.value?.message || ''}`
})
const progressPct = computed(() => {
  const total = job.value?.total || 0
  if (!total) return 0
  return Math.min(100, Math.round(((job.value?.current || 0) / total) * 100))
})

const loadSources = async () => {
  if (!token) return
  try {
    const result = await listHmwSources(token)
    sources.value = result.data || []
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLoadFailed')
  }
}

const loadJobs = async () => {
  if (!token) return
  try {
    const [result, catalog] = await Promise.all([
      listHmwJobs(token),
      listHmwCatalog(token, 1, 48).catch(() => ({ data: [] as HmwCatalogEntry[] })),
    ])
    jobs.value = result.data || []
    if (jobPage.value > 1 && (jobPage.value - 1) * JOB_PAGE_SIZE >= jobs.value.length) {
      jobPage.value = Math.max(1, Math.ceil(jobs.value.length / JOB_PAGE_SIZE) || 1)
    }
    const next = new Map<string, HmwCatalogEntry>()
    for (const item of catalog.data || []) {
      if (item.id != null) next.set(`id:${item.id}`, item)
      if (item.slug) next.set(`slug:${item.slug}`, item)
    }
    catalogIndex.value = next
  } catch {
    /* 列表失败不挡主流程 */
  }
}

const jobCover = (item: HmwJobStatus) =>
  item.cover_url
  || catalogIndex.value.get(item.manga_id != null ? `id:${item.manga_id}` : '')?.cover_url
  || catalogIndex.value.get(item.slug ? `slug:${item.slug}` : '')?.cover_url
  || null

const jobMeta = (item: HmwJobStatus) =>
  catalogIndex.value.get(item.manga_id != null ? `id:${item.manga_id}` : '')
  || catalogIndex.value.get(item.slug ? `slug:${item.slug}` : '')
  || null

const openJob = (item: HmwJobStatus) => {
  if (item.public_url) window.open(item.public_url, '_blank', 'noopener')
}

onMounted(() => {
  void accountsStore.ensureAccounts()
  telegramAccount.value = props.settings?.telegram_account_name || ''
  void loadSources()
  void loadJobs()
})

watch(
  () => props.status?.running,
  (running, previous) => {
    if (previous && !running) {
      void loadSources()
      void loadJobs()
    }
  },
)

const existingNumbers = computed(() => {
  const nums = new Set<number>()
  for (const item of existingManga.value?.chapters || []) {
    nums.add(Number(item.number))
  }
  return nums
})

const fillFromScan = (scan: { root: string; chapters: HmwChapterMap[] }) => {
  selectedPath.value = scan.root
  chapters.value = scan.chapters.map((item) => {
    const number = Number(item.number)
    const already = existingNumbers.value.has(number)
    return { ...item, action: already ? 'skip' : item.action || 'upsert' }
  })
  const folder = scan.root.split('/').pop() || scan.root
  if (!title.value) title.value = folder
  if (!slug.value) slug.value = folder
  preflight.value = null
}

const searchLibrary = async () => {
  if (!token) return
  busy.value = 'library'
  try {
    const result = await listHmwLibrary(token, libraryQuery.value.trim(), language.value)
    const payload = result as { items?: HmwLibraryManga[]; results?: HmwLibraryManga[] }
    libraryHits.value = payload.items || payload.results || []
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLibraryFailed')
  } finally {
    busy.value = ''
  }
}

const applyExisting = (detail: HmwLibraryManga, fallback?: HmwLibraryManga) => {
  existingManga.value = detail
  title.value = detail.title || fallback?.title || title.value
  slug.value = detail.slug || fallback?.slug || slug.value
  author.value = detail.author || fallback?.author || author.value
  description.value = detail.description || fallback?.description || description.value
  language.value = detail.language || fallback?.language || language.value
  statusValue.value = detail.status || fallback?.status || 'ongoing'
  if (chapters.value.length) fillFromScan({ root: selectedPath.value, chapters: chapters.value })
  toast.success(t('manga.hmwAppendReady', {
    title: detail.title || String(detail.id),
    count: (detail.chapters || []).length,
  }))
  document.getElementById('hmw-publish-source')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

const pickExisting = async (item: HmwLibraryManga) => {
  if (!token) return
  if (item.chapters?.length) {
    applyExisting(item)
    return
  }
  busy.value = 'library-detail'
  try {
    const detail = await getHmwLibraryManga(token, item.id)
    applyExisting(detail, item)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLibraryFailed')
  } finally {
    busy.value = ''
  }
}

const clearExisting = () => {
  existingManga.value = null
}

const unzip = async (item: HmwSourceItem) => {
  if (!token) return
  busy.value = `unzip:${item.name}`
  try {
    const extracted = await unzipHmwSource(token, item.name, zipPassword.value)
    toast.success(t('manga.hmwUnzip'))
    await loadSources()
    selectedPath.value = extracted.path
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwUnzipFailed')
  } finally {
    busy.value = ''
  }
}

const scan = async (path = selectedPath.value) => {
  if (!token || !path) return
  busy.value = `scan:${path}`
  try {
    const result = await scanHmwSource(token, path)
    fillFromScan(result)
    toast.success(`${result.chapters.length} / ${result.pages}`)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwScanFailed')
  } finally {
    busy.value = ''
  }
}

const onUpload = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!token || !file) return
  uploading.value = true
  try {
    const saved = await uploadHmwSource(token, file)
    await loadSources()
    selectedPath.value = saved.path
    toast.success(saved.name)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwUnzipFailed')
  } finally {
    uploading.value = false
  }
}

const listDocs = async () => {
  if (!token || !telegramChat.value.trim()) return
  busy.value = 'tg-list'
  try {
    const result = await listHmwTelegramDocs(token, telegramChat.value.trim(), telegramAccount.value)
    docs.value = result.data || []
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLoadFailed')
  } finally {
    busy.value = ''
  }
}

const pullAlbum = async () => {
  if (!token || !albumUrl.value.trim()) return
  busy.value = 'album'
  try {
    const result = await pullHmwTelegramAlbum(token, {
      url: albumUrl.value.trim(),
      account: telegramAccount.value,
    })
    await loadSources()
    if (!existingManga.value) {
      if (result.title) {
        title.value = result.title
        slug.value = result.title
      }
      if (result.author) author.value = result.author
    }
    const scanPath = result.path || result.chapter_path
    if (scanPath) await scan(scanPath)
    toast.success(`${result.chapter_title || result.title || ''} · ${result.pages || 0}`)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwAlbumFailed')
  } finally {
    busy.value = ''
  }
}

const downloadDoc = async (doc: HmwTelegramDoc) => {
  if (!token) return
  busy.value = `tg:${doc.message_id}`
  try {
    const saved = await downloadHmwTelegramDoc(token, {
      chat: telegramChat.value.trim(),
      message_id: doc.message_id,
      account: telegramAccount.value,
    })
    await loadSources()
    selectedPath.value = saved.path
    toast.success(saved.name)
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwLoadFailed')
  } finally {
    busy.value = ''
  }
}

const buildPayload = () => ({
  source_path: selectedPath.value,
  manga: {
    language: language.value,
    title: title.value.trim(),
    slug: slug.value.trim(),
    author: author.value.trim(),
    status: statusValue.value,
    description: description.value,
    genre_names: genres.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean),
  },
  chapters: chapters.value.map((item) => ({
    directory: item.directory,
    number: Number(item.number),
    title: item.title,
    action: item.action,
  })),
})

const runPreflight = async () => {
  if (!token) return
  busy.value = 'preflight'
  try {
    const result = await preflightHmw(token, buildPayload())
    preflight.value = result.preflight
    toast.success(result.preflight.manga_action || 'ok')
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwPublishFailed')
  } finally {
    busy.value = ''
  }
}

const publish = async () => {
  if (!token) return
  busy.value = 'publish'
  try {
    const result = await startHmwJob(token, buildPayload())
    toast.success(result.message)
    emit('refresh-status')
    await loadJobs()
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwPublishFailed')
  } finally {
    busy.value = ''
  }
}

const cancelJob = async () => {
  if (!token || !job.value?.task_id) return
  try {
    await cancelHmwJob(token, job.value.task_id)
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwPublishFailed')
  }
}

const resumeJob = async (taskId: string) => {
  if (!token) return
  busy.value = `resume:${taskId}`
  try {
    const result = await resumeHmwJob(token, taskId)
    toast.success(result.message)
    emit('refresh-status')
    await loadJobs()
  } catch (error: unknown) {
    notifyApiError(error, 'manga.hmwPublishFailed')
  } finally {
    busy.value = ''
  }
}

const updateChapter = (index: number, key: keyof HmwChapterMap, value: string | number) => {
  const next = [...chapters.value]
  next[index] = { ...next[index], [key]: value }
  chapters.value = next
}
</script>

<template>
  <div class="space-y-6">
    <section v-if="job?.running || job?.error || job?.public_url || (job?.live_chapters || []).length" class="ui-card p-5 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ job?.title || job?.slug || t('manga.hmwJob') }}</div>
          <p class="text-[11px] text-gray-500 mt-1">
            <span v-if="job?.chapter_total">{{ t('manga.hmwChapterProgress', { current: job?.chapter_index || 0, total: job?.chapter_total }) }} · </span>
            {{ progressLabel }}
          </p>
        </div>
        <div class="flex gap-2">
          <button v-if="job?.running && job?.task_id" type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="cancelJob">{{ t('manga.hmwCancelJob') }}</button>
          <a v-if="job?.public_url" :href="job?.public_url" target="_blank" rel="noreferrer" class="ui-btn-primary !px-3 !py-1.5 !text-xs inline-flex items-center gap-1">
            {{ t('manga.hmwOpenPublic') }} <ExternalLink class="w-3 h-3" />
          </a>
        </div>
      </div>
      <div class="h-2 rounded bg-gray-100 dark:bg-gray-800 overflow-hidden">
        <div class="h-full bg-sky-500 transition-all" :style="{ width: `${progressPct}%` }" />
      </div>
      <p v-if="job?.error" class="text-xs text-rose-600 dark:text-rose-400">{{ job?.error }}</p>
      <ul v-if="(job?.live_chapters || []).length" class="text-xs text-gray-600 dark:text-gray-300 space-y-1">
        <li v-for="item in (job?.live_chapters || [])" :key="item.number">
          {{ t('manga.hmwLiveChapter', { number: item.number, title: item.title || '', pages: item.pages || 0 }) }}
        </li>
      </ul>
    </section>

    <HmwCatalog
      :live-count="(job?.live_chapters || []).length"
      :running="running"
      @use-for-append="pickExisting"
    />

    <section id="hmw-publish-source" class="ui-card p-5 sm:p-6 space-y-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwSources') }}</div>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.hmwInboxHint') }}</p>
          <p v-if="existingManga" class="text-[10px] text-sky-600 dark:text-sky-400 mt-1">{{ t('manga.hmwAppendHowTo') }}</p>
        </div>
        <div class="flex flex-wrap gap-2">
          <label class="ui-btn-secondary !px-3 !py-1.5 !text-xs cursor-pointer">
            <input type="file" accept=".zip,.7z" class="hidden" :disabled="uploading" @change="onUpload">
            {{ uploading ? t('common.loading') : t('manga.hmwUpload') }}
          </label>
          <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="loadSources">{{ t('manga.hmwRefreshSources') }}</button>
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="space-y-1">
          <label class="ui-label">{{ t('manga.hmwPassword') }}</label>
          <input v-model="zipPassword" type="password" class="ui-input" :placeholder="t('manga.hmwPasswordPlaceholder')">
        </div>
      </div>
      <div v-if="!sources.length" class="text-sm text-gray-500">{{ t('manga.hmwNoSources') }}</div>
      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <tbody>
            <tr v-for="item in sources" :key="item.path" class="border-t border-gray-100 dark:border-gray-800">
              <td class="py-2 pr-3">
                <div class="font-medium text-gray-800 dark:text-gray-100">{{ item.name }}</div>
                <div class="text-[10px] text-gray-500">{{ item.location }} · {{ item.kind === 'archive' ? t('manga.hmwKindArchive') : t('manga.hmwKindDirectory') }} · {{ item.path }}</div>
              </td>
              <td class="py-2 text-right whitespace-nowrap">
                <button v-if="item.kind === 'archive'" type="button" class="ui-btn-secondary !px-2 !py-1 !text-xs mr-2" :disabled="busy.startsWith('unzip')" @click="unzip(item)">{{ t('manga.hmwUnzip') }}</button>
                <button v-else type="button" class="ui-btn-primary !px-2 !py-1 !text-xs" :disabled="Boolean(busy)" @click="scan(item.path)">{{ t('manga.hmwScan') }}</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="ui-card p-5 sm:p-6 space-y-4">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwTelegramTitle') }}</div>
      <p class="text-[10px] text-gray-500">{{ t('manga.hmwAlbumHint') }}</p>
      <div class="space-y-1">
        <label class="ui-label">{{ t('manga.hmwAlbumUrl') }}</label>
        <div class="flex flex-col sm:flex-row gap-2">
          <input v-model="albumUrl" class="ui-input" :placeholder="t('manga.hmwAlbumUrlPlaceholder')">
          <button type="button" class="ui-btn-primary !px-3 !text-xs shrink-0" :disabled="busy === 'album' || !albumUrl.trim()" @click="pullAlbum">
            <LoaderCircle v-if="busy === 'album'" class="w-3 h-3 animate-spin" />
            {{ t('manga.hmwAlbumPull') }}
          </button>
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div class="space-y-1">
          <label class="ui-label">{{ t('manga.ingestAccount') }}</label>
          <select v-model="telegramAccount" class="ui-input">
            <option value="">{{ t('manga.selectAccount') }}</option>
            <option v-for="account in accountsStore.accounts" :key="account.name" :value="account.name">
              {{ account.name }}{{ account.remark ? ` · ${account.remark}` : '' }}
            </option>
          </select>
        </div>
        <div class="space-y-1 md:col-span-2">
          <label class="ui-label">{{ t('manga.hmwTelegramChat') }}</label>
          <div class="flex gap-2">
            <input v-model="telegramChat" class="ui-input" :placeholder="t('manga.hmwTelegramChatPlaceholder')">
            <button type="button" class="ui-btn-secondary !px-3 !text-xs shrink-0" :disabled="busy === 'tg-list'" @click="listDocs">{{ t('manga.hmwTelegramList') }}</button>
          </div>
        </div>
      </div>
      <div v-if="!docs.length" class="text-xs text-gray-500">{{ t('manga.hmwNoDocs') }}</div>
      <ul v-else class="space-y-2">
        <li v-for="doc in docs" :key="doc.message_id" class="flex items-center justify-between gap-3 text-sm">
          <div>
            <div class="font-medium">{{ doc.file_name }}</div>
            <div class="text-[10px] text-gray-500">#{{ doc.message_id }} {{ doc.caption }}</div>
          </div>
          <button type="button" class="ui-btn-secondary !px-2 !py-1 !text-xs" :disabled="Boolean(busy)" @click="downloadDoc(doc)">{{ t('manga.hmwTelegramDownload') }}</button>
        </li>
      </ul>
    </section>

    <section class="ui-card p-5 sm:p-6 space-y-4">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwLibrary') }}</div>
      <p class="text-[10px] text-gray-500">{{ t('manga.hmwLibraryHint') }}</p>
      <div class="flex flex-col sm:flex-row gap-2">
        <input v-model="libraryQuery" class="ui-input" :placeholder="t('manga.hmwLibraryPlaceholder')" @keydown.enter="searchLibrary">
        <button type="button" class="ui-btn-secondary !px-3 !text-xs shrink-0" :disabled="busy.startsWith('library')" @click="searchLibrary">{{ t('common.search') }}</button>
        <button v-if="existingManga" type="button" class="ui-btn-secondary !px-3 !text-xs shrink-0" @click="clearExisting">{{ t('manga.hmwLibraryClear') }}</button>
      </div>
      <p v-if="existingManga" class="text-xs text-emerald-600 dark:text-emerald-400">
        {{ t('manga.hmwLibrarySelected', { title: existingManga.title || existingManga.slug || existingManga.id, count: (existingManga.chapters || []).length }) }}
      </p>
      <ul v-if="libraryHits.length" class="space-y-2 text-sm">
        <li v-for="item in libraryHits" :key="item.id" class="flex items-center justify-between gap-3">
          <div>
            <div class="font-medium">{{ item.title || item.slug }}</div>
            <div class="text-[10px] text-gray-500">{{ item.author }} · {{ item.slug }}</div>
          </div>
          <button type="button" class="ui-btn-secondary !px-2 !py-1 !text-xs" @click="pickExisting(item)">{{ t('manga.hmwLibraryUse') }}</button>
        </li>
      </ul>
    </section>

    <section class="ui-card p-5 sm:p-6 space-y-4">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwMeta') }}</div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwTitleField') }}</label><input v-model="title" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwSlug') }}</label><input v-model="slug" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwAuthor') }}</label><input v-model="author" class="ui-input"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwGenres') }}</label><input v-model="genres" class="ui-input" :placeholder="t('manga.hmwGenresPlaceholder')"></div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('manga.hmwLanguage') }}</label>
          <select v-model="language" class="ui-input">
            <option value="zh">zh</option>
            <option value="en">en</option>
          </select>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('manga.hmwStatus') }}</label>
          <select v-model="statusValue" class="ui-input">
            <option value="completed">{{ t('manga.hmwStatusCompleted') }}</option>
            <option value="ongoing">{{ t('manga.hmwStatusOngoing') }}</option>
            <option value="hiatus">{{ t('manga.hmwStatusHiatus') }}</option>
          </select>
        </div>
        <div class="md:col-span-2 space-y-1"><label class="ui-label">{{ t('manga.hmwDescription') }}</label><textarea v-model="description" rows="3" class="ui-input" /></div>
      </div>
    </section>

    <section v-if="chapters.length" class="ui-card p-5 sm:p-6 space-y-4">
      <div class="flex items-center justify-between gap-3">
        <div class="text-sm font-medium">{{ t('manga.hmwChapters') }} · {{ selectedPath }}</div>
        <div class="flex gap-2">
          <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="Boolean(busy) || running" @click="runPreflight">{{ t('manga.hmwPreflight') }}</button>
          <button type="button" class="ui-btn-primary !px-3 !py-1.5 !text-xs" :disabled="Boolean(busy) || running" @click="publish">
            <LoaderCircle v-if="busy === 'publish'" class="w-3 h-3 animate-spin" />
            {{ t('manga.hmwPublish') }}
          </button>
        </div>
      </div>
      <p v-if="preflight" class="text-xs text-gray-500">{{ preflight.manga_action }} · {{ (preflight.chapters || []).map((item) => `${item.number}:${item.action}`).join(' ') }}</p>
      <div class="overflow-x-auto">
        <table class="min-w-full text-sm">
          <thead class="text-[11px] text-gray-500">
            <tr>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterDir') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterNumber') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterTitle') }}</th>
              <th class="text-left py-1 pr-3">{{ t('manga.hmwChapterPages') }}</th>
              <th class="text-left py-1">{{ t('manga.hmwChapterAction') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(chapter, index) in chapters" :key="chapter.directory" class="border-t border-gray-100 dark:border-gray-800">
              <td class="py-2 pr-3 whitespace-nowrap">{{ chapter.directory }}</td>
              <td class="py-2 pr-3 w-24"><input :value="chapter.number" type="number" step="0.1" class="ui-input !py-1" @input="updateChapter(index, 'number', Number(($event.target as HTMLInputElement).value))"></td>
              <td class="py-2 pr-3"><input :value="chapter.title" class="ui-input !py-1" @input="updateChapter(index, 'title', ($event.target as HTMLInputElement).value)"></td>
              <td class="py-2 pr-3 text-gray-500">{{ chapter.pages }}</td>
              <td class="py-2">
                <select :value="chapter.action" class="ui-input !py-1" @change="updateChapter(index, 'action', ($event.target as HTMLSelectElement).value)">
                  <option value="upsert">{{ t('manga.hmwActionUpsert') }}</option>
                  <option value="skip">{{ t('manga.hmwActionSkip') }}</option>
                </select>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="space-y-4">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwRecentJobs') }}</div>
      <div v-if="!jobs.length" class="ui-card p-10 text-center text-sm text-gray-500">{{ t('common.noData') }}</div>
      <div v-else class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
        <HmwMangaCard
          v-for="item in pagedJobs"
          :key="item.task_id || item.slug"
          :title="item.title || item.slug || ''"
          :id-label="item.manga_id"
          :author="item.author || jobMeta(item)?.author"
          :cover-url="jobCover(item)"
          :page-count="item.page_count ?? jobMeta(item)?.page_count"
          :chapter-count="item.chapter_count || jobMeta(item)?.chapter_count || item.chapter_total"
          :updated-at="item.updated_at || jobMeta(item)?.last_published_at"
          :public-url="item.public_url || jobMeta(item)?.public_url"
          @click="openJob(item)"
        />
      </div>
      <div v-if="jobPages > 1" class="flex items-center justify-center gap-3 pt-2">
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="jobPage <= 1" @click="jobPage = Math.max(1, jobPage - 1)">{{ t('common.prev') }}</button>
        <span class="text-xs text-gray-500 tabular-nums">{{ jobPage }} / {{ jobPages }}</span>
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="jobPage >= jobPages" @click="jobPage = Math.min(jobPages, jobPage + 1)">{{ t('common.next') }}</button>
      </div>
      <div v-if="jobs.some((item) => item.task_id && item.stage === 'uploaded')" class="flex flex-wrap gap-2">
        <button
          v-for="item in jobs.filter((job) => job.task_id && job.stage === 'uploaded')"
          :key="`resume-${item.task_id}`"
          type="button"
          class="ui-btn-secondary !px-2 !py-1 !text-xs"
          @click="resumeJob(item.task_id!)"
        >
          {{ t('manga.hmwResume') }} · {{ item.title || item.slug }}
        </button>
      </div>
    </section>
  </div>
</template>

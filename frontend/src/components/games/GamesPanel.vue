<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ExternalLink, LoaderCircle } from 'lucide-vue-next'
import {
  cancelGamesJob,
  getGamesFile,
  listGamesCatalog,
  listGamesCategories,
  listGamesJobs,
  publishGamesJob,
  pullGamesTelegram,
  uploadGamesSource,
  type GamesCatalogEntry,
  type GamesCategory,
  type GamesJob,
  type GamesRuntimeStatus,
  type GamesSettings,
} from '../../lib/api'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'
import { useAccountsStore } from '../../stores/accounts'
import { isTelegramPostUrl } from '../../lib/telegram-post-url'
import GameCard from './GameCard.vue'

const props = defineProps<{
  settings: GamesSettings | null
  status: GamesRuntimeStatus | null
}>()

const emit = defineEmits<{
  'refresh-status': []
}>()

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const accountsStore = useAccountsStore()

const telegramUrl = ref('')
const telegramAccount = ref('')
const title = ref('')
const summary = ref('')
const tags = ref('')
const selectedCategories = ref<number[]>([640, 637])
const price = ref(5)
const pointsPrice = ref(20)
const vip1Price = ref<number | null>(null)
const vip2Price = ref<number | null>(null)
const vip1Points = ref<number | null>(null)
const vip2Points = ref<number | null>(null)
const payModo = ref('0')
const apate = ref(true)
const payEnabled = ref(true)
const extractPassword = ref('')
const packPassword = ref('')
const manualBaidu = ref('')
const manualPikpak = ref('')
const manualTerabox = ref('')
const manualQuark = ref('')
const cloudOptions = [
  { id: 'baidu', labelKey: 'games.linkBaidu' },
  { id: 'pikpak', labelKey: 'games.linkPikpak' },
  { id: 'terabox', labelKey: 'games.linkTerabox' },
  { id: 'quark', labelKey: 'games.linkQuark' },
] as const
const selectedClouds = ref<string[]>(cloudOptions.map((item) => item.id))
const busy = ref('')
const uploading = ref(false)
const jobId = ref('')
const images = ref<Array<{ path: string; name?: string }>>([])
const archives = ref<Array<{ path: string; name?: string; size?: number }>>([])
const previewUrls = ref<string[]>([])
const categories = ref<GamesCategory[]>([])
const catalog = ref<GamesCatalogEntry[]>([])
const catalogTotal = ref(0)
const catalogPages = ref(0)
const catalogPage = ref(1)
const catalogQuery = ref('')
const jobs = ref<GamesJob[]>([])

const job = computed(() => props.status)
const running = computed(() => Boolean(job.value?.running))
const telegramUrlValid = computed(() => isTelegramPostUrl(telegramUrl.value))
const telegramUrlDirty = computed(() => Boolean(telegramUrl.value.trim()) && !telegramUrlValid.value)
const selectedAccountMissing = computed(() => {
  const name = telegramAccount.value
  if (!name) return false
  return !accountsStore.accounts.some((item) => item.name === name)
})
const canPull = computed(
  () => telegramUrlValid.value && busy.value !== 'pull' && !running.value,
)
const canPublish = computed(
  () =>
    Boolean(jobId.value) &&
    archives.value.length > 0 &&
    selectedClouds.value.length > 0 &&
    busy.value !== 'publish' &&
    !running.value,
)
const cloudConfigured = (id: string) =>
  Boolean((props.status?.clouds as Record<string, { configured?: boolean }> | undefined)?.[id]?.configured)
const progressPct = computed(() => {
  const total = job.value?.total || 0
  if (!total) return 0
  return Math.min(100, Math.round(((job.value?.current || 0) / total) * 100))
})
const progressLabel = computed(() => {
  const current = job.value?.current || 0
  const total = job.value?.total || 0
  if (!total) return job.value?.message || ''
  return `${current}/${total} ${job.value?.message || ''}`
})
const cloudLabel = (id: string) => {
  const found = cloudOptions.find((item) => item.id === id)
  return found ? t(found.labelKey) : id
}
const cloudErrorEntries = computed(() =>
  Object.entries(job.value?.cloud_errors || {}).filter(([, message]) => Boolean(message)),
)
const cloudLinkNames = computed(() =>
  Object.entries(job.value?.links || {})
    .filter(([, url]) => Boolean(url))
    .map(([id]) => cloudLabel(id)),
)
const showJobStatus = computed(
  () =>
    Boolean(job.value?.running || job.value?.error || job.value?.public_url) ||
    cloudErrorEntries.value.length > 0,
)

const revokePreviews = () => {
  for (const url of previewUrls.value) URL.revokeObjectURL(url)
  previewUrls.value = []
}

const applyJob = async (next: GamesJob | null | undefined) => {
  if (!next) return
  if (next.id) jobId.value = String(next.id)
  if (next.title) title.value = next.title
  if (next.summary) summary.value = next.summary
  if (next.tags?.length) tags.value = next.tags.join(', ')
  if (next.category_ids?.length) selectedCategories.value = [...next.category_ids]
  if (next.price != null) price.value = Number(next.price)
  if (next.points_price != null) pointsPrice.value = Number(next.points_price)
  if (next.vip1_price !== undefined) vip1Price.value = next.vip1_price == null ? null : Number(next.vip1_price)
  if (next.vip2_price !== undefined) vip2Price.value = next.vip2_price == null ? null : Number(next.vip2_price)
  if (next.vip1_points !== undefined) vip1Points.value = next.vip1_points == null ? null : Number(next.vip1_points)
  if (next.vip2_points !== undefined) vip2Points.value = next.vip2_points == null ? null : Number(next.vip2_points)
  if (next.pay_modo) payModo.value = next.pay_modo === 'points' ? 'points' : '0'
  if (next.apate != null) apate.value = Boolean(next.apate)
  if (next.pay_enabled != null) payEnabled.value = Boolean(next.pay_enabled)
  if (next.images) images.value = next.images
  if (next.archives) archives.value = next.archives
  if (next.images?.length && token) {
    revokePreviews()
    const urls: string[] = []
    for (const item of next.images.slice(0, 8)) {
      try {
        const blob = await getGamesFile(token, item.path)
        urls.push(URL.createObjectURL(blob))
      } catch {
        /* skip broken preview */
      }
    }
    previewUrls.value = urls
  }
}

const loadCatalog = async () => {
  if (!token) return
  try {
    const result = await listGamesCatalog(token, catalogPage.value, 12, catalogQuery.value)
    catalog.value = result.data || []
    catalogTotal.value = result.total || 0
    catalogPages.value = result.total_pages || 0
    if (catalogPage.value > 1 && catalogPage.value > catalogPages.value) {
      catalogPage.value = Math.max(1, catalogPages.value)
    }
  } catch (error: unknown) {
    notifyApiError(error, 'games.loadFailed')
  }
}

const loadJobs = async () => {
  if (!token) return
  try {
    const result = await listGamesJobs(token)
    jobs.value = result.data || []
  } catch {
    /* ignore */
  }
}

const loadCategories = async () => {
  if (!token) return
  try {
    const result = await listGamesCategories(token)
    categories.value = result.data || []
  } catch {
    categories.value = [
      { id: 640, name: 'PC游戏' },
      { id: 637, name: '汉化游戏' },
      { id: 641, name: '安卓游戏' },
      { id: 639, name: '原生游戏' },
    ]
  }
}

const pullPost = async () => {
  if (!token || !canPull.value) return
  busy.value = 'pull'
  try {
    const result = await pullGamesTelegram(token, {
      url: telegramUrl.value.trim(),
      account: telegramAccount.value || undefined,
    })
    await applyJob(result)
    toast.success(t('games.pulled'))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'games.pullFailed')
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
    const result = await uploadGamesSource(token, file)
    await applyJob(result)
    toast.success(file.name)
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'games.publishFailed')
  } finally {
    uploading.value = false
  }
}

const parseOptionalAmount = (raw: string): number | null => {
  const text = raw.trim()
  if (!text) return null
  const value = Number(text)
  return Number.isFinite(value) && value >= 0 ? value : null
}

const toggleCategory = (id: number) => {
  if (selectedCategories.value.includes(id)) {
    selectedCategories.value = selectedCategories.value.filter((item) => item !== id)
  } else {
    selectedCategories.value = [...selectedCategories.value, id]
  }
}

const toggleCloud = (id: string) => {
  if (selectedClouds.value.includes(id)) {
    selectedClouds.value = selectedClouds.value.filter((item) => item !== id)
  } else {
    selectedClouds.value = [...selectedClouds.value, id]
  }
}

const publish = async () => {
  if (!token || !jobId.value || !canPublish.value) return
  busy.value = 'publish'
  try {
    const chosen = new Set(selectedClouds.value)
    const links: Record<string, string> = {}
    if (chosen.has('baidu') && manualBaidu.value.trim()) links.baidu = manualBaidu.value.trim()
    if (chosen.has('pikpak') && manualPikpak.value.trim()) links.pikpak = manualPikpak.value.trim()
    if (chosen.has('terabox') && manualTerabox.value.trim()) links.terabox = manualTerabox.value.trim()
    if (chosen.has('quark') && manualQuark.value.trim()) links.quark = manualQuark.value.trim()
    await publishGamesJob(token, {
      job_id: jobId.value,
      title: title.value.trim(),
      summary: summary.value.trim(),
      tags: tags.value,
      categories: selectedCategories.value,
      price: price.value,
      points_price: pointsPrice.value,
      vip1_price: vip1Price.value,
      vip2_price: vip2Price.value,
      vip1_points: vip1Points.value,
      vip2_points: vip2Points.value,
      pay_modo: payModo.value,
      pay_enabled: payEnabled.value,
      apate: apate.value,
      extract_password: extractPassword.value.trim() || undefined,
      pack_password: packPassword.value.trim() || undefined,
      links,
      clouds: selectedClouds.value,
    })
    toast.success(t('games.published'))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'games.publishFailed')
  } finally {
    busy.value = ''
  }
}

const cancelJob = async () => {
  if (!token || !job.value?.id) return
  try {
    await cancelGamesJob(token, String(job.value.id))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'games.publishFailed')
  }
}

watch(
  () => props.status?.id,
  (id) => {
    if (id && id !== jobId.value) void applyJob(props.status)
  },
)

watch(
  () => props.status?.stage,
  (stage) => {
    if (stage === 'ready' || stage === 'done' || stage === 'failed') {
      void applyJob(props.status)
      void loadCatalog()
      void loadJobs()
    }
  },
)

watch(
  () => props.settings,
  (next) => {
    if (!next) return
    if (!telegramAccount.value) {
      telegramAccount.value = next.telegram_account_name || accountsStore.accounts[0]?.name || ''
    }
    if (!packPassword.value && next.pack_password) {
      packPassword.value = next.pack_password
    }
  },
  { immediate: true },
)

onMounted(async () => {
  await accountsStore.ensureAccounts().catch(() => undefined)
  if (!telegramAccount.value) {
    telegramAccount.value = props.settings?.telegram_account_name || accountsStore.accounts[0]?.name || ''
  }
  if (props.settings?.wp_default_categories) {
    selectedCategories.value = props.settings.wp_default_categories
      .split(',')
      .map((item) => Number(item.trim()))
      .filter((item) => Number.isFinite(item) && item > 0)
  }
  if (props.settings?.wp_default_tags) tags.value = props.settings.wp_default_tags
  if (props.settings?.wp_pay_price != null) price.value = Number(props.settings.wp_pay_price)
  if (props.settings?.wp_points_price != null) pointsPrice.value = Number(props.settings.wp_points_price)
  if (props.settings?.wp_vip1_price !== undefined) vip1Price.value = props.settings.wp_vip1_price ?? null
  if (props.settings?.wp_vip2_price !== undefined) vip2Price.value = props.settings.wp_vip2_price ?? null
  if (props.settings?.wp_vip1_points !== undefined) vip1Points.value = props.settings.wp_vip1_points ?? null
  if (props.settings?.wp_vip2_points !== undefined) vip2Points.value = props.settings.wp_vip2_points ?? null
  if (props.settings?.wp_pay_modo) payModo.value = props.settings.wp_pay_modo === 'points' ? 'points' : '0'
  if (props.settings?.apate_enabled != null) apate.value = Boolean(props.settings.apate_enabled)
  await Promise.all([loadCatalog(), loadJobs(), loadCategories()])
  if (props.status?.id) await applyJob(props.status)
})

onUnmounted(() => {
  revokePreviews()
})
</script>

<template>
  <div class="space-y-6">
    <section v-if="showJobStatus" class="ui-card p-5 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ job?.title || t('games.job') }}</div>
          <p class="text-[11px] text-gray-500 mt-1">{{ progressLabel }}</p>
        </div>
        <div class="flex gap-2">
          <button v-if="job?.running && job?.id" type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" @click="cancelJob">{{ t('games.cancelJob') }}</button>
          <a v-if="job?.public_url" :href="job.public_url" target="_blank" rel="noreferrer" class="ui-btn-primary !px-3 !py-1.5 !text-xs inline-flex items-center gap-1">
            {{ t('games.openSite') }} <ExternalLink class="w-3 h-3" />
          </a>
        </div>
      </div>
      <div class="h-2 rounded bg-gray-100 dark:bg-gray-800 overflow-hidden">
        <div class="h-full bg-sky-500 transition-all" :style="{ width: `${progressPct}%` }" />
      </div>
      <p v-if="job?.error" class="text-xs text-rose-600 dark:text-rose-400">{{ job.error }}</p>
      <p
        v-for="[name, message] in cloudErrorEntries"
        :key="name"
        class="text-xs text-rose-600 dark:text-rose-400"
        data-testid="cloud-error"
      >
        {{ t('games.cloudFailed', { name: cloudLabel(name), error: message }) }}
      </p>
      <p v-if="cloudLinkNames.length" class="text-[10px] text-gray-500" data-testid="cloud-links">
        {{ t('games.cloudUploaded', { names: cloudLinkNames.join('、') }) }}
      </p>
    </section>

    <section class="ui-card p-5 sm:p-6 space-y-4">
      <div>
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.telegramUrl') }}</div>
        <p class="text-[10px] text-gray-500 mt-1">{{ t('games.pullHint') }}</p>
      </div>
      <div class="space-y-1">
        <label class="ui-label" for="games-telegram-url">{{ t('games.telegramUrl') }}</label>
        <div class="flex flex-col sm:flex-row gap-2">
          <input
            id="games-telegram-url"
            v-model="telegramUrl"
            type="text"
            inputmode="url"
            autocomplete="off"
            spellcheck="false"
            class="ui-input min-w-0 flex-1"
            :placeholder="t('games.telegramPlaceholder')"
            @keydown.enter.prevent="pullPost"
          >
          <button
            type="button"
            class="ui-btn-primary shrink-0"
            :disabled="!canPull"
            @click="pullPost"
          >
            <LoaderCircle v-if="busy === 'pull'" class="w-3.5 h-3.5 animate-spin" />
            {{ t('games.pull') }}
          </button>
        </div>
        <p v-if="telegramUrlDirty" class="text-[11px] text-amber-600 dark:text-amber-400" role="status">
          {{ t('games.telegramUrlInvalid') }}
        </p>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div class="space-y-1">
          <label class="ui-label" for="games-telegram-account">{{ t('games.telegramAccount') }}</label>
          <select id="games-telegram-account" v-model="telegramAccount" class="ui-input">
            <option value="">{{ t('games.selectAccount') }}</option>
            <option v-if="selectedAccountMissing && telegramAccount" :value="telegramAccount">
              {{ telegramAccount }}
            </option>
            <option v-for="account in accountsStore.accounts" :key="account.name" :value="account.name">
              {{ account.name }}{{ account.remark ? ` · ${account.remark}` : '' }}
            </option>
          </select>
          <p v-if="!accountsStore.accounts.length" class="text-[10px] text-amber-600 dark:text-amber-400">
            {{ t('games.noAccounts') }}
          </p>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.upload') }}</label>
          <label class="ui-btn-secondary w-full cursor-pointer">
            <input type="file" accept=".zip,.7z,.rar,.iso" class="hidden" :disabled="uploading || running" @change="onUpload">
            {{ uploading ? t('common.loading') : t('games.upload') }}
          </label>
        </div>
      </div>
      <div v-if="archives.length" class="text-xs text-gray-500">
        {{ t('games.archives') }}：
        <span v-for="item in archives" :key="item.path" class="mr-2">{{ item.name || item.path }}</span>
      </div>
      <div v-else class="text-xs text-gray-400">{{ t('games.noArchives') }}</div>
      <div v-if="previewUrls.length" class="space-y-2">
        <div class="ui-label">{{ t('games.images') }}</div>
        <div class="flex gap-2 overflow-x-auto pb-1">
          <img v-for="url in previewUrls" :key="url" :src="url" alt="" class="h-24 w-32 object-cover rounded bg-gray-100 dark:bg-gray-900">
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="space-y-1 md:col-span-2">
          <label class="ui-label">{{ t('games.draftTitle') }}</label>
          <input v-model="title" class="ui-input">
        </div>
        <div class="space-y-1 md:col-span-2">
          <label class="ui-label">{{ t('games.draftSummary') }}</label>
          <textarea v-model="summary" rows="6" class="ui-input min-h-[8rem]" />
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.draftTags') }}</label>
          <input v-model="tags" class="ui-input" placeholder="SLG, ADV">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpPayModo') }}</label>
          <select v-model="payModo" class="ui-input">
            <option value="0">{{ t('games.wpPayBalance') }}</option>
            <option value="points">{{ t('games.wpPayPoints') }}</option>
          </select>
          <p class="text-[10px] text-gray-500">{{ t('games.payModoHint') }}</p>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpPrice') }}</label>
          <input v-model.number="price" type="number" min="0" step="0.01" class="ui-input" :disabled="payModo === 'points'">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpPointsPrice') }}</label>
          <input v-model.number="pointsPrice" type="number" min="0" step="1" class="ui-input" :disabled="payModo !== 'points'">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpVip1Price') }}</label>
          <input :value="vip1Price ?? ''" type="number" min="0" step="0.01" class="ui-input" :placeholder="t('games.wpVipEmpty')" :disabled="payModo === 'points'" @input="vip1Price = parseOptionalAmount(($event.target as HTMLInputElement).value)">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpVip2Price') }}</label>
          <input :value="vip2Price ?? ''" type="number" min="0" step="0.01" class="ui-input" :placeholder="t('games.wpVipEmpty')" :disabled="payModo === 'points'" @input="vip2Price = parseOptionalAmount(($event.target as HTMLInputElement).value)">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpVip1Points') }}</label>
          <input :value="vip1Points ?? ''" type="number" min="0" step="1" class="ui-input" :placeholder="t('games.wpVipEmpty')" :disabled="payModo !== 'points'" @input="vip1Points = parseOptionalAmount(($event.target as HTMLInputElement).value)">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.wpVip2Points') }}</label>
          <input :value="vip2Points ?? ''" type="number" min="0" step="1" class="ui-input" :placeholder="t('games.wpVipEmpty')" :disabled="payModo !== 'points'" @input="vip2Points = parseOptionalAmount(($event.target as HTMLInputElement).value)">
        </div>
        <p class="text-[10px] text-gray-500 md:col-span-2">{{ t('games.wpVipHint') }}</p>
        <div class="space-y-2 md:col-span-2">
          <div class="ui-label">{{ t('games.draftCategories') }}</div>
          <div class="flex flex-wrap gap-2">
            <label v-for="item in categories" :key="item.id" class="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300 border border-[var(--sp-border)] rounded px-2 py-1">
              <input type="checkbox" :checked="selectedCategories.includes(item.id)" @change="toggleCategory(item.id)">
              {{ item.name }}
            </label>
          </div>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.draftExtract') }}</label>
          <input
            v-model="extractPassword"
            type="text"
            autocomplete="off"
            spellcheck="false"
            class="ui-input"
            :placeholder="settings?.extract_passwords || 'sakuramai,ixacg.top'"
          >
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('games.draftPack') }}</label>
          <input
            v-model="packPassword"
            type="text"
            autocomplete="off"
            spellcheck="false"
            class="ui-input"
            :placeholder="settings?.pack_password || 'sakuramai'"
          >
        </div>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input v-model="apate" type="checkbox" class="rounded border-gray-300">
          {{ t('games.draftApate') }}
        </label>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input v-model="payEnabled" type="checkbox" class="rounded border-gray-300">
          {{ t('games.wpPay') }}
        </label>
        <div class="space-y-2 md:col-span-2">
          <div class="ui-label">{{ t('games.uploadClouds') }}</div>
          <p class="text-[10px] text-gray-500">{{ t('games.uploadCloudsHint') }}</p>
          <div class="flex flex-wrap gap-2">
            <label
              v-for="item in cloudOptions"
              :key="item.id"
              class="inline-flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-300 border border-[var(--sp-border)] rounded px-2 py-1"
            >
              <input
                type="checkbox"
                :data-cloud="item.id"
                :checked="selectedClouds.includes(item.id)"
                @change="toggleCloud(item.id)"
              >
              {{ t(item.labelKey) }}
              <span v-if="status && !cloudConfigured(item.id)" class="text-[10px] text-gray-400">{{ t('games.cloudNotConfigured') }}</span>
            </label>
          </div>
        </div>
        <div class="space-y-1 md:col-span-2 text-[10px] text-gray-500">{{ t('games.manualLinks') }}</div>
        <div class="space-y-1"><label class="ui-label">{{ t('games.linkBaidu') }}</label><input v-model="manualBaidu" class="ui-input" :disabled="!selectedClouds.includes('baidu')"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('games.linkPikpak') }}</label><input v-model="manualPikpak" class="ui-input" :disabled="!selectedClouds.includes('pikpak')"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('games.linkTerabox') }}</label><input v-model="manualTerabox" class="ui-input" :disabled="!selectedClouds.includes('terabox')"></div>
        <div class="space-y-1"><label class="ui-label">{{ t('games.linkQuark') }}</label><input v-model="manualQuark" class="ui-input" :disabled="!selectedClouds.includes('quark')"></div>
      </div>
      <div class="flex justify-end">
        <button type="button" class="ui-btn-primary !px-4 !text-sm" :disabled="!canPublish" @click="publish">
          <LoaderCircle v-if="busy === 'publish'" class="w-3.5 h-3.5 animate-spin" />
          {{ t('games.publish') }}
        </button>
      </div>
    </section>

    <section v-if="jobs.length" class="space-y-3">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.job') }}</div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <GameCard
          v-for="item in jobs.slice(0, 6)"
          :key="String(item.id)"
          :title="item.title || item.stage || t('games.job')"
          :id-label="item.wp_id"
          :cover-url="item.cover_url"
          :tags="item.tags"
          :price="item.price"
          :points-price="item.points_price"
          :pay-modo="item.pay_modo"
          :updated-at="item.updated_at"
          :public-url="item.public_url"
          :subtitle="item.message || item.stage"
        />
      </div>
    </section>

    <section class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('games.catalogTitle') }}</div>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('games.catalogCount', { total: catalogTotal }) }}</p>
        </div>
        <input
          v-model="catalogQuery"
          class="ui-input max-w-xs"
          :placeholder="t('common.searchPlaceholder')"
          @keydown.enter="catalogPage = 1; loadCatalog()"
        >
      </div>
      <div v-if="!catalog.length" class="ui-card p-6 text-sm text-gray-500">{{ t('games.catalogEmpty') }}</div>
      <div v-else class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        <GameCard
          v-for="item in catalog"
          :key="String(item.job_id || item.id || item.title)"
          :title="item.title"
          :id-label="item.id"
          :cover-url="item.cover_url"
          :tags="item.tags"
          :price="item.price"
          :points-price="item.points_price"
          :pay-modo="item.pay_modo"
          :updated-at="item.updated_at"
          :public-url="item.public_url"
        />
      </div>
      <div v-if="catalogPages > 1" class="flex items-center justify-end gap-2">
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage <= 1" @click="catalogPage -= 1; loadCatalog()">{{ t('common.prev') }}</button>
        <span class="text-xs text-gray-500">{{ catalogPage }} / {{ catalogPages }}</span>
        <button type="button" class="ui-btn-secondary !px-3 !py-1.5 !text-xs" :disabled="catalogPage >= catalogPages" @click="catalogPage += 1; loadCatalog()">{{ t('common.next') }}</button>
      </div>
    </section>
  </div>
</template>

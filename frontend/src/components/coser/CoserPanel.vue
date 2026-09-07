<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ExternalLink, LoaderCircle } from 'lucide-vue-next'
import {
  cancelCoserJob,
  createCoserPerson,
  getCoserFile,
  listCoserCatalog,
  listCoserPeople,
  publishCoserJob,
  pullCoserTelegram,
  uploadCoserSource,
  type CoserCatalogEntry,
  type CoserJob,
  type CoserPerson,
  type CoserRuntimeStatus,
  type CoserSettings,
} from '../../lib/api'
import { getAuthToken } from '../../lib/api/core'
import { notifyApiError } from '../../lib/notify'
import { useI18n } from '../../composables/useI18n'
import { useToast } from '../../composables/useToast'
import { useAccountsStore } from '../../stores/accounts'
import { isTelegramPostUrl } from '../../lib/telegram-post-url'
import GameCard from '../games/GameCard.vue'

const props = defineProps<{
  settings: CoserSettings | null
  status: CoserRuntimeStatus | null
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
const selectedCoserId = ref<number | null>(null)
const newCoserName = ref('')
const isR18 = ref(false)
const apate = ref(true)
const extractPassword = ref('')
const packPassword = ref('')
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
const people = ref<CoserPerson[]>([])
const catalog = ref<CoserCatalogEntry[]>([])

const job = computed(() => props.status)
const running = computed(() => Boolean(job.value?.running))
const telegramUrlValid = computed(() => isTelegramPostUrl(telegramUrl.value))
const telegramUrlDirty = computed(() => Boolean(telegramUrl.value.trim()) && !telegramUrlValid.value)
const canPull = computed(
  () => telegramUrlValid.value && busy.value !== 'pull' && !running.value,
)
const canPublish = computed(
  () =>
    Boolean(jobId.value) &&
    archives.value.length > 0 &&
    selectedCoserId.value &&
    selectedClouds.value.length > 0 &&
    busy.value !== 'publish' &&
    !running.value,
)

const revokePreviews = () => {
  for (const url of previewUrls.value) URL.revokeObjectURL(url)
  previewUrls.value = []
}

const applyJob = async (next: CoserJob | null | undefined) => {
  if (!next) return
  if (next.id) jobId.value = String(next.id)
  if (next.title) title.value = next.title
  if (next.summary) summary.value = next.summary
  if (next.coser_id) selectedCoserId.value = Number(next.coser_id)
  if (next.is_r18 != null) isR18.value = Boolean(next.is_r18)
  if (next.apate != null) apate.value = Boolean(next.apate)
  if (next.images) images.value = next.images
  if (next.archives) archives.value = next.archives
  if (next.images?.length && token) {
    revokePreviews()
    const urls: string[] = []
    for (const item of next.images.slice(0, 8)) {
      try {
        const blob = await getCoserFile(token, item.path)
        urls.push(URL.createObjectURL(blob))
      } catch {
        /* skip */
      }
    }
    previewUrls.value = urls
  }
}

const loadCatalog = async () => {
  if (!token) return
  try {
    const result = await listCoserCatalog(token, 1, 12, '')
    catalog.value = result.data || []
  } catch (error: unknown) {
    notifyApiError(error, 'coser.loadFailed')
  }
}

const loadPeople = async () => {
  if (!token) return
  try {
    const result = await listCoserPeople(token)
    people.value = result.data || []
  } catch {
    people.value = []
  }
}

const pullPost = async () => {
  if (!token || !canPull.value) return
  busy.value = 'pull'
  try {
    const result = await pullCoserTelegram(token, {
      url: telegramUrl.value.trim(),
      account: telegramAccount.value || undefined,
    })
    await applyJob(result)
    toast.success(t('coser.pulled'))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'coser.pullFailed')
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
    const result = await uploadCoserSource(token, file)
    await applyJob(result)
    toast.success(file.name)
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'coser.publishFailed')
  } finally {
    uploading.value = false
  }
}

const createPerson = async () => {
  if (!token || !newCoserName.value.trim()) return
  try {
    const created = await createCoserPerson(token, newCoserName.value.trim())
    people.value = [created, ...people.value.filter((item) => item.id !== created.id)]
    selectedCoserId.value = created.id
    newCoserName.value = ''
    toast.success(created.name)
  } catch (error: unknown) {
    notifyApiError(error, 'coser.createCoserFailed')
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
  if (!token || !jobId.value || !canPublish.value || !selectedCoserId.value) return
  busy.value = 'publish'
  try {
    const person = people.value.find((item) => item.id === selectedCoserId.value)
    await publishCoserJob(token, {
      job_id: jobId.value,
      title: title.value.trim(),
      summary: summary.value.trim(),
      coser_id: selectedCoserId.value,
      coser_name: person?.name,
      is_r18: isR18.value,
      apate: apate.value,
      extract_password: extractPassword.value.trim() || undefined,
      pack_password: packPassword.value.trim() || undefined,
      clouds: selectedClouds.value,
    })
    toast.success(t('coser.published'))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'coser.publishFailed')
  } finally {
    busy.value = ''
  }
}

const cancelJob = async () => {
  if (!token || !job.value?.id) return
  try {
    await cancelCoserJob(token, String(job.value.id))
    emit('refresh-status')
  } catch (error: unknown) {
    notifyApiError(error, 'coser.publishFailed')
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

onMounted(() => {
  void loadPeople()
  void loadCatalog()
  void accountsStore.ensureAccounts().catch(() => undefined)
})
</script>

<template>
  <div class="space-y-6">
    <section v-if="job?.running || job?.error || job?.public_url" class="ui-card p-5 space-y-2">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ job.message || t('coser.job') }}</div>
      <p v-if="job.error" class="text-xs text-rose-600 dark:text-rose-400">{{ job.error }}</p>
      <a
        v-if="job.public_url"
        :href="job.public_url"
        target="_blank"
        rel="noreferrer"
        class="inline-flex items-center gap-1 text-xs text-sky-600 dark:text-sky-400"
      >
        {{ job.public_url }}
        <ExternalLink class="w-3 h-3" />
      </a>
      <button v-if="running" type="button" class="ui-btn-secondary !text-xs" @click="cancelJob">
        {{ t('common.cancel') }}
      </button>
    </section>

    <section class="ui-card p-5 sm:p-6 space-y-4">
      <div>
        <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('coser.telegramUrl') }}</div>
        <p class="text-[10px] text-gray-500 mt-1">{{ t('coser.pullHint') }}</p>
      </div>
      <div class="flex flex-col sm:flex-row gap-2">
        <input
          id="coser-telegram-url"
          v-model="telegramUrl"
          type="text"
          class="ui-input min-w-0 flex-1"
          :placeholder="t('coser.telegramPlaceholder')"
          @keydown.enter.prevent="pullPost"
        >
        <button type="button" class="ui-btn-primary shrink-0" :disabled="!canPull" @click="pullPost">
          <LoaderCircle v-if="busy === 'pull'" class="w-3.5 h-3.5 animate-spin" />
          {{ t('coser.pull') }}
        </button>
      </div>
      <p v-if="telegramUrlDirty" class="text-[11px] text-amber-600 dark:text-amber-400">{{ t('coser.telegramUrlInvalid') }}</p>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.telegramAccount') }}</label>
          <select v-model="telegramAccount" class="ui-input">
            <option value="">{{ t('coser.selectAccount') }}</option>
            <option v-for="account in accountsStore.accounts" :key="account.name" :value="account.name">
              {{ account.name }}
            </option>
          </select>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.upload') }}</label>
          <label class="ui-btn-secondary w-full cursor-pointer">
            <input type="file" accept=".zip,.7z,.rar" class="hidden" :disabled="uploading || running" @change="onUpload">
            {{ uploading ? t('common.loading') : t('coser.upload') }}
          </label>
        </div>
      </div>
      <div v-if="archives.length" class="text-xs text-gray-500">
        {{ t('coser.archives') }}：
        <span v-for="item in archives" :key="item.path" class="mr-2">{{ item.name || item.path }}</span>
      </div>
      <div v-if="previewUrls.length" class="flex gap-2 overflow-x-auto pb-1">
        <img v-for="url in previewUrls" :key="url" :src="url" alt="" class="h-24 w-32 object-cover rounded bg-gray-100">
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div class="space-y-1 md:col-span-2">
          <label class="ui-label">{{ t('coser.draftTitle') }}</label>
          <input v-model="title" class="ui-input">
        </div>
        <div class="space-y-1 md:col-span-2">
          <label class="ui-label">{{ t('coser.draftSummary') }}</label>
          <textarea v-model="summary" rows="5" class="ui-input min-h-[7rem]" />
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.person') }}</label>
          <select v-model.number="selectedCoserId" class="ui-input">
            <option :value="null">{{ t('coser.selectPerson') }}</option>
            <option v-for="person in people" :key="person.id" :value="person.id">{{ person.name }}</option>
          </select>
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.createPerson') }}</label>
          <div class="flex gap-2">
            <input v-model="newCoserName" class="ui-input min-w-0 flex-1" :placeholder="t('coser.personName')">
            <button type="button" class="ui-btn-secondary shrink-0" @click="createPerson">{{ t('coser.createPerson') }}</button>
          </div>
        </div>
        <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input v-model="isR18" type="checkbox" class="rounded border-gray-300">
          {{ t('coser.r18') }}
        </label>
        <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
          <input v-model="apate" type="checkbox" class="rounded border-gray-300">
          {{ t('coser.apateEnabled') }}
        </label>
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.draftExtract') }}</label>
          <input v-model="extractPassword" class="ui-input" :placeholder="settings?.extract_passwords || ''">
        </div>
        <div class="space-y-1">
          <label class="ui-label">{{ t('coser.draftPack') }}</label>
          <input v-model="packPassword" class="ui-input">
        </div>
        <div class="space-y-2 md:col-span-2">
          <div class="ui-label">{{ t('coser.clouds') }}</div>
          <div class="flex flex-wrap gap-2">
            <label v-for="item in cloudOptions" :key="item.id" class="inline-flex items-center gap-1.5 text-xs border border-[var(--sp-border)] rounded px-2 py-1">
              <input type="checkbox" :checked="selectedClouds.includes(item.id)" @change="toggleCloud(item.id)">
              {{ t(item.labelKey) }}
            </label>
          </div>
        </div>
      </div>
      <button type="button" class="ui-btn-primary" :disabled="!canPublish" @click="publish">
        <LoaderCircle v-if="busy === 'publish'" class="w-3.5 h-3.5 animate-spin" />
        {{ t('coser.publish') }}
      </button>
    </section>

    <section v-if="catalog.length" class="space-y-3">
      <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('coser.catalog') }}</div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <GameCard
          v-for="item in catalog"
          :key="String(item.id || item.job_id)"
          :title="item.title"
          :cover-url="item.cover_url"
          :public-url="item.public_url"
          :subtitle="item.coser_name"
          :updated-at="item.updated_at"
        />
      </div>
    </section>
  </div>
</template>

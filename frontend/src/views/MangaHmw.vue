<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { CheckCircle2, LoaderCircle, XCircle } from 'lucide-vue-next'
import {
  getHmwStatus,
  getMangaSettings,
  saveMangaSettings,
  type HmwRuntimeStatus,
  type MangaSettings,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import { useSecretReveal } from '../composables/useSecretReveal'
import HmwPanel from '../components/manga/HmwPanel.vue'
import SecretInput from '../components/SecretInput.vue'

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()

const settings = ref<MangaSettings | null>(null)
const hmwStatus = ref<HmwRuntimeStatus | null>(null)
const pageLoading = ref(true)
const saving = ref(false)
const errorMessage = ref('')
const {
  draft: secretDraft,
  reveal: revealSecrets,
  loading: secretsLoading,
  toggle: toggleSecret,
  reset: resetSecrets,
} = useSecretReveal(
  () => ({
    hmw_publisher_token: '',
    hmw_s3_access_key: '',
    hmw_s3_secret_key: '',
  }),
  {
    isSaved: (key) => Boolean(settings.value?.[`${key}_set` as keyof MangaSettings]),
    fetchRevealed: () => getMangaSettings(token!, true),
    onError: (error) => notifyApiError(error, 'manga.hmwLoadFailed'),
  },
)
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
      getHmwStatus(token),
    ])
    applySettings(settingsResult)
    hmwStatus.value = statusResult
  } catch (error: unknown) {
    errorMessage.value = t('manga.hmwLoadFailed')
    notifyApiError(error, 'manga.hmwLoadFailed')
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
      hmw_api_url: settings.value.hmw_api_url || '',
      hmw_s3_endpoint: settings.value.hmw_s3_endpoint || '',
      hmw_s3_region: settings.value.hmw_s3_region || '',
      hmw_s3_bucket: settings.value.hmw_s3_bucket || '',
      hmw_s3_public_url: settings.value.hmw_s3_public_url || '',
      hmw_s3_prefix: settings.value.hmw_s3_prefix || 'comics/zh',
      hmw_avif_quality: Number(settings.value.hmw_avif_quality || 50),
      hmw_convert_workers: Number(settings.value.hmw_convert_workers || 1),
      hmw_upload_workers: Number(settings.value.hmw_upload_workers || 6),
    }
    if (secretDraft.value.hmw_publisher_token.trim()) {
      payload.hmw_publisher_token = secretDraft.value.hmw_publisher_token.trim()
    }
    if (secretDraft.value.hmw_s3_access_key.trim()) {
      payload.hmw_s3_access_key = secretDraft.value.hmw_s3_access_key.trim()
    }
    if (secretDraft.value.hmw_s3_secret_key.trim()) {
      payload.hmw_s3_secret_key = secretDraft.value.hmw_s3_secret_key.trim()
    }
    const result = await saveMangaSettings(token, payload)
    applySettings(result.settings, true)
    hmwStatus.value = await getHmwStatus(token, true)
    toast.success(t('manga.saveSuccess'))
  } catch (error: unknown) {
    notifyApiError(error, 'manga.saveFailed')
  } finally {
    saving.value = false
  }
}

const pollStatus = async (probe = false) => {
  if (!token) return
  try {
    const next = await getHmwStatus(token, probe)
    const prev = hmwStatus.value
    hmwStatus.value = {
      ...prev,
      ...next,
      api_ok: probe ? next.api_ok : prev?.api_ok,
      s3_ok: probe ? next.s3_ok : prev?.s3_ok,
      configured: next.configured ?? prev?.configured,
    }
  } catch {
    /* 轮询失败时保留当前状态 */
  }
}

const schedulePoll = () => {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  const running = Boolean(hmwStatus.value?.running)
  pollTimer = window.setTimeout(async () => {
    await pollStatus(false)
    schedulePoll()
  }, running ? 1500 : 8000)
}

onMounted(async () => {
  await loadPage()
  schedulePoll()
})

onUnmounted(() => {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
})

const apiOk = computed(() => Boolean(hmwStatus.value?.api_ok))
const s3Ok = computed(() => Boolean(hmwStatus.value?.s3_ok))
const configured = computed(() => Boolean(hmwStatus.value?.configured))
const jobRunning = computed(() => Boolean(hmwStatus.value?.running))
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
        <div class="ui-section-label mb-2">{{ t('manga.eyebrow') }} / {{ t('nav.mangaHmw') }}</div>
        <h2 class="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100">{{ t('manga.hmwTitle') }}</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2 max-w-3xl">{{ t('manga.hmwPageHint') }}</p>
      </div>

      <div v-if="errorMessage" class="border border-rose-200 dark:border-rose-800/40 bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-300 px-4 py-3 text-sm" role="alert">
        {{ errorMessage }}
      </div>

      <div class="grid grid-cols-2 xl:grid-cols-4 gap-3">
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.hmwConfigured') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="configured ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'">
            <CheckCircle2 v-if="configured" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ configured ? t('manga.configured') : t('manga.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.hmwApi') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="apiOk ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            <CheckCircle2 v-if="apiOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ apiOk ? t('manga.connected') : t('manga.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.hmwStorage') }}</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="s3Ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-500'">
            <CheckCircle2 v-if="s3Ok" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ s3Ok ? t('manga.connected') : t('manga.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('manga.hmwJob') }}</div>
          <div class="mt-2 text-sm font-medium text-gray-800 dark:text-gray-100">
            {{ jobRunning ? t('manga.status.running') : (hmwStatus?.stage || t('manga.status.stopped')) }}
          </div>
          <p v-if="hmwStatus?.message" class="mt-1 text-[11px] text-gray-500 truncate">{{ hmwStatus.message }}</p>
        </div>
      </div>

      <section v-if="settings" class="ui-card p-5 sm:p-6 space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('manga.hmwSaveConnection') }}</div>
            <p class="text-[10px] text-gray-500 mt-1">{{ t('manga.configHint') }}</p>
          </div>
          <button type="button" class="ui-btn-primary !px-3 !py-2 !text-xs shrink-0" :disabled="saving" @click="saveSettings">
            <LoaderCircle v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            {{ t('common.save') }}
          </button>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwApiUrl') }}</label><input :value="settings.hmw_api_url" class="ui-input" @input="update('hmw_api_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('manga.hmwToken') }}</label>
            <SecretInput v-model="secretDraft.hmw_publisher_token" :revealed="revealSecrets.hmw_publisher_token" :loading="secretsLoading" :placeholder="settings.hmw_publisher_token_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('hmw_publisher_token')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwS3Endpoint') }}</label><input :value="settings.hmw_s3_endpoint" class="ui-input" @input="update('hmw_s3_endpoint', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwS3Region') }}</label><input :value="settings.hmw_s3_region" class="ui-input" @input="update('hmw_s3_region', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwS3Bucket') }}</label><input :value="settings.hmw_s3_bucket" class="ui-input" @input="update('hmw_s3_bucket', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwS3PublicUrl') }}</label><input :value="settings.hmw_s3_public_url" class="ui-input" @input="update('hmw_s3_public_url', ($event.target as HTMLInputElement).value)"></div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('manga.hmwS3AccessKey') }}</label>
            <SecretInput v-model="secretDraft.hmw_s3_access_key" :revealed="revealSecrets.hmw_s3_access_key" :loading="secretsLoading" :placeholder="settings.hmw_s3_access_key_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('hmw_s3_access_key')" />
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('manga.hmwS3SecretKey') }}</label>
            <SecretInput v-model="secretDraft.hmw_s3_secret_key" :revealed="revealSecrets.hmw_s3_secret_key" :loading="secretsLoading" :placeholder="settings.hmw_s3_secret_key_set ? t('manga.keepExisting') : t('manga.enterSecret')" @toggle="toggleSecret('hmw_s3_secret_key')" />
          </div>
          <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwS3Prefix') }}</label><input :value="settings.hmw_s3_prefix" class="ui-input" @input="update('hmw_s3_prefix', ($event.target as HTMLInputElement).value)"></div>
          <div class="grid grid-cols-3 gap-2">
            <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwAvifQuality') }}</label><input :value="settings.hmw_avif_quality" type="number" min="1" max="100" class="ui-input" @input="update('hmw_avif_quality', Number(($event.target as HTMLInputElement).value) || 50)"></div>
            <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwConvertWorkers') }}</label><input :value="settings.hmw_convert_workers" type="number" min="1" max="4" class="ui-input" @input="update('hmw_convert_workers', Number(($event.target as HTMLInputElement).value) || 1)"></div>
            <div class="space-y-1"><label class="ui-label">{{ t('manga.hmwUploadWorkers') }}</label><input :value="settings.hmw_upload_workers" type="number" min="1" max="32" class="ui-input" @input="update('hmw_upload_workers', Number(($event.target as HTMLInputElement).value) || 6)"></div>
          </div>
        </div>
      </section>

      <HmwPanel :settings="settings" :status="hmwStatus" @refresh-status="pollStatus" />
    </template>
  </div>
</template>

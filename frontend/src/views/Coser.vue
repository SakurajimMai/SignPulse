<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { CheckCircle2, Eye, EyeOff, LoaderCircle, XCircle } from 'lucide-vue-next'
import {
  getCoserSettings,
  getCoserStatus,
  saveCoserSettings,
  type CoserRuntimeStatus,
  type CoserSettings,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import { useAccountsStore } from '../stores/accounts'
import CoserPanel from '../components/coser/CoserPanel.vue'

const { t } = useI18n()
const toast = useToast()
const token = getAuthToken()
const accountsStore = useAccountsStore()

const settings = ref<CoserSettings | null>(null)
const status = ref<CoserRuntimeStatus | null>(null)
const pageLoading = ref(true)
const saving = ref(false)
const errorMessage = ref('')
type SecretField = 'site_password' | 's3_access_key' | 's3_secret_key'
const emptySecrets = () => ({ site_password: '', s3_access_key: '', s3_secret_key: '' })
const secretDraft = ref(emptySecrets())
const revealSecrets = ref({ site_password: false, s3_access_key: false, s3_secret_key: false })
const secretsLoaded = ref(false)
const revealing = ref(false)
let pollTimer: number | undefined

const applySettings = (value: CoserSettings, keepDrafts = false) => {
  settings.value = { ...value }
  if (!keepDrafts) {
    secretDraft.value = emptySecrets()
    secretsLoaded.value = false
    revealSecrets.value = { site_password: false, s3_access_key: false, s3_secret_key: false }
  }
}

const fillSecretDrafts = (value: CoserSettings) => {
  const next = { ...secretDraft.value }
  for (const key of ['site_password', 's3_access_key', 's3_secret_key'] as const) {
    if (!next[key].trim() && value[key]) next[key] = String(value[key])
  }
  secretDraft.value = next
  secretsLoaded.value = true
}

const loadRevealedSecrets = async () => {
  if (!token || secretsLoaded.value) return
  fillSecretDrafts(await getCoserSettings(token, true))
}

const toggleReveal = async (key: SecretField) => {
  if (revealSecrets.value[key]) {
    revealSecrets.value = { ...revealSecrets.value, [key]: false }
    return
  }
  const savedFlag = `${key}_set` as keyof CoserSettings
  const saved = Boolean(settings.value?.[savedFlag])
  if (!secretDraft.value[key].trim() && saved) {
    revealing.value = true
    try {
      await loadRevealedSecrets()
    } catch (error: unknown) {
      notifyApiError(error, 'coser.loadFailed')
      return
    } finally {
      revealing.value = false
    }
  }
  revealSecrets.value = { ...revealSecrets.value, [key]: true }
}

const selectedAccountMissing = computed(() => {
  const name = settings.value?.telegram_account_name
  if (!name) return false
  return !accountsStore.accounts.some((item) => item.name === name)
})

const siteOk = computed(() => Boolean(status.value?.site?.ok))
const s3Ok = computed(() => Boolean(status.value?.s3?.ok))
const sevenOk = computed(() => Boolean(status.value?.sevenzip?.ok))
const apateOk = computed(() => Boolean(status.value?.apate_tool?.ok))
const jobRunning = computed(() => Boolean(status.value?.running))

const loadPage = async () => {
  if (!token) return
  pageLoading.value = true
  errorMessage.value = ''
  try {
    const [settingsResult, statusResult] = await Promise.all([
      getCoserSettings(token),
      getCoserStatus(token),
    ])
    applySettings(settingsResult)
    status.value = statusResult
  } catch (error: unknown) {
    errorMessage.value = t('coser.loadFailed')
    notifyApiError(error, 'coser.loadFailed')
  } finally {
    pageLoading.value = false
  }
}

const pollStatus = async () => {
  if (!token) return
  try {
    status.value = await getCoserStatus(token, false)
  } catch {
    /* ignore */
  }
}

const update = (key: keyof CoserSettings, value: unknown) => {
  if (!settings.value) return
  settings.value = { ...settings.value, [key]: value } as CoserSettings
}

const saveSettings = async () => {
  if (!token || !settings.value) return
  saving.value = true
  try {
    const payload: Record<string, unknown> = { ...settings.value }
    if (secretDraft.value.site_password.trim()) {
      payload.site_password = secretDraft.value.site_password.trim()
    } else {
      delete payload.site_password
    }
    if (secretDraft.value.s3_access_key.trim()) {
      payload.s3_access_key = secretDraft.value.s3_access_key.trim()
    } else {
      delete payload.s3_access_key
    }
    if (secretDraft.value.s3_secret_key.trim()) {
      payload.s3_secret_key = secretDraft.value.s3_secret_key.trim()
    } else {
      delete payload.s3_secret_key
    }
    const result = await saveCoserSettings(token, payload)
    applySettings(result.settings, true)
    toast.success(result.message || t('coser.saved'))
    status.value = await getCoserStatus(token)
  } catch (error: unknown) {
    notifyApiError(error, 'coser.saveFailed')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void accountsStore.ensureAccounts().catch(() => undefined)
  void loadPage()
  pollTimer = window.setInterval(() => {
    void pollStatus()
  }, 4000)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
})
</script>

<template>
  <div class="max-w-7xl pb-10 space-y-6">
    <div>
      <div class="text-[11px] uppercase tracking-[0.18em] text-gray-400">{{ t('coser.eyebrow') }}</div>
      <h1 class="mt-1 text-xl font-medium text-gray-900 dark:text-gray-100">{{ t('coser.title') }}</h1>
      <p class="mt-1 text-sm text-gray-500">{{ t('coser.pageHint') }}</p>
    </div>

    <div v-if="pageLoading" class="grid grid-cols-1 md:grid-cols-3 gap-4" aria-busy="true">
      <div v-for="i in 3" :key="i" class="ui-card p-4 space-y-3">
        <div class="ui-skeleton h-4 w-24" />
        <div class="ui-skeleton h-8 w-full" />
      </div>
    </div>

    <p v-else-if="errorMessage" class="text-sm text-rose-600">{{ errorMessage }}</p>

    <template v-else>
      <div class="grid grid-cols-2 xl:grid-cols-5 gap-3">
        <div class="ui-card p-4">
          <div class="ui-section-label">icoser.de</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="siteOk ? 'text-emerald-600' : 'text-gray-500'">
            <CheckCircle2 v-if="siteOk" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ siteOk ? t('coser.connected') : t('coser.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">S3</div>
          <div class="mt-2 flex items-center gap-2 text-sm font-medium" :class="s3Ok ? 'text-emerald-600' : 'text-gray-500'">
            <CheckCircle2 v-if="s3Ok" class="w-4 h-4" />
            <XCircle v-else class="w-4 h-4" />
            {{ s3Ok ? t('coser.connected') : t('coser.notConfigured') }}
          </div>
          <p v-if="status?.s3?.error" class="mt-1 text-[11px] text-rose-600 line-clamp-2">{{ status.s3.error }}</p>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('coser.archive') }}</div>
          <div class="mt-2 text-sm font-medium" :class="sevenOk ? 'text-emerald-600' : 'text-amber-600'">
            {{ sevenOk ? t('coser.configured') : t('coser.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">Apate</div>
          <div class="mt-2 text-sm font-medium" :class="apateOk ? 'text-emerald-600' : 'text-amber-600'">
            {{ apateOk ? t('coser.configured') : t('coser.notConfigured') }}
          </div>
        </div>
        <div class="ui-card p-4">
          <div class="ui-section-label">{{ t('coser.job') }}</div>
          <div class="mt-2 text-sm font-medium">{{ jobRunning ? t('common.loading') : (status?.message || t('coser.idle')) }}</div>
        </div>
      </div>

      <section v-if="settings" class="ui-card p-5 sm:p-6 space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <div class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('coser.connection') }}</div>
            <p class="text-[10px] text-gray-500 mt-1">{{ t('coser.configHint') }}</p>
          </div>
          <button type="button" class="ui-btn-primary !px-3 !py-2 !text-xs shrink-0" :disabled="saving" @click="saveSettings">
            <LoaderCircle v-if="saving" class="w-3.5 h-3.5 animate-spin" />
            {{ t('common.save') }}
          </button>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.siteUrl') }}</label>
            <input :value="settings.site_url" class="ui-input" placeholder="https://icoser.de" @input="update('site_url', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.siteEmail') }}</label>
            <input :value="settings.site_email" class="ui-input" @input="update('site_email', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label" for="coser-site-password">{{ t('coser.sitePassword') }}</label>
            <div class="relative">
              <input
                id="coser-site-password"
                v-model="secretDraft.site_password"
                :type="revealSecrets.site_password ? 'text' : 'password'"
                class="ui-input pr-10"
                autocomplete="new-password"
                :placeholder="settings.site_password_set ? t('coser.keepExisting') : t('coser.enterSecret')"
              >
              <button
                id="coser-site-password-reveal"
                type="button"
                class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                :disabled="revealing"
                :aria-label="revealSecrets.site_password ? t('settings.hideSecret') : t('settings.showSecret')"
                @click="toggleReveal('site_password')"
              >
                <EyeOff v-if="revealSecrets.site_password" class="w-4 h-4" />
                <Eye v-else class="w-4 h-4" />
              </button>
            </div>
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.s3Endpoint') }}</label>
            <input :value="settings.s3_endpoint || ''" class="ui-input" placeholder="https://xxxx.r2.cloudflarestorage.com" @input="update('s3_endpoint', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.s3Bucket') }}</label>
            <input :value="settings.s3_bucket || ''" class="ui-input" @input="update('s3_bucket', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.s3Region') }}</label>
            <input :value="settings.s3_region || 'auto'" class="ui-input" @input="update('s3_region', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.s3PublicUrl') }}</label>
            <input :value="settings.s3_public_url || ''" class="ui-input" placeholder="https://cdn1.hxsl.org" @input="update('s3_public_url', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1 md:col-span-2">
            <label class="ui-label">{{ t('coser.s3Prefix') }}</label>
            <input :value="settings.s3_prefix || 'coser'" class="ui-input" placeholder="coser" @input="update('s3_prefix', ($event.target as HTMLInputElement).value)">
            <p class="text-[11px] text-gray-500">{{ t('coser.s3PrefixHint') }}</p>
          </div>
          <div class="space-y-1">
            <label class="ui-label" for="coser-s3-access-key">{{ t('coser.s3AccessKey') }}</label>
            <div class="relative">
              <input
                id="coser-s3-access-key"
                v-model="secretDraft.s3_access_key"
                :type="revealSecrets.s3_access_key ? 'text' : 'password'"
                class="ui-input pr-10"
                autocomplete="new-password"
                :placeholder="settings.s3_access_key_set ? t('coser.keepExisting') : t('coser.enterSecret')"
              >
              <button
                id="coser-s3-access-key-reveal"
                type="button"
                class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                :disabled="revealing"
                :aria-label="revealSecrets.s3_access_key ? t('settings.hideSecret') : t('settings.showSecret')"
                @click="toggleReveal('s3_access_key')"
              >
                <EyeOff v-if="revealSecrets.s3_access_key" class="w-4 h-4" />
                <Eye v-else class="w-4 h-4" />
              </button>
            </div>
          </div>
          <div class="space-y-1">
            <label class="ui-label" for="coser-s3-secret-key">{{ t('coser.s3SecretKey') }}</label>
            <div class="relative">
              <input
                id="coser-s3-secret-key"
                v-model="secretDraft.s3_secret_key"
                :type="revealSecrets.s3_secret_key ? 'text' : 'password'"
                class="ui-input pr-10"
                autocomplete="new-password"
                :placeholder="settings.s3_secret_key_set ? t('coser.keepExisting') : t('coser.enterSecret')"
              >
              <button
                id="coser-s3-secret-key-reveal"
                type="button"
                class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                :disabled="revealing"
                :aria-label="revealSecrets.s3_secret_key ? t('settings.hideSecret') : t('settings.showSecret')"
                @click="toggleReveal('s3_secret_key')"
              >
                <EyeOff v-if="revealSecrets.s3_secret_key" class="w-4 h-4" />
                <Eye v-else class="w-4 h-4" />
              </button>
            </div>
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.telegramAccount') }}</label>
            <select class="ui-input" :value="settings.telegram_account_name || ''" @change="update('telegram_account_name', ($event.target as HTMLSelectElement).value)">
              <option value="">{{ t('coser.selectAccount') }}</option>
              <option v-if="selectedAccountMissing && settings.telegram_account_name" :value="settings.telegram_account_name">
                {{ settings.telegram_account_name }}
              </option>
              <option v-for="account in accountsStore.accounts" :key="account.name" :value="account.name">
                {{ account.name }}
              </option>
            </select>
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.baiduDir') }}</label>
            <input :value="settings.baidu_remote_dir" class="ui-input" @input="update('baidu_remote_dir', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.pikpakDir') }}</label>
            <input :value="settings.pikpak_remote_dir" class="ui-input" @input="update('pikpak_remote_dir', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.teraboxDir') }}</label>
            <input :value="settings.terabox_remote_dir" class="ui-input" @input="update('terabox_remote_dir', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.quarkDir') }}</label>
            <input :value="settings.quark_remote_dir" class="ui-input" @input="update('quark_remote_dir', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.packPassword') }}</label>
            <input :value="settings.pack_password || ''" class="ui-input" @input="update('pack_password', ($event.target as HTMLInputElement).value)">
          </div>
          <div class="space-y-1">
            <label class="ui-label">{{ t('coser.extractPasswords') }}</label>
            <input :value="settings.extract_passwords || ''" class="ui-input" @input="update('extract_passwords', ($event.target as HTMLInputElement).value)">
          </div>
          <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" class="rounded border-gray-300" :checked="settings.apate_enabled" @change="update('apate_enabled', ($event.target as HTMLInputElement).checked)">
            {{ t('coser.apateEnabled') }}
          </label>
          <label class="inline-flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
            <input type="checkbox" class="rounded border-gray-300" :checked="settings.cleanup_after_publish !== false" @change="update('cleanup_after_publish', ($event.target as HTMLInputElement).checked)">
            {{ t('coser.cleanupAfterPublish') }}
          </label>
        </div>
        <p class="text-[11px] text-gray-500">
          {{ t('coser.cloudReuseHint') }}
          <RouterLink to="/games" class="underline underline-offset-2">{{ t('nav.games') }}</RouterLink>
        </p>
      </section>

      <CoserPanel :settings="settings" :status="status" @refresh-status="pollStatus" />
    </template>
  </div>
</template>

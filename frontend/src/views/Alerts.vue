<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n as useVueI18n } from 'vue-i18n'
import { Bell, CheckCircle2, LoaderCircle, Mail, Save, XCircle } from 'lucide-vue-next'
import {
  getAlertRules,
  saveAlertRules,
  testAlertMail,
  type AlertRule,
  type AlertRecent,
  type AlertsPayload,
} from '../lib/api'
import { getAuthToken } from '../lib/api/core'
import { notifyApiError } from '../lib/notify'
import { formatShortDateTime } from '../lib/datetime'
import PageRetry from '../components/PageRetry.vue'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'

const { t } = useI18n()
const { te } = useVueI18n()
const toast = useToast()

const pageLoading = ref(true)
const saving = ref(false)
const testingMail = ref(false)
const errorMessage = ref('')
const saveErrorMessage = ref('')
const testMailMessage = ref('')
const testMailOk = ref(false)
const smtpReady = ref(false)
const botReady = ref(false)
const smtpEnabled = ref(false)
const quietHours = ref(false)
const rules = ref<AlertRule[]>([])
const recent = ref<AlertRecent[]>([])
const cooldownDrafts = ref<Record<string, string>>({})
const cooldownErrors = ref<Record<string, string>>({})
const baselineRules = ref('')
const baselineDrafts = ref<Record<string, string>>({})
const baselineReady = ref(false)

const knownGroupOrder = ['core', 'manga', 'games', 'cloud', 'system']
const groupLabelKeys: Record<string, string> = {
  core: 'alerts.groupCore',
  manga: 'alerts.groupManga',
  games: 'alerts.groupGames',
  cloud: 'alerts.groupCloud',
  system: 'alerts.groupSystem',
}

const groupIdFor = (item: AlertRule) => String(item.group || '').trim() || 'other'

const groupLabel = (id: string) => {
  const key = groupLabelKeys[id]
  return key ? t(key) : t('alerts.groupOther', { group: id })
}

const rulesByGroup = computed(() => {
  const ids = [...new Set(rules.value.map(groupIdFor))]
  ids.sort((left, right) => {
    const leftIndex = knownGroupOrder.indexOf(left)
    const rightIndex = knownGroupOrder.indexOf(right)
    if (leftIndex === -1 && rightIndex === -1) return left.localeCompare(right)
    if (leftIndex === -1) return 1
    if (rightIndex === -1) return -1
    return leftIndex - rightIndex
  })
  return ids.map((id) => ({
    id,
    label: groupLabel(id),
    items: rules.value.filter((item) => groupIdFor(item) === id),
  }))
})

const ruleSnapshot = (items: AlertRule[]) =>
  JSON.stringify(
    items
      .map((item) => ({
        id: item.id,
        enabled: item.enabled,
        email_enabled: item.email_enabled,
        telegram_enabled: item.telegram_enabled,
        cooldown_minutes: item.cooldown_minutes,
      }))
      .sort((left, right) => left.id.localeCompare(right.id)),
  )

const draftsChanged = computed(() => {
  const ids = new Set([
    ...Object.keys(cooldownDrafts.value),
    ...Object.keys(baselineDrafts.value),
  ])
  return [...ids].some(
    (id) => cooldownDrafts.value[id] !== baselineDrafts.value[id],
  )
})

const isDirty = computed(
  () =>
    baselineReady.value &&
    (ruleSnapshot(rules.value) !== baselineRules.value || draftsChanged.value),
)
const hasValidationErrors = computed(() =>
  Object.values(cooldownErrors.value).some(Boolean),
)

const cooldownError = (raw: string) => {
  const value = raw.trim()
  if (!value) return t('alerts.cooldownRequired')
  if (!/^\d+$/.test(value)) return t('alerts.cooldownInteger')
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed < 1 || parsed > 1440) {
    return t('alerts.cooldownRange')
  }
  return ''
}

const markClean = () => {
  baselineRules.value = ruleSnapshot(rules.value)
  baselineDrafts.value = { ...cooldownDrafts.value }
  baselineReady.value = true
}

const applyPayload = (payload: AlertsPayload) => {
  rules.value = Array.isArray(payload.rules)
    ? payload.rules.map((item) => ({
        ...item,
        severity: item.severity || 'warning',
        email_enabled: item.email_enabled !== false,
        telegram_enabled: Boolean(item.telegram_enabled),
      }))
    : []
  cooldownDrafts.value = Object.fromEntries(
    rules.value.map((item) => [item.id, String(item.cooldown_minutes)]),
  )
  cooldownErrors.value = Object.fromEntries(
    Object.entries(cooldownDrafts.value)
      .map(([id, value]) => [id, cooldownError(value)])
      .filter(([, error]) => Boolean(error)),
  )
  recent.value = Array.isArray(payload.recent) ? payload.recent : []
  smtpReady.value = Boolean(payload.smtp_ready)
  botReady.value = Boolean(payload.bot_ready)
  smtpEnabled.value = Boolean(payload.smtp_enabled)
  quietHours.value = Boolean(payload.quiet_hours)
  markClean()
}

const ruleLabel = (item: AlertRule) => {
  const key = `alerts.rules.${item.id}`
  return te(key) ? t(key) : item.title || item.id
}

const ruleHint = (item: AlertRule) => {
  const key = `alerts.ruleHints.${item.id}`
  return te(key) ? t(key) : ''
}

const recentTitle = (item: AlertRecent) => {
  if (item.title) return item.title
  const rule = rules.value.find((candidate) => candidate.id === item.rule_id)
  return rule ? ruleLabel(rule) : item.rule_id || '-'
}

const loadPage = async () => {
  pageLoading.value = true
  errorMessage.value = ''
  saveErrorMessage.value = ''
  testMailMessage.value = ''
  const token = getAuthToken()
  if (!token) {
    errorMessage.value = t('alerts.authRequired')
    pageLoading.value = false
    return
  }
  try {
    applyPayload(await getAlertRules(token))
  } catch (error: unknown) {
    errorMessage.value = t('alerts.loadFailed')
    notifyApiError(error, 'alerts.loadFailed')
  } finally {
    pageLoading.value = false
  }
}

const toggleRule = (id: string) => {
  rules.value = rules.value.map((item) =>
    item.id === id ? { ...item, enabled: !item.enabled } : item,
  )
  saveErrorMessage.value = ''
}

const toggleChannel = (id: string, channel: 'email_enabled' | 'telegram_enabled') => {
  rules.value = rules.value.map((item) =>
    item.id === id ? { ...item, [channel]: !item[channel] } : item,
  )
  saveErrorMessage.value = ''
}

const setCooldown = (id: string, raw: string) => {
  cooldownDrafts.value = { ...cooldownDrafts.value, [id]: raw }
  const error = cooldownError(raw)
  const nextErrors = { ...cooldownErrors.value }
  if (error) nextErrors[id] = error
  else delete nextErrors[id]
  cooldownErrors.value = nextErrors
  if (!error) {
    rules.value = rules.value.map((item) =>
      item.id === id ? { ...item, cooldown_minutes: Number(raw) } : item,
    )
  }
  saveErrorMessage.value = ''
}

const cooldownInputId = (id: string) =>
  `alert-cooldown-${id.replace(/[^a-zA-Z0-9_-]/g, '-')}`

const save = async () => {
  if (saving.value || !isDirty.value || hasValidationErrors.value) return
  const token = getAuthToken()
  if (!token) {
    saveErrorMessage.value = t('alerts.authRequired')
    return
  }
  saving.value = true
  saveErrorMessage.value = ''
  try {
    const payload = await saveAlertRules(
      token,
      rules.value.map((item) => ({
        id: item.id,
        enabled: item.enabled,
        email_enabled: item.email_enabled,
        telegram_enabled: item.telegram_enabled,
        cooldown_minutes: item.cooldown_minutes,
      })),
    )
    applyPayload(payload)
    toast.success(t('alerts.saveSuccess'))
  } catch (error: unknown) {
    saveErrorMessage.value = t('alerts.saveFailed')
    notifyApiError(error, 'alerts.saveFailed')
  } finally {
    saving.value = false
  }
}

const sendTestMail = async () => {
  if (!smtpReady.value || testingMail.value) return
  const token = getAuthToken()
  if (!token) {
    testMailOk.value = false
    testMailMessage.value = t('alerts.authRequired')
    return
  }
  testingMail.value = true
  testMailMessage.value = ''
  try {
    const result = await testAlertMail(token)
    testMailOk.value = result.success
    testMailMessage.value = result.success
      ? t('alerts.testSuccess')
      : result.message || t('alerts.testFailed')
    if (result.success) toast.success(testMailMessage.value)
    else toast.error(testMailMessage.value)
  } catch (error: unknown) {
    testMailOk.value = false
    testMailMessage.value = t('alerts.testFailed')
    notifyApiError(error, 'alerts.testFailed')
  } finally {
    testingMail.value = false
  }
}

const statusLabel = (status?: string) => {
  if (status === 'sent') return t('alerts.statusSent')
  if (status === 'partial') return t('alerts.statusPartial')
  if (status === 'failed') return t('alerts.statusFailed')
  if (status === 'skipped') return t('alerts.statusSkipped')
  if (status === 'ignored') return t('alerts.statusIgnored')
  return status || '-'
}

const reasonLabel = (reason?: string) => {
  if (!reason) return ''
  const key = `alerts.reasons.${reason}`
  return te(key) ? t(key) : reason
}

const statusClass = (status?: string) => {
  if (status === 'sent') return 'text-emerald-600 dark:text-emerald-400'
  if (status === 'partial') return 'text-amber-600 dark:text-amber-300'
  if (status === 'skipped') return 'text-amber-600 dark:text-amber-300'
  if (status === 'ignored') return 'text-gray-500 dark:text-gray-400'
  if (status === 'failed') return 'text-rose-600 dark:text-rose-300'
  return 'text-gray-500 dark:text-gray-400'
}

const severityLabel = (severity?: string) => {
  if (severity === 'critical') return t('alerts.severityCritical')
  if (severity === 'info') return t('alerts.severityInfo')
  return t('alerts.severityWarning')
}

const severityClass = (severity?: string) => {
  if (severity === 'critical') return 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/50 dark:bg-rose-500/10 dark:text-rose-300'
  if (severity === 'info') return 'border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-800/50 dark:bg-sky-500/10 dark:text-sky-300'
  return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800/50 dark:bg-amber-500/10 dark:text-amber-300'
}

const channelLabel = (channel?: string) => {
  if (channel === 'email') return t('alerts.channelEmail')
  if (channel === 'telegram') return t('alerts.channelTelegram')
  return channel || '-'
}

onMounted(() => {
  void loadPage()
})
</script>

<template>
  <div class="space-y-6" :aria-busy="pageLoading">
    <div v-if="pageLoading" class="ui-card p-8 text-sm text-gray-500" role="status">
      {{ t('common.loading') }}
    </div>
    <template v-else>
      <div>
        <div class="ui-section-label mb-2">{{ t('alerts.eyebrow') }} / {{ t('nav.alerts') }}</div>
        <h2 class="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100">{{ t('alerts.title') }}</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-2 max-w-3xl">{{ t('alerts.pageHint') }}</p>
      </div>

      <PageRetry
        v-if="errorMessage"
        :message="errorMessage"
        :loading="pageLoading"
        @retry="loadPage"
      />

      <template v-else>
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          <div class="ui-card p-4">
            <div class="ui-section-label">{{ t('alerts.smtpStatus') }}</div>
            <div
              class="mt-2 flex items-center gap-2 text-sm font-medium"
              :class="smtpReady ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'"
            >
              <CheckCircle2 v-if="smtpReady" class="w-4 h-4" aria-hidden="true" />
              <XCircle v-else class="w-4 h-4" aria-hidden="true" />
              {{ smtpReady ? t('alerts.smtpReady') : (smtpEnabled ? t('alerts.smtpIncomplete') : t('alerts.smtpOff')) }}
            </div>
          </div>
          <div class="ui-card p-4">
            <div class="ui-section-label">{{ t('alerts.botStatus') }}</div>
            <div
              class="mt-2 flex items-center gap-2 text-sm font-medium"
              :class="botReady ? 'text-emerald-600 dark:text-emerald-400' : 'text-amber-600 dark:text-amber-400'"
            >
              <CheckCircle2 v-if="botReady" class="w-4 h-4" aria-hidden="true" />
              <XCircle v-else class="w-4 h-4" aria-hidden="true" />
              {{ botReady ? t('alerts.botReady') : t('alerts.botOff') }}
            </div>
          </div>
          <div class="ui-card p-4">
            <div class="ui-section-label">{{ t('alerts.quietStatus') }}</div>
            <div class="mt-2 text-sm font-medium text-gray-700 dark:text-gray-200">
              {{ quietHours ? t('alerts.quietOn') : t('alerts.quietOff') }}
            </div>
          </div>
          <div class="ui-card p-4 flex flex-col items-start gap-3">
            <div class="flex items-start gap-3">
              <Mail class="w-4 h-4 mt-0.5 text-gray-400 shrink-0" aria-hidden="true" />
              <p class="text-xs text-gray-500">{{ t('alerts.smtpHint') }}</p>
            </div>
            <button
              type="button"
              class="ui-btn-secondary inline-flex items-center gap-2 !px-3 !py-1.5 !text-xs"
              data-testid="test-alert-mail"
              :disabled="!smtpReady || testingMail"
              @click="sendTestMail"
            >
              <LoaderCircle v-if="testingMail" class="w-3.5 h-3.5 animate-spin" aria-hidden="true" />
              <Mail v-else class="w-3.5 h-3.5" aria-hidden="true" />
              {{ testingMail ? t('alerts.testing') : t('alerts.testMail') }}
            </button>
          </div>
        </div>

        <div
          v-if="testMailMessage"
          class="px-4 py-3 text-sm border"
          :class="testMailOk
            ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/40 dark:bg-emerald-500/10 dark:text-emerald-300'
            : 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/40 dark:bg-rose-500/10 dark:text-rose-300'"
          :role="testMailOk ? 'status' : 'alert'"
        >
          {{ testMailMessage }}
        </div>

        <div
          v-if="isDirty"
          class="flex flex-wrap items-center justify-between gap-2 border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800 dark:border-amber-800/50 dark:bg-amber-500/10 dark:text-amber-200"
          role="status"
        >
          <span>{{ t('alerts.unsaved') }}</span>
          <span v-if="hasValidationErrors" class="font-medium">{{ t('alerts.fixValidation') }}</span>
        </div>

        <div v-if="rulesByGroup.length" class="space-y-4">
          <section v-for="group in rulesByGroup" :key="group.id" class="ui-card p-5 space-y-4">
            <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ group.label }}</h3>
            <div
              v-for="item in group.items"
              :key="item.id"
              class="flex flex-col sm:flex-row sm:items-start gap-3 border-t border-gray-100 dark:border-white/[0.06] pt-4 first:border-0 first:pt-0"
            >
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <div class="text-sm text-gray-800 dark:text-gray-100">{{ ruleLabel(item) }}</div>
                  <span class="border px-1.5 py-0.5 text-[10px]" :class="severityClass(item.severity)">
                    {{ severityLabel(item.severity) }}
                  </span>
                </div>
                <p v-if="ruleHint(item)" class="text-[11px] text-gray-500 mt-1">{{ ruleHint(item) }}</p>
              </div>
              <div class="flex flex-wrap items-start gap-x-4 gap-y-3 shrink-0">
                <div class="space-y-1">
                  <span class="text-[11px] text-gray-500 block">{{ t('alerts.channelEmail') }}</span>
                  <button
                    type="button"
                    class="ui-switch"
                    role="switch"
                    :aria-label="t('alerts.channelToggleLabel', { channel: t('alerts.channelEmail'), rule: ruleLabel(item) })"
                    :aria-checked="item.email_enabled"
                    :class="item.email_enabled ? 'ui-switch-on' : ''"
                    @click="toggleChannel(item.id, 'email_enabled')"
                  >
                    <span class="ui-switch-knob" />
                  </button>
                </div>
                <div class="space-y-1">
                  <span class="text-[11px] text-gray-500 block">{{ t('alerts.channelTelegram') }}</span>
                  <button
                    type="button"
                    class="ui-switch"
                    role="switch"
                    :aria-label="t('alerts.channelToggleLabel', { channel: t('alerts.channelTelegram'), rule: ruleLabel(item) })"
                    :aria-checked="item.telegram_enabled"
                    :class="item.telegram_enabled ? 'ui-switch-on' : ''"
                    @click="toggleChannel(item.id, 'telegram_enabled')"
                  >
                    <span class="ui-switch-knob" />
                  </button>
                </div>
                <div class="space-y-1">
                  <label
                    class="text-[11px] text-gray-500 block"
                    :for="cooldownInputId(item.id)"
                  >
                    {{ t('alerts.cooldown') }} ({{ t('alerts.minutes') }})
                  </label>
                  <input
                    :id="cooldownInputId(item.id)"
                    :value="cooldownDrafts[item.id] ?? ''"
                    type="number"
                    inputmode="numeric"
                    min="1"
                    max="1440"
                    step="1"
                    class="ui-input !w-24 !py-1.5 !text-xs"
                    :class="cooldownErrors[item.id] ? '!border-rose-400 dark:!border-rose-500' : ''"
                    :aria-invalid="Boolean(cooldownErrors[item.id])"
                    :aria-describedby="cooldownErrors[item.id] ? `${cooldownInputId(item.id)}-error` : undefined"
                    @input="setCooldown(item.id, ($event.target as HTMLInputElement).value)"
                  >
                  <p
                    v-if="cooldownErrors[item.id]"
                    :id="`${cooldownInputId(item.id)}-error`"
                    class="max-w-44 text-[11px] text-rose-600 dark:text-rose-300"
                    role="alert"
                  >
                    {{ cooldownErrors[item.id] }}
                  </p>
                </div>
                <div class="space-y-1">
                  <span class="text-[11px] text-gray-500 block">{{ t('alerts.ruleEnabled') }}</span>
                  <button
                    type="button"
                    class="ui-switch shrink-0"
                    role="switch"
                    :aria-label="t('alerts.toggleLabel', { rule: ruleLabel(item) })"
                    :aria-checked="item.enabled"
                    :class="item.enabled ? 'ui-switch-on' : ''"
                    @click="toggleRule(item.id)"
                  >
                    <span class="ui-switch-knob" />
                  </button>
                </div>
              </div>
            </div>
          </section>
        </div>
        <div v-else class="ui-empty text-sm text-gray-500">
          {{ t('alerts.rulesEmpty') }}
        </div>

        <div v-if="saveErrorMessage" class="ui-alert-error" role="alert">
          {{ saveErrorMessage }}
        </div>

        <div class="flex justify-end">
          <button
            type="button"
            class="ui-btn-primary inline-flex items-center gap-2 !px-4 !py-2"
            :disabled="saving || !isDirty || hasValidationErrors"
            @click="save"
          >
            <LoaderCircle v-if="saving" class="w-4 h-4 animate-spin" aria-hidden="true" />
            <Save v-else class="w-4 h-4" aria-hidden="true" />
            {{ saving ? t('settings.saving') : t('alerts.save') }}
          </button>
        </div>

        <section class="ui-card p-5">
          <div class="flex items-center gap-2 mb-4">
            <Bell class="w-4 h-4 text-gray-400" aria-hidden="true" />
            <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('alerts.recent') }}</h3>
          </div>
          <div v-if="!recent.length" class="text-sm text-gray-500">{{ t('alerts.recentEmpty') }}</div>
          <ul v-else class="space-y-3">
            <li v-for="(item, index) in [...recent].reverse()" :key="`${item.at}-${index}`" class="border-t border-gray-100 dark:border-white/[0.06] pt-3 first:border-0 first:pt-0">
              <div class="flex flex-wrap items-baseline justify-between gap-2">
                <div class="text-sm text-gray-800 dark:text-gray-100">{{ recentTitle(item) }}</div>
                <div class="text-[11px] text-gray-400">{{ formatShortDateTime(item.at) }}</div>
              </div>
              <div class="mt-1 text-[11px]" :class="statusClass(item.status)">
                {{ statusLabel(item.status) }}
                <span v-if="item.reason"> · {{ reasonLabel(item.reason) }}</span>
                <span v-if="item.error"> · {{ item.error }}</span>
              </div>
              <ul v-if="item.deliveries?.length" class="mt-2 flex flex-wrap gap-2">
                <li
                  v-for="delivery in item.deliveries"
                  :key="`${delivery.channel}-${delivery.status}`"
                  class="border border-gray-200 dark:border-gray-700 px-2 py-1 text-[10px] text-gray-500 dark:text-gray-400"
                >
                  {{ channelLabel(delivery.channel) }} · {{ statusLabel(delivery.status) }}
                  <span v-if="delivery.reason"> · {{ reasonLabel(delivery.reason) }}</span>
                  <span v-if="delivery.error"> · {{ delivery.error }}</span>
                </li>
              </ul>
              <p v-if="item.detail" class="mt-1 text-xs text-gray-500 whitespace-pre-wrap break-words">{{ item.detail }}</p>
            </li>
          </ul>
        </section>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
/**
 * Telegram Bot 通知与控制区块：Token、用户 ID、通知开关、免打扰、Mini App。
 * 父组件 Settings.vue 持有 settings 状态与 revealSecrets，通过 v-model 同步并触发保存/测试。
 */
import { computed } from 'vue'
import { Activity, Bot, Eye, EyeOff, RefreshCw, ShieldCheck, Smartphone } from 'lucide-vue-next'
import { useI18n } from '../../composables/useI18n'
import type { TelegramBotRuntimeStatus } from '../../lib/api'
import {
  validateBotControlSettings,
  type SettingsFormState,
} from '../../lib/settings-form'

interface RevealSecrets {
  botToken: boolean
}

const props = defineProps<{
  /** 全局表单状态（v-model） */
  modelValue: SettingsFormState
  /** 服务端是否已保存 Bot Token */
  botTokenSet?: boolean
  /** 密钥显隐（仅 botToken） */
  reveal: Pick<RevealSecrets, 'botToken'>
  /** 保存中 */
  botLoading?: boolean
  /** 测试 Bot 中 */
  botTestLoading?: boolean
  /** Bot 长轮询运行快照（不含 Token） */
  runtimeStatus?: TelegramBotRuntimeStatus | null
  /** 刷新运行快照中 */
  runtimeLoading?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: SettingsFormState): void
  (e: 'save'): void
  (e: 'test'): void
  (e: 'refresh-status'): void
  (e: 'toggle-reveal', key: 'botToken'): void
}>()

const { t } = useI18n()

const controlValidation = computed(() => validateBotControlSettings(props.modelValue))
const hasControlErrors = computed(() => Boolean(
  controlValidation.value.miniAppUrl || controlValidation.value.allowedUserIds,
))
const validationMessage = (code?: string) => code ? t(`apiErrors.${code}`) : ''
const knownRuntimeStates = new Set([
  'running',
  'standby',
  'webhook_conflict',
  'polling_conflict',
  'disabled',
  'unconfigured',
  'stopped',
  'error',
  'unknown',
])
const runtimeState = computed(() => props.runtimeStatus?.state || 'unknown')
const runtimeStateLabel = computed(() => {
  const state = runtimeState.value
  if (!knownRuntimeStates.has(state)) return state
  return t(`settings.botRuntimeStates.${state}`)
})
const runtimeToneClass = computed(() => {
  if (runtimeState.value === 'running') return 'text-emerald-700 dark:text-emerald-300'
  if (runtimeState.value === 'standby' || runtimeState.value === 'disabled' || runtimeState.value === 'stopped') {
    return 'text-gray-600 dark:text-gray-300'
  }
  return 'text-amber-700 dark:text-amber-300'
})
const runtimeBotLabel = computed(() => {
  const bot = props.runtimeStatus?.bot
  if (bot?.username) return `@${bot.username}`
  return bot?.first_name || ''
})

const update = <K extends keyof SettingsFormState>(key: K, value: SettingsFormState[K]) => {
  emit('update:modelValue', { ...props.modelValue, [key]: value } as SettingsFormState)
}

const onStringInput = (key: keyof SettingsFormState, e: Event) => {
  update(key, (e.target as HTMLInputElement).value as never)
}

const onCheckbox = (key: keyof SettingsFormState, e: Event) => {
  update(key, (e.target as HTMLInputElement).checked as never)
}
</script>

<template>
  <section class="ui-card p-6">
    <div class="mb-6 border-b border-gray-200 dark:border-gray-800/60 pb-3 flex items-center justify-between gap-3">
      <div class="flex items-start gap-3 min-w-0">
        <span class="ui-section-icon" aria-hidden="true"><Bot class="w-3.5 h-3.5" /></span>
        <div class="min-w-0">
          <h2 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('settings.botNotify') }}</h2>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('settings.botDesc') }}</p>
        </div>
      </div>
      <button
        type="button"
        class="ui-switch shrink-0"
        role="switch"
        :aria-label="t('settings.botNotifyToggle')"
        :aria-checked="modelValue.botEnabled"
        :class="modelValue.botEnabled ? 'ui-switch-on' : ''"
        @click="update('botEnabled', !modelValue.botEnabled)"
      >
        <span class="ui-switch-knob" />
      </button>
    </div>

    <div class="space-y-5">
      <div class="space-y-1.5">
        <label class="ui-label" for="settings-bot-token">{{ t('settings.botToken') }}</label>
        <div class="relative">
          <input
            id="settings-bot-token"
            :value="modelValue.botToken"
            @input="onStringInput('botToken', $event)"
            :type="reveal.botToken ? 'text' : 'password'"
            :placeholder="botTokenSet ? t('settings.botTokenSavedHint') : '123456:ABC-DEF...'"
            class="ui-input pr-10"
          >
          <button type="button" class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200" :aria-label="reveal.botToken ? t('settings.hideSecret') : t('settings.showSecret')" @click="emit('toggle-reveal', 'botToken')">
            <EyeOff v-if="reveal.botToken" class="w-4 h-4" /><Eye v-else class="w-4 h-4" />
          </button>
        </div>
      </div>
      <div class="flex flex-wrap gap-x-6 gap-y-3 pt-2">
        <label class="flex items-center gap-2 cursor-pointer group">
          <input :checked="modelValue.botLoginNotify" @change="onCheckbox('botLoginNotify', $event)" type="checkbox" class="w-4 h-4 accent-sky-500 bg-gray-100 border-gray-300 rounded focus:ring-0 dark:bg-gray-800 dark:border-gray-600">
          <span class="text-sm text-gray-700 dark:text-gray-300 group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">{{ t('settings.loginFailNotify') }}</span>
        </label>
        <label class="flex items-center gap-2 cursor-pointer group">
          <input :checked="modelValue.botTaskFailure" @change="onCheckbox('botTaskFailure', $event)" type="checkbox" class="w-4 h-4 accent-sky-500 bg-gray-100 border-gray-300 rounded focus:ring-0 dark:bg-gray-800 dark:border-gray-600">
          <span class="text-sm text-gray-700 dark:text-gray-300 group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">{{ t('settings.taskFailNotify') }}</span>
        </label>
        <label class="flex items-center gap-2 cursor-pointer group">
          <input :checked="modelValue.botTaskSuccess" @change="onCheckbox('botTaskSuccess', $event)" type="checkbox" class="w-4 h-4 accent-sky-500 bg-gray-100 border-gray-300 rounded focus:ring-0 dark:bg-gray-800 dark:border-gray-600">
          <span class="text-sm text-gray-700 dark:text-gray-300 group-hover:text-gray-900 dark:group-hover:text-gray-100 transition-colors">{{ t('settings.taskSuccessNotify') }}</span>
        </label>
      </div>
      <div class="p-3 bg-gray-50 dark:bg-white/[0.02] border border-gray-200 dark:border-gray-800/60 space-y-3">
        <div class="flex items-center justify-between gap-3">
          <div>
            <label class="text-xs text-gray-600 dark:text-gray-300 block">{{ t('settings.quietHours') }}</label>
            <p class="text-[10px] text-gray-500 mt-1">{{ t('settings.quietHoursDesc') }}</p>
          </div>
          <button
            type="button"
            class="ui-switch"
            role="switch"
            :aria-label="t('settings.quietHoursToggle')"
            :aria-checked="modelValue.quietEnabled"
            :class="modelValue.quietEnabled ? 'ui-switch-on' : ''"
            @click="update('quietEnabled', !modelValue.quietEnabled)"
          >
            <span class="ui-switch-knob" />
          </button>
        </div>
        <div class="grid grid-cols-2 gap-2" v-if="modelValue.quietEnabled">
          <div class="space-y-1">
            <label class="text-[10px] text-gray-500">{{ t('settings.quietStart') }}</label>
            <input :value="modelValue.quietStart" @input="onStringInput('quietStart', $event)" type="text" placeholder="23:00" class="ui-input" />
          </div>
          <div class="space-y-1">
            <label class="text-[10px] text-gray-500">{{ t('settings.quietEnd') }}</label>
            <input :value="modelValue.quietEnd" @input="onStringInput('quietEnd', $event)" type="text" placeholder="07:00" class="ui-input" />
          </div>
        </div>
      </div>

      <div class="border-t border-gray-200 dark:border-gray-800/60 pt-5 space-y-4">
        <div class="flex items-start justify-between gap-3">
          <div class="flex items-start gap-3 min-w-0">
            <span class="ui-section-icon" aria-hidden="true"><Smartphone class="w-3.5 h-3.5" /></span>
            <div class="min-w-0">
              <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">{{ t('settings.botControl') }}</h3>
              <p class="text-[11px] leading-relaxed text-gray-500 mt-1">{{ t('settings.botControlDesc') }}</p>
            </div>
          </div>
          <button
            type="button"
            class="ui-switch shrink-0"
            role="switch"
            :aria-label="t('settings.botControlToggle')"
            :aria-checked="modelValue.botControlEnabled"
            :class="modelValue.botControlEnabled ? 'ui-switch-on' : ''"
            @click="update('botControlEnabled', !modelValue.botControlEnabled)"
          >
            <span class="ui-switch-knob" />
          </button>
        </div>

        <div
          class="border-y border-gray-200 py-3 dark:border-gray-800/60"
          data-testid="bot-runtime-status"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <div class="flex items-center justify-between gap-3">
            <div class="flex min-w-0 items-center gap-2">
              <Activity class="h-4 w-4 shrink-0" :class="runtimeToneClass" aria-hidden="true" />
              <span class="text-xs text-gray-500">{{ t('settings.botRuntimeStatus') }}</span>
              <strong class="truncate text-xs font-medium" :class="runtimeToneClass">
                {{ runtimeStateLabel }}
              </strong>
            </div>
            <button
              type="button"
              class="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center text-gray-500 transition-colors hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:text-gray-100"
              :disabled="runtimeLoading"
              :aria-label="t('settings.botRuntimeRefresh')"
              :title="t('settings.botRuntimeRefresh')"
              @click="emit('refresh-status')"
            >
              <RefreshCw class="h-4 w-4" :class="{ 'animate-spin': runtimeLoading }" aria-hidden="true" />
            </button>
          </div>
          <p v-if="runtimeBotLabel" class="mt-1 break-words text-[11px] text-gray-500">
            {{ t('settings.botRuntimeIdentity') }}: {{ runtimeBotLabel }}
          </p>
          <p
            v-if="runtimeStatus?.last_error"
            class="mt-1 break-words text-[11px] text-amber-700 dark:text-amber-300"
          >
            {{ t('settings.botRuntimeLastError') }}:
            <code>{{ runtimeStatus.last_error }}</code>
          </p>
        </div>

        <div class="space-y-1.5">
          <label class="ui-label" for="settings-bot-allowed-users">{{ t('settings.botAllowedUserIds') }}</label>
          <input
            id="settings-bot-allowed-users"
            :value="modelValue.botAllowedUserIds"
            @input="onStringInput('botAllowedUserIds', $event)"
            type="text"
            inputmode="numeric"
            autocomplete="off"
            :placeholder="t('settings.botAllowedUserIdsPlaceholder')"
            class="ui-input"
            :class="controlValidation.allowedUserIds ? '!border-rose-400 dark:!border-rose-500' : ''"
            :aria-invalid="Boolean(controlValidation.allowedUserIds)"
            :aria-describedby="controlValidation.allowedUserIds ? 'settings-bot-allowed-users-error' : 'settings-bot-allowed-users-hint'"
          >
          <p id="settings-bot-allowed-users-hint" class="text-[11px] text-gray-500 flex items-start gap-1.5">
            <ShieldCheck class="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden="true" />
            <span>{{ t('settings.botAllowedUserIdsHint') }}</span>
          </p>
          <p
            v-if="controlValidation.allowedUserIds"
            id="settings-bot-allowed-users-error"
            class="text-[11px] text-rose-600 dark:text-rose-300"
            role="alert"
          >
            {{ validationMessage(controlValidation.allowedUserIds) }}
          </p>
        </div>

        <div class="space-y-1.5">
          <label class="ui-label" for="settings-mini-app-url">{{ t('settings.miniAppUrl') }}</label>
          <input
            id="settings-mini-app-url"
            :value="modelValue.botMiniAppUrl"
            @input="onStringInput('botMiniAppUrl', $event)"
            type="url"
            inputmode="url"
            autocomplete="url"
            placeholder="https://panel.example.com/mini-app"
            class="ui-input"
            :class="controlValidation.miniAppUrl ? '!border-rose-400 dark:!border-rose-500' : ''"
            :aria-invalid="Boolean(controlValidation.miniAppUrl)"
            :aria-describedby="controlValidation.miniAppUrl ? 'settings-mini-app-url-error' : 'settings-mini-app-url-hint'"
          >
          <p id="settings-mini-app-url-hint" class="text-[11px] text-gray-500">{{ t('settings.miniAppUrlHint') }}</p>
          <p
            v-if="controlValidation.miniAppUrl"
            id="settings-mini-app-url-error"
            class="text-[11px] text-rose-600 dark:text-rose-300"
            role="alert"
          >
            {{ validationMessage(controlValidation.miniAppUrl) }}
          </p>
        </div>

      </div>

      <div class="pt-2 flex flex-col sm:flex-row gap-2">
        <button type="button" class="ui-btn-primary flex-1 py-2.5" :disabled="botLoading || hasControlErrors" @click="emit('save')">{{ botLoading ? t('settings.saving') : t('settings.saveChanges') }}</button>
        <button type="button" class="ui-btn-secondary flex-1 py-2.5" :disabled="botTestLoading" @click="emit('test')">{{ botTestLoading ? t('settings.testing') : t('settings.testBot') }}</button>
      </div>
    </div>
  </section>
</template>

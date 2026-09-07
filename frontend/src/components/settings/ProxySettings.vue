<script setup lang="ts">
import { computed } from 'vue'
import {
  Eye,
  EyeOff,
  Network,
  Save,
  TestTube2,
  Trash2,
} from 'lucide-vue-next'
import CustomSelect from '../CustomSelect.vue'
import { useI18n } from '../../composables/useI18n'
import {
  parseNumberInputValue,
  validateProxySettings,
  type ProxyScheme,
  type SettingsFormState,
} from '../../lib/settings-form'

interface RevealSecrets {
  proxyPassword: boolean
}

const props = defineProps<{
  modelValue: SettingsFormState
  passwordSet?: boolean
  reveal: Pick<RevealSecrets, 'proxyPassword'>
  loading?: boolean
  testLoading?: boolean
  testDisabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: SettingsFormState): void
  (e: 'save'): void
  (e: 'test'): void
  (e: 'toggle-reveal', key: 'proxyPassword'): void
}>()

const { t } = useI18n()

const schemeOptions = [
  { label: 'HTTP', value: 'http' },
  { label: 'SOCKS5', value: 'socks5' },
]

const validation = computed(() => validateProxySettings(props.modelValue))
const hasValidationErrors = computed(() => Boolean(
  validation.value.scheme || validation.value.host || validation.value.port,
))
const hasCredentials = computed(() => Boolean(
  props.passwordSet ||
  props.modelValue.proxyUsername ||
  props.modelValue.proxyPassword ||
  props.modelValue.proxyClearCredentials,
))

const update = <K extends keyof SettingsFormState>(
  key: K,
  value: SettingsFormState[K],
) => {
  const next = { ...props.modelValue, [key]: value } as SettingsFormState
  if (key === 'proxyUsername' || key === 'proxyPassword') {
    next.proxyClearCredentials = false
  }
  emit('update:modelValue', next)
}

const onStringInput = (key: keyof SettingsFormState, event: Event) => {
  update(key, (event.target as HTMLInputElement).value as never)
}

const onPortInput = (event: Event) => {
  update(
    'proxyPort',
    parseNumberInputValue((event.target as HTMLInputElement).value),
  )
}

const clearCredentials = () => {
  emit('update:modelValue', {
    ...props.modelValue,
    proxyUsername: '',
    proxyPassword: '',
    proxyClearCredentials: true,
  })
}
</script>

<template>
  <section class="ui-card p-6">
    <div class="mb-6 flex items-center justify-between gap-3 border-b border-gray-200 pb-3 dark:border-gray-800/60">
      <div class="flex min-w-0 items-start gap-3">
        <span class="ui-section-icon" aria-hidden="true">
          <Network class="h-3.5 w-3.5" />
        </span>
        <div class="min-w-0">
          <h2 class="text-base font-medium text-gray-900 dark:text-gray-100">
            {{ t('settings.proxyTitle') }}
          </h2>
          <p class="mt-1 text-[10px] leading-relaxed text-gray-500">
            {{ t('settings.proxyDesc') }}
          </p>
        </div>
      </div>
      <button
        type="button"
        class="ui-switch shrink-0"
        role="switch"
        :aria-label="t('settings.proxyToggle')"
        :aria-checked="modelValue.proxyEnabled"
        :class="modelValue.proxyEnabled ? 'ui-switch-on' : ''"
        @click="update('proxyEnabled', !modelValue.proxyEnabled)"
      >
        <span class="ui-switch-knob" />
      </button>
    </div>

    <div class="space-y-5">
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-[8rem_minmax(0,1fr)_7rem]">
        <div class="space-y-1.5">
          <label class="ui-label" for="settings-proxy-scheme">
            {{ t('settings.proxyScheme') }}
          </label>
          <CustomSelect
            id="settings-proxy-scheme"
            :model-value="modelValue.proxyScheme"
            :options="schemeOptions"
            :disabled="!modelValue.proxyEnabled"
            :aria-label="t('settings.proxyScheme')"
            class-name="w-full"
            @update:model-value="update('proxyScheme', String($event) as ProxyScheme)"
          />
        </div>

        <div class="space-y-1.5">
          <label class="ui-label" for="settings-proxy-host">
            {{ t('settings.proxyHost') }}
          </label>
          <input
            id="settings-proxy-host"
            :value="modelValue.proxyHost"
            type="text"
            inputmode="url"
            autocomplete="off"
            placeholder="127.0.0.1"
            class="ui-input disabled:cursor-not-allowed disabled:opacity-50"
            :class="validation.host ? '!border-rose-400 dark:!border-rose-500' : ''"
            :disabled="!modelValue.proxyEnabled"
            :aria-invalid="Boolean(validation.host)"
            :aria-describedby="validation.host ? 'settings-proxy-host-error' : undefined"
            @input="onStringInput('proxyHost', $event)"
          >
          <p
            v-if="validation.host"
            id="settings-proxy-host-error"
            class="text-[11px] text-rose-600 dark:text-rose-300"
            role="alert"
          >
            {{ t(`apiErrors.${validation.host}`) }}
          </p>
        </div>

        <div class="space-y-1.5">
          <label class="ui-label" for="settings-proxy-port">
            {{ t('settings.proxyPort') }}
          </label>
          <input
            id="settings-proxy-port"
            :value="modelValue.proxyPort"
            type="number"
            inputmode="numeric"
            min="1"
            max="65535"
            placeholder="7890"
            class="ui-input disabled:cursor-not-allowed disabled:opacity-50"
            :class="validation.port ? '!border-rose-400 dark:!border-rose-500' : ''"
            :disabled="!modelValue.proxyEnabled"
            :aria-invalid="Boolean(validation.port)"
            :aria-describedby="validation.port ? 'settings-proxy-port-error' : undefined"
            @input="onPortInput"
          >
          <p
            v-if="validation.port"
            id="settings-proxy-port-error"
            class="text-[11px] text-rose-600 dark:text-rose-300"
            role="alert"
          >
            {{ t(`apiErrors.${validation.port}`) }}
          </p>
        </div>
      </div>

      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div class="space-y-1.5">
          <label class="ui-label" for="settings-proxy-username">
            {{ t('settings.proxyUsername') }}
          </label>
          <input
            id="settings-proxy-username"
            :value="modelValue.proxyUsername"
            type="text"
            autocomplete="username"
            class="ui-input disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="!modelValue.proxyEnabled"
            @input="onStringInput('proxyUsername', $event)"
          >
        </div>

        <div class="space-y-1.5">
          <label class="ui-label" for="settings-proxy-password">
            {{ t('settings.proxyPassword') }}
          </label>
          <div class="relative">
            <input
              id="settings-proxy-password"
              :value="modelValue.proxyPassword"
              :type="reveal.proxyPassword ? 'text' : 'password'"
              autocomplete="new-password"
              class="ui-input pr-12 disabled:cursor-not-allowed disabled:opacity-50"
              :placeholder="passwordSet ? t('settings.proxyPasswordSavedHint') : t('settings.proxyPasswordPlaceholder')"
              :disabled="!modelValue.proxyEnabled"
              @input="onStringInput('proxyPassword', $event)"
            >
            <button
              type="button"
              class="absolute right-0 top-1/2 inline-flex min-h-11 min-w-11 -translate-y-1/2 items-center justify-center text-gray-400 transition-colors hover:text-gray-700 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:text-gray-200"
              :disabled="!modelValue.proxyEnabled"
              :aria-label="reveal.proxyPassword ? t('settings.hideSecret') : t('settings.showSecret')"
              :title="reveal.proxyPassword ? t('settings.hideSecret') : t('settings.showSecret')"
              @click="emit('toggle-reveal', 'proxyPassword')"
            >
              <EyeOff v-if="reveal.proxyPassword" class="h-4 w-4" aria-hidden="true" />
              <Eye v-else class="h-4 w-4" aria-hidden="true" />
            </button>
          </div>
          <p class="text-[10px] leading-relaxed text-gray-500">
            {{ t('settings.proxyPasswordHint') }}
          </p>
        </div>
      </div>

      <div class="space-y-1.5">
        <label class="ui-label" for="settings-proxy-no-proxy">
          {{ t('settings.proxyNoProxy') }}
        </label>
        <textarea
          id="settings-proxy-no-proxy"
          :value="modelValue.proxyNoProxy"
          rows="3"
          class="ui-input min-h-24 resize-y disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="!modelValue.proxyEnabled"
          :placeholder="t('settings.proxyNoProxyPlaceholder')"
          @input="onStringInput('proxyNoProxy', $event)"
        />
        <p class="text-[10px] leading-relaxed text-gray-500">
          {{ t('settings.proxyNoProxyHint') }}
        </p>
      </div>

      <div class="border-t border-gray-200 pt-4 dark:border-gray-800/60">
        <button
          type="button"
          class="inline-flex min-h-11 items-center gap-2 px-1 text-xs text-rose-600 transition-colors hover:text-rose-700 disabled:cursor-not-allowed disabled:opacity-40 dark:text-rose-300 dark:hover:text-rose-200"
          :disabled="!hasCredentials || modelValue.proxyClearCredentials"
          @click="clearCredentials"
        >
          <Trash2 class="h-4 w-4" aria-hidden="true" />
          {{ t('settings.proxyClearCredentials') }}
        </button>
        <p
          v-if="modelValue.proxyClearCredentials"
          class="mt-1 text-[11px] leading-relaxed text-amber-700 dark:text-amber-300"
          role="status"
        >
          {{ t('settings.proxyClearCredentialsPending') }}
        </p>
      </div>

      <div class="flex flex-col gap-2 pt-1 sm:flex-row">
        <button
          type="button"
          class="ui-btn-primary flex min-h-11 flex-1 items-center justify-center gap-2 py-2.5"
          :disabled="loading || hasValidationErrors"
          @click="emit('save')"
        >
          <Save class="h-4 w-4" aria-hidden="true" />
          {{ loading ? t('settings.saving') : t('settings.proxySave') }}
        </button>
        <button
          type="button"
          class="ui-btn-secondary flex min-h-11 flex-1 items-center justify-center gap-2 py-2.5"
          :disabled="testLoading || loading || testDisabled || !modelValue.proxyEnabled"
          :title="t('settings.proxyTestHint')"
          @click="emit('test')"
        >
          <TestTube2 class="h-4 w-4" aria-hidden="true" />
          {{ testLoading ? t('settings.testing') : t('settings.proxyTest') }}
        </button>
      </div>
      <p class="text-[10px] leading-relaxed text-gray-500">
        {{ t('settings.proxyTestHint') }}
      </p>
    </div>
  </section>
</template>

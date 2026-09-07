<script setup lang="ts">
/**
 * SMTP 邮件通知：主机 / 端口 / 加密 / 账号 / 发件人 / 通知邮箱。
 * 密码 GET 不回传，空串保存表示保留原值。
 */
import { Mail, Eye, EyeOff } from 'lucide-vue-next'
import CustomSelect from '../CustomSelect.vue'
import { useI18n } from '../../composables/useI18n'
import { parseNumberInputValue, type SettingsFormState } from '../../lib/settings-form'

interface RevealSecrets {
  smtpPassword: boolean
}

const props = defineProps<{
  modelValue: SettingsFormState
  smtpPasswordSet?: boolean
  reveal: Pick<RevealSecrets, 'smtpPassword'>
  smtpLoading?: boolean
  smtpTestLoading?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: SettingsFormState): void
  (e: 'save'): void
  (e: 'test'): void
  (e: 'toggle-reveal', key: 'smtpPassword'): void
}>()

const { t } = useI18n()

const encryptionOptions = [
  { label: 'SSL / TLS', value: 'ssl' },
  { label: 'STARTTLS', value: 'starttls' },
  { label: t('settings.smtpEncryptionNone'), value: 'none' },
]

const update = <K extends keyof SettingsFormState>(key: K, value: SettingsFormState[K]) => {
  emit('update:modelValue', { ...props.modelValue, [key]: value } as SettingsFormState)
}

const onStringInput = (key: keyof SettingsFormState, e: Event) => {
  update(key, (e.target as HTMLInputElement).value as never)
}

const onNumberInput = (key: keyof SettingsFormState, e: Event) => {
  update(key, parseNumberInputValue((e.target as HTMLInputElement).value) as never)
}
</script>

<template>
  <section class="ui-card p-6">
    <div class="mb-6 border-b border-gray-200 dark:border-gray-800/60 pb-3 flex items-center justify-between gap-3">
      <div class="flex items-start gap-3 min-w-0">
        <span class="ui-section-icon" aria-hidden="true"><Mail class="w-3.5 h-3.5" /></span>
        <div class="min-w-0">
          <h2 class="text-base font-medium text-gray-900 dark:text-gray-100">{{ t('settings.smtpTitle') }}</h2>
          <p class="text-[10px] text-gray-500 mt-1">{{ t('settings.smtpDesc') }}</p>
        </div>
      </div>
      <button
        type="button"
        class="ui-switch shrink-0"
        role="switch"
        :aria-checked="modelValue.smtpEnabled"
        :class="modelValue.smtpEnabled ? 'ui-switch-on' : ''"
        @click="update('smtpEnabled', !modelValue.smtpEnabled)"
      >
        <span class="ui-switch-knob" />
      </button>
    </div>

    <div class="space-y-5">
      <div class="grid grid-cols-1 sm:grid-cols-[1fr_7rem] gap-3">
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('settings.smtpHost') }}</label>
          <input
            :value="modelValue.smtpHost"
            @input="onStringInput('smtpHost', $event)"
            type="text"
            placeholder="smtp.example.com"
            class="ui-input"
          >
        </div>
        <div class="space-y-1.5">
          <label class="ui-label">{{ t('settings.smtpPort') }}</label>
          <input
            :value="modelValue.smtpPort"
            @input="onNumberInput('smtpPort', $event)"
            type="number"
            min="1"
            max="65535"
            placeholder="465"
            class="ui-input"
          >
        </div>
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('settings.smtpEncryption') }}</label>
        <CustomSelect
          :modelValue="modelValue.smtpEncryption"
          @update:modelValue="update('smtpEncryption', String($event ?? 'ssl') as SettingsFormState['smtpEncryption'])"
          :options="encryptionOptions"
          className="w-full"
        />
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('settings.smtpUsername') }}</label>
        <input
          :value="modelValue.smtpUsername"
          @input="onStringInput('smtpUsername', $event)"
          type="text"
          autocomplete="username"
          class="ui-input"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('settings.smtpPassword') }}</label>
        <div class="relative">
          <input
            :value="modelValue.smtpPassword"
            @input="onStringInput('smtpPassword', $event)"
            :type="reveal.smtpPassword ? 'text' : 'password'"
            :placeholder="smtpPasswordSet ? t('settings.smtpPasswordSavedHint') : t('settings.smtpPasswordPlaceholder')"
            class="ui-input pr-10"
            autocomplete="new-password"
          >
          <button
            type="button"
            class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
            :aria-label="reveal.smtpPassword ? t('settings.hideSecret') : t('settings.showSecret')"
            @click="emit('toggle-reveal', 'smtpPassword')"
          >
            <EyeOff v-if="reveal.smtpPassword" class="w-4 h-4" /><Eye v-else class="w-4 h-4" />
          </button>
        </div>
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('settings.smtpFrom') }}</label>
        <input
          :value="modelValue.smtpFrom"
          @input="onStringInput('smtpFrom', $event)"
          type="text"
          :placeholder="t('settings.smtpFromPlaceholder')"
          class="ui-input"
        >
      </div>
      <div class="space-y-1.5">
        <label class="ui-label">{{ t('settings.smtpNotifyEmail') }}</label>
        <input
          :value="modelValue.smtpNotifyEmail"
          @input="onStringInput('smtpNotifyEmail', $event)"
          type="text"
          :placeholder="t('settings.smtpNotifyEmailPlaceholder')"
          class="ui-input"
        >
        <p class="text-[10px] text-gray-500">{{ t('settings.smtpNotifyEmailHint') }}</p>
      </div>
      <div class="pt-2 flex flex-col sm:flex-row gap-2">
        <button type="button" class="ui-btn-primary flex-1 py-2.5" :disabled="smtpLoading" @click="emit('save')">
          {{ smtpLoading ? t('settings.saving') : t('settings.saveChanges') }}
        </button>
        <button type="button" class="ui-btn-secondary flex-1 py-2.5" :disabled="smtpTestLoading" @click="emit('test')">
          {{ smtpTestLoading ? t('settings.testing') : t('settings.smtpTest') }}
        </button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { Eye, EyeOff, LoaderCircle } from 'lucide-vue-next'
import { useI18n } from '../composables/useI18n'

withDefaults(
  defineProps<{
    modelValue: string
    revealed: boolean
    loading?: boolean
    placeholder?: string
    inputId?: string
    disabled?: boolean
    autocomplete?: string
  }>(),
  { autocomplete: 'new-password' },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  toggle: []
}>()

const { t } = useI18n()
</script>

<template>
  <div class="relative">
    <input
      :id="inputId"
      :value="modelValue"
      :type="revealed ? 'text' : 'password'"
      class="ui-input pr-10"
      :placeholder="placeholder"
      :disabled="disabled"
      :autocomplete="autocomplete"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    >
    <button
      type="button"
      class="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 disabled:opacity-50"
      :disabled="disabled || loading"
      :aria-label="revealed ? t('settings.hideSecret') : t('settings.showSecret')"
      @click="emit('toggle')"
    >
      <LoaderCircle v-if="loading" class="w-4 h-4 animate-spin" />
      <EyeOff v-else-if="revealed" class="w-4 h-4" />
      <Eye v-else class="w-4 h-4" />
    </button>
  </div>
</template>

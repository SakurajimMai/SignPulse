<script setup lang="ts">
import { useI18n } from '../../composables/useI18n'

defineProps<{
  modelValue?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const { t } = useI18n()
</script>

<template>
  <select
    class="ui-input"
    :value="modelValue || 'imgbed'"
    @change="emit('update:modelValue', ($event.target as HTMLSelectElement).value)"
  >
    <option value="imgbed">{{ t('manga.uploadImgbed') }}</option>
    <option value="ftp">{{ t('manga.uploadFtp') }}</option>
    <option value="sftp">{{ t('manga.uploadSftp') }}</option>
    <option value="s3">{{ t('manga.uploadS3') }}</option>
    <option value="r2">{{ t('manga.uploadR2') }}</option>
    <option value="b2">{{ t('manga.uploadB2') }}</option>
  </select>
</template>

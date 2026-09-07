<script setup lang="ts">
import { ref, watch } from 'vue'
import { ExternalLink, Image } from 'lucide-vue-next'
import { formatShortDateTime } from '../../lib/datetime'
import { useI18n } from '../../composables/useI18n'

const props = defineProps<{
  title: string
  idLabel?: string | number | null
  author?: string | null
  coverUrl?: string | null
  pageCount?: number | null
  chapterCount?: number | null
  updatedAt?: string | null
  publicUrl?: string | null
  selected?: boolean
  subtitle?: string | null
}>()

const emit = defineEmits<{
  click: []
}>()

const { t } = useI18n()
const coverFailed = ref(false)

watch(
  () => props.coverUrl,
  () => {
    coverFailed.value = false
  },
)

const showCover = () => Boolean(props.coverUrl) && !coverFailed.value
</script>

<template>
  <article
    class="ui-card ui-card-hover overflow-hidden flex min-h-[156px] text-left cursor-pointer"
    :class="selected ? 'ring-1 ring-sky-400/70' : ''"
    @click="emit('click')"
  >
    <div class="relative w-24 sm:w-28 shrink-0 self-stretch overflow-hidden bg-gray-100 dark:bg-gray-900/60 min-h-[156px]">
      <img
        v-if="showCover()"
        :src="coverUrl || ''"
        alt=""
        loading="lazy"
        decoding="async"
        referrerpolicy="no-referrer"
        class="absolute inset-0 h-full w-full object-cover object-center"
        @error="coverFailed = true"
      >
      <div v-else class="absolute inset-0 flex items-center justify-center text-gray-400">
        <Image class="w-6 h-6" />
      </div>
    </div>
    <div class="min-w-0 flex-1 p-4 flex flex-col">
      <div class="flex items-start justify-between gap-2">
        <h4 class="font-medium text-sm text-gray-900 dark:text-gray-100 line-clamp-2" :title="title">{{ title }}</h4>
        <span v-if="idLabel != null && idLabel !== ''" class="text-[10px] font-mono text-gray-400 shrink-0">#{{ idLabel }}</span>
      </div>
      <p v-if="author" class="text-xs text-gray-500 mt-2 line-clamp-1">{{ author }}</p>
      <p v-else-if="subtitle" class="text-xs text-gray-500 mt-2 line-clamp-1">{{ subtitle }}</p>
      <p class="text-[11px] text-gray-500 mt-1 tabular-nums">
        {{ pageCount ?? 0 }} P · {{ chapterCount ?? 0 }} {{ t('manga.chapters') }}
      </p>
      <div class="mt-auto pt-3 flex items-center justify-between gap-2">
        <span class="text-[10px] text-gray-400 truncate">{{ formatShortDateTime(updatedAt) }}</span>
        <a
          v-if="publicUrl"
          :href="publicUrl"
          target="_blank"
          rel="noopener"
          class="ui-row-action !p-1"
          :title="t('manga.openSite')"
          @click.stop
        >
          <ExternalLink class="w-3.5 h-3.5" />
        </a>
      </div>
    </div>
  </article>
</template>

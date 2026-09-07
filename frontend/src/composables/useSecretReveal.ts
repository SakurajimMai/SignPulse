import { ref, type Ref } from 'vue'

export function useSecretReveal<T extends Record<string, string>>(
  createEmpty: () => T,
  options: {
    isSaved: (key: keyof T & string) => boolean
    fetchRevealed: () => Promise<unknown>
    onError?: (error: unknown) => void
  },
) {
  const keys = Object.keys(createEmpty()) as Array<keyof T & string>
  const emptyReveal = () =>
    Object.fromEntries(keys.map((key) => [key, false])) as Record<keyof T & string, boolean>

  const draft = ref(createEmpty()) as Ref<T>
  const reveal = ref(emptyReveal())
  const loaded = ref(false)
  const loading = ref(false)

  const reset = () => {
    draft.value = createEmpty()
    reveal.value = emptyReveal()
    loaded.value = false
  }

  const fillFrom = (values: unknown) => {
    if (!values || typeof values !== 'object') return
    const record = values as Record<string, unknown>
    const next = { ...draft.value }
    for (const key of keys) {
      if (String(next[key] || '').trim()) continue
      const incoming = record[key]
      if (incoming != null && String(incoming).trim()) {
        next[key] = String(incoming) as T[typeof key]
      }
    }
    draft.value = next
    loaded.value = true
  }

  const toggle = async (key: keyof T & string) => {
    if (reveal.value[key]) {
      reveal.value = { ...reveal.value, [key]: false }
      return
    }
    if (!String(draft.value[key] || '').trim() && options.isSaved(key) && !loaded.value) {
      loading.value = true
      try {
        fillFrom(await options.fetchRevealed())
      } catch (error) {
        options.onError?.(error)
        return
      } finally {
        loading.value = false
      }
    }
    reveal.value = { ...reveal.value, [key]: true }
  }

  return { draft, reveal, loading, loaded, reset, fillFrom, toggle }
}

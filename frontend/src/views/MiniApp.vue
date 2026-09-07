<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  Bell,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  Play,
  RefreshCw,
  ShieldAlert,
  TriangleAlert,
  Zap,
} from 'lucide-vue-next'
import {
  authenticateMiniApp,
  getMiniAppAlerts,
  getMiniAppBootstrap,
  getMiniAppTaskRunStatus,
  runMiniAppTask,
  type MiniAppActiveRun,
  type MiniAppAlertsResponse,
  type MiniAppBootstrap,
  type MiniAppRunStatus,
  type MiniAppTask,
  type MiniAppUser,
} from '../lib/api'
import { formatShortDateTime } from '../lib/datetime'
import { resolveApiErrorMessage } from '../lib/notify'
import { getErrorCode } from '../lib/types'
import {
  miniAppHaptic,
  prepareTelegramWebApp,
  type TelegramWebApp,
} from '../lib/telegram-webapp'
import { useConfirm } from '../composables/useConfirm'
import { useI18n } from '../composables/useI18n'
import { useToast } from '../composables/useToast'
import PageRetry from '../components/PageRetry.vue'

type MiniTab = 'status' | 'tasks' | 'alerts'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const { confirm } = useConfirm()
const toast = useToast()

const tabFromQuery = (value: unknown): MiniTab => {
  const tab = Array.isArray(value) ? value[0] : value
  return tab === 'tasks' || tab === 'alerts' ? tab : 'status'
}

const tabs = [
  { id: 'status' as const, icon: Activity, labelKey: 'miniApp.tabStatus' },
  { id: 'tasks' as const, icon: Zap, labelKey: 'miniApp.tabTasks' },
  { id: 'alerts' as const, icon: Bell, labelKey: 'miniApp.tabAlerts' },
]

const activeTab = ref<MiniTab>(tabFromQuery(route.query.tab))
const pageLoading = ref(true)
const refreshing = ref(false)
const alertsLoading = ref(false)
const errorMessage = ref('')
const initialErrorCode = ref('')
const alertsError = ref('')
const launchRequired = ref(false)
const miniToken = ref('')
const user = ref<MiniAppUser | null>(null)
const bootstrap = ref<MiniAppBootstrap | null>(null)
const alertFeed = ref<MiniAppAlertsResponse | null>(null)
const selectedAccounts = ref<Record<string, string>>({})
const runningTask = ref('')
const runStates = ref<Record<string, MiniAppRunStatus>>({})
const runErrors = ref<Record<string, string>>({})
const telegramDark = ref(false)
const rootEl = ref<HTMLElement | null>(null)

let webApp: TelegramWebApp | null = null
let disposed = false
const pollTimers = new Map<string, ReturnType<typeof setTimeout>>()
const POLL_INTERVAL_MS = 1800
const PRIMARY_UNAVAILABLE_RETRY_MS = 3000
const POLL_RETRY_MAX_MS = 15_000
const INITIAL_LOAD_ATTEMPTS = 3
const TRANSIENT_POLL_ERROR_CODES = new Set([
  'MINI_APP_PRIMARY_UNAVAILABLE',
  'NETWORK_TIMEOUT',
  'NETWORK_ERROR',
  'NETWORK_ABORTED',
  'ORIGIN_HTML_ERROR',
])
const REOPEN_ERROR_CODES = new Set([
  'MINI_APP_INIT_DATA_INVALID',
  'MINI_APP_NOT_AUTHENTICATED',
  'MINI_APP_SESSION_INVALID',
  'MINI_APP_SESSION_REVOKED',
])

const initialErrorHint = computed(() => {
  if (REOPEN_ERROR_CODES.has(initialErrorCode.value)) return t('miniApp.reopenHint')
  if (initialErrorCode.value === 'MINI_APP_OPERATOR_NOT_ALLOWED') {
    return t('miniApp.permissionHint')
  }
  if (initialErrorCode.value === 'MINI_APP_DISABLED') return t('miniApp.settingsHint')
  return t('common.retryHint')
})

const isActiveState = (state?: string | null) => state === 'running'
const taskKey = (taskName: string, accountName: string) => `${taskName}\u0000${accountName}`

const makeRequestId = () => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

const taskAccounts = (task: MiniAppTask) => {
  const values = Array.isArray(task.account_names) ? [...task.account_names] : []
  if (task.account_name && task.account_name !== '*') values.push(task.account_name)
  return [...new Set(values.map((item) => String(item || '').trim()).filter(Boolean))]
}

const taskIdentity = (task: MiniAppTask) =>
  JSON.stringify([task.name, ...taskAccounts(task).slice().sort()])

const initializeSelections = (payload: MiniAppBootstrap) => {
  const next = { ...selectedAccounts.value }
  for (const task of payload.tasks || []) {
    const identity = taskIdentity(task)
    const accounts = taskAccounts(task)
    if (task.active_run?.account_name && accounts.includes(task.active_run.account_name)) {
      next[identity] = task.active_run.account_name
    } else if (!accounts.includes(next[identity] || '')) {
      next[identity] = accounts.length === 1 ? accounts[0] : ''
    }
  }
  selectedAccounts.value = next
}

const currentRun = (task: MiniAppTask): MiniAppRunStatus | MiniAppActiveRun | null => {
  const account = selectedAccounts.value[taskIdentity(task)] || task.active_run?.account_name || ''
  const activeRun = bootstrap.value?.active_runs.find(
    (run) => run.task_name === task.name && run.account_name === account,
  )
  if (activeRun) return activeRun
  const accountRun = runStates.value[taskKey(task.name, account)]
  if (accountRun) return accountRun
  return task.active_run?.account_name === account ? task.active_run : null
}

const recentAlerts = computed(() =>
  alertFeed.value?.items || bootstrap.value?.recent_alerts || [],
)

const userDisplayName = computed(() => {
  const value = [user.value?.first_name, user.value?.last_name].filter(Boolean).join(' ').trim()
  return value || (user.value?.username ? `@${user.value.username}` : t('miniApp.operator'))
})

const syncTelegramViewport = () => {
  telegramDark.value = webApp?.colorScheme === 'dark'
  if (rootEl.value && webApp?.viewportStableHeight) {
    rootEl.value.style.setProperty(
      '--mini-app-stable-height',
      `${webApp.viewportStableHeight}px`,
    )
  }
}

const setTab = async (tab: MiniTab) => {
  activeTab.value = tab
  const query = { ...route.query }
  if (tab === 'status') delete query.tab
  else query.tab = tab
  await router.replace({ query })
}

const handleTelegramBack = () => {
  void setTab('status')
}

const syncBackButton = () => {
  if (!webApp?.BackButton) return
  if (activeTab.value === 'status') webApp.BackButton.hide()
  else webApp.BackButton.show()
}

const runStatusLabel = (state?: string | null) => {
  if (!state) return '-'
  const key = `runStatus.state.${state}`
  const translated = t(key)
  return translated === key ? state : translated
}

const runStatusClass = (run: MiniAppRunStatus | MiniAppActiveRun | null) => {
  if (!run) return 'text-[var(--mini-muted)]'
  if (isActiveState(run.state)) return 'text-sky-600 dark:text-sky-300'
  if ('success' in run && run.success === true) return 'text-emerald-600 dark:text-emerald-300'
  if ('success' in run && run.success === false) return 'text-rose-600 dark:text-rose-300'
  return 'text-[var(--mini-muted)]'
}

const runSucceeded = (run: MiniAppRunStatus | MiniAppActiveRun | null) =>
  Boolean(run && 'success' in run && run.success === true)

const executionModeLabel = (mode?: string) => {
  if (mode === 'listen') return t('miniApp.modeListen')
  if (mode === 'range') return t('miniApp.modeRange')
  return t('miniApp.modeFixed')
}

const severityClass = (severity?: string | null) => {
  if (severity === 'critical') return 'mini-severity-critical'
  if (severity === 'info') return 'mini-severity-info'
  return 'mini-severity-warning'
}

const alertStatusLabel = (status?: string | null) => {
  if (status === 'sent') return t('alerts.statusSent')
  if (status === 'partial') return t('alerts.statusPartial')
  if (status === 'failed') return t('alerts.statusFailed')
  if (status === 'skipped') return t('alerts.statusSkipped')
  if (status === 'ignored') return t('alerts.statusIgnored')
  return status || '-'
}

const alertReasonLabel = (reason?: string | null) => {
  if (!reason) return ''
  const key = `alerts.reasons.${reason}`
  const translated = t(key)
  return translated === key ? reason : translated
}

const deliveryChannelLabel = (channel?: string) =>
  channel === 'email' ? t('alerts.channelEmail') : channel === 'telegram' ? t('alerts.channelTelegram') : channel || '-'

const applyBootstrap = (payload: MiniAppBootstrap) => {
  const normalized = {
    ...payload,
    tasks: Array.isArray(payload.tasks) ? payload.tasks : [],
    active_runs: Array.isArray(payload.active_runs) ? payload.active_runs : [],
    recent_alerts: Array.isArray(payload.recent_alerts) ? payload.recent_alerts : [],
    capabilities: Array.isArray(payload.capabilities) ? payload.capabilities : [],
  }
  bootstrap.value = normalized

  const activeKeys = new Set(
    normalized.active_runs
      .filter((run) => isActiveState(run.state))
      .map((run) => taskKey(run.task_name, run.account_name)),
  )
  for (const task of normalized.tasks) {
    const active = task.active_run
    if (active && isActiveState(active.state)) {
      activeKeys.add(taskKey(active.task_name, active.account_name))
    }
  }
  const nextStates = { ...runStates.value }
  const nextErrors = { ...runErrors.value }
  let reconciled = false
  for (const [key, run] of Object.entries(nextStates)) {
    if (isActiveState(run.state) && !activeKeys.has(key)) {
      delete nextStates[key]
      delete nextErrors[key]
      reconciled = true
    }
  }
  if (reconciled) {
    runStates.value = nextStates
    runErrors.value = nextErrors
  }
  initializeSelections(bootstrap.value)
}

const isTransientPollError = (error: unknown) => {
  const code = getErrorCode(error)
  if (code && TRANSIENT_POLL_ERROR_CODES.has(code)) return true
  if (!error || typeof error !== 'object') return false
  const status = Number((error as { status?: unknown }).status)
  return Number.isFinite(status) && status >= 500
}

const loadInitialData = async (initData: string) => {
  let auth: Awaited<ReturnType<typeof authenticateMiniApp>> | null = null
  let lastError: unknown = new Error('Mini App initialization failed')

  for (let attempt = 0; attempt < INITIAL_LOAD_ATTEMPTS; attempt += 1) {
    try {
      if (!auth) auth = await authenticateMiniApp(initData)
      const initialBootstrap = await getMiniAppBootstrap(auth.access_token)
      return { auth, bootstrap: initialBootstrap }
    } catch (error: unknown) {
      lastError = error
      if (!isTransientPollError(error) || attempt === INITIAL_LOAD_ATTEMPTS - 1) throw error
      await new Promise((resolve) => setTimeout(resolve, 600 * 2 ** attempt))
      if (disposed) throw error
    }
  }

  throw lastError
}

const refreshBootstrap = async (showFeedback = false) => {
  if (!miniToken.value || refreshing.value) return
  refreshing.value = true
  try {
    applyBootstrap(await getMiniAppBootstrap(miniToken.value))
    if (showFeedback) miniAppHaptic(webApp, 'success')
  } catch (error: unknown) {
    if (showFeedback) {
      miniAppHaptic(webApp, 'error')
      toast.error(resolveApiErrorMessage(error, 'miniApp.refreshFailed'))
    }
  } finally {
    refreshing.value = false
  }
}

const loadAlerts = async () => {
  if (!miniToken.value || alertsLoading.value) return
  alertsLoading.value = true
  alertsError.value = ''
  try {
    alertFeed.value = await getMiniAppAlerts(miniToken.value, 30)
  } catch (error: unknown) {
    alertsError.value = resolveApiErrorMessage(error, 'miniApp.alertsLoadFailed')
  } finally {
    alertsLoading.value = false
  }
}

const clearPoll = (key: string) => {
  const timer = pollTimers.get(key)
  if (timer) clearTimeout(timer)
  pollTimers.delete(key)
}

const schedulePoll = (
  taskName: string,
  accountName: string,
  runId: string,
  delayMs = POLL_INTERVAL_MS,
  transientFailures = 0,
) => {
  const key = taskKey(taskName, accountName)
  clearPoll(key)
  if (disposed || !miniToken.value || !runId) return
  pollTimers.set(
    key,
    setTimeout(async () => {
      pollTimers.delete(key)
      if (disposed) return
      if (document.hidden) {
        schedulePoll(taskName, accountName, runId, POLL_INTERVAL_MS, transientFailures)
        return
      }
      try {
        const status = await getMiniAppTaskRunStatus(
          miniToken.value,
          taskName,
          accountName,
          runId,
        )
        if (runErrors.value[key]) {
          const nextErrors = { ...runErrors.value }
          delete nextErrors[key]
          runErrors.value = nextErrors
        }
        runStates.value = { ...runStates.value, [key]: status }
        if (isActiveState(status.state)) {
          schedulePoll(taskName, accountName, runId)
        } else {
          if (status.success === true) miniAppHaptic(webApp, 'success')
          else if (status.success === false) miniAppHaptic(webApp, 'error')
          await refreshBootstrap()
        }
      } catch (error: unknown) {
        runErrors.value = {
          ...runErrors.value,
          [key]: resolveApiErrorMessage(error, 'miniApp.runStatusFailed'),
        }
        if (isTransientPollError(error)) {
          const failures = transientFailures + 1
          const retryDelay = Math.min(
            PRIMARY_UNAVAILABLE_RETRY_MS * 2 ** Math.min(failures - 1, 3),
            POLL_RETRY_MAX_MS,
          )
          schedulePoll(
            taskName,
            accountName,
            runId,
            retryDelay,
            failures,
          )
        }
      }
    }, delayMs),
  )
}

const startTask = async (task: MiniAppTask) => {
  const account = selectedAccounts.value[taskIdentity(task)] || ''
  const key = taskKey(task.name, account)
  if (!task.enabled || !account || runningTask.value || isActiveState(currentRun(task)?.state)) return
  const approved = await confirm({
    title: t('miniApp.runConfirmTitle'),
    message: t('miniApp.runConfirmMessage', { task: task.name, account }),
    confirmText: t('miniApp.runTask'),
  })
  if (!approved || disposed) return

  runningTask.value = key
  const nextErrors = { ...runErrors.value }
  delete nextErrors[key]
  runErrors.value = nextErrors
  try {
    const status = await runMiniAppTask(
      miniToken.value,
      task.name,
      account,
      makeRequestId(),
    )
    runStates.value = { ...runStates.value, [key]: status }
    if (isActiveState(status.state)) {
      miniAppHaptic(webApp, 'success')
      toast.success(t('miniApp.runStarted'))
      schedulePoll(task.name, account, status.run_id)
    } else {
      if (status.success === true) {
        miniAppHaptic(webApp, 'success')
        toast.success(t('miniApp.runStarted'))
      } else if (status.success === false) {
        miniAppHaptic(webApp, 'error')
      }
      await refreshBootstrap()
    }
  } catch (error: unknown) {
    const message = resolveApiErrorMessage(error, 'miniApp.runFailed')
    runErrors.value = { ...runErrors.value, [key]: message }
    miniAppHaptic(webApp, 'error')
    toast.error(message)
  } finally {
    runningTask.value = ''
  }
}

const initialize = async () => {
  pageLoading.value = true
  errorMessage.value = ''
  initialErrorCode.value = ''
  launchRequired.value = false
  try {
    webApp = await prepareTelegramWebApp()
    if (disposed) return
    if (!webApp?.initData) {
      launchRequired.value = true
      return
    }
    syncTelegramViewport()
    webApp.onEvent?.('themeChanged', syncTelegramViewport)
    webApp.onEvent?.('viewportChanged', syncTelegramViewport)
    webApp.BackButton?.onClick(handleTelegramBack)
    syncBackButton()

    const initial = await loadInitialData(webApp.initData)
    if (disposed) return
    miniToken.value = initial.auth.access_token
    user.value = initial.auth.user
    applyBootstrap(initial.bootstrap)
    if (activeTab.value === 'alerts') await loadAlerts()
  } catch (error: unknown) {
    initialErrorCode.value = getErrorCode(error) || ''
    errorMessage.value = resolveApiErrorMessage(error, 'miniApp.loadFailed')
    miniAppHaptic(webApp, 'error')
  } finally {
    if (!disposed) pageLoading.value = false
  }
}

const handleVisibility = () => {
  if (!document.hidden && miniToken.value) void refreshBootstrap()
}

watch(
  () => route.query.tab,
  (value) => {
    activeTab.value = tabFromQuery(value)
  },
)

watch(activeTab, (tab) => {
  syncBackButton()
  if (tab === 'alerts' && !alertFeed.value) void loadAlerts()
})

onMounted(() => {
  document.body.classList.add('telegram-mini-app-active')
  document.addEventListener('visibilitychange', handleVisibility)
  void initialize()
})

onUnmounted(() => {
  disposed = true
  document.body.classList.remove('telegram-mini-app-active')
  document.removeEventListener('visibilitychange', handleVisibility)
  webApp?.offEvent?.('themeChanged', syncTelegramViewport)
  webApp?.offEvent?.('viewportChanged', syncTelegramViewport)
  webApp?.BackButton?.offClick(handleTelegramBack)
  webApp?.BackButton?.hide()
  for (const key of pollTimers.keys()) clearPoll(key)
})
</script>

<template>
  <div
    ref="rootEl"
    class="mini-app-root"
    :class="{ 'mini-app-dark': telegramDark }"
    :aria-busy="pageLoading"
  >
    <header class="mini-header">
      <div class="min-w-0">
        <div class="flex items-center gap-2">
          <span class="mini-brand-mark" aria-hidden="true">TG</span>
          <h1 class="truncate text-base font-semibold">SignPulse</h1>
        </div>
        <p v-if="user" class="mt-1 truncate text-xs text-[var(--mini-muted)]">
          {{ userDisplayName }}
        </p>
      </div>
      <button
        v-if="miniToken"
        type="button"
        class="mini-icon-button"
        :aria-label="t('miniApp.refresh')"
        :disabled="refreshing"
        @click="refreshBootstrap(true)"
      >
        <RefreshCw class="w-5 h-5" :class="{ 'animate-spin': refreshing }" aria-hidden="true" />
      </button>
    </header>

    <main class="mini-content">
      <div v-if="pageLoading" class="space-y-3" role="status" aria-live="polite">
        <div class="mini-skeleton h-20" />
        <div class="grid grid-cols-2 gap-3">
          <div v-for="item in 4" :key="item" class="mini-skeleton h-24" />
        </div>
        <span class="sr-only">{{ t('common.loading') }}</span>
      </div>

      <section v-else-if="launchRequired" class="mini-state" role="alert">
        <ShieldAlert class="w-8 h-8 text-amber-500" aria-hidden="true" />
        <h2>{{ t('miniApp.launchRequired') }}</h2>
        <p>{{ t('miniApp.launchRequiredHint') }}</p>
      </section>

      <PageRetry
        v-else-if="errorMessage"
        :message="errorMessage"
        :hint="initialErrorHint"
        :loading="pageLoading"
        @retry="initialize"
      />

      <template v-else-if="bootstrap">
        <section v-if="activeTab === 'status'" class="space-y-4" data-testid="mini-tab-status">
          <div class="mini-status-line">
            <span
              class="mini-status-dot"
              :class="bootstrap.system.status === 'ready' ? 'bg-emerald-500' : 'bg-amber-500'"
              aria-hidden="true"
            />
            <span class="font-medium">
              {{ bootstrap.system.status === 'ready' ? t('miniApp.systemReady') : t('miniApp.systemUnavailable') }}
            </span>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <article class="mini-stat">
              <span>{{ t('miniApp.accounts') }}</span>
              <strong>{{ bootstrap.system.accounts_connected }}/{{ bootstrap.system.accounts_total }}</strong>
            </article>
            <article class="mini-stat">
              <span>{{ t('miniApp.enabledTasks') }}</span>
              <strong>{{ bootstrap.system.tasks_enabled }}/{{ bootstrap.system.tasks_total }}</strong>
            </article>
            <article class="mini-stat col-span-2">
              <span>{{ t('miniApp.activeRuns') }}</span>
              <strong>{{ bootstrap.system.active_runs_total }}</strong>
            </article>
          </div>

          <section class="mini-section">
            <div class="mini-section-heading">
              <Activity class="w-4 h-4" aria-hidden="true" />
              <h2>{{ t('miniApp.activeRuns') }}</h2>
            </div>
            <div v-if="!bootstrap.active_runs.length" class="mini-empty">
              {{ t('miniApp.noActiveRuns') }}
            </div>
            <ul v-else class="divide-y divide-[var(--mini-border)]">
              <li v-for="run in bootstrap.active_runs" :key="run.run_id" class="py-3 first:pt-0 last:pb-0">
                <div class="flex items-center justify-between gap-3">
                  <span class="min-w-0 truncate text-sm font-medium">{{ run.task_name }}</span>
                  <span class="shrink-0 text-xs text-sky-600 dark:text-sky-300">{{ runStatusLabel(run.state) }}</span>
                </div>
                <div class="mt-1 flex items-center justify-between gap-3 text-xs text-[var(--mini-muted)]">
                  <span class="truncate">{{ run.account_name }}</span>
                  <span class="truncate">{{ run.phase_detail || run.phase || '-' }}</span>
                </div>
              </li>
            </ul>
          </section>
        </section>

        <section v-else-if="activeTab === 'tasks'" class="space-y-3" data-testid="mini-tab-tasks">
          <div v-if="!bootstrap.tasks.length" class="mini-state">
            <Zap class="w-7 h-7 text-[var(--mini-muted)]" aria-hidden="true" />
            <h2>{{ t('miniApp.noTasks') }}</h2>
          </div>
          <article v-for="(task, taskIndex) in bootstrap.tasks" v-else :key="taskIdentity(task)" class="mini-task">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0">
                <h2 class="truncate text-sm font-semibold">{{ task.name }}</h2>
                <div class="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--mini-muted)]">
                  <span>{{ executionModeLabel(task.execution_mode) }}</span>
                  <span v-if="task.sign_at"><Clock3 class="inline w-3 h-3 mr-1" aria-hidden="true" />{{ task.sign_at }}</span>
                </div>
              </div>
              <span class="mini-badge" :class="task.enabled ? 'mini-badge-on' : 'mini-badge-off'">
                {{ task.enabled ? t('miniApp.enabled') : t('miniApp.disabled') }}
              </span>
            </div>

            <label class="mt-4 block text-xs font-medium" :for="`mini-account-${taskIndex}`">
              {{ t('miniApp.selectAccount') }}
            </label>
            <select
              :id="`mini-account-${taskIndex}`"
              v-model="selectedAccounts[taskIdentity(task)]"
              class="mini-select mt-1.5"
              :disabled="!task.enabled || Boolean(runningTask)"
            >
              <option value="" disabled>{{ t('miniApp.selectAccountPlaceholder') }}</option>
              <option v-for="account in taskAccounts(task)" :key="account" :value="account">
                {{ account }}
              </option>
            </select>

            <div v-if="task.last_run" class="mt-3 text-xs text-[var(--mini-muted)]">
              {{ t('miniApp.lastRun') }} · {{ formatShortDateTime(task.last_run.time) }} ·
              <span :class="task.last_run.success ? 'text-emerald-600 dark:text-emerald-300' : 'text-rose-600 dark:text-rose-300'">
                {{ task.last_run.success ? t('miniApp.success') : t('miniApp.failed') }}
              </span>
            </div>

            <div
              v-if="currentRun(task)"
              class="mt-3 flex items-center gap-2 text-xs"
              :class="runStatusClass(currentRun(task))"
              aria-live="polite"
            >
              <LoaderCircle v-if="isActiveState(currentRun(task)?.state)" class="w-3.5 h-3.5 animate-spin" aria-hidden="true" />
              <CheckCircle2 v-else-if="runSucceeded(currentRun(task))" class="w-3.5 h-3.5" aria-hidden="true" />
              <TriangleAlert v-else class="w-3.5 h-3.5" aria-hidden="true" />
              <span>{{ runStatusLabel(currentRun(task)?.state) }}</span>
              <span v-if="currentRun(task)?.phase_detail" class="truncate">· {{ currentRun(task)?.phase_detail }}</span>
            </div>
            <p
              v-if="runErrors[taskKey(task.name, selectedAccounts[taskIdentity(task)] || '')]"
              class="mt-3 text-xs text-rose-600 dark:text-rose-300"
              role="alert"
            >
              {{ runErrors[taskKey(task.name, selectedAccounts[taskIdentity(task)] || '')] }}
            </p>

            <button
              type="button"
              class="mini-primary-button mt-4 w-full"
              :disabled="
                !task.enabled ||
                !selectedAccounts[taskIdentity(task)] ||
                Boolean(runningTask) ||
                isActiveState(currentRun(task)?.state)
              "
              @click="startTask(task)"
            >
              <LoaderCircle
                v-if="runningTask === taskKey(task.name, selectedAccounts[taskIdentity(task)] || '')"
                class="w-5 h-5 animate-spin"
                aria-hidden="true"
              />
              <Play v-else class="w-5 h-5" aria-hidden="true" />
              {{ isActiveState(currentRun(task)?.state) ? t('miniApp.running') : t('miniApp.runTask') }}
            </button>
          </article>
        </section>

        <section v-else class="space-y-3" data-testid="mini-tab-alerts">
          <div class="flex justify-end">
            <button
              type="button"
              class="mini-secondary-button"
              :disabled="alertsLoading"
              @click="loadAlerts"
            >
              <RefreshCw class="w-4 h-4" :class="{ 'animate-spin': alertsLoading }" aria-hidden="true" />
              {{ t('miniApp.refresh') }}
            </button>
          </div>
          <PageRetry
            v-if="alertsError"
            :message="alertsError"
            :loading="alertsLoading"
            @retry="loadAlerts"
          />
          <div v-else-if="alertsLoading && !recentAlerts.length" class="mini-skeleton h-28" />
          <div v-else-if="!recentAlerts.length" class="mini-state">
            <Bell class="w-7 h-7 text-[var(--mini-muted)]" aria-hidden="true" />
            <h2>{{ t('miniApp.noAlerts') }}</h2>
          </div>
          <article v-for="(alert, index) in recentAlerts" v-else :key="`${alert.at}-${alert.rule_id}-${index}`" class="mini-alert">
            <div class="flex items-start gap-3">
              <span class="mini-severity" :class="severityClass(alert.severity)" aria-hidden="true" />
              <div class="min-w-0 flex-1">
                <div class="flex items-start justify-between gap-3">
                  <h2 class="min-w-0 break-words text-sm font-semibold">{{ alert.title || alert.rule_id || '-' }}</h2>
                  <time class="shrink-0 text-[11px] text-[var(--mini-muted)]">{{ formatShortDateTime(alert.at) }}</time>
                </div>
                <p v-if="alert.detail" class="mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed text-[var(--mini-muted)]">
                  {{ alert.detail }}
                </p>
                <div class="mt-2 text-xs" :class="alert.status === 'failed' ? 'text-rose-600 dark:text-rose-300' : 'text-[var(--mini-muted)]'">
                  {{ alertStatusLabel(alert.status) }}
                  <span v-if="alert.reason"> · {{ alertReasonLabel(alert.reason) }}</span>
                  <span v-if="alert.error"> · {{ alert.error }}</span>
                </div>
                <ul v-if="alert.deliveries?.length" class="mt-2 flex flex-wrap gap-2">
                  <li v-for="delivery in alert.deliveries" :key="`${delivery.channel}-${delivery.status}`" class="mini-delivery">
                    {{ deliveryChannelLabel(delivery.channel) }} · {{ alertStatusLabel(delivery.status) }}
                  </li>
                </ul>
              </div>
            </div>
          </article>
        </section>
      </template>
    </main>

    <nav v-if="miniToken && bootstrap" class="mini-tabbar" :aria-label="t('miniApp.navigation')">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        type="button"
        class="mini-tab-button"
        :class="{ 'mini-tab-active': activeTab === tab.id }"
        :aria-current="activeTab === tab.id ? 'page' : undefined"
        @click="setTab(tab.id)"
      >
        <component :is="tab.icon" class="w-5 h-5" aria-hidden="true" />
        <span>{{ t(tab.labelKey) }}</span>
      </button>
    </nav>
  </div>
</template>

<style scoped>
.mini-app-root {
  --mini-bg: var(--tg-theme-bg-color, var(--sp-bg));
  --mini-surface: var(--tg-theme-secondary-bg-color, var(--sp-bg-elevated));
  --mini-text: var(--tg-theme-text-color, var(--sp-text));
  --mini-muted: var(--tg-theme-hint-color, var(--sp-text-muted));
  --mini-link: var(--tg-theme-link-color, var(--sp-accent));
  --mini-button: var(--tg-theme-button-color, #111827);
  --mini-button-text: var(--tg-theme-button-text-color, #ffffff);
  --mini-border: color-mix(in srgb, var(--mini-muted) 22%, transparent);
  min-height: var(--mini-app-stable-height, var(--tg-viewport-stable-height, 100dvh));
  background: var(--mini-bg);
  color: var(--mini-text);
  overscroll-behavior: contain;
}
.mini-app-dark {
  color-scheme: dark;
}
.mini-header {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  min-height: calc(3.75rem + env(safe-area-inset-top, 0px));
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding: calc(0.5rem + env(safe-area-inset-top, 0px)) 1rem 0.5rem;
  border-bottom: 1px solid var(--mini-border);
  background: color-mix(in srgb, var(--mini-bg) 94%, transparent);
  backdrop-filter: blur(12px);
}
.mini-brand-mark {
  display: inline-flex;
  width: 2rem;
  height: 2rem;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--mini-border);
  background: var(--mini-button);
  color: var(--mini-button-text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.6875rem;
  font-weight: 700;
}
.mini-content {
  width: min(100%, 44rem);
  margin: 0 auto;
  padding: 1rem 1rem calc(5.75rem + env(safe-area-inset-bottom, 0px));
}
.mini-icon-button,
.mini-primary-button,
.mini-secondary-button,
.mini-tab-button,
.mini-select {
  min-height: 3rem;
  touch-action: manipulation;
}
.mini-icon-button {
  display: inline-flex;
  width: 3rem;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--mini-border);
  color: var(--mini-text);
}
.mini-icon-button:disabled,
.mini-primary-button:disabled,
.mini-secondary-button:disabled,
.mini-select:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}
.mini-status-line,
.mini-section,
.mini-stat,
.mini-task,
.mini-alert,
.mini-state {
  border: 1px solid var(--mini-border);
  background: var(--mini-surface);
}
.mini-status-line {
  display: flex;
  min-height: 3rem;
  align-items: center;
  gap: 0.625rem;
  padding: 0.75rem 1rem;
  font-size: 0.875rem;
}
.mini-status-dot {
  width: 0.5rem;
  height: 0.5rem;
  flex: none;
  border-radius: 999px;
}
.mini-stat {
  display: flex;
  min-height: 6rem;
  flex-direction: column;
  justify-content: space-between;
  padding: 1rem;
}
.mini-stat span {
  color: var(--mini-muted);
  font-size: 0.75rem;
}
.mini-stat strong {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 1.5rem;
  font-weight: 600;
}
.mini-section,
.mini-task,
.mini-alert {
  padding: 1rem;
}
.mini-section-heading {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 1rem;
  font-size: 0.875rem;
  font-weight: 600;
}
.mini-empty {
  padding: 1.5rem 0;
  text-align: center;
  color: var(--mini-muted);
  font-size: 0.8125rem;
}
.mini-state {
  display: flex;
  min-height: 12rem;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  padding: 1.5rem;
  text-align: center;
}
.mini-state h2 {
  font-size: 0.9375rem;
  font-weight: 600;
}
.mini-state p {
  max-width: 22rem;
  color: var(--mini-muted);
  font-size: 0.8125rem;
  line-height: 1.5;
}
.mini-badge,
.mini-delivery {
  display: inline-flex;
  align-items: center;
  min-height: 1.5rem;
  border: 1px solid var(--mini-border);
  padding: 0.125rem 0.5rem;
  font-size: 0.6875rem;
}
.mini-badge-on {
  border-color: rgb(16 185 129 / 0.35);
  color: #059669;
}
.mini-badge-off {
  color: var(--mini-muted);
}
.mini-select {
  width: 100%;
  border: 1px solid var(--mini-border);
  background: var(--mini-bg);
  padding: 0 0.75rem;
  color: var(--mini-text);
  font-size: 0.875rem;
}
.mini-primary-button,
.mini-secondary-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  border: 1px solid transparent;
  padding: 0.625rem 1rem;
  font-size: 0.875rem;
  font-weight: 600;
}
.mini-primary-button {
  background: var(--mini-button);
  color: var(--mini-button-text);
}
.mini-secondary-button {
  border-color: var(--mini-border);
  background: var(--mini-surface);
  color: var(--mini-text);
}
.mini-severity {
  width: 0.25rem;
  min-height: 2.5rem;
  align-self: stretch;
  flex: none;
}
.mini-severity-critical {
  background: #f43f5e;
}
.mini-severity-warning {
  background: #f59e0b;
}
.mini-severity-info {
  background: #0ea5e9;
}
.mini-delivery {
  color: var(--mini-muted);
}
.mini-tabbar {
  position: fixed;
  right: 0;
  bottom: 0;
  left: 0;
  z-index: 30;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.5rem;
  padding: 0.375rem 0.75rem max(0.375rem, env(safe-area-inset-bottom, 0px));
  border-top: 1px solid var(--mini-border);
  background: color-mix(in srgb, var(--mini-bg) 96%, transparent);
  backdrop-filter: blur(12px);
}
.mini-tab-button {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.1875rem;
  color: var(--mini-muted);
  font-size: 0.6875rem;
}
.mini-tab-active {
  color: var(--mini-link);
  font-weight: 600;
}
.mini-skeleton {
  border: 1px solid var(--mini-border);
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--mini-muted) 7%, transparent) 25%,
    color-mix(in srgb, var(--mini-muted) 15%, transparent) 50%,
    color-mix(in srgb, var(--mini-muted) 7%, transparent) 75%
  );
  background-size: 200% 100%;
  animation: mini-shimmer 1.3s ease-in-out infinite;
}
@keyframes mini-shimmer {
  from { background-position: 200% 0; }
  to { background-position: -200% 0; }
}
@media (min-width: 48rem) {
  .mini-content {
    padding-right: 1.5rem;
    padding-left: 1.5rem;
  }
  .mini-tabbar {
    right: 50%;
    left: auto;
    width: min(100%, 44rem);
    transform: translateX(50%);
    border-right: 1px solid var(--mini-border);
    border-left: 1px solid var(--mini-border);
  }
}
@media (prefers-reduced-motion: reduce) {
  .mini-skeleton {
    animation: none;
  }
}
:global(body.telegram-mini-app-active > .fixed.bottom-6.right-6) {
  right: 1rem;
  bottom: calc(5.25rem + env(safe-area-inset-bottom, 0px));
  left: 1rem;
  width: auto;
  max-width: none;
}
</style>

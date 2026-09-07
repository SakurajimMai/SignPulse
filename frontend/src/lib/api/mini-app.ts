import { request } from './core'

const MINI_APP_ALERTS_MAX_LIMIT = 40

export interface MiniAppUser {
  id: number
  first_name: string
  last_name?: string | null
  username?: string | null
  language_code?: string | null
  photo_url?: string | null
}

export interface MiniAppAuthResponse {
  access_token: string
  token_type: 'bearer' | string
  expires_in: number
  user: MiniAppUser
}

export interface MiniAppActiveRun {
  run_id: string
  state: string
  phase?: string | null
  phase_detail?: string | null
  account_name: string
  task_name: string
  started_at?: string | null
  wait_seconds?: number | null
}

export interface MiniAppTask {
  name: string
  account_name: string
  account_names: string[]
  enabled: boolean
  sign_at: string
  execution_mode: string
  last_run?: {
    time: string
    success: boolean
    message?: string | null
  } | null
  active_run?: MiniAppActiveRun | null
}

export interface MiniAppAlertDelivery {
  channel: 'email' | 'telegram' | string
  status: 'sent' | 'failed' | 'skipped' | string
  reason?: string | null
  error?: string | null
}

export interface MiniAppAlert {
  at?: string | null
  rule_id?: string | null
  title?: string | null
  detail?: string | null
  status?: string | null
  reason?: string | null
  error?: string | null
  severity?: 'critical' | 'warning' | 'info' | string
  channel?: string | null
  deliveries?: MiniAppAlertDelivery[]
}

export interface MiniAppBootstrap {
  system: {
    status: string
    accounts_total: number
    accounts_connected: number
    tasks_total: number
    tasks_enabled: number
    active_runs_total: number
  }
  tasks: MiniAppTask[]
  active_runs: MiniAppActiveRun[]
  recent_alerts: MiniAppAlert[]
  capabilities: string[]
}

export interface MiniAppAlertRuleSummary {
  id: string
  group: string
  title: string
  enabled: boolean
}

export interface MiniAppAlertsResponse {
  items: MiniAppAlert[]
  total: number
  rules: MiniAppAlertRuleSummary[]
}

export interface MiniAppRunStatus {
  request_id?: string | null
  run_id: string
  state: string
  success?: boolean | null
  error?: string | null
  output?: string | null
  started_at?: string | null
  finished_at?: string | null
  phase?: string | null
  phase_detail?: string | null
  wait_seconds?: number | null
  account_name: string
  task_name: string
  failure_category?: string | null
  timeout_seconds?: number | null
  retry_count_effective?: number | null
}

export const authenticateMiniApp = (initData: string) =>
  request<MiniAppAuthResponse>('/mini-app/auth', {
    method: 'POST',
    body: JSON.stringify({ init_data: initData }),
  })

export const getMiniAppBootstrap = (token: string) =>
  request<MiniAppBootstrap>('/mini-app/bootstrap', {}, token)

export const runMiniAppTask = (
  token: string,
  taskName: string,
  accountName: string,
  requestId: string,
) =>
  request<MiniAppRunStatus>(
    `/mini-app/tasks/${encodeURIComponent(taskName)}/run`,
    {
      method: 'POST',
      body: JSON.stringify({ account_name: accountName, request_id: requestId }),
    },
    token,
  )

export const getMiniAppTaskRunStatus = (
  token: string,
  taskName: string,
  accountName: string,
  runId: string,
) => {
  const params = new URLSearchParams({ account_name: accountName, run_id: runId })
  return request<MiniAppRunStatus>(
    `/mini-app/tasks/${encodeURIComponent(taskName)}/run/status?${params.toString()}`,
    {},
    token,
  )
}

export const getMiniAppAlerts = (token: string, limit = 20) =>
  request<MiniAppAlertsResponse>(
    `/mini-app/alerts?limit=${encodeURIComponent(String(Math.max(1, Math.min(MINI_APP_ALERTS_MAX_LIMIT, limit))))}`,
    {},
    token,
  )

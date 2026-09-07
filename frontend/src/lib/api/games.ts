import {
  DEFAULT_TIMEOUT_MS,
  LONG_TIMEOUT_MS,
  MEDIUM_TIMEOUT_MS,
  fetchWithAuth,
  request,
  requestBlob,
} from './core'

export interface GamesSettings {
  wp_url: string
  wp_user: string
  wp_app_password?: string | null
  wp_app_password_set?: boolean
  wp_default_categories: string
  wp_default_tags: string
  wp_pay_enabled: boolean
  wp_pay_modo?: string
  wp_pay_price: number
  wp_points_price?: number
  wp_vip1_price?: number | null
  wp_vip2_price?: number | null
  wp_vip1_points?: number | null
  wp_vip2_points?: number | null
  wp_pay_extra_template?: string
  wp_apate_url?: string
  wp_status: string
  telegram_account_name?: string
  telegram_source_channels?: string
  auto_publish_enabled?: boolean
  telegram_poll_seconds?: number
  telegram_backfill_limit?: number
  auto_retry_limit?: number
  extract_passwords?: string
  extract_passwords_set?: boolean
  pack_password?: string
  pack_password_set?: boolean
  split_volume_mb?: number
  ad_keywords: string
  apate_enabled: boolean
  apate_bin: string
  baidu_enabled?: boolean
  baidu_cookie?: string | null
  baidu_cookie_set?: boolean
  baidu_remote_dir?: string
  pikpak_username?: string
  pikpak_password?: string | null
  pikpak_password_set?: boolean
  pikpak_refresh_token?: string | null
  pikpak_refresh_token_set?: boolean
  pikpak_folder_id?: string
  pikpak_remote_dir?: string
  terabox_cookie?: string | null
  terabox_cookie_set?: boolean
  terabox_remote_dir?: string
  quark_cookie?: string | null
  quark_cookie_set?: boolean
  quark_folder_id?: string
  quark_remote_dir?: string
  openlist_url?: string
  openlist_token?: string | null
  openlist_token_set?: boolean
  openlist_baidu_path?: string
  openlist_pikpak_path?: string
  openlist_terabox_path?: string
  openlist_quark_path?: string
  cleanup_after_publish?: boolean
  ai_enabled?: boolean
  ai_refine_prompt?: string
  ai_refine_prompt_default?: string
  ai_skip_non_games?: boolean
  ai_model_configured?: boolean
}

export interface GamesCloudStatus {
  configured?: boolean
  via?: string
}

export interface GamesKeepaliveTarget {
  ok?: boolean
  skipped?: boolean
  refreshed?: boolean
  message?: string
  checked_at?: string | null
}

export interface GamesKeepaliveStatus {
  interval_hours?: number
  last_run_at?: string | null
  results?: Record<string, GamesKeepaliveTarget>
}

export interface GamesJob {
  id?: string
  running?: boolean
  stage?: string
  current?: number
  total?: number
  message?: string
  error?: string
  title?: string
  summary?: string
  tags?: string[]
  category_ids?: number[]
  price?: number
  points_price?: number
  vip1_price?: number | null
  vip2_price?: number | null
  vip1_points?: number | null
  vip2_points?: number | null
  pay_modo?: string
  pay_enabled?: boolean
  apate?: boolean
  source_url?: string
  images?: Array<{ path: string; name?: string; size?: number }>
  archives?: Array<{ path: string; name?: string; size?: number }>
  archive_members?: Array<{ path: string; name?: string; size?: number }>
  links?: Record<string, string>
  share_pwd?: string
  cloud_errors?: Record<string, string>
  public_url?: string | null
  wp_id?: number | null
  cover_url?: string | null
  password?: string
  created_at?: string
  updated_at?: string
}

export interface GamesRuntimeStatus extends GamesJob {
  configured?: boolean
  wp?: { ok?: boolean; configured?: boolean; error?: string; user?: string }
  sevenzip?: { ok?: boolean; bin?: string | null }
  apate_tool?: { ok?: boolean; bin?: string; error?: string }
  clouds?: {
    baidu?: GamesCloudStatus
    pikpak?: GamesCloudStatus
    terabox?: GamesCloudStatus
    quark?: GamesCloudStatus
    openlist?: GamesCloudStatus
  }
  telegram_account?: string
  keepalive?: GamesKeepaliveStatus
  automation?: GamesAutomationStatus
}

export interface GamesAutomationRequirements {
  ready: boolean
  missing: string[]
  message?: string
  account?: string | null
  telegram_account?: boolean
  channels?: boolean
  wordpress?: boolean
  cloud?: boolean
  sevenzip?: boolean
  apate?: boolean
}

export interface GamesAutomationItem {
  source_key?: string
  url?: string
  status?: string
  attempts?: number
  job_id?: string | null
  error?: string
  caption?: string
  public_url?: string | null
  updated_at?: string
}

export interface GamesAutomationStatus {
  enabled: boolean
  listening: boolean
  worker_status: string
  channels: string[]
  poll_seconds: number
  backfill_limit: number
  retry_limit: number
  concurrency?: number
  processing_mode?: string
  queued: number
  failed: number
  processed: number
  skipped?: number
  active_source?: string | null
  last_scan_at?: string | null
  last_success_at?: string | null
  last_error?: string | null
  requirements: GamesAutomationRequirements
  disk?: {
    free_bytes: number
    total_bytes: number
    free_gb: number
  }
  recent?: GamesAutomationItem[]
}

export interface GamesCatalogEntry {
  id?: number | string | null
  title: string
  summary?: string
  cover_url?: string | null
  public_url?: string | null
  tags?: string[]
  categories?: number[]
  price?: number | null
  points_price?: number | null
  pay_modo?: string
  links?: Record<string, string>
  source_url?: string
  job_id?: string
  apate?: boolean
  updated_at?: string | null
  created_at?: string | null
}

export interface GamesCatalogListResponse {
  data: GamesCatalogEntry[]
  page: number
  limit: number
  total: number
  total_pages: number
}

export interface GamesCategory {
  id: number
  name: string
  slug?: string
}

export const getGamesSettings = (token: string, reveal = false) =>
  request<GamesSettings>(`/games/settings${reveal ? '?reveal=1' : ''}`, {}, token)

export const saveGamesSettings = (
  token: string,
  settings: Partial<GamesSettings> & Record<string, unknown>,
) =>
  request<{ success: boolean; message: string; settings: GamesSettings }>(
    '/games/settings',
    { method: 'PATCH', body: JSON.stringify(settings) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const getGamesStatus = (token: string, probe = true) =>
  request<GamesRuntimeStatus>(
    `/games/status?probe=${probe ? '1' : '0'}`,
    {},
    token,
    probe ? MEDIUM_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
  )

export const refreshGamesClouds = (token: string) =>
  request<{ success: boolean; message: string; keepalive: GamesKeepaliveStatus }>(
    '/games/clouds/refresh',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const scanGamesAutomation = (token: string) =>
  request<GamesAutomationStatus>(
    '/games/automation/scan',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const retryGamesAutomation = (token: string) =>
  request<GamesAutomationStatus>(
    '/games/automation/retry',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const listGamesCategories = (token: string) =>
  request<{ data: GamesCategory[] }>('/games/categories', {}, token, MEDIUM_TIMEOUT_MS)

export const pullGamesTelegram = (
  token: string,
  payload: { url: string; account?: string },
) =>
  request<GamesJob>(
    '/games/pull',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const publishGamesJob = (
  token: string,
  payload: {
    job_id: string
    title?: string
    summary?: string
    tags?: string[] | string
    categories?: number[] | string
    price?: number
    points_price?: number
    vip1_price?: number | null
    vip2_price?: number | null
    vip1_points?: number | null
    vip2_points?: number | null
    pay_modo?: string
    pay_enabled?: boolean
    apate?: boolean
    extract_password?: string
    pack_password?: string
    status?: string
    links?: Record<string, string>
    clouds?: string[]
  },
) =>
  request<GamesJob>(
    '/games/publish',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const cancelGamesJob = (token: string, jobId: string) =>
  request<GamesJob>(`/games/jobs/${jobId}/cancel`, { method: 'POST' }, token, MEDIUM_TIMEOUT_MS)

export const listGamesJobs = (token: string, limit = 24) =>
  request<{ data: GamesJob[] }>(`/games/jobs?limit=${limit}`, {}, token)

export const getGamesJob = (token: string, jobId: string) =>
  request<GamesJob>(`/games/jobs/${jobId}`, {}, token)

export const listGamesCatalog = (token: string, page = 1, limit = 12, query = '') => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) })
  if (query.trim()) params.set('q', query.trim())
  return request<GamesCatalogListResponse>(`/games/catalog?${params.toString()}`, {}, token)
}

export const deleteGamesCatalog = (token: string, key: string) =>
  request<{ success: boolean }>(`/games/catalog/${encodeURIComponent(key)}`, { method: 'DELETE' }, token)

export const getGamesFile = (token: string, path: string) =>
  requestBlob(`/games/file?path=${encodeURIComponent(path)}`, {}, token, LONG_TIMEOUT_MS)

export const uploadGamesSource = async (token: string, file: File) => {
  const body = new FormData()
  body.append('file', file)
  const res = await fetchWithAuth(
    '/games/upload',
    {},
    { method: 'POST', body },
    token,
    null,
  )
  return res.json() as Promise<GamesJob>
}

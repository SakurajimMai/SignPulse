import {
  DEFAULT_TIMEOUT_MS,
  LONG_TIMEOUT_MS,
  MEDIUM_TIMEOUT_MS,
  fetchWithAuth,
  request,
  requestBlob,
} from './core'

export interface CoserSettings {
  site_url: string
  site_email: string
  site_password?: string | null
  site_password_set?: boolean
  s3_endpoint?: string
  s3_access_key?: string | null
  s3_access_key_set?: boolean
  s3_secret_key?: string | null
  s3_secret_key_set?: boolean
  s3_bucket?: string
  s3_region?: string
  s3_public_url?: string
  s3_prefix?: string
  telegram_account_name?: string
  telegram_source_channels?: string
  extract_passwords?: string
  extract_passwords_set?: boolean
  pack_password?: string
  pack_password_set?: boolean
  split_volume_mb?: number
  ad_keywords: string
  apate_enabled: boolean
  baidu_remote_dir?: string
  pikpak_remote_dir?: string
  terabox_remote_dir?: string
  quark_remote_dir?: string
  cleanup_after_publish?: boolean
}

export interface CoserCloudStatus {
  configured?: boolean
  via?: string
}

export interface CoserJob {
  id?: string
  running?: boolean
  stage?: string
  current?: number
  total?: number
  message?: string
  error?: string
  title?: string
  summary?: string
  coser_id?: number | null
  coser_name?: string
  is_r18?: boolean
  apate?: boolean
  source_url?: string
  images?: Array<{ path: string; name?: string; size?: number }>
  archives?: Array<{ path: string; name?: string; size?: number }>
  links?: Record<string, string>
  share_pwd?: string
  cloud_errors?: Record<string, string>
  public_url?: string | null
  site_work_id?: number | null
  cover_url?: string | null
  image_urls?: string[]
  created_at?: string
  updated_at?: string
}

export interface CoserRuntimeStatus extends CoserJob {
  configured?: boolean
  site?: { ok?: boolean; configured?: boolean; error?: string; user?: string }
  s3?: { ok?: boolean; configured?: boolean; error?: string }
  sevenzip?: { ok?: boolean; bin?: string | null }
  apate_tool?: { ok?: boolean; bin?: string; error?: string }
  clouds?: {
    baidu?: CoserCloudStatus
    pikpak?: CoserCloudStatus
    terabox?: CoserCloudStatus
    quark?: CoserCloudStatus
  }
  telegram_account?: string
}

export interface CoserPerson {
  id: number
  name: string
  avatar?: string | null
  works_count?: number
}

export interface CoserCatalogEntry {
  id?: number | string | null
  title: string
  summary?: string
  cover_url?: string | null
  public_url?: string | null
  coser_id?: number | null
  coser_name?: string
  is_r18?: boolean
  links?: Record<string, string>
  source_url?: string
  job_id?: string
  apate?: boolean
  updated_at?: string | null
}

export const getCoserSettings = (token: string, reveal = false) =>
  request<CoserSettings>(
    `/coser/settings${reveal ? '?reveal=1' : ''}`,
    {},
    token,
  )

export const saveCoserSettings = (
  token: string,
  settings: Partial<CoserSettings> & Record<string, unknown>,
) =>
  request<{ success: boolean; message: string; settings: CoserSettings }>(
    '/coser/settings',
    { method: 'PATCH', body: JSON.stringify(settings) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const getCoserStatus = (token: string, probe = true) =>
  request<CoserRuntimeStatus>(
    `/coser/status?probe=${probe ? '1' : '0'}`,
    {},
    token,
    probe ? MEDIUM_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
  )

export const listCoserPeople = (token: string, query = '') => {
  const params = new URLSearchParams()
  if (query.trim()) params.set('q', query.trim())
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return request<{ data: CoserPerson[] }>(`/coser/cosers${suffix}`, {}, token, MEDIUM_TIMEOUT_MS)
}

export const createCoserPerson = (token: string, name: string) =>
  request<CoserPerson>(
    '/coser/cosers',
    { method: 'POST', body: JSON.stringify({ name }) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const pullCoserTelegram = (
  token: string,
  payload: { url: string; account?: string },
) =>
  request<CoserJob>(
    '/coser/pull',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const publishCoserJob = (
  token: string,
  payload: {
    job_id: string
    title?: string
    summary?: string
    coser_id?: number
    coser_name?: string
    is_r18?: boolean
    apate?: boolean
    extract_password?: string
    pack_password?: string
    links?: Record<string, string>
    clouds?: string[]
  },
) =>
  request<CoserJob>(
    '/coser/publish',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const cancelCoserJob = (token: string, jobId: string) =>
  request<CoserJob>(`/coser/jobs/${jobId}/cancel`, { method: 'POST' }, token, MEDIUM_TIMEOUT_MS)

export const listCoserJobs = (token: string, limit = 24) =>
  request<{ data: CoserJob[] }>(`/coser/jobs?limit=${limit}`, {}, token)

export const listCoserCatalog = (token: string, page = 1, limit = 12, query = '') => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) })
  if (query.trim()) params.set('q', query.trim())
  return request<{ data: CoserCatalogEntry[]; total: number; total_pages: number; page: number }>(
    `/coser/catalog?${params.toString()}`,
    {},
    token,
  )
}

export const getCoserFile = (token: string, path: string) =>
  requestBlob(`/coser/file?path=${encodeURIComponent(path)}`, {}, token, LONG_TIMEOUT_MS)

export const uploadCoserSource = async (token: string, file: File) => {
  const body = new FormData()
  body.append('file', file)
  const res = await fetchWithAuth(
    '/coser/upload',
    {},
    { method: 'POST', body },
    token,
    null,
  )
  return res.json() as Promise<CoserJob>
}

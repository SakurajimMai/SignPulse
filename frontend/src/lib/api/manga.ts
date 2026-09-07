import { DEFAULT_TIMEOUT_MS, LONG_TIMEOUT_MS, MEDIUM_TIMEOUT_MS, fetchWithAuth, request } from './core'

export interface HmwLiveChapter {
  number: number
  title?: string
  pages?: number
}

export interface HmwJobStatus {
  task_id?: string | null
  running?: boolean
  stage?: string
  current?: number
  total?: number
  message?: string
  error?: string
  source_path?: string
  slug?: string
  title?: string
  language?: string
  public_url?: string | null
  manga_id?: number | null
  author?: string
  cover_url?: string | null
  page_count?: number
  chapter_count?: number
  updated_at?: string | null
  configured?: boolean
  chapter_index?: number
  chapter_total?: number
  chapter_title?: string
  live_chapters?: HmwLiveChapter[]
}

export interface HmwRuntimeStatus extends HmwJobStatus {
  configured?: boolean
  api_ok?: boolean
  s3_ok?: boolean
}

export interface MangaRuntimeStatus {
  enabled: boolean
  worker_status: 'running' | 'starting' | 'stopped' | 'disabled' | string
  last_error?: string | null
  source_chats: number
  source_bindings?: number
  site_publish_enabled: boolean
  outbound_publish_enabled?: boolean
  outbound_bot_configured?: boolean
  outbound_forward_videos?: boolean
  imgbed_configured: boolean
  telegram_configured: boolean
  telegram_authorized?: boolean
  telegram_account_name?: string | null
  ehentai?: EhentaiRuntimeStatus
  hmw?: HmwRuntimeStatus
}

export interface MangaSourceBinding {
  channel: string
  discussion: string
  ingest_mode?: 'auto' | 'discussion' | 'telegraph' | string
  forward_videos?: boolean
  ingest_enabled?: boolean
}

export interface MangaSettings {
  enabled: boolean
  telegram_account_name?: string | null
  telegram_api_id?: number | null
  telegram_api_id_set?: boolean
  telegram_api_hash?: string | null
  telegram_api_hash_set?: boolean
  telegram_session_file?: string
  tg_source_chats: string
  source_bindings?: MangaSourceBinding[]
  tg_allowed_sender_ids: string
  discussion_only: boolean
  ignore_user_comments: boolean
  chapter_idle_seconds: number
  chapter_reply_idle_seconds?: number
  chapter_max_pages: number
  history_backfill_limit?: number
  accept_image_documents: boolean
  cfbed_upload_url: string
  cfbed_auth_code?: string | null
  cfbed_auth_code_set?: boolean
  cfbed_api_token?: string | null
  cfbed_api_token_set?: boolean
  cfbed_extra_query: string
  cfbed_public_base: string
  cfbed_file_field: string
  cfbed_retry_delay_seconds: number
  site_publish_url: string
  site_publish_secret?: string | null
  site_publish_secret_set?: boolean
  outbound_enabled?: boolean
  outbound_channel?: string
  outbound_preview_count?: number
  outbound_button_text?: string
  outbound_site_base?: string
  outbound_bot_token?: string | null
  outbound_bot_token_set?: boolean
  outbound_forward_videos?: boolean
  outbound_video_block_keywords?: string
  outbound_video_allow_keywords?: string
  outbound_video_min_seconds?: number
  outbound_video_max_seconds?: number
  ehentai_enabled?: boolean
  ehentai_cookie?: string | null
  ehentai_cookie_set?: boolean
  ehentai_exhentai?: boolean
  ehentai_search?: string
  ehentai_cats?: string
  ehentai_max_pages?: number
  ehentai_search_pages?: number
  ehentai_delay_seconds?: number
  ehentai_gallery_delay_seconds?: number
  ehentai_poll_seconds?: number
  ehentai_translation_url?: string
  ehentai_translation_auto?: boolean
  hmw_api_url?: string
  hmw_publisher_token?: string | null
  hmw_publisher_token_set?: boolean
  hmw_s3_endpoint?: string
  hmw_s3_region?: string
  hmw_s3_bucket?: string
  hmw_s3_access_key?: string | null
  hmw_s3_access_key_set?: boolean
  hmw_s3_secret_key?: string | null
  hmw_s3_secret_key_set?: boolean
  hmw_s3_public_url?: string
  hmw_s3_prefix?: string
  hmw_avif_quality?: number
  hmw_convert_workers?: number
  hmw_upload_workers?: number
  hmw_request_timeout?: number
  ai_enabled?: boolean
  ai_refine_prompt?: string
  ai_refine_prompt_default?: string
  ai_model_configured?: boolean
  database_url?: string | null
  data_dir?: string | null
  temp_dir?: string | null
}

export interface MangaSummary {
  id: number
  slug: string
  title: string
  author?: string | null
  tags: string[]
  description?: string | null
  cover_url?: string | null
  chapter_count: number
  page_count: number
  source_chat_title?: string | null
  updated_at?: string | null
}

export interface MangaListResponse {
  data: MangaSummary[]
  page: number
  limit: number
  total: number
  total_pages: number
}

export const getMangaRuntimeStatus = (token: string) =>
  request<MangaRuntimeStatus>('/manga/status', {}, token, MEDIUM_TIMEOUT_MS)

export const getMangaSettings = (token: string, reveal = false) =>
  request<MangaSettings>(`/manga/settings${reveal ? '?reveal=1' : ''}`, {}, token)

export const saveMangaSettings = (
  token: string,
  settings: Partial<MangaSettings> & Record<string, unknown>,
) =>
  request<{
    success: boolean
    message: string
    settings: MangaSettings
    status: MangaRuntimeStatus
  }>('/manga/settings', {
    method: 'PATCH',
    body: JSON.stringify(settings),
  }, token, MEDIUM_TIMEOUT_MS)

export const startMangaWorker = (token: string) =>
  request<{ success: boolean; message: string; status: MangaRuntimeStatus }>(
    '/manga/worker/start',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const stopMangaWorker = (token: string) =>
  request<{ success: boolean; message: string; status: MangaRuntimeStatus }>(
    '/manga/worker/stop',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export interface EhentaiJob {
  url: string
  title: string
  status: string
  pages?: number
  detail?: string
  at?: string
  manga_id?: number
  slug?: string
  cover_url?: string | null
  author?: string | null
  chapter_count?: number
  page_count?: number
  source_key?: string
}

export interface EhentaiRuntimeStatus {
  worker_status: 'running' | 'starting' | 'stopped' | 'disabled' | string
  last_error?: string | null
  cookie_configured?: boolean
  exhentai?: boolean
  search_count?: number
  current?: {
    title?: string
    url?: string
    pages?: number
    total?: number
  }
  processed?: number
  skipped?: number
  failed?: number
  recent?: EhentaiJob[]
  translations?: EhentaiTranslationStatus
  phase?: 'idle' | 'searching' | 'waiting' | string
  next_pass_at?: string | null
  poll_seconds?: number
  listening?: boolean
}

export interface EhentaiTranslationStatus {
  editor_url?: string
  source?: string
  repo?: string
  version?: string
  sha?: string
  tag_count?: number
  namespaces?: number
  updated_at?: string
  using_bundled?: boolean
  path?: string
}

export const getEhentaiStatus = (token: string) =>
  request<EhentaiRuntimeStatus>('/manga/ehentai/status', {}, token, MEDIUM_TIMEOUT_MS)

export const startEhentaiWorker = (token: string) =>
  request<{ success: boolean; message: string; status: EhentaiRuntimeStatus }>(
    '/manga/ehentai/start',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const stopEhentaiWorker = (token: string) =>
  request<{ success: boolean; message: string; status: EhentaiRuntimeStatus }>(
    '/manga/ehentai/stop',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const runEhentaiPass = (token: string) =>
  request<{ success: boolean; message: string; status: EhentaiRuntimeStatus }>(
    '/manga/ehentai/run-once',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const refreshEhentaiTranslations = (token: string) =>
  request<{
    success: boolean
    message: string
    status: EhentaiRuntimeStatus
    translations: EhentaiTranslationStatus
  }>(
    '/manga/ehentai/translations/refresh',
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const listMangaCatalog = (token: string, page = 1, limit = 12, query = '', source = '') => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) })
  if (query.trim()) params.set('q', query.trim())
  if (source.trim()) params.set('source', source.trim())
  return request<MangaListResponse>(`/manga/catalog?${params.toString()}`, {}, token)
}

export interface HmwSourceItem {
  name: string
  path: string
  kind: 'archive' | 'directory' | string
  location?: string
  size?: number | null
}

export interface HmwChapterMap {
  directory: string
  name?: string
  number: number
  title: string
  pages?: number
  action: 'upsert' | 'skip' | string
}

export interface HmwScanResult {
  root: string
  cover: string
  chapters: HmwChapterMap[]
  pages: number
}

export interface HmwMangaMeta {
  language: string
  title: string
  slug: string
  author?: string
  status?: string
  description?: string
  genre_names?: string[]
  genre_ids?: number[]
}

export interface HmwTelegramDoc {
  message_id: number
  file_name: string
  size?: number | null
  mime_type?: string | null
  date?: string | null
  caption?: string
}

export interface HmwPreflightPlan {
  manga_id?: number | null
  manga_action?: string
  field_changes?: Record<string, unknown>
  chapters?: Array<{ number: number; title: string; action: string }>
  expected_version?: string
}

export const getHmwStatus = (token: string, probe = true) =>
  request<HmwRuntimeStatus>(
    `/manga/hmw/status?probe=${probe ? '1' : '0'}`,
    {},
    token,
    probe ? MEDIUM_TIMEOUT_MS : DEFAULT_TIMEOUT_MS,
  )

export interface HmwChapter {
  id?: number
  number: number
  title?: string
  page_count?: number
  pages?: number
}

export interface HmwLibraryManga {
  id: number
  title?: string
  slug?: string
  author?: string
  status?: string
  language?: string
  description?: string
  alternative_title?: string
  cover_url?: string | null
  chapter_count?: number
  page_count?: number
  version?: string
  year?: number | null
  source_url?: string
  public_url?: string | null
  chapters?: HmwChapter[]
}

export interface HmwCatalogEntry {
  id: number
  slug: string
  title: string
  author?: string
  language?: string
  status?: string
  cover_url?: string | null
  description?: string
  alternative_title?: string
  chapter_count: number
  page_count: number
  public_url?: string | null
  updated_at?: string | null
  last_published_at?: string | null
  source_path?: string
  version?: string
  chapters?: HmwChapter[]
}

export interface HmwCatalogListResponse {
  data: HmwCatalogEntry[]
  page: number
  limit: number
  total: number
  total_pages: number
}

export const listHmwLibrary = (token: string, query = '', language = 'zh', page = 1) => {
  const params = new URLSearchParams({
    query,
    language,
    page: String(page),
    page_size: '20',
  })
  return request<{ items?: HmwLibraryManga[]; total?: number }>(
    `/manga/hmw/library?${params.toString()}`,
    {},
    token,
    MEDIUM_TIMEOUT_MS,
  )
}

export const getHmwLibraryManga = (token: string, mangaId: number) =>
  request<HmwLibraryManga>(`/manga/hmw/library/${mangaId}`, {}, token, MEDIUM_TIMEOUT_MS)

export const listHmwCatalog = (token: string, page = 1, limit = 12, query = '') => {
  const params = new URLSearchParams({ page: String(page), limit: String(limit) })
  if (query.trim()) params.set('q', query.trim())
  return request<HmwCatalogListResponse>(`/manga/hmw/catalog?${params.toString()}`, {}, token)
}

export const getHmwCatalogManga = (token: string, mangaId: number) =>
  request<{ manga: HmwLibraryManga; catalog: HmwCatalogEntry; offline?: boolean; error?: string }>(
    `/manga/hmw/catalog/${mangaId}`,
    {},
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const syncHmwCatalog = (
  token: string,
  payload: { query?: string; language?: string; limit?: number } = {},
) =>
  request<{ imported: number; total?: number; data: HmwCatalogEntry[] }>(
    '/manga/hmw/catalog/sync',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    LONG_TIMEOUT_MS,
  )

export const updateHmwManga = (
  token: string,
  mangaId: number,
  payload: {
    title?: string
    slug?: string
    author?: string
    status?: string
    description?: string
    alternative_title?: string
    expected_version?: string | null
  },
) =>
  request<{ manga: HmwLibraryManga; catalog: HmwCatalogEntry }>(
    `/manga/hmw/library/${mangaId}`,
    { method: 'PUT', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const deleteHmwManga = (token: string, mangaId: number, expectedVersion?: string) => {
  const params = new URLSearchParams()
  if (expectedVersion) params.set('expected_version', expectedVersion)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return request<{ result: Record<string, unknown> }>(
    `/manga/hmw/library/${mangaId}${suffix}`,
    { method: 'DELETE' },
    token,
    MEDIUM_TIMEOUT_MS,
  )
}

export const updateHmwChapter = (
  token: string,
  chapterId: number,
  payload: { number?: number; title?: string; expected_version?: string; manga_id?: number },
) =>
  request<{ manga: HmwLibraryManga; catalog: HmwCatalogEntry | null }>(
    `/manga/hmw/chapters/${chapterId}`,
    { method: 'PUT', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const deleteHmwChapter = (
  token: string,
  chapterId: number,
  payload: { expected_version?: string; manga_id?: number },
) => {
  const params = new URLSearchParams()
  if (payload.expected_version) params.set('expected_version', payload.expected_version)
  if (payload.manga_id != null) params.set('manga_id', String(payload.manga_id))
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return request<{ manga: HmwLibraryManga; catalog: HmwCatalogEntry | null }>(
    `/manga/hmw/chapters/${chapterId}${suffix}`,
    { method: 'DELETE' },
    token,
    MEDIUM_TIMEOUT_MS,
  )
}

export const listHmwSources = (token: string) =>
  request<{ data: HmwSourceItem[] }>('/manga/hmw/sources', {}, token)

export const unzipHmwSource = (token: string, name: string, password?: string) =>
  request<HmwSourceItem>(
    '/manga/hmw/sources/unzip',
    { method: 'POST', body: JSON.stringify({ name, password: password || null }) },
    token,
    LONG_TIMEOUT_MS,
  )

export const uploadHmwSource = async (token: string, file: File) => {
  const body = new FormData()
  body.append('file', file)
  const res = await fetchWithAuth(
    '/manga/hmw/sources/upload',
    {},
    { method: 'POST', body },
    token,
    LONG_TIMEOUT_MS,
  )
  return res.json() as Promise<HmwSourceItem>
}

export const listHmwTelegramDocs = (token: string, chat: string, account = '', limit = 50) => {
  const params = new URLSearchParams({ chat, limit: String(limit) })
  if (account.trim()) params.set('account', account.trim())
  return request<{ data: HmwTelegramDoc[] }>(
    `/manga/hmw/telegram/docs?${params.toString()}`,
    {},
    token,
    MEDIUM_TIMEOUT_MS,
  )
}

export const downloadHmwTelegramDoc = (
  token: string,
  payload: { chat: string; message_id: number; account?: string },
) =>
  request<HmwSourceItem>(
    '/manga/hmw/telegram/download',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    LONG_TIMEOUT_MS,
  )

export interface HmwAlbumResult extends HmwSourceItem {
  chapter_path?: string
  title?: string
  chapter_title?: string
  author?: string
  pages?: number
  comment_id?: number | null
  post_id?: number | null
}

export const pullHmwTelegramAlbum = (
  token: string,
  payload: { url: string; account?: string; title?: string; manga_title?: string },
) =>
  request<HmwAlbumResult>(
    '/manga/hmw/telegram/album',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    LONG_TIMEOUT_MS,
  )

export const scanHmwSource = (token: string, path: string) =>
  request<HmwScanResult>(
    '/manga/hmw/scan',
    { method: 'POST', body: JSON.stringify({ path }) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const preflightHmw = (
  token: string,
  payload: { source_path: string; manga: HmwMangaMeta; chapters: HmwChapterMap[]; cover_path?: string | null },
) =>
  request<{ scan: HmwScanResult; preflight: HmwPreflightPlan; manga: HmwMangaMeta; genre_ids: number[] }>(
    '/manga/hmw/preflight',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const startHmwJob = (
  token: string,
  payload: {
    source_path: string
    manga: HmwMangaMeta
    chapters: HmwChapterMap[]
    cover_path?: string | null
    task_id?: string | null
  },
) =>
  request<{ success: boolean; message: string; job: HmwJobStatus }>(
    '/manga/hmw/jobs',
    { method: 'POST', body: JSON.stringify(payload) },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const listHmwJobs = (token: string) =>
  request<{ current: HmwJobStatus; data: HmwJobStatus[] }>('/manga/hmw/jobs', {}, token)

export const cancelHmwJob = (token: string, taskId: string) =>
  request<{ success: boolean; job: HmwJobStatus }>(
    `/manga/hmw/jobs/${encodeURIComponent(taskId)}/cancel`,
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

export const resumeHmwJob = (token: string, taskId: string) =>
  request<{ success: boolean; message: string; job: HmwJobStatus }>(
    `/manga/hmw/jobs/${encodeURIComponent(taskId)}/resume`,
    { method: 'POST' },
    token,
    MEDIUM_TIMEOUT_MS,
  )

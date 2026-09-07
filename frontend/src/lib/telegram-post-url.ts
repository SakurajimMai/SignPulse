const HOSTS = new Set(['t.me', 'telegram.me', 'telegram.dog', 'www.t.me'])
const POST_PATH = /^\/(?:c\/\d+|[A-Za-z0-9_]+)\/\d+\/?$/i

/** 与后端 parse_telegram_post_url 对齐：必须带频道和帖子编号，允许 ?single。 */
export function isTelegramPostUrl(raw: string): boolean {
  const text = String(raw || '').trim()
  if (!text) return false
  try {
    const href = text.includes('://') ? text : `https://${text.replace(/^\/+/, '')}`
    const url = new URL(href)
    if (!HOSTS.has((url.hostname || '').toLowerCase())) return false
    return POST_PATH.test(url.pathname)
  } catch {
    return false
  }
}

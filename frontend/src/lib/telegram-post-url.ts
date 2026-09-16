const HOSTS = new Set(['t.me', 'telegram.me', 'telegram.dog', 'www.t.me'])
const POST_PATH = /^\/(?:c\/\d+|[A-Za-z0-9_]+)\/\d+\/?$/i
const USERNAME = /^[A-Za-z][A-Za-z0-9_]{3,31}$/

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

/** 公开频道用户名：支持 @name、name、t.me/name、带帖子编号的链接。私有 t.me/c/ 返回空串。 */
export function parseTelegramChannelRef(raw: string): string {
  const text = String(raw || '').trim()
  if (!text) return ''
  const lowered = text.toLowerCase()
  let name = text
  const marker = lowered.includes('t.me/') ? 't.me/' : lowered.includes('telegram.me/') ? 'telegram.me/' : ''
  if (marker) {
    const path = text.slice(lowered.indexOf(marker) + marker.length).replace(/^\/+/, '').split(/[?#]/, 1)[0]
    const parts = path.split('/').filter(Boolean)
    if (parts[0]?.toLowerCase() === 'c' && /^\d+$/.test(parts[1] || '')) return ''
    name = parts[0] || ''
  }
  name = name.replace(/^@+/, '').trim()
  return USERNAME.test(name) ? name : ''
}

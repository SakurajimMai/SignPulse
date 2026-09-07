import { describe, expect, it } from 'vitest'
import { isTelegramPostUrl } from '../lib/telegram-post-url'

describe('isTelegramPostUrl', () => {
  it('接受带帖子编号和 ?single 的频道链接', () => {
    expect(isTelegramPostUrl('https://t.me/Zhzbzx/19706?single')).toBe(true)
    expect(isTelegramPostUrl('https://t.me/Zhzbzx/19706')).toBe(true)
    expect(isTelegramPostUrl('t.me/Zhzbzx/19706?single')).toBe(true)
    expect(isTelegramPostUrl('https://t.me/c/2285859531/88')).toBe(true)
  })

  it('拒绝缺少帖子编号的前缀或无关链接', () => {
    expect(isTelegramPostUrl('')).toBe(false)
    expect(isTelegramPostUrl('https://t.me/Zhzbzx/')).toBe(false)
    expect(isTelegramPostUrl('https://t.me/Zhzbzx')).toBe(false)
    expect(isTelegramPostUrl('https://example.com/Zhzbzx/19706')).toBe(false)
  })
})

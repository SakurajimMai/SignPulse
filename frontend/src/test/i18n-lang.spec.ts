import { describe, expect, it, vi, beforeEach } from 'vitest'

/**
 * i18n 与 <html lang> 同步回归：首帧初始化与运行时切换语言后，
 * documentElement.lang 必须与界面语言一致（屏读器/SEO 依赖）。
 */
describe('i18n html lang 同步', () => {
  beforeEach(() => {
    vi.resetModules()
    localStorage.clear()
  })

  it('无语言偏好（默认中文）时设置 zh-CN', async () => {
    document.documentElement.lang = ''
    await import('../i18n')
    expect(document.documentElement.lang).toBe('zh-CN')
  })

  it('localStorage 存 en 时设置 en-US', async () => {
    localStorage.setItem('tg-signer-locale', 'en')
    document.documentElement.lang = ''
    await import('../i18n')
    expect(document.documentElement.lang).toBe('en-US')
  })

  it('localStorage 存 zh 时保持 zh-CN（非法值回落默认）', async () => {
    localStorage.setItem('tg-signer-locale', 'zh')
    document.documentElement.lang = 'en-US'
    await import('../i18n')
    expect(document.documentElement.lang).toBe('zh-CN')
  })

  it('验证码倒计时文案使用单层占位符', async () => {
    const { default: i18n } = await import('../i18n')
    expect(i18n.global.t('addAccount.resendIn', { s: 8 })).toBe('重新获取 (8s)')
  })

  it('漫画页占位符里的 @ 不能触发 linked format 解析失败', async () => {
    const { default: i18n } = await import('../i18n')
    expect(i18n.global.t('manga.channelPlaceholder')).toBe('@channel 或 -100…')
    expect(i18n.global.t('manga.discussionPlaceholder')).toBe('@group 或 -100…，Telegraph 频道可留空')
    expect(i18n.global.t('manga.ehentaiTranslationUrlPlaceholder')).toContain('DatabaseReleases@master/db.text.json')
    expect(i18n.global.t('manga.hmwTelegramChatPlaceholder')).toContain('@channel')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('manga.channelPlaceholder')).toBe('@channel or -100…')
    expect(i18n.global.t('manga.discussionPlaceholder')).toBe('@group or -100…; optional for Telegraph')
    expect(i18n.global.t('manga.ehentaiTranslationUrlPlaceholder')).toContain('DatabaseReleases@master/db.text.json')
    expect(i18n.global.t('manga.hmwTelegramChatPlaceholder')).toContain('@channel')
  })
})

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import router from '../router'

const viewsDir = resolve(dirname(fileURLToPath(import.meta.url)), '../views')

describe('漫画采集二级栏目路由', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('E-Hentai 是独立路径 /manga/ehentai，不是 /manga 锚点', () => {
    const eh = router.getRoutes().find((route) => route.name === 'manga-ehentai')
    const manga = router.getRoutes().find((route) => route.name === 'manga')
    expect(eh?.path).toBe('/manga/ehentai')
    expect(manga?.path).toBe('/manga')
    expect(eh?.path).not.toBe(manga?.path)
  })

  it('HMW 发布是独立路径 /manga/hmw', () => {
    const hmw = router.getRoutes().find((route) => route.name === 'manga-hmw')
    expect(hmw?.path).toBe('/manga/hmw')
  })

  it('告警规则是独立路径 /alerts', () => {
    const alerts = router.getRoutes().find((route) => route.name === 'alerts')
    expect(alerts?.path).toBe('/alerts')
  })
})

describe('漫画采集页布局', () => {
  it('三个采集页根容器铺满主栏并水平居中，不再左贴 max-w-7xl', () => {
    for (const file of ['Manga.vue', 'MangaEhentai.vue', 'MangaHmw.vue']) {
      const src = readFileSync(resolve(viewsDir, file), 'utf8')
      const match = src.match(/<template>\s*<div class="([^"]+)"/)
      expect(match?.[1], file).toContain('mx-auto')
      expect(match?.[1], file).toContain('w-full')
      expect(match?.[1], file).not.toContain('max-w-7xl')
    }
  })
})

describe('游戏发布栏目路由', () => {
  it('游戏发布是独立路径 /games', () => {
    const games = router.getRoutes().find((route) => route.name === 'games')
    expect(games?.path).toBe('/games')
  })
})

describe('Coser 发布栏目路由', () => {
  it('Coser 发布是独立路径 /coser', () => {
    const coser = router.getRoutes().find((route) => route.name === 'coser')
    expect(coser?.path).toBe('/coser')
  })

  it('中英文都有 Coser 导航', async () => {
    const { default: i18n } = await import('../i18n')
    i18n.global.locale.value = 'zh-CN'
    expect(i18n.global.t('nav.coser')).toBe('Coser 发布')
    expect(i18n.global.t('coser.pageHint')).toContain('S3')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('nav.coser')).toBe('Coser publish')
    i18n.global.locale.value = 'zh-CN'
  })
})

describe('HMW 页面文案', () => {
  it('中英文都有 HMW 导航和 inbox 提示', async () => {
    const { default: i18n } = await import('../i18n')
    expect(i18n.global.t('nav.mangaHmw')).toBe('HMW 发布')
    expect(i18n.global.t('manga.hmwInboxHint')).toContain('inbox')
    expect(i18n.global.t('manga.hmwCatalogTitle')).toContain('目录')
    expect(i18n.global.t('manga.hmwSaveChapter')).toBe('保存章节')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('nav.mangaHmw')).toBe('HMW publish')
    expect(i18n.global.t('manga.hmwCatalogTitle')).toBe('HMW catalog')
  })
})

describe('游戏发布页面文案', () => {
  it('中英文都有游戏导航和拉取提示', async () => {
    const { default: i18n } = await import('../i18n')
    i18n.global.locale.value = 'zh-CN'
    expect(i18n.global.t('nav.games')).toBe('游戏发布')
    expect(i18n.global.t('games.telegramPlaceholder')).toContain('t.me/Zhzbzx/19706?single')
    expect(i18n.global.t('games.telegramUrlInvalid')).toContain('帖子编号')
    expect(i18n.global.t('games.catalogTitle')).toContain('目录')
    i18n.global.locale.value = 'en-US'
    expect(i18n.global.t('nav.games')).toBe('Game publish')
    expect(i18n.global.t('games.catalogTitle')).toBe('Published catalog')
    expect(i18n.global.t('games.telegramPlaceholder')).toContain('?single')
    i18n.global.locale.value = 'zh-CN'
  })
})

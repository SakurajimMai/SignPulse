import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import GamesPanel from '../components/games/GamesPanel.vue'
import type { GamesSettings } from '../lib/api'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    listGamesCatalog: vi.fn().mockResolvedValue({ data: [], total: 0, total_pages: 0, page: 1, limit: 12 }),
    listGamesJobs: vi.fn().mockResolvedValue({ data: [] }),
    listGamesCategories: vi.fn().mockResolvedValue({ data: [] }),
    getGamesFile: vi.fn(),
    publishGamesJob: vi.fn().mockResolvedValue({ id: 'job-1' }),
  }
})

const settings = (): GamesSettings =>
  ({
    wp_url: 'https://www.ixacg.top',
    wp_user: 'sakura',
    wp_default_categories: '640,637',
    wp_default_tags: 'SLG',
    wp_pay_enabled: true,
    wp_pay_modo: '0',
    wp_pay_price: 5,
    wp_points_price: 20,
    wp_status: 'publish',
    telegram_account_name: 'sakuramaix',
    extract_passwords: 'sakuramai,ixacg.top',
    pack_password: 'sakuramai',
    ad_keywords: '',
    apate_enabled: true,
    apate_bin: 'apate',
  }) as GamesSettings

describe('游戏发布拉取栏', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('帖链接独立成栏，账号用下拉，解压/打包密码明文', async () => {
    const wrapper = mount(GamesPanel, {
      props: { settings: settings(), status: null },
      global: { plugins: [i18n] },
    })
    await wrapper.vm.$nextTick()

    const url = wrapper.get('#games-telegram-url')
    expect((url.element as HTMLInputElement).value).toBe('')
    expect(url.attributes('placeholder')).toContain('t.me/Zhzbzx/19706?single')
    expect(wrapper.find('#games-telegram-account').element.tagName).toBe('SELECT')

    const pull = wrapper.findAll('button').find((item) => item.text().includes('拉取帖子'))
    expect(pull).toBeTruthy()
    expect(pull!.attributes('disabled')).toBeDefined()

    await url.setValue('https://t.me/Zhzbzx/')
    expect(wrapper.text()).toContain('请粘贴完整帖链接')
    expect(pull!.attributes('disabled')).toBeDefined()

    await url.setValue('https://t.me/Zhzbzx/19706?single')
    expect(wrapper.text()).not.toContain('请粘贴完整帖链接')
    expect(pull!.attributes('disabled')).toBeUndefined()

    const extract = wrapper.findAll('input').find((item) => (item.element as HTMLInputElement).placeholder?.includes('ixacg.top'))
    const pack = wrapper.findAll('input').find((item) => (item.element as HTMLInputElement).value === 'sakuramai')
    expect(extract?.attributes('type')).toBe('text')
    expect(pack?.attributes('type')).toBe('text')

    wrapper.unmount()
  })

  it('发布表单可勾选要上传的网盘', async () => {
    const wrapper = mount(GamesPanel, {
      props: { settings: settings(), status: null },
      global: { plugins: [i18n] },
    })
    await wrapper.vm.$nextTick()

    expect(wrapper.text()).toContain('黄金会员价格')
    expect(wrapper.text()).toContain('钻石会员积分')
    expect(wrapper.text()).toContain('上传到网盘')
    const baidu = wrapper.get('[data-cloud="baidu"]').element as HTMLInputElement
    const quark = wrapper.get('[data-cloud="quark"]').element as HTMLInputElement
    expect(baidu.checked).toBe(true)
    expect(quark.checked).toBe(true)

    await wrapper.get('[data-cloud="quark"]').setValue(false)
    expect((wrapper.get('[data-cloud="quark"]').element as HTMLInputElement).checked).toBe(false)
    expect((wrapper.get('[data-cloud="baidu"]').element as HTMLInputElement).checked).toBe(true)

    wrapper.unmount()
  })

  it('任务状态展示百度失败和已出链', async () => {
    const wrapper = mount(GamesPanel, {
      props: {
        settings: settings(),
        status: {
          id: 'job-1',
          title: '巨乳幻想3',
          running: false,
          public_url: 'https://www.ixacg.top/16413.html',
          cloud_errors: { baidu: 'All connection attempts failed' },
          links: {
            pikpak: 'https://mypikpak.com/s/x',
            terabox: 'https://1024terabox.com/s/x',
          },
        },
      },
      global: { plugins: [i18n] },
    })
    await wrapper.vm.$nextTick()
    const error = wrapper.get('[data-testid="cloud-error"]')
    expect(error.text()).toContain('百度网盘')
    expect(error.text()).toContain('All connection attempts failed')
    expect(wrapper.get('[data-testid="cloud-links"]').text()).toContain('PikPak')
    wrapper.unmount()
  })
})

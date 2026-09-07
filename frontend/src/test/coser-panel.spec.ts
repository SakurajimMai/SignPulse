import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import CoserPanel from '../components/coser/CoserPanel.vue'
import type { CoserSettings } from '../lib/api'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  return {
    ...actual,
    listCoserCatalog: vi.fn().mockResolvedValue({ data: [], total: 0, total_pages: 0, page: 1 }),
    listCoserJobs: vi.fn().mockResolvedValue({ data: [] }),
    listCoserPeople: vi.fn().mockResolvedValue({ data: [{ id: 1, name: 'Mika' }] }),
    getCoserFile: vi.fn(),
    publishCoserJob: vi.fn().mockResolvedValue({ id: 'job-1' }),
  }
})

const settings = (): CoserSettings =>
  ({
    site_url: 'https://icoser.de',
    site_email: 'admin@icoser.de',
    telegram_account_name: 'sakuramaix',
    extract_passwords: 'sakuramai',
    pack_password: 'sakuramai',
    ad_keywords: '',
    apate_enabled: true,
    baidu_remote_dir: '/网站/icoser.de/coser',
    pikpak_remote_dir: '/site/icoser.de/coser',
    terabox_remote_dir: '/website/icoser.de/coser',
    quark_remote_dir: '/网站/icoser.de/coser',
  }) as CoserSettings

describe('Coser 发布拉取栏', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('帖链接独立成栏，可选择 Coser', async () => {
    const wrapper = mount(CoserPanel, {
      props: { settings: settings(), status: null },
      global: { plugins: [i18n] },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('#coser-telegram-url').exists()).toBe(true)
    expect(wrapper.text()).toContain('Coser')
    wrapper.unmount()
  })
})

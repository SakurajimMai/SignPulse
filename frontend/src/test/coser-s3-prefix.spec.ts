import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import i18n from '../i18n'
import Coser from '../views/Coser.vue'
import { getCoserSettings } from '../lib/api'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../lib/api')>('../lib/api')
  const base = {
    site_url: 'https://icoser.de',
    site_email: 'admin@icoser.de',
    s3_public_url: 'https://cdn1.hxsl.org',
    s3_prefix: 'coser',
    extract_passwords: 'sakuramai',
    pack_password: 'sakuramai',
    ad_keywords: '',
    apate_enabled: true,
    baidu_remote_dir: '/网站/icoser.de/coser',
    pikpak_remote_dir: '/site/icoser.de/coser',
    terabox_remote_dir: '/website/icoser.de/coser',
    quark_remote_dir: '/网站/icoser.de/coser',
    site_password_set: true,
    s3_access_key_set: true,
    s3_secret_key_set: true,
  }
  return {
    ...actual,
    getCoserSettings: vi.fn().mockImplementation((_token: string, reveal = false) =>
      Promise.resolve(
        reveal
          ? { ...base, site_password: 'AdminPass1', s3_access_key: 'AKIAEXAMPLE', s3_secret_key: 's3-secret-one' }
          : { ...base, site_password: null, s3_access_key: null, s3_secret_key: null },
      ),
    ),
    getCoserStatus: vi.fn().mockResolvedValue({ running: false, message: '' }),
    listCoserCatalog: vi.fn().mockResolvedValue({ data: [], total: 0, total_pages: 0, page: 1 }),
    listCoserPeople: vi.fn().mockResolvedValue({ data: [] }),
  }
})

vi.mock('../lib/api/core', () => ({
  getAuthToken: () => 'token',
}))

vi.mock('../stores/accounts', () => ({
  useAccountsStore: () => ({
    accounts: [],
    ensureAccounts: vi.fn().mockResolvedValue(undefined),
  }),
}))

describe('Coser S3 路径模板', () => {
  beforeEach(() => {
    i18n.global.locale.value = 'zh-CN'
  })

  it('展示稳定 AVIF 路径模板说明，默认前缀 coser', async () => {
    const wrapper = mount(Coser, {
      global: {
        plugins: [i18n],
        stubs: { CoserPanel: true, RouterLink: true },
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('coser/rioko/538rem_cosplay/001.avif')
    const prefix = wrapper.findAll('input').find((item) => item.element.getAttribute('placeholder') === 'coser')
    expect(prefix).toBeTruthy()
    wrapper.unmount()
  })

  it('点击小眼睛拉取并显示已保存的 S3 Secret', async () => {
    const wrapper = mount(Coser, {
      global: {
        plugins: [i18n],
        stubs: { CoserPanel: true, RouterLink: true },
      },
    })
    await flushPromises()
    const input = wrapper.get('#coser-s3-secret-key')
    expect((input.element as HTMLInputElement).type).toBe('password')
    await wrapper.get('#coser-s3-secret-key-reveal').trigger('click')
    await flushPromises()
    const secretInput = wrapper.get('#coser-s3-secret-key')
    expect((secretInput.element as HTMLInputElement).type).toBe('text')
    expect((secretInput.element as HTMLInputElement).value).toBe('s3-secret-one')
    expect(getCoserSettings).toHaveBeenCalledWith('token', true)
    wrapper.unmount()
  })
})

import { mount } from '@vue/test-utils'
import { createRouter, createMemoryHistory } from 'vue-router'
import { describe, expect, it } from 'vitest'
import i18n from '../i18n'
import Layout from '../views/Layout.vue'

const PageStub = { template: '<div />' }

const createTestRouter = () => createRouter({
  history: createMemoryHistory(),
  routes: [
    {
      path: '/',
      component: PageStub,
      children: [
        { path: 'dashboard', name: 'dashboard', component: PageStub },
        { path: 'accounts', name: 'accounts', component: PageStub },
        { path: 'tasks', name: 'tasks', component: PageStub },
        { path: 'logs', name: 'logs', component: PageStub },
        { path: 'manga', name: 'manga', component: PageStub },
        { path: 'manga/ehentai', name: 'manga-ehentai', component: PageStub },
        { path: 'manga/hmw', name: 'manga-hmw', component: PageStub },
        { path: 'games', name: 'games', component: PageStub },
        { path: 'coser', name: 'coser', component: PageStub },
        { path: 'alerts', name: 'alerts', component: PageStub },
        { path: 'settings', name: 'settings', component: PageStub },
      ],
    },
  ],
})

describe('侧边栏二级栏目', () => {
  it('一级栏目可切换展开状态，进入二级路由时自动展开', async () => {
    const router = createTestRouter()
    await router.push('/dashboard')
    await router.isReady()

    const wrapper = mount(Layout, {
      global: {
        plugins: [router, i18n],
        stubs: { UserProfileModal: true },
      },
    })
    const parent = wrapper.get('[aria-controls="sidebar-subnav-manga"]')

    expect(parent.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('#sidebar-subnav-manga').exists()).toBe(false)

    await parent.trigger('click')
    expect(parent.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('#sidebar-subnav-manga').exists()).toBe(true)

    await parent.trigger('click')
    expect(parent.attributes('aria-expanded')).toBe('false')

    await router.push('/manga/ehentai')
    await wrapper.vm.$nextTick()
    expect(parent.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('#sidebar-subnav-manga').exists()).toBe(true)

    wrapper.unmount()
  })
})

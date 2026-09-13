import { createRouter, createWebHistory } from 'vue-router'
import Layout from '../views/Layout.vue'
import { useAuthStore } from '../stores/auth'
import { resolveAuthRedirect } from '../lib/auth-guard'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/mini-app',
      name: 'mini-app',
      component: () => import('../views/MiniApp.vue'),
    },
    {
      path: '/',
      component: Layout,
      redirect: '/dashboard',
      children: [
        { path: 'dashboard', name: 'dashboard', component: () => import('../views/Dashboard.vue') },
        { path: 'accounts', name: 'accounts', component: () => import('../views/Accounts.vue') },
        { path: 'tasks', name: 'tasks', component: () => import('../views/Tasks.vue') },
        { path: 'logs', name: 'logs', component: () => import('../views/Logs.vue') },
        {
          path: 'manga',
          name: 'manga',
          component: () => import('../views/Manga.vue'),
          beforeEnter: (to) => {
            const hash = String(to.hash || '').toLowerCase()
            if (hash.includes('ehentai')) {
              return { name: 'manga-ehentai', replace: true }
            }
            if (hash.includes('wnacg')) {
              return { name: 'manga-wnacg', replace: true }
            }
          },
        },
        {
          path: 'manga/ehentai',
          name: 'manga-ehentai',
          component: () => import('../views/MangaEhentai.vue'),
        },
        {
          path: 'manga/wnacg',
          name: 'manga-wnacg',
          component: () => import('../views/MangaWnacg.vue'),
        },
        {
          path: 'manga/hmw',
          name: 'manga-hmw',
          component: () => import('../views/MangaHmw.vue'),
        },
        {
          path: 'games',
          name: 'games',
          component: () => import('../views/Games.vue'),
        },
        {
          path: 'coser',
          name: 'coser',
          component: () => import('../views/Coser.vue'),
        },
        { path: 'alerts', name: 'alerts', component: () => import('../views/Alerts.vue') },
        { path: 'settings', name: 'settings', component: () => import('../views/Settings.vue') }
      ]
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('../views/Login.vue')
    }
  ]
})

router.beforeEach((to) => {
  const authStore = useAuthStore()
  const authRedirect = resolveAuthRedirect(typeof to.name === 'string' ? to.name : null, authStore)
  if (authRedirect) return authRedirect
  // 旧锚点 /manga#manga-ehentai 必须进独立二级页，不能留在频道采集页
  const mangaHash = String(to.hash || '').toLowerCase()
  if (to.name === 'manga' && mangaHash.includes('ehentai')) {
    return { name: 'manga-ehentai', replace: true }
  }
  if (to.name === 'manga' && mangaHash.includes('wnacg')) {
    return { name: 'manga-wnacg', replace: true }
  }
})

export default router

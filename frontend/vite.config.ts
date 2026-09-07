/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    vue(),
    VitePWA({
      registerType: 'autoUpdate',
      injectRegister: false,
      includeAssets: ['favicon.svg'],
      manifest: {
        name: 'TG-SignPulse',
        short_name: 'SignPulse',
        description: 'Telegram Automation Panel',
        theme_color: '#0f172a',
        background_color: '#f9fafb',
        display: 'standalone',
        start_url: '/',
        icons: [
          {
            src: '/pwa-192x192.png',
            sizes: '192x192',
            type: 'image/png'
          },
          {
            src: '/pwa-512x512.png',
            sizes: '512x512',
            type: 'image/png'
          }
        ]
      },
      workbox: {
        skipWaiting: true,
        clientsClaim: true,
        cleanupOutdatedCaches: true,
        navigateFallbackDenylist: [/^\/mini-app(?:\/|$)/],
        globPatterns: ['**/*.{js,css,html,ico,png,svg}'],
        runtimeCaching: [
          {
            // 仅缓存常规 GET API；排除鉴权/SSE 流/运行时状态/命中记录等
            // 敏感或动态数据，避免令牌过期后的旧响应与流式响应被缓存。
            // 账号相关接口（含扫码 qr/status、状态检测、头像、列表）必须实时：
            // 否则网络抖动时 NetworkFirst 会回放已过期的二维码 token/登录状态，
            // 手机扫码会提示“发生错误”，且新登录账号在列表里滞后不显示。
            urlPattern: ({ url }) =>
              url.pathname.startsWith('/api/') &&
              !url.pathname.startsWith('/api/auth/') &&
              !url.pathname.startsWith('/api/events/') &&
              !url.pathname.startsWith('/api/mini-app/') &&
              !url.pathname.startsWith('/api/ops/') &&
              !url.pathname.startsWith('/api/keyword-hits/') &&
              url.pathname !== '/api/accounts' &&
              !url.pathname.startsWith('/api/accounts/'),
            handler: 'NetworkFirst',
            options: {
              cacheName: 'api-cache',
              expiration: { maxEntries: 50, maxAgeSeconds: 300 }
            }
          }
        ]
      }
    })
  ],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,
      }
    }
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/lib/**/*.ts', 'src/stores/**/*.ts', 'src/composables/**/*.ts'],
      exclude: ['src/test/**', 'src/**/*.d.ts'],
    },
  },
})

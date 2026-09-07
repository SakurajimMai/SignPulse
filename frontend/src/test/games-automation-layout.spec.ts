import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const src = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../views/Games.vue'),
  'utf8',
)

describe('游戏自动发布操作按钮布局', () => {
  it('立即检查 / 重试失败项与回溯输入框对齐，不被提示文案顶到底部', () => {
    expect(src).toContain("t('games.automationScanNow')")
    expect(src).toContain("t('games.automationRetryFailed')")
    expect(src).not.toMatch(/md:col-span-2 flex flex-wrap items-end gap-2/)
    expect(src).toMatch(/ui-label hidden md:block invisible/)
    expect(src).toMatch(/flex flex-wrap items-stretch gap-2/)
  })
})

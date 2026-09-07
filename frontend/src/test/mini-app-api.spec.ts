import { beforeEach, describe, expect, it, vi } from 'vitest'

const request = vi.hoisted(() => vi.fn())

vi.mock('../lib/api/core', () => ({ request }))

import {
  authenticateMiniApp,
  getMiniAppAlerts,
  getMiniAppBootstrap,
  getMiniAppTaskRunStatus,
  runMiniAppTask,
} from '../lib/api/mini-app'

describe('Mini App API', () => {
  beforeEach(() => {
    request.mockReset()
  })

  it('auth exchanges Telegram initData without an admin token', async () => {
    request.mockResolvedValue({ access_token: 'mini' })
    await authenticateMiniApp('query_id=abc')
    expect(request).toHaveBeenCalledWith('/mini-app/auth', {
      method: 'POST',
      body: JSON.stringify({ init_data: 'query_id=abc' }),
    })
  })

  it('uses the scoped token for bootstrap and alert reads', async () => {
    request.mockResolvedValue({})
    await getMiniAppBootstrap('mini-token')
    await getMiniAppAlerts('mini-token', 500)
    expect(request).toHaveBeenNthCalledWith(1, '/mini-app/bootstrap', {}, 'mini-token')
    expect(request).toHaveBeenNthCalledWith(2, '/mini-app/alerts?limit=40', {}, 'mini-token')
  })

  it('encodes task paths and run identifiers', async () => {
    request.mockResolvedValue({})
    await runMiniAppTask('mini', 'daily/sign', 'acc one', 'request-1')
    expect(request).toHaveBeenNthCalledWith(
      1,
      '/mini-app/tasks/daily%2Fsign/run',
      {
        method: 'POST',
        body: JSON.stringify({ account_name: 'acc one', request_id: 'request-1' }),
      },
      'mini',
    )

    await getMiniAppTaskRunStatus('mini', 'daily/sign', 'acc one', 'run/1')
    expect(request).toHaveBeenNthCalledWith(
      2,
      '/mini-app/tasks/daily%2Fsign/run/status?account_name=acc+one&run_id=run%2F1',
      {},
      'mini',
    )
  })
})

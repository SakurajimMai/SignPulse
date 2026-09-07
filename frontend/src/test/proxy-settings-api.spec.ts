import { beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.fn()
vi.stubGlobal('fetch', fetchMock)

import { testProxyConnection } from '../lib/api/settings'

describe('proxy settings API', () => {
  beforeEach(() => {
    fetchMock.mockReset()
  })

  it('tests the saved proxy against the server endpoint without a request body', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ success: true, message: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(testProxyConnection('token')).resolves.toEqual({
      success: true,
      message: 'ok',
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/config/proxy/test',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          Authorization: 'Bearer token',
        }),
      }),
    )
    expect((fetchMock.mock.calls[0]?.[1] as RequestInit).body).toBeUndefined()
  })
})

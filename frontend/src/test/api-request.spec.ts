import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAuthStore } from '../stores/auth'

// mock fetch
const mockFetch = vi.fn()
vi.stubGlobal('fetch', mockFetch)

const jsonResponse = (body: unknown, status: number) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

async function importApi() {
  return import('../lib/api')
}

describe('api.request - 401 处理', () => {
  beforeEach(async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockFetch.mockReset()
    const core = await import('../lib/api/core')
    core.resetAuthRedirectGateForTests()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('401 响应时清除 token 并跳转', async () => {
    const store = useAuthStore()
    store.setToken('expired-token')

    mockFetch.mockResolvedValueOnce(jsonResponse({ detail: 'Unauthorized' }, 401))

    const api = await importApi()
    await expect(api.listAccounts('expired-token')).rejects.toThrow('Unauthorized')

    expect(store.token).toBeNull()
    expect(window.location.href).toContain('/')
  })

  it('并发多个 401 只触发一次跳转闸门', async () => {
    const store = useAuthStore()
    store.setToken('expired-token')
    mockFetch
      .mockResolvedValueOnce(jsonResponse({ detail: 'Unauthorized' }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Unauthorized' }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: 'Unauthorized' }, 401))

    const clearSpy = vi.spyOn(store, 'clearToken')
    const api = await importApi()
    const results = await Promise.allSettled([
      api.listAccounts('expired-token'),
      api.listAccounts('expired-token'),
      api.listAccounts('expired-token'),
    ])
    expect(results.every((r) => r.status === 'rejected')).toBe(true)
    expect(store.token).toBeNull()
    // 闸门保证 clearToken 只执行一次，避免批量头像 401 风暴
    expect(clearSpy).toHaveBeenCalledTimes(1)
  })

  it('401 不匹配当前 token 时不清除', async () => {
    const store = useAuthStore()
    store.setToken('current-token')

    mockFetch.mockResolvedValueOnce(jsonResponse({ detail: 'Unauthorized' }, 401))

    const api = await importApi()
    await expect(api.listAccounts('old-token')).rejects.toThrow('Unauthorized')

    // token 未被清除（请求中的 token 与 store 不一致）
    expect(store.token).toBe('current-token')
  })

  it('非 401 错误不触发清除', async () => {
    const store = useAuthStore()
    store.setToken('valid-token')

    mockFetch.mockResolvedValueOnce(jsonResponse({ detail: 'Server Error' }, 500))

    const api = await importApi()
    await expect(api.listAccounts('valid-token')).rejects.toThrow('Server Error')

    expect(store.token).toBe('valid-token')
  })

  it('FastAPI 校验错误格式正确提取 msg', async () => {
    const store = useAuthStore()
    store.setToken('valid-token')

    mockFetch.mockResolvedValueOnce(
      jsonResponse(
        {
          detail: [
            { loc: ['body', 'name'], msg: 'field required', type: 'value_error.missing' },
          ],
        },
        422,
      ),
    )

    const api = await importApi()
    await expect(api.createSignTask('valid-token', {} as never)).rejects.toThrow('field required')
  })

  it('非 JSON 错误响应使用文本', async () => {
    const store = useAuthStore()
    store.setToken('valid-token')

    mockFetch.mockResolvedValueOnce(
      new Response('Service Unavailable', {
        status: 503,
        headers: { 'Content-Type': 'text/plain' },
      }),
    )

    const api = await importApi()
    await expect(api.listAccounts('valid-token')).rejects.toThrow('Service Unavailable')
  })

  it('Cloudflare HTML 错误页收敛为 ORIGIN_HTML_ERROR', async () => {
    const store = useAuthStore()
    store.setToken('valid-token')
    const html =
      '<!DOCTYPE html><!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->' +
      '<title>ixacg.de | 520: Web server is returning an unknown error</title>'

    mockFetch.mockResolvedValueOnce(
      new Response(html, {
        status: 520,
        headers: { 'Content-Type': 'text/html; charset=UTF-8' },
      }),
    )

    const api = await importApi()
    try {
      await api.listAccounts('valid-token')
      throw new Error('expected failure')
    } catch (e: unknown) {
      const err = e as { message?: string; code?: string }
      expect(err.code).toBe('ORIGIN_HTML_ERROR')
      expect(err.message).toBe('ORIGIN_HTML_ERROR')
      expect(String(err.message)).not.toContain('oldie')
    }
  })

  it.each(['detail', 'message'] as const)(
    'JSON %s 中的 HTML 错误页也收敛为 ORIGIN_HTML_ERROR',
    async (field) => {
      const html = '<!DOCTYPE html><html><title>522: Connection timed out</title></html>'
      mockFetch.mockResolvedValueOnce(jsonResponse({ [field]: html }, 502))

      const api = await importApi()
      try {
        await api.listAccounts('valid-token')
        throw new Error('expected failure')
      } catch (e: unknown) {
        const err = e as { message?: string; code?: string; status?: number }
        expect(err.code).toBe('ORIGIN_HTML_ERROR')
        expect(err.message).toBe('ORIGIN_HTML_ERROR')
        expect(err.status).toBe(502)
      }
    },
  )

  it('2xx text/html 响应统一抛出 ORIGIN_HTML_ERROR', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('<html><title>Proxy error</title></html>', {
        status: 200,
        headers: { 'Content-Type': 'text/html; charset=UTF-8' },
      }),
    )

    const api = await importApi()
    await expect(api.listAccounts('valid-token')).rejects.toMatchObject({
      code: 'ORIGIN_HTML_ERROR',
      message: 'ORIGIN_HTML_ERROR',
      status: 200,
    })
  })

  it('2xx 正文为 HTML 时即使类型错误也统一拦截', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('<!DOCTYPE html><html><title>Gateway error</title></html>', {
        status: 200,
        headers: { 'Content-Type': 'text/plain' },
      }),
    )

    const api = await importApi()
    await expect(api.listAccounts('valid-token')).rejects.toMatchObject({
      code: 'ORIGIN_HTML_ERROR',
      message: 'ORIGIN_HTML_ERROR',
    })
  })

  it('2xx JSON 内嵌 HTML 告警详情时仍按合法 JSON 返回', async () => {
    const payload = {
      recent_alerts: [
        {
          detail:
            '<!DOCTYPE html><html><title>522: Connection timed out</title></html>',
        },
      ],
    }
    mockFetch.mockResolvedValueOnce(jsonResponse(payload, 200))

    const core = await import('../lib/api/core')
    await expect(core.request('/mini-app/bootstrap', {}, 'valid-token')).resolves.toEqual(payload)
  })

  it('requestText 保留合法 text/plain 正文', async () => {
    mockFetch.mockResolvedValueOnce(
      new Response('plain export content', {
        status: 200,
        headers: { 'Content-Type': 'text/plain' },
      }),
    )

    const core = await import('../lib/api/core')
    await expect(core.requestText('/plain-export', {}, 'valid-token')).resolves.toBe(
      'plain export content',
    )
  })

  it('requestBlob 拦截伪装为 text/plain 的 HTML 且保留普通 Blob', async () => {
    const html = '<!DOCTYPE html><html><title>Gateway error</title></html>'
    mockFetch
      .mockResolvedValueOnce(
        new Response(html, {
          status: 200,
          headers: { 'Content-Type': 'text/plain' },
        }),
      )
      .mockResolvedValueOnce(
        new Response('plain file content', {
          status: 200,
          headers: { 'Content-Type': 'text/plain' },
        }),
      )

    const core = await import('../lib/api/core')
    await expect(core.requestBlob('/disguised-html', {}, 'valid-token')).rejects.toMatchObject({
      code: 'ORIGIN_HTML_ERROR',
      message: 'ORIGIN_HTML_ERROR',
    })
    const blob = await core.requestBlob('/plain-file', {}, 'valid-token')
    expect(await blob.text()).toBe('plain file content')
  })

  it('错误响应携带 status 和 code', async () => {
    const store = useAuthStore()
    store.setToken('valid-token')

    mockFetch.mockResolvedValueOnce(
      jsonResponse({ detail: 'Forbidden', code: 'INSUFFICIENT_PERMISSIONS' }, 403),
    )

    const api = await importApi()
    try {
      await api.listAccounts('valid-token')
    } catch (e: unknown) {
      const err = e as { status?: number; code?: string }
      expect(err.status).toBe(403)
      expect(err.code).toBe('INSUFFICIENT_PERMISSIONS')
    }
  })

  it('错误响应正文读取失败时仍保留 401 状态并清除匹配 token', async () => {
    const store = useAuthStore()
    store.setToken('expired-token')

    mockFetch.mockResolvedValueOnce(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.error(new Error('stream failed'))
          },
        }),
        { status: 401 },
      ),
    )

    const api = await importApi()
    try {
      await api.listAccounts('expired-token')
      throw new Error('expected request to fail')
    } catch (e: unknown) {
      const err = e as { message?: string; status?: number }
      expect(err.message).toBe('Request failed (401)')
      expect(err.status).toBe(401)
    }
    expect(store.token).toBeNull()
  })

  it('完整备份使用长超时，不受 30 秒默认限制', async () => {
    vi.useFakeTimers()
    mockFetch.mockImplementationOnce((_url, options: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => {
          resolve(jsonResponse({ success: true, filename: 'backup.tar.gz' }, 200))
        }, 31_000)
        options.signal?.addEventListener(
          'abort',
          () => {
            clearTimeout(timer)
            reject(new DOMException('Aborted', 'AbortError'))
          },
          { once: true },
        )
      }),
    )

    const api = await importApi()
    await Promise.all([
      expect(api.exportBackupArchive('valid-token')).resolves.toMatchObject({
        mode: 'webdav',
        filename: 'backup.tar.gz',
      }),
      vi.advanceTimersByTimeAsync(31_000),
    ])
  })

  it('WebDAV 备份下载使用长超时，不受 30 秒默认限制', async () => {
    vi.useFakeTimers()
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:backup')
    const revokeObjectUrl = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
    mockFetch.mockImplementationOnce((_url, options: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => {
          resolve(
            new Response('backup', {
              status: 200,
              headers: { 'Content-Disposition': 'attachment; filename="remote.tar.gz"' },
            }),
          )
        }, 31_000)
        options.signal?.addEventListener(
          'abort',
          () => {
            clearTimeout(timer)
            reject(new DOMException('Aborted', 'AbortError'))
          },
          { once: true },
        )
      }),
    )

    const api = await importApi()
    await Promise.all([
      expect(api.downloadWebdavBackup('valid-token', 'remote.tar.gz')).resolves.toEqual({
        filename: 'remote.tar.gz',
      }),
      vi.advanceTimersByTimeAsync(31_000),
    ])
    expect(click).toHaveBeenCalledOnce()
    expect(createObjectUrl).toHaveBeenCalledOnce()
    // revokeObjectURL 延迟到宏任务执行（避免 Firefox 下载未开始即回收对象 URL）
    await vi.runAllTimersAsync()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:backup')
  })

  it('全量配置导出使用长超时', async () => {
    vi.useFakeTimers()
    mockFetch.mockImplementationOnce((_url, options: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        const timer = setTimeout(() => {
          resolve(new Response('{"signs":{}}', { status: 200 }))
        }, 31_000)
        options.signal?.addEventListener(
          'abort',
          () => {
            clearTimeout(timer)
            reject(new DOMException('Aborted', 'AbortError'))
          },
          { once: true },
        )
      }),
    )

    const api = await importApi()
    await Promise.all([
      expect(api.exportAllConfigs('valid-token')).resolves.toContain('signs'),
      vi.advanceTimersByTimeAsync(31_000),
    ])
  })

  it('默认请求在 30 秒后标记 NETWORK_TIMEOUT', async () => {
    vi.useFakeTimers()
    mockFetch.mockImplementationOnce((_url, options: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        )
      }),
    )

    const api = await importApi()
    // 显式 catch，避免 fake timer 推进时出现 unhandled rejection
    let caught: unknown
    const pending = api.listAccounts('valid-token').then(
      () => {
        throw new Error('expected NETWORK_TIMEOUT')
      },
      (e: unknown) => {
        caught = e
      },
    )
    await Promise.all([pending, vi.advanceTimersByTimeAsync(30_000)])
    expect(caught).toMatchObject({
      message: 'NETWORK_TIMEOUT',
      code: 'NETWORK_TIMEOUT',
      status: 0,
    })
  })

  it('外部 AbortSignal 取消标记为 NETWORK_ABORTED 而非超时', async () => {
    const controller = new AbortController()
    mockFetch.mockImplementationOnce((_url, options: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        )
      }),
    )

    const core = await import('../lib/api/core')
    let caught: unknown
    const pending = core
      .request('/accounts', { signal: controller.signal }, 'valid-token')
      .then(
        () => {
          throw new Error('expected NETWORK_ABORTED')
        },
        (e: unknown) => {
          caught = e
        },
      )
    controller.abort()
    await pending
    expect(caught).toMatchObject({
      message: 'NETWORK_ABORTED',
      code: 'NETWORK_ABORTED',
      status: 0,
    })
  })
})

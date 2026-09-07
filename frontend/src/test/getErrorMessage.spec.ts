import { describe, expect, it } from 'vitest'
import {
  getErrorCode,
  getErrorMessage,
  getLocalizedErrorMessage,
  looksLikeHtmlError,
  type ApiError,
} from '../lib/types'

describe('getErrorMessage', () => {
  it('返回 Error 对象的 message', () => {
    expect(getErrorMessage(new Error('network failed'))).toBe('network failed')
  })

  it('直接返回 string', () => {
    expect(getErrorMessage('plain string error')).toBe('plain string error')
  })

  it('优先提取 object.detail', () => {
    expect(getErrorMessage({ code: 42, detail: 'bad input' })).toBe('bad input')
  })

  it('优先提取 object.message', () => {
    expect(getErrorMessage({ message: 'boom' })).toBe('boom')
  })

  it('无 message/detail 的 object 序列化为 JSON', () => {
    expect(getErrorMessage({ code: 42 })).toBe('{"code":42}')
  })

  it('超长序列化结果截断防刷屏', () => {
    const huge = { code: 42, extra: 'x'.repeat(1000) }
    const result = getErrorMessage(huge)
    expect(result.length).toBeLessThanOrEqual(201)
    expect(result.endsWith('…')).toBe(true)
  })

  it('长 detail 字段截断防刷屏', () => {
    const longDetail = { code: 42, detail: 'y'.repeat(1000) }
    const result = getErrorMessage(longDetail)
    expect(result.length).toBeLessThanOrEqual(201)
    expect(result.endsWith('…')).toBe(true)
  })

  it('长 Error message 截断防刷屏', () => {
    const result = getErrorMessage(new Error('z'.repeat(500)))
    expect(result.length).toBeLessThanOrEqual(201)
    expect(result.endsWith('…')).toBe(true)
  })

  it('空 object 回退默认文案', () => {
    expect(getErrorMessage({})).toBe('Unknown error')
  })

  it('null 返回默认英文回退', () => {
    expect(getErrorMessage(null)).toBe('Unknown error')
  })

  it('undefined 返回默认英文回退', () => {
    expect(getErrorMessage(undefined)).toBe('Unknown error')
  })

  it('number 返回默认英文回退', () => {
    expect(getErrorMessage(42)).toBe('Unknown error')
  })

  it('Error 子类（TypeError）正确提取 message', () => {
    expect(getErrorMessage(new TypeError('invalid type'))).toBe('invalid type')
  })

  it('空字符串回退默认文案', () => {
    expect(getErrorMessage('')).toBe('Unknown error')
  })

  it('空白字符串回退默认文案', () => {
    expect(getErrorMessage('   ')).toBe('Unknown error')
  })

  it('支持自定义 fallback', () => {
    expect(getErrorMessage(null, '操作失败')).toBe('操作失败')
  })

  it('Error 空 message 使用 fallback', () => {
    expect(getErrorMessage(new Error(''), 'fallback')).toBe('fallback')
  })

  it('映射 NETWORK_TIMEOUT / NETWORK_ABORTED / NETWORK_ERROR', () => {
    expect(getErrorMessage(new Error('NETWORK_TIMEOUT'))).toBe('Request timed out')
    expect(getErrorMessage(new Error('NETWORK_ABORTED'))).toBe('Request cancelled')
    expect(getErrorMessage(new Error('NETWORK_ERROR'))).toBe('Network error')
  })

  it('映射 ACCOUNT_SESSION_INVALID code', () => {
    const err = new Error('session gone') as ApiError
    err.code = 'ACCOUNT_SESSION_INVALID'
    expect(getErrorCode(err)).toBe('ACCOUNT_SESSION_INVALID')
    expect(getErrorMessage(err)).toBe('Account session invalid, please re-login')
  })

  it('410 旧任务只读映射', () => {
    const err = new Error(
      'Legacy /api/tasks has been removed; use /api/sign-tasks',
    ) as ApiError
    err.status = 410
    expect(getErrorMessage(err)).toContain('sign-tasks')
  })

  it('getLocalizedErrorMessage 使用 t 映射', () => {
    const err = new Error('NETWORK_TIMEOUT') as ApiError
    err.code = 'NETWORK_TIMEOUT'
    const t = (key: string) =>
      key === 'apiErrors.NETWORK_TIMEOUT' ? '请求超时' : key
    expect(getLocalizedErrorMessage(err, t)).toBe('请求超时')
  })

  it('映射新增 WEBDAV / BACKUP / AI 解密错误码', () => {
    expect(getErrorMessage(new Error('WEBDAV_NOT_CONFIGURED'))).toBe('WebDAV is not configured')
    expect(getErrorMessage(new Error('BACKUP_EMPTY'))).toBe('Nothing to back up')
    expect(getErrorMessage(new Error('AI_KEY_DECRYPT_FAILED'))).toContain('APP_SECRET_KEY')
  })

  it('将 Mini App 主实例不可用错误码映射为可读提示', () => {
    expect(getErrorMessage(new Error('MINI_APP_PRIMARY_UNAVAILABLE'))).toContain(
      'primary service',
    )
  })

  it('将 Mini App 鉴权错误码映射为可读提示', () => {
    expect(getErrorMessage(new Error('MINI_APP_DISABLED'))).toContain('disabled')
    expect(getErrorMessage(new Error('MINI_APP_INIT_DATA_INVALID'))).toContain(
      'expired',
    )
    expect(getErrorMessage(new Error('MINI_APP_SESSION_REVOKED'))).toContain(
      'revoked',
    )
  })

  it('Cloudflare HTML 错误页不会进入展示文案', () => {
    const html =
      '<!DOCTYPE html> <!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->' +
      '<title>ixacg.de | 520: Web server is returning an unknown error</title>'
    expect(looksLikeHtmlError(html)).toBe(true)
    const message = getErrorMessage(html)
    expect(message).toContain('520')
    expect(message).not.toContain('<!DOCTYPE')
    expect(message).not.toContain('oldie')
    expect(message).not.toContain('ixacg.de')
    expect(getErrorMessage(new Error(html))).not.toContain('<html')
  })

  it('识别并清洗带 JSON 解析错误前缀的 HTML', () => {
    const parseError =
      `Unexpected token '<', "<!DOCTYPE html><html>` +
      '<title>ixacg.de | 522: Connection timed out</title>" is not valid JSON'
    expect(looksLikeHtmlError(parseError)).toBe(true)
    const message = getErrorMessage(new SyntaxError(parseError))
    expect(message).toContain('522')
    expect(message).not.toContain('<!DOCTYPE')
    expect(message).not.toContain('ixacg.de')
  })

  it('映射 ORIGIN_HTML_ERROR', () => {
    expect(getErrorMessage(new Error('ORIGIN_HTML_ERROR'))).toContain('Cloudflare')
  })
})

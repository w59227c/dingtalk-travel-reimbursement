import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const bridge = vi.hoisted(() => ({}) as Record<string, unknown>)

vi.mock('dingtalk-jsapi', () => ({ default: bridge }))

import { downloadAndOpenDingTalkDocument, requestDingTalkAuthCode } from './dingtalk'

describe('requestDingTalkAuthCode', () => {
  beforeEach(() => {
    for (const key of Object.keys(bridge)) delete bridge[key]
  })

  afterEach(() => vi.useRealTimers())

  it('uses the current top-level dd.requestAuthCode API', async () => {
    bridge.ready = (callback: () => void) => callback()
    bridge.requestAuthCode = vi.fn(
      (input: {
        corpId: string
        clientId?: string
        success?: (result: { code: string }) => void
      }) => {
        input.success?.({ code: 'current-auth-code' })
      },
    )

    await expect(requestDingTalkAuthCode('corp-1', 'client-1')).resolves.toBe(
      'current-auth-code',
    )
    expect(bridge.requestAuthCode).toHaveBeenCalledWith(
      expect.objectContaining({ corpId: 'corp-1', clientId: 'client-1' }),
    )
  })

  it('rejects unsupported clients without calling the legacy runtime API', async () => {
    const legacy = vi.fn()
    bridge.ready = (callback: () => void) => callback()
    bridge.runtime = { permission: { requestAuthCode: legacy } }

    await expect(requestDingTalkAuthCode('corp-1', 'client-1')).rejects.toThrow(
      '当前钉钉客户端不支持 dd.requestAuthCode，请升级客户端后重试',
    )
    expect(legacy).not.toHaveBeenCalled()
  })

  it('rejects instead of leaving the page blank when DingTalk never returns a result', async () => {
    vi.useFakeTimers()
    bridge.ready = (callback: () => void) => callback()
    bridge.requestAuthCode = vi.fn()

    const result = expect(requestDingTalkAuthCode('corp-1', 'client-1')).rejects.toThrow(
      '钉钉免登响应超时，请从公司钉钉工作台重新打开本应用',
    )
    await vi.advanceTimersByTimeAsync(8_000)

    await result
  })
})

describe('downloadAndOpenDingTalkDocument', () => {
  beforeEach(() => {
    for (const key of Object.keys(bridge)) delete bridge[key]
  })

  it('downloads with the native API and opens the returned local document', async () => {
    bridge.ready = (callback: () => void) => callback()
    bridge.downloadFile = vi.fn((input: {
      success?: (result: { filePath: string }) => void
    }) => input.success?.({ filePath: 'file:///local/preview.xlsx' }))
    bridge.openDocument = vi.fn((input: {
      success?: (result: Record<string, never>) => void
    }) => input.success?.({}))

    await downloadAndOpenDingTalkDocument({
      url: 'https://expense.example/api/preview/native',
      headers: { 'X-Reimbursement-Download-Token': 'signed-ticket' },
      fileType: 'xlsx',
    })

    expect(bridge.downloadFile).toHaveBeenCalledWith(expect.objectContaining({
      url: 'https://expense.example/api/preview/native',
      header: { 'X-Reimbursement-Download-Token': 'signed-ticket' },
    }))
    expect(bridge.openDocument).toHaveBeenCalledWith(expect.objectContaining({
      filePath: 'file:///local/preview.xlsx',
      fileType: 'xlsx',
    }))
  })

  it('reports an unsupported DingTalk client instead of opening a browser', async () => {
    bridge.ready = (callback: () => void) => callback()

    await expect(downloadAndOpenDingTalkDocument({
      url: 'https://expense.example/api/preview/native',
      headers: {},
      fileType: 'xlsx',
    })).rejects.toThrow('当前钉钉客户端不支持文件预览')
  })
})

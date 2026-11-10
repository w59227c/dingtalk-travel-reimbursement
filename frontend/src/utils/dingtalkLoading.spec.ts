import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

describe('DingTalk SDK loading deadline', () => {
  let finishImport: (value: { default: Record<string, unknown> }) => void
  let importStarted: Promise<void>
  let bridge: Record<string, unknown>

  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetModules()
    bridge = { ready: vi.fn((callback: () => void) => callback()),
      requestAuthCode: vi.fn(), downloadFile: vi.fn(), openDocument: vi.fn() }
    importStarted = new Promise<void>((started) => {
      vi.doMock('dingtalk-jsapi', () => new Promise((resolve) => {
        finishImport = resolve
        started()
      }))
    })
  })

  afterEach(async () => {
    finishImport?.({ default: bridge })
    await vi.runAllTimersAsync()
    vi.useRealTimers()
    vi.doUnmock('dingtalk-jsapi')
  })

  it.each(['auth', 'preview'] as const)('bounds the entire %s operation, including import', async (kind) => {
    const api = await import('./dingtalk')
    const operation = kind === 'auth'
      ? api.requestDingTalkAuthCode('synthetic-corp', 'synthetic-client')
      : api.downloadAndOpenDingTalkDocument({ url: '/synthetic.xlsx', headers: {}, fileType: 'xlsx' })
    let failure: Error | undefined
    void operation.catch((error: Error) => { failure = error })
    await importStarted
    await vi.advanceTimersByTimeAsync(kind === 'auth' ? 8_000 : 75_000)
    expect(failure?.message).toContain('超时')
    finishImport({ default: bridge })
    await vi.advanceTimersByTimeAsync(0)
    expect(bridge.ready).not.toHaveBeenCalled()
    expect(bridge.requestAuthCode).not.toHaveBeenCalled()
    expect(bridge.downloadFile).not.toHaveBeenCalled()
  })

  it('cancels immediately during import and ignores its eventual completion', async () => {
    const { downloadAndOpenDingTalkDocument } = await import('./dingtalk')
    const controller = new AbortController()
    let failure: Error | undefined
    void downloadAndOpenDingTalkDocument({
      url: '/synthetic.xlsx', headers: {}, fileType: 'xlsx', signal: controller.signal,
    }).catch((error: Error) => { failure = error })
    await importStarted
    controller.abort()
    await vi.advanceTimersByTimeAsync(0)
    expect(failure?.name).toBe('AbortError')
    finishImport({ default: bridge })
    await vi.advanceTimersByTimeAsync(0)
    expect(bridge.ready).not.toHaveBeenCalled()
    expect(bridge.downloadFile).not.toHaveBeenCalled()
  })

  it('does not restart the preview deadline after a slow import', async () => {
    const { downloadAndOpenDingTalkDocument } = await import('./dingtalk')
    let failure: Error | undefined
    void downloadAndOpenDingTalkDocument({
      url: '/synthetic.xlsx', headers: {}, fileType: 'xlsx',
    }).catch((error: Error) => { failure = error })
    await importStarted
    await vi.advanceTimersByTimeAsync(74_000)
    finishImport({ default: bridge })
    await vi.advanceTimersByTimeAsync(0)
    expect(bridge.downloadFile).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(failure?.message).toContain('超时')
    expect(bridge.openDocument).not.toHaveBeenCalled()
  })
})

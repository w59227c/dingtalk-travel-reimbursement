import { afterEach, describe, expect, it, vi } from 'vitest'

import { ensurePromiseWithResolvers } from './pdfRuntime'

describe('mobile PDF runtime compatibility', () => {
  const promiseConstructor = Promise as PromiseConstructor & { withResolvers?: unknown }
  const originalWithResolvers = promiseConstructor.withResolvers

  afterEach(() => {
    Object.defineProperty(promiseConstructor, 'withResolvers', {
      configurable: true,
      writable: true,
      value: originalWithResolvers,
    })
    vi.resetModules()
  })

  it('loads a validated PDF when the WebView lacks Promise.withResolvers', async () => {
    Object.defineProperty(promiseConstructor, 'withResolvers', {
      configurable: true,
      writable: true,
      value: undefined,
    })
    ensurePromiseWithResolvers()
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs')
    const source = `%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj
trailer<</Root 1 0 R>>
%%EOF`
    const task = pdfjs.getDocument({
      data: new TextEncoder().encode(source),
      isEvalSupported: false,
      useWorkerFetch: false,
    })
    const document = await task.promise
    const page = await document.getPage(1)

    expect(document.numPages).toBe(1)
    expect(page.getViewport({ scale: 1 }).width).toBe(10)
    await document.destroy()
  })
})

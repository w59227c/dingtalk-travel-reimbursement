import { describe, expect, it, vi } from 'vitest'

describe('PDF preview runtime compatibility', () => {
  it('loads a PDF page when Promise.withResolvers is unavailable', async () => {
    const promiseConstructor = Promise as PromiseConstructor & { withResolvers?: unknown }
    const originalWithResolvers = promiseConstructor.withResolvers
    Object.defineProperty(promiseConstructor, 'withResolvers', {
      configurable: true,
      writable: true,
      value: undefined,
    })
    vi.resetModules()

    try {
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
      })
      const document = await task.promise
      const page = await document.getPage(1)

      expect(document.numPages).toBe(1)
      expect(page.getViewport({ scale: 1 }).width).toBe(10)
      await document.destroy()
    } finally {
      Object.defineProperty(promiseConstructor, 'withResolvers', {
        configurable: true,
        writable: true,
        value: originalWithResolvers,
      })
      vi.resetModules()
    }
  })
})

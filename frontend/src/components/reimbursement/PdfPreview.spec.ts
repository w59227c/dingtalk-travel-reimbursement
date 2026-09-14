import ElementPlus from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import PdfPreview from './PdfPreview.vue'

const pdfMocks = vi.hoisted(() => ({
  getDocument: vi.fn(),
  workerOptions: { workerSrc: '' },
}))

vi.mock('pdfjs-dist/legacy/build/pdf.mjs', () => ({
  getDocument: pdfMocks.getDocument,
  GlobalWorkerOptions: pdfMocks.workerOptions,
}))

vi.mock('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url', () => ({
  default: '/assets/pdf.worker.mjs',
}))

describe('PdfPreview', () => {
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }))
  const getPage = vi.fn(async () => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: 200 * scale }),
    render,
  }))
  const destroyDocument = vi.fn(async () => undefined)
  const destroyLoadingTask = vi.fn(async () => undefined)

  beforeEach(() => {
    vi.clearAllMocks()
    pdfMocks.workerOptions.workerSrc = ''
    pdfMocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 2, getPage, destroy: destroyDocument }),
      destroy: destroyLoadingTask,
    })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
  })

  it('renders the authenticated PDF blob in-page and supports paging', async () => {
    const wrapper = mount(PdfPreview, {
      props: { source: new Blob(['pdf'], { type: 'application/pdf' }) },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await vi.waitFor(() => expect(getPage).toHaveBeenCalledWith(1))
    expect(pdfMocks.getDocument).toHaveBeenCalledWith({ data: expect.any(Uint8Array) })
    expect(pdfMocks.workerOptions.workerSrc).toBe('/assets/pdf.worker.mjs')
    expect(wrapper.text()).toContain('第 1 / 2 页')
    expect(render).toHaveBeenCalledOnce()

    await wrapper.get('[aria-label="下一页"]').trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(getPage).toHaveBeenCalledWith(2))
    expect(wrapper.text()).toContain('第 2 / 2 页')
    wrapper.unmount()
  })

  it('keeps a retry action in the dialog when the PDF cannot be decoded', async () => {
    pdfMocks.getDocument.mockReturnValue({
      promise: Promise.reject(new Error('invalid pdf')),
      destroy: destroyLoadingTask,
    })
    const wrapper = mount(PdfPreview, {
      props: { source: new Blob(['broken'], { type: 'application/pdf' }) },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await vi.waitFor(() => expect(wrapper.text()).toContain('PDF 加载失败，请重试'))
    expect(wrapper.findAll('button').some((button) => button.text().includes('重试'))).toBe(true)
    wrapper.unmount()
  })
})

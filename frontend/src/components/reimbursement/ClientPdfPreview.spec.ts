import ElementPlus from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ClientPdfPreview from './ClientPdfPreview.vue'

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

describe('ClientPdfPreview', () => {
  const render = vi.fn(() => ({ promise: Promise.resolve(), cancel: vi.fn() }))
  const getPage = vi.fn(async () => ({
    getViewport: ({ scale }: { scale: number }) => ({ width: 100 * scale, height: 200 * scale }),
    render,
  }))
  const destroyDocument = vi.fn(async () => undefined)
  const destroyLoadingTask = vi.fn(async () => undefined)
  let originalWithResolvers: unknown

  beforeEach(() => {
    vi.clearAllMocks()
    pdfMocks.workerOptions.workerSrc = ''
    pdfMocks.getDocument.mockReturnValue({
      promise: Promise.resolve({ numPages: 2, getPage, destroy: destroyDocument }),
      destroy: destroyLoadingTask,
    })
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as CanvasRenderingContext2D)
    const promiseConstructor = Promise as PromiseConstructor & { withResolvers?: unknown }
    originalWithResolvers = promiseConstructor.withResolvers
  })

  afterEach(() => {
    Object.defineProperty(Promise, 'withResolvers', {
      configurable: true,
      writable: true,
      value: originalWithResolvers,
    })
    vi.restoreAllMocks()
  })

  it('renders an authenticated PDF blob on canvas and supports paging in an older WebView runtime', async () => {
    Object.defineProperty(Promise, 'withResolvers', {
      configurable: true,
      writable: true,
      value: undefined,
    })
    const wrapper = mount(ClientPdfPreview, {
      props: { source: new Blob(['pdf'], { type: 'application/pdf' }) },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await vi.waitFor(() => expect(getPage).toHaveBeenCalledWith(1))
    expect(typeof (Promise as PromiseConstructor & { withResolvers?: unknown }).withResolvers).toBe('function')
    expect(pdfMocks.getDocument).toHaveBeenCalledWith({
      data: expect.any(Uint8Array),
      isEvalSupported: false,
      useWorkerFetch: false,
    })
    expect(pdfMocks.workerOptions.workerSrc).toBe('/assets/pdf.worker.mjs')
    expect(wrapper.text()).toContain('第 1 / 2 页')
    expect(render).toHaveBeenCalledOnce()
    expect(wrapper.emitted('failed')).toBeUndefined()

    await wrapper.get('[aria-label="下一页"]').trigger('click')
    await flushPromises()
    await vi.waitFor(() => expect(getPage).toHaveBeenCalledWith(2))
    expect(wrapper.text()).toContain('第 2 / 2 页')
    wrapper.unmount()
  })

  it('renders at the real mobile pixel ratio so fitted PDF text stays sharp', async () => {
    vi.spyOn(window, 'devicePixelRatio', 'get').mockReturnValue(3)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(342)
    getPage.mockResolvedValueOnce({
      getViewport: ({ scale }: { scale: number }) => ({
        width: 595 * scale,
        height: 842 * scale,
      }),
      render,
    })
    const wrapper = mount(ClientPdfPreview, {
      props: { source: new Blob(['pdf'], { type: 'application/pdf' }) },
      global: { plugins: [ElementPlus] },
    })

    await vi.waitFor(() => expect(render).toHaveBeenCalledOnce())

    const canvas = wrapper.get('canvas').element as HTMLCanvasElement
    expect(canvas.style.width).toBe('318px')
    expect(canvas.width).toBe(954)
    expect(canvas.height).toBe(1351)
    wrapper.unmount()
  })

  it('hands unsupported client rendering to the compatible preview exactly once', async () => {
    pdfMocks.getDocument.mockReturnValue({
      promise: Promise.reject(new Error('unsupported runtime')),
      destroy: destroyLoadingTask,
    })
    const wrapper = mount(ClientPdfPreview, {
      props: { source: new Blob(['pdf'], { type: 'application/pdf' }) },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await vi.waitFor(() => expect(wrapper.emitted('failed')).toHaveLength(1))
    expect(wrapper.emitted('failed')?.[0]).toEqual(['手机端 PDF 加载失败'])
    wrapper.unmount()
  })
})

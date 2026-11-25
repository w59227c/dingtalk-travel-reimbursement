import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, reactive, type EffectScope } from 'vue'

import { getReimbursementFileContent, getReimbursementPdfPreviewPage } from '@/api/reimbursements'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { useMaterialPreview } from './useMaterialPreview'

vi.mock('@/api/reimbursements', () => ({
  getReimbursementFileContent: vi.fn(),
  getReimbursementPdfPreviewPage: vi.fn(),
}))

function file(id = 'image', mediaType = 'image/png'): ReimbursementDraftFile {
  return {
    id, name: `${id}.${mediaType === 'application/pdf' ? 'pdf' : 'png'}`, mediaType,
    role: 'ATTACHMENT_ONLY', attachmentKind: 'other', status: 'ACTIVE',
    sortOrder: 0, sizeBytes: 10, ocrStatus: 'NOT_REQUESTED', ocrResult: null,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const image = new Blob(['synthetic-image'], { type: 'image/png' })
const pdf = new Blob(['synthetic-pdf'], { type: 'application/pdf' })

describe.each([false, true])('material preview lifecycle, mobile=%s', (mobile) => {
  let scope: EffectScope
  let context: { draftId: string | undefined; submitted: boolean; files: ReimbursementDraftFile[] }
  let preview: ReturnType<typeof useMaterialPreview>
  let notify: ReturnType<typeof vi.fn<(message: string) => void>>
  let createUrl: ReturnType<typeof vi.fn<(blob: Blob | MediaSource) => string>>
  let revokeUrl: ReturnType<typeof vi.fn<(url: string) => void>>

  beforeEach(() => {
    vi.resetAllMocks()
    let sequence = 0
    createUrl = vi.fn(() => `blob:preview-${++sequence}`)
    revokeUrl = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = createUrl
      static revokeObjectURL = revokeUrl
    })
    context = reactive({ draftId: 'draft-1', submitted: false, files: [file(), file('pdf', 'application/pdf')] })
    notify = vi.fn()
    scope = effectScope()
    preview = scope.run(() => useMaterialPreview({
      draftId: () => context.draftId,
      files: () => context.files,
      mobile: () => mobile,
      submitted: () => context.submitted,
      onError: notify,
    }))!
    vi.mocked(getReimbursementFileContent).mockResolvedValue(image)
    vi.mocked(getReimbursementPdfPreviewPage).mockResolvedValue({ blob: image, pageNumber: 1, pageCount: 2 })
  })

  afterEach(() => {
    scope.stop()
    vi.unstubAllGlobals()
  })

  it('releases replaced and closed image URLs exactly once', async () => {
    await preview.open(context.files[0])
    expect(preview.state).toMatchObject({ visible: true, loading: false, name: 'image.png', url: 'blob:preview-1' })
    await preview.open(context.files[0])
    expect(revokeUrl.mock.calls).toEqual([['blob:preview-1']])
    preview.close()
    expect(preview.state).toMatchObject({ visible: false, url: '', blob: null, error: '', loading: false })
    scope.stop()
    expect(revokeUrl.mock.calls).toEqual([['blob:preview-1'], ['blob:preview-2']])
  })

  it('uses the appropriate direct PDF renderer and can return from compatible mode', async () => {
    vi.mocked(getReimbursementFileContent).mockResolvedValue(pdf)
    await preview.open(context.files[1])
    expect(preview.state.blob).toBe(mobile ? pdf : null)
    expect(preview.state.url).toBe(mobile ? '' : 'blob:preview-1')
    expect(getReimbursementPdfPreviewPage).not.toHaveBeenCalled()
    await preview.setMode('compatible')
    expect(preview.state).toMatchObject({ mode: 'compatible', blob: null, page: 1, pageCount: 2 })
    const compatibleUrl = preview.state.url
    await preview.setMode('direct')
    expect(revokeUrl).toHaveBeenCalledWith(compatibleUrl)
    expect(preview.state.mode).toBe('direct')
    expect(getReimbursementFileContent).toHaveBeenCalledTimes(2)
  })

  it('retries a failed original without reopening a closed dialog', async () => {
    vi.mocked(getReimbursementFileContent).mockRejectedValueOnce(new Error('材料暂时无法读取'))
    await preview.open(context.files[0])
    expect(preview.state.error).toBe('材料暂时无法读取')
    expect(notify).toHaveBeenCalledOnce()
    await preview.retry()
    expect(preview.state).toMatchObject({ error: '', url: 'blob:preview-1' })
    preview.close()
    await preview.retry()
    expect(getReimbursementFileContent).toHaveBeenCalledTimes(2)
  })

  it('retries only one transient server renderer failure', async () => {
    const resourceError = Object.assign(new Error('renderer unavailable'), {
      isAxiosError: true,
      response: { data: { error: { code: 'PDF_PREVIEW_RESOURCE_LIMIT', message: '兼容预览暂时繁忙' } } },
    })
    vi.mocked(getReimbursementPdfPreviewPage).mockRejectedValue(resourceError)
    await preview.open(context.files[1])
    await preview.setMode('compatible')
    expect(getReimbursementPdfPreviewPage).toHaveBeenCalledTimes(2)
    expect(preview.state).toMatchObject({ loading: false, error: '兼容预览暂时繁忙' })
    vi.mocked(getReimbursementPdfPreviewPage).mockResolvedValueOnce({ blob: image, pageNumber: 1, pageCount: 2 })
    await preview.retry()
    expect(preview.state).toMatchObject({ error: '', page: 1, pageCount: 2 })
    expect(preview.state.url).not.toBe('')
  })

  it('keeps the current page after a page failure and retries the failed page', async () => {
    await preview.open(context.files[1])
    await preview.setMode('compatible')
    const firstUrl = preview.state.url
    vi.mocked(getReimbursementPdfPreviewPage).mockRejectedValueOnce(new Error('第二页加载失败'))
    await preview.goToPage(2)
    expect(preview.state).toMatchObject({ page: 1, url: firstUrl, error: '第二页加载失败' })
    expect(revokeUrl).not.toHaveBeenCalledWith(firstUrl)
    vi.mocked(getReimbursementPdfPreviewPage).mockResolvedValueOnce({ blob: image, pageNumber: 2, pageCount: 2 })
    await preview.retry()
    expect(getReimbursementPdfPreviewPage).toHaveBeenLastCalledWith('draft-1', 'pdf', 2, { signal: expect.any(AbortSignal) })
    expect(preview.state).toMatchObject({ page: 2, error: '' })
    expect(revokeUrl).toHaveBeenCalledWith(firstUrl)
    await preview.goToPage(2)
    await preview.goToPage(0)
    await preview.goToPage(3)
    await preview.goToPage(1.5)
    expect(getReimbursementPdfPreviewPage).toHaveBeenCalledTimes(3)
  })

  it.each(['resolve', 'reject'] as const)('ignores a late original %s after switching mode', async (settlement) => {
    const pending = deferred<Blob>()
    vi.mocked(getReimbursementFileContent).mockReturnValueOnce(pending.promise)
    const opening = preview.open(context.files[1])
    const signal = vi.mocked(getReimbursementFileContent).mock.calls[0]![2]!.signal!
    await preview.setMode('compatible')
    const compatibleUrl = preview.state.url
    expect(signal.aborted).toBe(true)
    if (settlement === 'resolve') pending.resolve(pdf)
    else pending.reject(new Error('迟到的原件错误'))
    await opening
    expect(preview.state).toMatchObject({ mode: 'compatible', url: compatibleUrl, error: '', loading: false })
    expect(notify).not.toHaveBeenCalled()
  })

  it('does not let an old compatible request clear a newer original loading state', async () => {
    await preview.open(context.files[1])
    const oldPage = deferred<Awaited<ReturnType<typeof getReimbursementPdfPreviewPage>>>()
    vi.mocked(getReimbursementPdfPreviewPage).mockReturnValueOnce(oldPage.promise)
    const switching = preview.setMode('compatible')
    const oldSignal = vi.mocked(getReimbursementPdfPreviewPage).mock.calls[0]![3]!.signal!
    const newOriginal = deferred<Blob>()
    vi.mocked(getReimbursementFileContent).mockReturnValueOnce(newOriginal.promise)
    const opening = preview.open(context.files[0])
    expect(oldSignal.aborted).toBe(true)
    oldPage.resolve({ blob: image, pageNumber: 1, pageCount: 2 })
    await switching
    expect(preview.state).toMatchObject({ name: 'image.png', mode: 'direct', loading: true, url: '', error: '' })
    newOriginal.resolve(image)
    await opening
    expect(preview.state.loading).toBe(false)
    expect(preview.state.url).not.toBe('')
  })

  it.each(['close', 'draft', 'removed', 'purged', 'submitted', 'dispose'] as const)(
    'cancels preview on %s and suppresses late errors', async (change) => {
      const pending = deferred<Blob>()
      vi.mocked(getReimbursementFileContent).mockReturnValueOnce(pending.promise)
      const opening = preview.open(context.files[0])
      const signal = vi.mocked(getReimbursementFileContent).mock.calls[0]![2]!.signal!
      if (change === 'close') preview.close()
      if (change === 'draft') context.draftId = 'draft-2'
      if (change === 'removed') context.files = []
      if (change === 'purged') context.files[0]!.status = 'PURGED'
      if (change === 'submitted') context.submitted = true
      if (change === 'dispose') scope.stop()
      expect(signal.aborted).toBe(true)
      pending.reject(new Error('旧预览失败'))
      await opening
      expect(preview.state).toMatchObject({ visible: false, loading: false, url: '', blob: null, error: '' })
      expect(notify).not.toHaveBeenCalled()
      expect(createUrl).not.toHaveBeenCalled()
    },
  )

  it('ignores duplicate retry and page clicks while a request is running', async () => {
    await preview.open(context.files[1])
    const pending = deferred<Awaited<ReturnType<typeof getReimbursementPdfPreviewPage>>>()
    vi.mocked(getReimbursementPdfPreviewPage).mockReturnValueOnce(pending.promise)
    const switching = preview.setMode('compatible')
    await preview.setMode('compatible')
    await preview.goToPage(1)
    await preview.retry()
    expect(getReimbursementPdfPreviewPage).toHaveBeenCalledOnce()
    pending.resolve({ blob: image, pageNumber: 1, pageCount: 2 })
    await switching
    expect(preview.state.loading).toBe(false)
  })

  it('does not open missing, inactive, foreign or submitted files', async () => {
    await preview.open(undefined)
    await preview.open(file('foreign'))
    context.files[0]!.status = 'PURGED'
    await preview.open(context.files[0])
    context.submitted = true
    await preview.open(context.files[1])
    context.submitted = false
    context.draftId = undefined
    await preview.open(context.files[1])
    expect(getReimbursementFileContent).not.toHaveBeenCalled()
    expect(preview.state.visible).toBe(false)
  })
})

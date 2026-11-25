import { onScopeDispose, ref, shallowReactive, shallowReadonly, watch } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { getReimbursementFileContent, getReimbursementPdfPreviewPage } from '@/api/reimbursements'
import type { ReimbursementDraftFile } from '@/types/reimbursements'

type PreviewMode = 'direct' | 'compatible'

interface MaterialPreviewOptions {
  draftId: () => string | undefined
  files: () => readonly ReimbursementDraftFile[]
  mobile: () => boolean
  submitted: () => boolean
  onError: (message: string) => void
}

/** Owns one preview session, including requests and temporary browser resources. */
export function useMaterialPreview(options: MaterialPreviewOptions) {
  const state = shallowReactive({
    visible: false,
    name: '',
    kind: 'image' as 'image' | 'pdf',
    mode: 'direct' as PreviewMode,
    url: '',
    blob: null as Blob | null,
    page: 1,
    pageCount: 1,
    error: '',
    loading: false,
  })
  let previewDraftId: string | undefined
  const fileId = ref('')
  let failedPage = 1
  let activeRequest: AbortController | null = null
  let disposed = false

  function currentFile() {
    return options.files().find((file) => file.id === fileId.value && file.status === 'ACTIVE')
  }

  function cancelRequest(): void {
    activeRequest?.abort()
    activeRequest = null
    state.loading = false
  }

  function releaseContent(): void {
    if (state.url) URL.revokeObjectURL(state.url)
    state.url = ''
    state.blob = null
  }

  function close(): void {
    state.visible = false
    cancelRequest()
    releaseContent()
    previewDraftId = undefined
    fileId.value = ''
    failedPage = 1
    Object.assign(state, {
      visible: false, name: '', kind: 'image', mode: 'direct',
      page: 1, pageCount: 1, error: '',
    })
  }

  function beginRequest(): AbortController {
    cancelRequest()
    const controller = new AbortController()
    activeRequest = controller
    state.loading = true
    state.error = ''
    return controller
  }

  function isCurrent(controller: AbortController, mode: PreviewMode): boolean {
    return activeRequest === controller && !controller.signal.aborted && !disposed
      && !options.submitted() && state.visible && state.mode === mode
      && previewDraftId === options.draftId() && Boolean(currentFile())
  }

  function finishRequest(controller: AbortController): void {
    // A canceled request may settle after a newer preview has started.
    if (activeRequest !== controller) return
    activeRequest = null
    state.loading = false
  }

  function replaceUrl(blob: Blob): void {
    const previousUrl = state.url
    state.url = URL.createObjectURL(blob)
    if (previousUrl) URL.revokeObjectURL(previousUrl)
  }

  function showError(error: unknown, fallback: string): void {
    state.error = apiErrorMessage(error, fallback)
    options.onError(state.error)
  }

  async function open(file: ReimbursementDraftFile | undefined): Promise<void> {
    const draftId = options.draftId()
    if (disposed || options.submitted() || !draftId || !file || file.status !== 'ACTIVE'
      || !options.files().some((entry) => entry.id === file.id && entry.status === 'ACTIVE')) return
    close()
    previewDraftId = draftId
    fileId.value = file.id
    state.name = file.name
    state.kind = file.mediaType === 'application/pdf' ? 'pdf' : 'image'
    state.visible = true
    const controller = beginRequest()
    try {
      const blob = await getReimbursementFileContent(draftId, file.id, { signal: controller.signal })
      if (!isCurrent(controller, 'direct')) return
      if (state.kind === 'pdf' && options.mobile()) state.blob = blob
      else replaceUrl(blob)
    } catch (error) {
      if (isCurrent(controller, 'direct')) showError(error, '材料预览失败，请重试')
    } finally {
      finishRequest(controller)
    }
  }

  async function goToPage(page: number): Promise<void> {
    const draftId = previewDraftId
    const requestedFileId = fileId.value
    if (disposed || options.submitted() || !draftId || draftId !== options.draftId()
      || !state.visible || !currentFile() || state.kind !== 'pdf' || state.mode !== 'compatible'
      || state.loading || !Number.isInteger(page) || page < 1 || page > state.pageCount
      || (page === state.page && Boolean(state.url))) return
    const controller = beginRequest()
    try {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          const preview = await getReimbursementPdfPreviewPage(draftId, requestedFileId, page, {
            signal: controller.signal,
          })
          if (!isCurrent(controller, 'compatible')) return
          replaceUrl(preview.blob)
          state.page = preview.pageNumber
          state.pageCount = preview.pageCount
          return
        } catch (error) {
          if (!isCurrent(controller, 'compatible')) return
          if (apiErrorCode(error) === 'PDF_PREVIEW_RESOURCE_LIMIT' && attempt === 0) continue
          failedPage = page
          showError(error, 'PDF 页面加载失败，请重试')
          return
        }
      }
    } finally {
      finishRequest(controller)
    }
  }

  async function setMode(mode: PreviewMode): Promise<void> {
    if (disposed || !state.visible || state.kind !== 'pdf' || state.mode === mode) return
    if (mode === 'direct') {
      await open(currentFile())
      return
    }
    cancelRequest()
    releaseContent()
    state.mode = 'compatible'
    state.page = 1
    state.pageCount = 1
    failedPage = 1
    await goToPage(1)
  }

  async function retry(): Promise<void> {
    if (state.loading || !state.visible) return
    if (state.mode === 'compatible') await goToPage(failedPage)
    else await open(currentFile())
  }

  watch(
    () => [options.draftId(), options.submitted(), currentFile()?.id] as const,
    () => {
      if (state.visible && (options.submitted() || previewDraftId !== options.draftId() || !currentFile())) close()
    },
    { flush: 'sync' },
  )
  onScopeDispose(() => {
    disposed = true
    close()
  })

  return { state: shallowReadonly(state), open, close, setMode, goToPage, retry }
}

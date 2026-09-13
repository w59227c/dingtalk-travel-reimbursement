import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import {
  createReimbursementDraft,
  deleteReimbursementDraft,
  deleteReimbursementDraftFile,
  clearReimbursementDraftFiles,
  getReimbursementDraft,
  getReimbursementDraftExcelPreview,
  getOaReimbursementOptions,
  listOaTravelApprovals,
  listReimbursementDraftFiles,
  listReimbursementDrafts,
  markReimbursementDraftReviewReady,
  recognizeReimbursementDraftFile,
  requestReimbursementDraftExcelPreviewTicket,
  replaceReimbursementRelatedApprovals,
  updateReimbursementDraft,
  updateReimbursementDraftFile,
  uploadReimbursementDraftFile,
} from '@/api/reimbursements'
import type { ListOaTravelApprovalsOptions } from '@/api/reimbursements'
import { downloadBlob } from '@/api/excel'
import { downloadAndOpenDingTalkDocument } from '@/utils/dingtalk'
import type {
  OaReimbursementOptions,
  OaTravelApproval,
  OaTravelApprovalQueryWindow,
  ReimbursementDraft,
  ReimbursementDraftDeletion,
  ReimbursementDraftFile,
  ReimbursementDraftFileDeletion,
  ReimbursementDraftFileMutation,
  ReimbursementDraftFileRole,
  ReimbursementDraftFileUpdate,
  ReimbursementDraftInput,
  ReimbursementDraftSummary,
  ReimbursementRelatedApprovalSelection,
} from '@/types/reimbursements'

interface RequestContext {
  controller: AbortController
  epoch: number
}

interface ActiveDetailRead {
  controller: AbortController
  draftId: string
  intentVersion: number
  requestVersion: number
}

interface DraftReadGuard {
  draftId: string
  intentVersion: number
  stateGeneration: number
}

interface RevisionedResult {
  revision: number
}

interface MutationOptions {
  reloadAfterFailure?: boolean
  retryRevisionConflictOnce?: boolean
  pipeline?: FilePipeline
  benignErrorCodes?: readonly string[]
}

interface FilePipeline {
  id: number
  draftId: string
  departmentId: string
  epoch: number
  intentVersion: number
  inputSignature: string
  controllers: Set<AbortController>
  uploadedIds: Set<string>
  recognizedIds: Set<string>
  fileRevisions: Map<string, number>
  uploading: boolean
  recognizing: boolean
  needsRefresh: boolean
}

const CONSISTENT_READ_ATTEMPTS = 2

export const useReimbursementDraftStore = defineStore('reimbursementDraft', () => {
  const drafts = ref<ReimbursementDraftSummary[]>([])
  const draftListOffset = ref(0)
  const draftListLimit = ref(50)
  const draftListTotal = ref(0)
  const reimbursementOptions = ref<OaReimbursementOptions | null>(null)
  const travelApprovals = ref<OaTravelApproval[]>([])
  const travelApprovalQueryWindow = ref<OaTravelApprovalQueryWindow | null>(null)
  const travelApprovalTemplateConfigVersion = ref<number | null>(null)
  const currentDraft = ref<ReimbursementDraft | null>(null)
  const files = ref<ReimbursementDraftFile[]>([])
  const loadingDrafts = ref(false)
  const loadingCurrentDraft = ref(false)
  const loadingReimbursementOptions = ref(false)
  const loadingTravelApprovals = ref(false)
  const pendingMutations = ref(0)
  const downloadingPreview = ref(false)
  const uploading = ref(false)
  const processingFiles = ref(false)
  const filePipelineActive = ref(false)
  const uploadProgress = ref<number | null>(null)
  const listError = ref('')
  const loadError = ref('')
  const reimbursementOptionsError = ref('')
  const travelApprovalsError = ref('')
  const mutationError = ref('')
  const revisionConflict = ref(false)

  let lifecycleEpoch = 0
  let mutationQueue: Promise<unknown> | null = null
  let filePipeline: FilePipeline | null = null
  let filePipelineSequence = 0
  let currentIntentVersion = 0
  let draftCollectionVersion = 0
  let listRequestVersion = 0
  let detailRequestVersion = 0
  let reimbursementOptionsRequestVersion = 0
  let travelApprovalsRequestVersion = 0
  let previewRequestVersion = 0
  let uploadRequestVersion = 0
  const draftStateGenerations = new Map<string, number>()
  let activeListController: AbortController | null = null
  let activeDetailRead: ActiveDetailRead | null = null
  let activeReimbursementOptionsController: AbortController | null = null
  let activeTravelApprovalsController: AbortController | null = null
  let activePreviewController: AbortController | null = null
  let fileRefreshFlight: Promise<void> | null = null
  const controllers = new Set<AbortController>()

  const busy = computed(() =>
    loadingDrafts.value
    || loadingCurrentDraft.value
    || loadingReimbursementOptions.value
    || loadingTravelApprovals.value
    || pendingMutations.value > 0
    || processingFiles.value
    || filePipelineActive.value
    || downloadingPreview.value,
  )
  const currentRevision = computed(() => currentDraft.value?.revision ?? null)

  function isFilePipelineActive(id: number): boolean {
    return filePipeline?.id === id && filePipeline.epoch === lifecycleEpoch
      && filePipeline.intentVersion === currentIntentVersion
      && currentDraft.value?.id === filePipeline.draftId
      && currentDraft.value.department.id === filePipeline.departmentId
      && ['DRAFT', 'REVIEW_READY'].includes(currentDraft.value.status)
  }

  function beginFilePipeline(): number {
    const target = requireCurrentDraft()
    if (filePipeline || mutationQueue || pendingMutations.value || loadingCurrentDraft.value
      || !['DRAFT', 'REVIEW_READY'].includes(target.status)) {
      throw new Error('请等待当前文件操作完成后再上传')
    }
    filePipeline = {
      id: ++filePipelineSequence, draftId: target.id, departmentId: target.department.id,
      epoch: lifecycleEpoch, intentVersion: currentIntentVersion, inputSignature: JSON.stringify(target.input),
      controllers: new Set(), uploadedIds: new Set(), recognizedIds: new Set(), fileRevisions: new Map(),
      uploading: false, recognizing: false, needsRefresh: false,
    }
    filePipelineActive.value = true
    return filePipeline.id
  }

  function cancelFilePipeline(id: number): void {
    if (filePipeline?.id !== id) return
    for (const controller of filePipeline.controllers) controller.abort()
    filePipeline = null
    filePipelineActive.value = false
    uploadRequestVersion += 1
    uploading.value = false
    uploadProgress.value = null
  }

  function requireFilePipeline(id: number): FilePipeline {
    if (!isFilePipelineActive(id)) throw new Error('本批文件处理已取消，请重新操作')
    return filePipeline!
  }

  function applyPipelineFileMutation(
    result: ReimbursementDraftFileMutation, draftId: string, context: RequestContext, pipeline: FilePipeline,
  ): void {
    if (!isFilePipelineActive(pipeline.id) || !accepts(context) || result.draftId !== draftId) return
    // Whole-draft revisions can arrive out of order across the two lanes.
    // Preserve the newer draft revision, but do not drop a different file's OCR.
    adoptFileRevision(draftId, Math.max(currentDraft.value!.revision, result.revision), context)
    if (result.revision < (pipeline.fileRevisions.get(result.file.id) ?? -1)) return
    pipeline.fileRevisions.set(result.file.id, result.revision)
    upsertFile(result.file)
  }

  async function refreshPipelineRevision(pipeline: FilePipeline): Promise<void> {
    const context = requestContext()
    pipeline.controllers.add(context.controller)
    try {
      // A failed upload may already have reserved a revision. Read only the
      // draft here; an in-flight OCR must not be replaced by a file-list read.
      const result = await getReimbursementDraft(pipeline.draftId, { signal: context.controller.signal })
      if (!isFilePipelineActive(pipeline.id) || !accepts(context)) return
      if (result.id !== pipeline.draftId || !['DRAFT', 'REVIEW_READY'].includes(result.status)
        || JSON.stringify(result.input) !== pipeline.inputSignature) {
        throw new Error('报销内容已发生变化，请刷新检查后继续')
      }
      adoptFileRevision(result.id, Math.max(currentDraft.value!.revision, result.revision), context)
    } catch (error) {
      if (isFilePipelineActive(pipeline.id)) {
        mutationError.value = apiErrorMessage(error, '文件状态同步失败，请刷新后继续')
        cancelFilePipeline(pipeline.id)
      }
      throw error
    } finally {
      pipeline.controllers.delete(context.controller)
      releaseRequest(context)
    }
  }

  async function finishFilePipeline(id: number): Promise<boolean> {
    if (!isFilePipelineActive(id)) return false
    const pipeline = filePipeline!
    if (pipeline.uploading || pipeline.recognizing) throw new Error('本批文件尚未处理完成')
    const context = requestContext()
    pipeline.controllers.add(context.controller)
    try {
      if (pipeline.needsRefresh) {
        const result = await readConsistentDraft(pipeline.draftId, context.controller.signal)
        if (!isFilePipelineActive(id) || !accepts(context)) return false
        if (result.draft.id !== pipeline.draftId || !['DRAFT', 'REVIEW_READY'].includes(result.draft.status)
          || JSON.stringify(result.draft.input) !== pipeline.inputSignature) {
          throw new Error('报销内容已发生变化，请刷新检查后继续')
        }
        if (adoptDraft(result.draft, context, pipeline.draftId)) files.value = result.files
      }
      return true
    } catch (error) {
      if (isFilePipelineActive(id)) mutationError.value = apiErrorMessage(error, '文件状态同步失败，请刷新后继续')
      return false
    } finally {
      pipeline.controllers.delete(context.controller)
      releaseRequest(context)
      if (filePipeline?.id === id) {
        filePipeline = null
        filePipelineActive.value = false
      }
    }
  }

  function requestContext(): RequestContext {
    const controller = new AbortController()
    controllers.add(controller)
    return { controller, epoch: lifecycleEpoch }
  }

  function releaseRequest(context: RequestContext): void {
    controllers.delete(context.controller)
  }

  function accepts(context: RequestContext): boolean {
    return context.epoch === lifecycleEpoch && !context.controller.signal.aborted
  }

  function invalidateDetailRead(draftId?: string): void {
    if (draftId !== undefined && activeDetailRead?.draftId !== draftId) return
    detailRequestVersion += 1
    activeDetailRead?.controller.abort()
    activeDetailRead = null
    loadingCurrentDraft.value = false
  }

  function draftStateGeneration(draftId: string): number {
    return draftStateGenerations.get(draftId) ?? 0
  }

  function advanceDraftState(draftId: string): number {
    const generation = draftStateGeneration(draftId) + 1
    draftStateGenerations.set(draftId, generation)
    invalidateDetailRead(draftId)
    return generation
  }

  function draftReadGuard(draftId: string, intentVersion: number): DraftReadGuard {
    return {
      draftId,
      intentVersion,
      stateGeneration: draftStateGeneration(draftId),
    }
  }

  function tombstoneDraft(draftId: string, decrementTotalIfAbsent = false): void {
    const affectsCurrentIntent = currentDraft.value?.id === draftId
      || activeDetailRead?.draftId === draftId
    const previousLength = drafts.value.length
    drafts.value = drafts.value.filter((draft) => draft.id !== draftId)
    const removedCount = previousLength - drafts.value.length
    draftListTotal.value = Math.max(
      0,
      draftListTotal.value - Math.max(removedCount, decrementTotalIfAbsent ? 1 : 0),
    )
    draftCollectionVersion += 1
    if (affectsCurrentIntent) currentIntentVersion += 1
    advanceDraftState(draftId)
    if (currentDraft.value?.id === draftId) {
      previewRequestVersion += 1
      uploadRequestVersion += 1
      activePreviewController?.abort()
      activePreviewController = null
      currentDraft.value = null
      files.value = []
      loadingCurrentDraft.value = false
      downloadingPreview.value = false
      uploading.value = false
      uploadProgress.value = null
      loadError.value = ''
    }
  }

  function acceptsDraftRead(context: RequestContext, guard: DraftReadGuard): boolean {
    return accepts(context)
      && guard.intentVersion === currentIntentVersion
      && guard.stateGeneration === draftStateGeneration(guard.draftId)
  }

  function isCancellation(error: unknown): boolean {
    return axios.isCancel(error)
      || (error instanceof DOMException && error.name === 'AbortError')
  }

  function summaryFromDraft(draft: ReimbursementDraft): ReimbursementDraftSummary {
    return {
      id: draft.id,
      status: draft.status,
      revision: draft.revision,
      department: draft.department,
      templateConfigVersion: draft.templateConfigVersion,
      relatedApprovalCount: draft.relatedApprovalCount,
      expiresAt: draft.expiresAt,
      createdAt: draft.createdAt,
      updatedAt: draft.updatedAt,
      lockedAt: draft.lockedAt,
    }
  }

  function upsertSummary(summary: ReimbursementDraftSummary): void {
    const next = drafts.value.filter((item) => item.id !== summary.id)
    next.unshift(summary)
    drafts.value = next
    draftListTotal.value = Math.max(draftListTotal.value, next.length)
    draftCollectionVersion += 1
  }

  function adoptDraft(
    draft: ReimbursementDraft,
    context: RequestContext,
    expectedDraftId?: string,
  ): boolean {
    if (!accepts(context)) return false
    if (expectedDraftId !== undefined && draft.id !== expectedDraftId) return false
    if (
      currentDraft.value?.id === draft.id
      && currentDraft.value.revision > draft.revision
    ) return false
    currentDraft.value = draft
    upsertSummary(summaryFromDraft(draft))
    return true
  }

  function adoptDraftRead(
    result: { draft: ReimbursementDraft; files: ReimbursementDraftFile[] },
    context: RequestContext,
    guard: DraftReadGuard,
  ): boolean {
    if (result.draft.id !== guard.draftId || !acceptsDraftRead(context, guard)) return false
    if (!adoptDraft(result.draft, context, guard.draftId)) return false
    files.value = result.files
    return true
  }

  function adoptFileRevision(
    draftId: string,
    revision: number,
    context: RequestContext,
  ): boolean {
    if (!accepts(context) || currentDraft.value?.id !== draftId) return false
    if (revision < currentDraft.value.revision) return false
    currentDraft.value = {
      ...currentDraft.value,
      status: 'DRAFT',
      revision,
    }
    draftCollectionVersion += 1
    const index = drafts.value.findIndex((item) => item.id === draftId)
    if (index >= 0) {
      drafts.value[index] = {
        ...drafts.value[index]!,
        status: 'DRAFT',
        revision,
      }
    }
    return true
  }

  function upsertFile(file: ReimbursementDraftFile): void {
    const index = files.value.findIndex((item) => item.id === file.id)
    if (index >= 0) files.value[index] = file
    else files.value = [...files.value, file]
    files.value.sort((left, right) => left.sortOrder - right.sortOrder)
  }

  function clearTravelApprovals(): void {
    travelApprovals.value = []
    travelApprovalQueryWindow.value = null
    travelApprovalTemplateConfigVersion.value = null
  }

  async function loadReimbursementOptions(): Promise<void> {
    const requestVersion = ++reimbursementOptionsRequestVersion
    activeReimbursementOptionsController?.abort()
    const context = requestContext()
    activeReimbursementOptionsController = context.controller
    loadingReimbursementOptions.value = true
    reimbursementOptionsError.value = ''
    try {
      const result = await getOaReimbursementOptions({
        signal: context.controller.signal,
      })
      if (
        !accepts(context)
        || requestVersion !== reimbursementOptionsRequestVersion
      ) return
      if (
        travelApprovalTemplateConfigVersion.value !== null
        && travelApprovalTemplateConfigVersion.value !== result.templateConfigVersion
      ) clearTravelApprovals()
      reimbursementOptions.value = result
    } catch (error) {
      if (
        accepts(context)
        && requestVersion === reimbursementOptionsRequestVersion
        && !isCancellation(error)
      ) {
        reimbursementOptionsError.value = apiErrorMessage(
          error,
          '报销选项加载失败，请重试',
        )
      }
    } finally {
      releaseRequest(context)
      if (requestVersion === reimbursementOptionsRequestVersion) {
        loadingReimbursementOptions.value = false
        if (activeReimbursementOptionsController === context.controller) {
          activeReimbursementOptionsController = null
        }
      }
    }
  }

  async function loadTravelApprovals(
    query: Omit<ListOaTravelApprovalsOptions, 'signal'> = {},
  ): Promise<void> {
    const requestVersion = ++travelApprovalsRequestVersion
    activeTravelApprovalsController?.abort()
    const context = requestContext()
    activeTravelApprovalsController = context.controller
    loadingTravelApprovals.value = true
    travelApprovalsError.value = ''
    try {
      const result = await listOaTravelApprovals({
        ...query,
        signal: context.controller.signal,
      })
      if (
        !accepts(context)
        || requestVersion !== travelApprovalsRequestVersion
      ) return
      if (
        reimbursementOptions.value !== null
        && result.templateConfigVersion !== reimbursementOptions.value.templateConfigVersion
      ) {
        clearTravelApprovals()
        travelApprovalsError.value = '审批模板配置已更新，请重新加载报销选项和出差审批'
        return
      }
      travelApprovals.value = result.items
      travelApprovalQueryWindow.value = result.queryWindow
      travelApprovalTemplateConfigVersion.value = result.templateConfigVersion
    } catch (error) {
      if (
        accepts(context)
        && requestVersion === travelApprovalsRequestVersion
        && !isCancellation(error)
      ) {
        travelApprovalsError.value = apiErrorMessage(
          error,
          '出差审批加载失败，请重试',
        )
      }
    } finally {
      releaseRequest(context)
      if (requestVersion === travelApprovalsRequestVersion) {
        loadingTravelApprovals.value = false
        if (activeTravelApprovalsController === context.controller) {
          activeTravelApprovalsController = null
        }
      }
    }
  }

  async function loadDrafts(offset = 0, limit = 50): Promise<void> {
    const collectionVersion = draftCollectionVersion
    const requestVersion = ++listRequestVersion
    activeListController?.abort()
    const context = requestContext()
    activeListController = context.controller
    loadingDrafts.value = true
    listError.value = ''
    try {
      const result = await listReimbursementDrafts({
        offset,
        limit,
        signal: context.controller.signal,
      })
      if (
        !accepts(context)
        || collectionVersion !== draftCollectionVersion
        || requestVersion !== listRequestVersion
      ) return
      drafts.value = result.items
      draftListOffset.value = result.offset
      draftListLimit.value = result.limit
      draftListTotal.value = result.total
      draftCollectionVersion += 1
    } catch (error) {
      if (accepts(context) && requestVersion === listRequestVersion && !isCancellation(error)) {
        listError.value = apiErrorMessage(error, '报销内容列表加载失败，请重试')
      }
    } finally {
      releaseRequest(context)
      if (requestVersion === listRequestVersion) {
        loadingDrafts.value = false
        if (activeListController === context.controller) activeListController = null
      }
    }
  }

  async function readConsistentDraft(
    draftId: string,
    signal: AbortSignal,
  ): Promise<{ draft: ReimbursementDraft; files: ReimbursementDraftFile[] }> {
    for (let attempt = 0; attempt < CONSISTENT_READ_ATTEMPTS; attempt += 1) {
      const [draft, fileList] = await Promise.all([
        getReimbursementDraft(draftId, { signal }),
        listReimbursementDraftFiles(draftId, { signal }),
      ])
      if (fileList.draftId === draft.id && fileList.revision === draft.revision) {
        return { draft, files: fileList.items }
      }
    }
    throw new Error('报销内容读取期间持续发生变化，请稍后重试')
  }

  async function loadDraft(draftId: string): Promise<void> {
    if (filePipeline) cancelFilePipeline(filePipeline.id)
    const previousDraftId = currentDraft.value?.id
    const intentVersion = previousDraftId === draftId
      ? currentIntentVersion
      : ++currentIntentVersion
    if (previousDraftId !== undefined && previousDraftId !== draftId) {
      currentDraft.value = null
      files.value = []
    }
    invalidateDetailRead()
    const requestVersion = ++detailRequestVersion
    const context = requestContext()
    const guard = draftReadGuard(draftId, intentVersion)
    activeDetailRead = {
      controller: context.controller,
      draftId,
      intentVersion,
      requestVersion,
    }
    loadingCurrentDraft.value = true
    loadError.value = ''
    try {
      const result = await readConsistentDraft(draftId, context.controller.signal)
      if (requestVersion !== detailRequestVersion) return
      adoptDraftRead(result, context, guard)
    } catch (error) {
      const canHandleFailure = (
        accepts(context)
        && intentVersion === currentIntentVersion
        && requestVersion === detailRequestVersion
        && !isCancellation(error)
      )
      if (
        canHandleFailure
        && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_NOT_FOUND'
      ) {
        tombstoneDraft(draftId)
      } else if (canHandleFailure) {
        loadError.value = apiErrorMessage(error, '报销内容加载失败，请重试')
      }
    } finally {
      releaseRequest(context)
      if (requestVersion === detailRequestVersion) {
        loadingCurrentDraft.value = false
        if (activeDetailRead?.controller === context.controller) activeDetailRead = null
      }
    }
  }

  async function refreshCurrentFiles(): Promise<void> {
    if (fileRefreshFlight) return fileRefreshFlight
    const target = requireCurrentDraft()
    const draftId = target.id
    const revision = target.revision
    const intentVersion = currentIntentVersion
    const stateGeneration = draftStateGeneration(draftId)
    const context = requestContext()
    const operation = (async () => {
      try {
        const result = await listReimbursementDraftFiles(draftId, {
          signal: context.controller.signal,
        })
        if (
          accepts(context)
          && currentIntentVersion === intentVersion
          && draftStateGeneration(draftId) === stateGeneration
          && currentDraft.value?.id === draftId
          && currentDraft.value.revision === revision
          && result.draftId === draftId
          && result.revision === revision
        ) {
          files.value = result.items
        }
      } finally {
        releaseRequest(context)
      }
    })().finally(() => {
      if (fileRefreshFlight === operation) fileRefreshFlight = null
    })
    fileRefreshFlight = operation
    return operation
  }

  async function createDraft(input: ReimbursementDraftInput): Promise<ReimbursementDraft> {
    const intentVersion = ++currentIntentVersion
    invalidateDetailRead()
    const context = requestContext()
    let createdDraftId: string | null = null
    pendingMutations.value += 1
    mutationError.value = ''
    revisionConflict.value = false
    try {
      const created = await createReimbursementDraft(input, {
        signal: context.controller.signal,
      })
      createdDraftId = created.id
      if (
        intentVersion === currentIntentVersion
        && adoptDraft(created, context)
      ) files.value = []
      return created
    } catch (error) {
      if (
        accepts(context)
        && intentVersion === currentIntentVersion
        && !isCancellation(error)
      ) {
        mutationError.value = apiErrorMessage(error, '报销内容创建失败，请重试')
      }
      throw error
    } finally {
      releaseRequest(context)
      if (context.epoch === lifecycleEpoch) {
        if (createdDraftId !== null) advanceDraftState(createdDraftId)
        pendingMutations.value -= 1
      }
    }
  }

  function requireCurrentDraft(): ReimbursementDraft {
    if (currentDraft.value === null) throw new Error('报销内容尚未加载，请稍后重试')
    return currentDraft.value
  }

  async function reloadAuthoritativeDraft(
    context: RequestContext,
    guard: DraftReadGuard,
  ): Promise<boolean> {
    const { draftId } = guard
    if (
      !acceptsDraftRead(context, guard)
      || currentDraft.value?.id !== draftId
    ) return false
    const refresh = requestContext()
    try {
      const result = await readConsistentDraft(draftId, refresh.controller.signal)
      if (!acceptsDraftRead(context, guard) || currentDraft.value?.id !== draftId) return false
      return adoptDraftRead(result, refresh, guard)
    } catch (error) {
      if (
        !isCancellation(error)
        && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_NOT_FOUND'
        && accepts(context)
        && accepts(refresh)
      ) {
        tombstoneDraft(draftId)
        return true
      }
      return false
    } finally {
      releaseRequest(refresh)
    }
  }

  async function reloadDraftSummary(
    draftId: string,
    context: RequestContext,
  ): Promise<boolean> {
    if (!accepts(context)) return false
    const stateGeneration = draftStateGeneration(draftId)
    const collectionVersion = draftCollectionVersion
    const refresh = requestContext()
    const acceptsSummaryReload = () => accepts(context)
      && accepts(refresh)
      && stateGeneration === draftStateGeneration(draftId)
      && collectionVersion === draftCollectionVersion
    try {
      const draft = await getReimbursementDraft(draftId, {
        signal: refresh.controller.signal,
      })
      if (
        !acceptsSummaryReload()
        || draft.id !== draftId
      ) return false
      upsertSummary(summaryFromDraft(draft))
      return true
    } catch (error) {
      if (
        acceptsSummaryReload()
        && !isCancellation(error)
        && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_NOT_FOUND'
      ) {
        tombstoneDraft(draftId)
        return true
      }
      return false
    } finally {
      releaseRequest(refresh)
    }
  }

  async function performMutation<T extends RevisionedResult>(
    request: (
      draftId: string,
      expectedRevision: number,
      signal: AbortSignal,
    ) => Promise<T>,
    apply: (result: T, draftId: string, context: RequestContext) => void,
    fallbackMessage: string,
    options: MutationOptions = {},
  ): Promise<T> {
    const target = requireCurrentDraft()
    const draftId = target.id
    let expectedRevision = target.revision
    let canRetryRevisionConflict = options.retryRevisionConflictOnce === true
    const intentVersion = currentIntentVersion
    advanceDraftState(draftId)
    const refreshGuard = draftReadGuard(draftId, intentVersion)
    const context = requestContext()
    options.pipeline?.controllers.add(context.controller)
    pendingMutations.value += 1
    mutationError.value = ''
    revisionConflict.value = false
    try {
      while (true) {
        try {
          const result = await request(draftId, expectedRevision, context.controller.signal)
          if (
            accepts(context)
            && intentVersion === currentIntentVersion
            && currentDraft.value?.id === draftId
          ) {
            apply(result, draftId, context)
          }
          return result
        } catch (error) {
          const cancelled = isCancellation(error)
          const errorCode = apiErrorCode(error)
          const conflict = errorCode === 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT'
          const benign = errorCode !== null && options.benignErrorCodes?.includes(errorCode) === true
          const shouldReload = !benign && !options.pipeline && (conflict || options.reloadAfterFailure === true)
          const refreshed = accepts(context) && !cancelled && shouldReload
            ? await reloadAuthoritativeDraft(
              context,
              refreshGuard,
            )
            : false
          const refreshedRevision = currentDraft.value?.id === draftId
            ? currentDraft.value.revision
            : null
          if (
            conflict
            && refreshed
            && canRetryRevisionConflict
            && refreshedRevision !== null
            && refreshedRevision !== expectedRevision
            && accepts(context)
            && intentVersion === currentIntentVersion
          ) {
            canRetryRevisionConflict = false
            expectedRevision = refreshedRevision
            continue
          }
          if (
            accepts(context)
            && intentVersion === currentIntentVersion
            && currentDraft.value?.id === draftId
            && !cancelled
          ) {
            if (benign) {
              mutationError.value = ''
            } else if (conflict) {
              revisionConflict.value = true
              mutationError.value = refreshed
                ? '报销内容在操作期间已更新，已加载最新内容；请检查后重新操作'
                : '报销内容在操作期间已更新，但最新内容加载失败；请手动重新加载'
            } else {
              const message = apiErrorMessage(error, fallbackMessage)
              mutationError.value = options.reloadAfterFailure
                ? refreshed
                  ? `${message}；已同步报销内容最新状态`
                  : `${message}；最新状态同步失败，请手动重新加载`
                : message
            }
          }
          throw error
        }
      }
    } finally {
      options.pipeline?.controllers.delete(context.controller)
      releaseRequest(context)
      if (context.epoch === lifecycleEpoch) {
        advanceDraftState(draftId)
        pendingMutations.value -= 1
      }
    }
  }

  function mutateCurrent<T extends RevisionedResult>(
    request: (draftId: string, expectedRevision: number, signal: AbortSignal) => Promise<T>,
    apply: (result: T, draftId: string, context: RequestContext) => void,
    fallbackMessage: string,
    options: MutationOptions = {},
  ): Promise<T> {
    if (filePipeline) return Promise.reject(new Error('请等待本批文件处理完成'))
    const draftId = requireCurrentDraft().id
    const epoch = lifecycleEpoch
    const run = () => {
      if (epoch !== lifecycleEpoch || currentDraft.value?.id !== draftId) {
        throw new Error('当前报销已切换，请重新操作')
      }
      return performMutation(request, apply, fallbackMessage, options)
    }
    const operation = mutationQueue ? mutationQueue.then(run) : run()
    const settled = operation.catch(() => undefined).finally(() => {
      if (mutationQueue === settled) mutationQueue = null
    })
    mutationQueue = settled
    return operation
  }

  function applyDraftMutation(
    result: ReimbursementDraft,
    draftId: string,
    context: RequestContext,
  ): void {
    adoptDraft(result, context, draftId)
  }

  function applyFileMutation(
    result: ReimbursementDraftFileMutation,
    draftId: string,
    context: RequestContext,
  ): void {
    if (result.draftId !== draftId) return
    if (adoptFileRevision(draftId, result.revision, context)) upsertFile(result.file)
  }

  async function saveDraft(input: ReimbursementDraftInput): Promise<ReimbursementDraft> {
    return mutateCurrent(
      (draftId, revision, signal) => updateReimbursementDraft(
        draftId,
        revision,
        input,
        { signal },
      ),
      applyDraftMutation,
      '报销内容保存失败，请重试',
    )
  }

  async function deleteDraft(
    draftId: string,
    expectedRevision: number,
  ): Promise<ReimbursementDraftDeletion> {
    const intentVersion = currentIntentVersion
    const collectionVersion = draftCollectionVersion
    advanceDraftState(draftId)
    const refreshGuard = draftReadGuard(draftId, intentVersion)
    const context = requestContext()
    pendingMutations.value += 1
    mutationError.value = ''
    revisionConflict.value = false
    try {
      const result = await deleteReimbursementDraft(
        draftId,
        expectedRevision,
        { signal: context.controller.signal },
      )
      if (result.deletedDraftId !== draftId) {
        throw new Error('报销内容删除响应无效，请刷新后确认报销内容状态')
      }
      if (!accepts(context)) return result
      tombstoneDraft(draftId, collectionVersion === draftCollectionVersion)
      return result
    } catch (error) {
      const cancelled = isCancellation(error)
      const conflict = apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT'
      const targetsCurrentDraft = intentVersion === currentIntentVersion
        && currentDraft.value?.id === draftId
      const refreshed = !cancelled
        ? targetsCurrentDraft
          ? await reloadAuthoritativeDraft(context, refreshGuard)
          : await reloadDraftSummary(draftId, context)
        : false
      if (accepts(context) && !cancelled) {
        if (conflict) {
          revisionConflict.value = true
          mutationError.value = refreshed
            ? '报销内容在删除期间已更新，已加载最新内容；请检查后重新删除'
            : '报销内容在删除期间已更新，未删除报销内容；请刷新后重试'
        } else {
          const message = apiErrorMessage(error, '报销内容删除失败，请重试')
          mutationError.value = refreshed
            ? `${message}；已同步报销内容最新状态`
            : `${message}；最新状态同步失败，请手动重新加载`
        }
      }
      throw error
    } finally {
      releaseRequest(context)
      if (context.epoch === lifecycleEpoch) {
        advanceDraftState(draftId)
        pendingMutations.value -= 1
      }
    }
  }

  async function deleteCurrentDraft(): Promise<ReimbursementDraftDeletion> {
    const target = requireCurrentDraft()
    return deleteDraft(target.id, target.revision)
  }

  async function saveRelatedApprovals(
    selections: ReimbursementRelatedApprovalSelection[],
  ): Promise<ReimbursementDraft> {
    return mutateCurrent(
      (draftId, revision, signal) => replaceReimbursementRelatedApprovals(
        draftId,
        revision,
        selections,
        { signal },
      ),
      applyDraftMutation,
      '关联出差审批保存失败，请重试',
    )
  }

  async function markReviewReady(): Promise<ReimbursementDraft> {
    return mutateCurrent(
      (draftId, revision, signal) => markReimbursementDraftReviewReady(
        draftId,
        revision,
        { signal },
      ),
      applyDraftMutation,
      '报销内容检查失败，请核对内容后重试',
      { reloadAfterFailure: true },
    )
  }

  async function uploadFile(
    file: File,
    role: ReimbursementDraftFileRole = 'EXPENSE_SOURCE',
    attachmentKind: ReimbursementDraftFile['attachmentKind'] = 'other',
    autoClassify = false,
    pipelineId?: number,
  ): Promise<ReimbursementDraftFileMutation> {
    if (uploading.value) throw new Error('已有文件正在上传，请稍后再试')
    const pipeline = pipelineId === undefined ? null : requireFilePipeline(pipelineId)
    if (pipeline && !autoClassify) throw new Error('并行上传只支持本批自动分类材料')
    if (pipeline) pipeline.uploading = true
    const requestVersion = ++uploadRequestVersion
    uploading.value = true
    uploadProgress.value = 0
    try {
      const result = await (pipeline ? performMutation : mutateCurrent)(
        (draftId, revision, signal) => uploadReimbursementDraftFile(
          draftId,
          revision,
          file,
          {
            role,
            attachmentKind,
            ...(autoClassify ? { autoClassify: true } : {}),
            signal,
            onProgress: (percent) => {
              if (
                requestVersion === uploadRequestVersion
                && !signal.aborted
              ) uploadProgress.value = percent
            },
          },
        ),
        pipeline ? (result, draftId, context) => applyPipelineFileMutation(result, draftId, context, pipeline) : applyFileMutation,
        '文件上传失败，请检查格式后重试',
        pipeline
          ? { pipeline, benignErrorCodes: ['REIMBURSEMENT_FILE_DUPLICATE'] }
          : { reloadAfterFailure: true, benignErrorCodes: ['REIMBURSEMENT_FILE_DUPLICATE'] },
      )
      if (pipeline && isFilePipelineActive(pipeline.id)) pipeline.uploadedIds.add(result.file.id)
      return result
    } catch (error) {
      if (
        pipeline
        && isFilePipelineActive(pipeline.id)
        && apiErrorCode(error) !== 'REIMBURSEMENT_FILE_DUPLICATE'
      ) {
        pipeline.needsRefresh = true
        await refreshPipelineRevision(pipeline).catch(() => undefined)
      }
      throw error
    } finally {
      if (pipeline) pipeline.uploading = false
      if (requestVersion === uploadRequestVersion) {
        uploading.value = false
        uploadProgress.value = null
      }
    }
  }

  async function updateFile(
    fileId: string,
    change: Omit<ReimbursementDraftFileUpdate, 'expectedRevision'>,
  ): Promise<ReimbursementDraftFileMutation> {
    return mutateCurrent(
      (draftId, revision, signal) => updateReimbursementDraftFile(
        draftId,
        fileId,
        { expectedRevision: revision, ...change },
        { signal },
      ),
      applyFileMutation,
      '文件信息保存失败，请重试',
    )
  }

  async function removeFile(fileId: string): Promise<ReimbursementDraftFileDeletion> {
    return mutateCurrent(
      (draftId, revision, signal) => deleteReimbursementDraftFile(
        draftId,
        fileId,
        revision,
        { signal },
      ),
      (result, draftId, context) => {
        if (result.draftId !== draftId) return
        if (adoptFileRevision(draftId, result.revision, context)) {
          files.value = files.value.filter((file) => file.id !== result.deletedFileId)
        }
      },
      '文件删除失败，请重试',
      { reloadAfterFailure: true },
    )
  }

  async function clearFiles(): Promise<Awaited<ReturnType<typeof clearReimbursementDraftFiles>>> {
    return mutateCurrent(
      (draftId, revision, signal) => clearReimbursementDraftFiles(draftId, revision, { signal }),
      (result, draftId, context) => {
        if (result.draftId !== draftId) return
        if (adoptFileRevision(draftId, result.revision, context)) {
          const removed = new Set(result.deletedFileIds)
          files.value = files.value.filter((file) => !removed.has(file.id))
        }
      },
      '清空文件失败，请重试',
      { reloadAfterFailure: true },
    )
  }

  async function recognizeFile(
    fileId: string,
    tripYear?: number,
    pipelineId?: number,
  ): Promise<ReimbursementDraftFileMutation> {
    const pipeline = pipelineId === undefined ? null : requireFilePipeline(pipelineId)
    if (pipeline) {
      if (pipeline.recognizing || !pipeline.uploadedIds.has(fileId) || pipeline.recognizedIds.has(fileId)) {
        throw new Error('本批仅可逐份识别已上传的新文件')
      }
      pipeline.recognizing = true
      pipeline.recognizedIds.add(fileId)
    }
    try {
      return await (pipeline ? performMutation : mutateCurrent)(
        (draftId, revision, signal) => recognizeReimbursementDraftFile(
          draftId,
          fileId,
          {
            expectedRevision: revision,
            ...(tripYear === undefined ? {} : { tripYear }),
            ...(pipeline ? { allowUploadOverlap: true } : {}),
          },
          { signal },
        ),
        pipeline ? (result, draftId, context) => applyPipelineFileMutation(result, draftId, context, pipeline) : applyFileMutation,
        '票据识别失败，请重试或手工填写',
        pipeline ? { pipeline } : {
          reloadAfterFailure: true,
          retryRevisionConflictOnce: true,
        },
      )
    } catch (error) {
      if (pipeline) pipeline.needsRefresh = true
      throw error
    } finally {
      if (pipeline) pipeline.recognizing = false
    }
  }

  async function downloadExcelPreview(): Promise<void> {
    const target = requireCurrentDraft()
    const draftId = target.id
    const revision = target.revision
    const intentVersion = currentIntentVersion
    const refreshGuard = draftReadGuard(draftId, intentVersion)
    const requestVersion = ++previewRequestVersion
    activePreviewController?.abort()
    const context = requestContext()
    activePreviewController = context.controller
    downloadingPreview.value = true
    mutationError.value = ''
    revisionConflict.value = false
    try {
      const preview = await getReimbursementDraftExcelPreview(draftId, revision, {
        signal: context.controller.signal,
      })
      if (
        accepts(context)
        && intentVersion === currentIntentVersion
        && requestVersion === previewRequestVersion
        && currentDraft.value?.id === draftId
        && currentDraft.value.revision === revision
      ) {
        downloadBlob(preview.blob, preview.filename)
      }
    } catch (error) {
      if (
        accepts(context)
        && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT'
      ) {
        const refreshed = await reloadAuthoritativeDraft(
          context,
          refreshGuard,
        )
        if (
          accepts(context)
          && intentVersion === currentIntentVersion
          && requestVersion === previewRequestVersion
          && currentDraft.value?.id === draftId
        ) {
          revisionConflict.value = true
          mutationError.value = refreshed
            ? '报销内容在操作期间已更新，已加载最新内容；请检查后重新操作'
            : '报销内容在操作期间已更新，但最新内容加载失败；请手动重新加载'
        }
      } else if (
        accepts(context)
        && intentVersion === currentIntentVersion
        && requestVersion === previewRequestVersion
        && currentDraft.value?.id === draftId
        && !isCancellation(error)
      ) {
        mutationError.value = apiErrorMessage(error, 'Excel 预览生成失败，请重试')
      }
      throw error
    } finally {
      releaseRequest(context)
      if (
        context.epoch === lifecycleEpoch
        && requestVersion === previewRequestVersion
      ) {
        downloadingPreview.value = false
        if (activePreviewController === context.controller) activePreviewController = null
      }
    }
  }

  async function openExcelPreviewInDingTalk(): Promise<void> {
    const target = requireCurrentDraft()
    const draftId = target.id
    const revision = target.revision
    const intentVersion = currentIntentVersion
    const refreshGuard = draftReadGuard(draftId, intentVersion)
    const requestVersion = ++previewRequestVersion
    activePreviewController?.abort()
    const context = requestContext()
    activePreviewController = context.controller
    downloadingPreview.value = true
    mutationError.value = ''
    revisionConflict.value = false
    try {
      const ticket = await requestReimbursementDraftExcelPreviewTicket(draftId, revision, {
        signal: context.controller.signal,
      })
      if (
        !accepts(context)
        || intentVersion !== currentIntentVersion
        || requestVersion !== previewRequestVersion
        || currentDraft.value?.id !== draftId
        || currentDraft.value.revision !== revision
      ) return
      await downloadAndOpenDingTalkDocument({
        url: new URL(ticket.downloadUrl, window.location.origin).href,
        headers: { 'X-Reimbursement-Download-Token': ticket.downloadToken },
        fileType: ticket.fileType,
      })
    } catch (error) {
      if (
        accepts(context)
        && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT'
      ) {
        const refreshed = await reloadAuthoritativeDraft(context, refreshGuard)
        if (
          accepts(context)
          && intentVersion === currentIntentVersion
          && requestVersion === previewRequestVersion
          && currentDraft.value?.id === draftId
        ) {
          revisionConflict.value = true
          mutationError.value = refreshed
            ? '报销内容在操作期间已更新，已加载最新内容；请检查后重新操作'
            : '报销内容在操作期间已更新，但最新内容加载失败；请手动重新加载'
        }
      } else if (
        accepts(context)
        && intentVersion === currentIntentVersion
        && requestVersion === previewRequestVersion
        && currentDraft.value?.id === draftId
        && !isCancellation(error)
      ) {
        mutationError.value = apiErrorMessage(error, 'Excel 预览打开失败，请重试')
      }
      throw error
    } finally {
      releaseRequest(context)
      if (
        context.epoch === lifecycleEpoch
        && requestVersion === previewRequestVersion
      ) {
        downloadingPreview.value = false
        if (activePreviewController === context.controller) activePreviewController = null
      }
    }
  }

  function reset(): void {
    if (filePipeline) cancelFilePipeline(filePipeline.id)
    lifecycleEpoch += 1
    mutationQueue = null
    currentIntentVersion += 1
    draftCollectionVersion += 1
    listRequestVersion += 1
    detailRequestVersion += 1
    reimbursementOptionsRequestVersion += 1
    travelApprovalsRequestVersion += 1
    previewRequestVersion += 1
    uploadRequestVersion += 1
    for (const controller of controllers) controller.abort()
    controllers.clear()
    draftStateGenerations.clear()
    activeListController = null
    activeDetailRead = null
    activeReimbursementOptionsController = null
    activeTravelApprovalsController = null
    activePreviewController = null
    drafts.value = []
    draftListOffset.value = 0
    draftListLimit.value = 50
    draftListTotal.value = 0
    reimbursementOptions.value = null
    clearTravelApprovals()
    currentDraft.value = null
    files.value = []
    loadingDrafts.value = false
    loadingCurrentDraft.value = false
    loadingReimbursementOptions.value = false
    loadingTravelApprovals.value = false
    pendingMutations.value = 0
    downloadingPreview.value = false
    uploading.value = false
    processingFiles.value = false
    uploadProgress.value = null
    listError.value = ''
    loadError.value = ''
    reimbursementOptionsError.value = ''
    travelApprovalsError.value = ''
    mutationError.value = ''
    revisionConflict.value = false
  }

  return {
    drafts,
    draftListOffset,
    draftListLimit,
    draftListTotal,
    reimbursementOptions,
    travelApprovals,
    travelApprovalQueryWindow,
    travelApprovalTemplateConfigVersion,
    currentDraft,
    files,
    loadingDrafts,
    loadingCurrentDraft,
    loadingReimbursementOptions,
    loadingTravelApprovals,
    pendingMutations,
    downloadingPreview,
    uploading,
    processingFiles,
    uploadProgress,
    listError,
    loadError,
    reimbursementOptionsError,
    travelApprovalsError,
    mutationError,
    revisionConflict,
    busy,
    currentRevision,
    loadReimbursementOptions,
    loadTravelApprovals,
    loadDrafts,
    loadDraft,
    refreshCurrentFiles,
    createDraft,
    saveDraft,
    deleteDraft,
    deleteCurrentDraft,
    saveRelatedApprovals,
    markReviewReady,
    uploadFile,
    beginFilePipeline,
    isFilePipelineActive,
    cancelFilePipeline,
    finishFilePipeline,
    updateFile,
    removeFile,
    clearFiles,
    recognizeFile,
    downloadExcelPreview,
    openExcelPreviewInDingTalk,
    reset,
  }
})

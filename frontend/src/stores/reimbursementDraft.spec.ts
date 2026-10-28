import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { downloadBlob } from '@/api/excel'
import {
  createReimbursementDraft,
  deleteReimbursementDraft,
  deleteReimbursementDraftFile,
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
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { downloadAndOpenDingTalkDocument } from '@/utils/dingtalk'
import type {
  ReimbursementDraft,
  ReimbursementDraftFile,
  ReimbursementDraftInput,
} from '@/types/reimbursements'

vi.mock('@/api/excel', () => ({ downloadBlob: vi.fn() }))
vi.mock('@/api/reimbursements', () => ({
  createReimbursementDraft: vi.fn(),
  deleteReimbursementDraft: vi.fn(),
  deleteReimbursementDraftFile: vi.fn(),
  getReimbursementDraft: vi.fn(),
  getReimbursementDraftExcelPreview: vi.fn(),
  getOaReimbursementOptions: vi.fn(),
  listOaTravelApprovals: vi.fn(),
  listReimbursementDraftFiles: vi.fn(),
  listReimbursementDrafts: vi.fn(),
  markReimbursementDraftReviewReady: vi.fn(),
  recognizeReimbursementDraftFile: vi.fn(),
  requestReimbursementDraftExcelPreviewTicket: vi.fn(),
  replaceReimbursementRelatedApprovals: vi.fn(),
  updateReimbursementDraft: vi.fn(),
  updateReimbursementDraftFile: vi.fn(),
  uploadReimbursementDraftFile: vi.fn(),
}))
vi.mock('@/utils/dingtalk', () => ({ downloadAndOpenDingTalkDocument: vi.fn() }))

const input: ReimbursementDraftInput = {
  ocrDispositionVersion: 1,
  companyValue: '北京',
  budgetCodeValue: '26007',
  project: { mode: 'manual', text: '示例项目' },
  trip: null,
  dismissedOcrFileIds: [],
  items: [{
    category: 'other',
    date: '2026-09-01',
    displayDate: '2026-09-01',
    description: '打车费',
    amount: '44.89',
    receiptCount: 1,
  }],
}

function draft(id = 'draft-1', revision = 1): ReimbursementDraft {
  return {
    id,
    status: 'DRAFT',
    revision,
    department: { id: '100', name: '测试部门' },
    templateConfigVersion: 12,
    relatedApprovalCount: 0,
    expiresAt: '2026-10-04T00:00:00Z',
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: `2026-09-04T00:00:0${revision}Z`,
    lockedAt: null,
    template: {
      processCode: 'PROC-REIMBURSEMENT',
      configVersion: 12,
      schemaFingerprint: 'a'.repeat(64),
    },
    input,
    totals: {
      expenseTotal: '44.89',
      subsidyTotal: '0.00',
      totalAmount: '44.89',
      receiptCount: 1,
      uppercaseAmount: '肆拾肆元捌角玖分',
      subsidy: null,
    },
    relatedApprovals: [],
    relatedApprovalSummary: null,
  }
}

function serverFile(id = 'file-1'): ReimbursementDraftFile {
  return {
    id,
    name: '票据.jpg',
    role: 'EXPENSE_SOURCE',
    attachmentKind: 'other',
    sortOrder: 0,
    status: 'ACTIVE',
    mediaType: 'image/jpeg',
    sizeBytes: 5,
    ocrStatus: 'NOT_REQUESTED',
    ocrResult: null,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function responseError(code: string, message: string, status = 422): Error {
  return Object.assign(new Error(message), {
    isAxiosError: true,
    response: { status, data: { error: { code, message } } },
  })
}

describe('persistent reimbursement draft store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(listReimbursementDrafts).mockResolvedValue({
      items: [], offset: 0, limit: 50, total: 0,
    })
  })

  it('aborts superseded OA option loads and discards their late responses', async () => {
    const first = deferred<Awaited<ReturnType<typeof getOaReimbursementOptions>>>()
    const second = deferred<Awaited<ReturnType<typeof getOaReimbursementOptions>>>()
    const signals: AbortSignal[] = []
    vi.mocked(getOaReimbursementOptions)
      .mockImplementationOnce((options) => {
        signals.push(options!.signal!)
        return first.promise
      })
      .mockImplementationOnce((options) => {
        signals.push(options!.signal!)
        return second.promise
      })
    const store = useReimbursementDraftStore()

    const stale = store.loadReimbursementOptions()
    const current = store.loadReimbursementOptions()
    second.resolve({
      templateConfigVersion: 2,
      reimbursementProcessCode: 'PROC-2',
      companyOptions: [],
      budgetCodeOptions: [],
      travelProfiles: [],
    })
    await current
    first.resolve({
      templateConfigVersion: 1,
      reimbursementProcessCode: 'PROC-1',
      companyOptions: [],
      budgetCodeOptions: [],
      travelProfiles: [],
    })
    await stale

    expect(signals[0]?.aborted).toBe(true)
    expect(store.reimbursementOptions?.templateConfigVersion).toBe(2)
    expect(store.loadingReimbursementOptions).toBe(false)
  })

  it('keeps only the newest cancellable travel approval search', async () => {
    const first = deferred<Awaited<ReturnType<typeof listOaTravelApprovals>>>()
    const second = deferred<Awaited<ReturnType<typeof listOaTravelApprovals>>>()
    const signals: AbortSignal[] = []
    vi.mocked(listOaTravelApprovals)
      .mockImplementationOnce((options) => {
        signals.push(options!.signal!)
        return first.promise
      })
      .mockImplementationOnce((options) => {
        signals.push(options!.signal!)
        return second.promise
      })
    const store = useReimbursementDraftStore()

    const stale = store.loadTravelApprovals({ query: '旧查询' })
    const current = store.loadTravelApprovals({ query: '新查询' })
    second.resolve({
      templateConfigVersion: 2,
      queryWindow: { from: '2026-08-01', to: '2026-09-04' },
      items: [{
        processInstanceId: 'new-instance',
        profileKey: 'business',
        profileDisplayName: '商务出差',
        sourceProcessCode: 'PROC-TRAVEL',
        travelTypeOption: { value: 'business', label: '商务出差', key: null },
        title: '新查询结果',
        businessId: 'BIZ-2',
        startDate: '2026-09-01',
        endDate: '2026-09-02',
        createdAt: '2026-09-01T00:00:00Z',
        finishedAt: '2026-09-02T00:00:00Z',
      }],
    })
    await current
    first.resolve({
      templateConfigVersion: 1,
      queryWindow: { from: '2026-07-01', to: '2026-08-01' },
      items: [],
    })
    await stale

    expect(signals[0]?.aborted).toBe(true)
    expect(listOaTravelApprovals).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ query: '新查询', signal: expect.any(AbortSignal) }),
    )
    expect(store.travelApprovals.map((approval) => approval.processInstanceId))
      .toEqual(['new-instance'])
    expect(store.travelApprovalTemplateConfigVersion).toBe(2)
    expect(store.loadingTravelApprovals).toBe(false)
  })

  it('keeps only the authoritative server file record after upload', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 2,
      file: serverFile(),
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    const localFile = new File(['bytes'], 'local-selection.jpg', { type: 'image/jpeg' })

    await store.uploadFile(localFile)

    expect(uploadReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      1,
      localFile,
      expect.objectContaining({ role: 'EXPENSE_SOURCE' }),
    )
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files).toEqual([serverFile()])
    expect(store.files[0]).not.toHaveProperty('file')
    expect(store.files[0]!.name).not.toBe(localFile.name)
  })

  it('aborts the old detail request and discards its late response', async () => {
    const firstDraft = deferred<ReimbursementDraft>()
    const firstFiles = deferred<{
      draftId: string
      revision: number
      items: ReimbursementDraftFile[]
    }>()
    const seenSignals: AbortSignal[] = []
    vi.mocked(getReimbursementDraft).mockImplementation((id, options) => {
      seenSignals.push(options!.signal!)
      return id === 'draft-a' ? firstDraft.promise : Promise.resolve(draft('draft-b', 2))
    })
    vi.mocked(listReimbursementDraftFiles).mockImplementation((id) => (
      id === 'draft-a'
        ? firstFiles.promise
        : Promise.resolve({ draftId: 'draft-b', revision: 2, items: [serverFile('file-b')] })
    ))
    const store = useReimbursementDraftStore()

    const openingA = store.loadDraft('draft-a')
    const openingB = store.loadDraft('draft-b')
    await openingB
    firstDraft.resolve(draft('draft-a'))
    firstFiles.resolve({ draftId: 'draft-a', revision: 1, items: [serverFile('file-a')] })
    await openingA

    expect(seenSignals[0]!.aborted).toBe(true)
    expect(store.currentDraft?.id).toBe('draft-b')
    expect(store.files.map((file) => file.id)).toEqual(['file-b'])
  })

  it('isolates the previous draft and tombstones a missing target while switching', async () => {
    vi.mocked(getReimbursementDraft).mockResolvedValueOnce(draft('draft-a'))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: 'draft-a', revision: 1, items: [serverFile('file-a')],
    })
    const store = useReimbursementDraftStore()
    await store.loadDraft('draft-a')
    store.drafts = [draft('draft-a'), draft('draft-b')]
    store.draftListTotal = 2
    vi.mocked(getReimbursementDraft).mockRejectedValueOnce(responseError(
      'REIMBURSEMENT_DRAFT_NOT_FOUND',
      '草稿不存在',
      404,
    ))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: 'draft-b', revision: 1, items: [],
    })

    const openingB = store.loadDraft('draft-b')

    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-a', 'draft-b'])
    await openingB
    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-a'])
    expect(store.draftListTotal).toBe(1)
  })

  it('tombstones the current draft when an ordinary reload confirms it is missing', async () => {
    vi.mocked(getReimbursementDraft).mockResolvedValueOnce(draft('draft-a'))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: 'draft-a', revision: 1, items: [serverFile('file-a')],
    })
    const store = useReimbursementDraftStore()
    await store.loadDraft('draft-a')
    vi.mocked(getReimbursementDraft).mockRejectedValueOnce(responseError(
      'REIMBURSEMENT_DRAFT_NOT_FOUND',
      '草稿不存在',
      404,
    ))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: 'draft-a', revision: 1, items: [serverFile('stale-file')],
    })

    await store.loadDraft('draft-a')

    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts).toEqual([])
    expect(store.draftListTotal).toBe(0)
  })

  it('does not let an older create response replace a draft opened afterward', async () => {
    const created = deferred<ReimbursementDraft>()
    vi.mocked(createReimbursementDraft).mockReturnValue(created.promise)
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-opened', 3))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-opened', revision: 3, items: [serverFile('opened-file')],
    })
    const store = useReimbursementDraftStore()

    const creating = store.createDraft(input)
    await store.loadDraft('draft-opened')
    created.resolve(draft('draft-created', 1))
    await creating

    expect(store.currentDraft?.id).toBe('draft-opened')
    expect(store.files.map((file) => file.id)).toEqual(['opened-file'])
  })

  it('keeps a pending draft load alive when an older create settles first', async () => {
    const created = deferred<ReimbursementDraft>()
    const openedDraft = deferred<ReimbursementDraft>()
    const openedFiles = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(createReimbursementDraft).mockReturnValue(created.promise)
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options?.signal
      return openedDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(openedFiles.promise)
    const store = useReimbursementDraftStore()

    const creating = store.createDraft(input)
    const opening = store.loadDraft('draft-opened')
    created.resolve(draft('draft-created'))
    await creating

    expect(detailSignal?.aborted).toBe(false)
    openedDraft.resolve(draft('draft-opened', 3))
    openedFiles.resolve({
      draftId: 'draft-opened', revision: 3, items: [serverFile('opened-file')],
    })
    await opening

    expect(store.currentDraft?.id).toBe('draft-opened')
    expect(store.files.map((file) => file.id)).toEqual(['opened-file'])
  })

  it('discards a mutation response after the user opens another draft', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-a'))
    const saved = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValue(saved.promise)
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-b', 4))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-b', revision: 4, items: [serverFile('file-b')],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const saving = store.saveDraft(input)
    await store.loadDraft('draft-b')
    saved.resolve(draft('draft-a', 2))
    await saving

    expect(store.currentDraft?.id).toBe('draft-b')
    expect(store.currentDraft?.revision).toBe(4)
    expect(store.files.map((file) => file.id)).toEqual(['file-b'])
  })

  it('keeps a pending new-draft load alive when an older mutation settles first', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-a'))
    const saved = deferred<ReimbursementDraft>()
    const openedDraft = deferred<ReimbursementDraft>()
    const openedFiles = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(updateReimbursementDraft).mockReturnValue(saved.promise)
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options?.signal
      return openedDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(openedFiles.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const saving = store.saveDraft(input)
    const opening = store.loadDraft('draft-b')
    saved.resolve(draft('draft-a', 2))
    await saving

    expect(detailSignal?.aborted).toBe(false)
    openedDraft.resolve(draft('draft-b', 4))
    openedFiles.resolve({
      draftId: 'draft-b', revision: 4, items: [serverFile('file-b')],
    })
    await opening

    expect(store.currentDraft?.id).toBe('draft-b')
    expect(store.currentDraft?.revision).toBe(4)
    expect(store.files.map((file) => file.id)).toEqual(['file-b'])
  })

  it('aborts a detail read started before a mutation and rejects its equal-revision files', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const staleDraft = deferred<ReimbursementDraft>()
    const staleFiles = deferred<{
      draftId: string
      revision: number
      items: ReimbursementDraftFile[]
    }>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options!.signal
      return staleDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(staleFiles.promise)
    vi.mocked(deleteReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1', revision: 2, deletedFileId: 'file-1',
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile()]

    const loading = store.loadDraft('draft-1')
    await store.removeFile('file-1')

    expect(detailSignal?.aborted).toBe(true)
    staleDraft.resolve(draft('draft-1', 2))
    staleFiles.resolve({
      draftId: 'draft-1',
      revision: 2,
      items: [{ ...serverFile(), status: 'DELETING' }],
    })
    await loading

    expect(deleteReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files).toEqual([])
    expect(store.loadingCurrentDraft).toBe(false)
  })

  it('does not let an equal-revision WRITING snapshot replace a completed upload', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const staleDraft = deferred<ReimbursementDraft>()
    const staleFiles = deferred<{
      draftId: string
      revision: number
      items: ReimbursementDraftFile[]
    }>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options!.signal
      return staleDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(staleFiles.promise)
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1', revision: 2, file: serverFile(),
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const loading = store.loadDraft('draft-1')
    await store.uploadFile(new File(['bytes'], '票据.jpg'))

    expect(detailSignal?.aborted).toBe(true)
    staleDraft.resolve(draft('draft-1', 2))
    staleFiles.resolve({
      draftId: 'draft-1',
      revision: 2,
      items: [{ ...serverFile(), status: 'WRITING' }],
    })
    await loading

    expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files[0]?.status).toBe('ACTIVE')
  })

  it('aborts a detail read started during a mutation when that mutation finishes', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const ocr = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    const staleDraft = deferred<ReimbursementDraft>()
    const staleFiles = deferred<{
      draftId: string
      revision: number
      items: ReimbursementDraftFile[]
    }>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValue(ocr.promise)
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options!.signal
      return staleDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(staleFiles.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile()]

    const recognizing = store.recognizeFile('file-1', 2026)
    const loading = store.loadDraft('draft-1')
    ocr.resolve({
      draftId: 'draft-1',
      revision: 2,
      file: { ...serverFile(), ocrStatus: 'COMPLETE' },
    })
    await recognizing

    expect(detailSignal?.aborted).toBe(true)
    staleDraft.resolve(draft('draft-1', 2))
    staleFiles.resolve({
      draftId: 'draft-1',
      revision: 2,
      items: [{ ...serverFile(), ocrStatus: 'RUNNING' }],
    })
    await loading

    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files[0]?.ocrStatus).toBe('COMPLETE')
    expect(store.loadingCurrentDraft).toBe(false)
  })

  it('serializes the next mutation behind a failed operation and uses the refreshed revision', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const staleDraft = deferred<ReimbursementDraft>()
    const staleFiles = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    const failure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '文件物理清理失败',
      500,
    )
    vi.mocked(deleteReimbursementDraftFile).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockReturnValue(staleDraft.promise)
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(staleFiles.promise)
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 3,
      file: { ...serverFile(), ocrStatus: 'COMPLETE' },
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile()]

    const removing = store.removeFile('file-1')
    void removing.catch(() => undefined)
    await vi.waitFor(() => expect(getReimbursementDraft).toHaveBeenCalledOnce())
    const recognizing = store.recognizeFile('file-1', 2026)
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    staleDraft.resolve(draft('draft-1', 2))
    staleFiles.resolve({
      draftId: 'draft-1',
      revision: 2,
      items: [{ ...serverFile(), status: 'DELETING' }],
    })
    await expect(removing).rejects.toBe(failure)
    await recognizing

    expect(recognizeReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1', 'file-1', { expectedRevision: 2, tripYear: 2026 }, expect.any(Object),
    )
    expect(store.currentDraft?.revision).toBe(3)
    expect(store.files[0]?.status).toBe('ACTIVE')
    expect(store.files[0]?.ocrStatus).toBe('COMPLETE')
  })

  it('retries an inconsistent read only once and adopts a matching snapshot', async () => {
    vi.mocked(getReimbursementDraft)
      .mockResolvedValueOnce(draft('draft-1', 1))
      .mockResolvedValueOnce(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, items: [serverFile()] })
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, items: [serverFile()] })
    const store = useReimbursementDraftStore()

    await store.loadDraft('draft-1')

    expect(getReimbursementDraft).toHaveBeenCalledTimes(2)
    expect(listReimbursementDraftFiles).toHaveBeenCalledTimes(2)
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files).toEqual([serverFile()])
  })

  it('discards a list snapshot made stale by a completed mutation', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-1', 1))
    const listing = deferred<{
      items: ReimbursementDraft[]
      offset: number
      limit: number
      total: number
    }>()
    vi.mocked(listReimbursementDrafts).mockReturnValue(listing.promise)
    vi.mocked(updateReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const loading = store.loadDrafts()
    await store.saveDraft(input)
    listing.resolve({
      items: [draft('draft-1', 1)], offset: 0, limit: 50, total: 1,
    })
    await loading

    expect(store.currentDraft?.revision).toBe(2)
    expect(store.drafts[0]?.revision).toBe(2)
  })

  it('refreshes after a revision conflict without replaying the write', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(updateReimbursementDraft).mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          error: {
            code: 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
            message: '报销内容版本已更新，请刷新后重试',
          },
        },
      },
    })
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 3))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 3, items: [serverFile()],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.saveDraft(input)).rejects.toBeTruthy()

    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(3)
    expect(store.revisionConflict).toBe(true)
    expect(store.mutationError).toContain('已加载最新内容')
  })

  it('deletes one expected revision and removes or clears its local state', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(deleteReimbursementDraft).mockResolvedValue({ deletedDraftId: 'draft-1' })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile()]

    await expect(store.deleteCurrentDraft()).resolves.toEqual({
      deletedDraftId: 'draft-1',
    })

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(deleteReimbursementDraft).toHaveBeenCalledWith(
      'draft-1',
      1,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(store.drafts).toEqual([])
    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
  })

  it('keeps a pending new-draft load alive when an older delete settles first', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-a'))
    const deleted = deferred<Awaited<ReturnType<typeof deleteReimbursementDraft>>>()
    const openedDraft = deferred<ReimbursementDraft>()
    const openedFiles = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    let detailSignal: AbortSignal | undefined
    vi.mocked(deleteReimbursementDraft).mockReturnValue(deleted.promise)
    vi.mocked(getReimbursementDraft).mockImplementation((_draftId, options) => {
      detailSignal = options?.signal
      return openedDraft.promise
    })
    vi.mocked(listReimbursementDraftFiles).mockReturnValue(openedFiles.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const deleting = store.deleteCurrentDraft()
    const opening = store.loadDraft('draft-b')
    deleted.resolve({ deletedDraftId: 'draft-a' })
    await deleting

    expect(detailSignal?.aborted).toBe(false)
    openedDraft.resolve(draft('draft-b', 4))
    openedFiles.resolve({
      draftId: 'draft-b', revision: 4, items: [serverFile('file-b')],
    })
    await opening

    expect(store.currentDraft?.id).toBe('draft-b')
    expect(store.files.map((file) => file.id)).toEqual(['file-b'])
  })

  it('clears a draft opened after its non-current deletion succeeds', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const deletion = deferred<Awaited<ReturnType<typeof deleteReimbursementDraft>>>()
    vi.mocked(deleteReimbursementDraft).mockReturnValue(deletion.promise)
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-target'))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-target', revision: 1, items: [serverFile('target-file')],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-target')]
    store.draftListTotal = 2

    const deleting = store.deleteDraft('draft-target', 1)
    await store.loadDraft('draft-target')
    expect(store.currentDraft?.id).toBe('draft-target')

    deletion.resolve({ deletedDraftId: 'draft-target' })
    await deleting

    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
  })

  it('does not decrement an authoritative list total twice when delete settles later', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const deletion = deferred<Awaited<ReturnType<typeof deleteReimbursementDraft>>>()
    vi.mocked(deleteReimbursementDraft).mockReturnValue(deletion.promise)
    vi.mocked(listReimbursementDrafts).mockResolvedValue({
      items: [draft('draft-current')], offset: 0, limit: 50, total: 1,
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-target')]
    store.draftListTotal = 2

    const deleting = store.deleteDraft('draft-target', 1)
    await store.loadDrafts()
    expect(store.draftListTotal).toBe(1)

    deletion.resolve({ deletedDraftId: 'draft-target' })
    await deleting

    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
  })

  it('reloads a conflicting draft deletion once without replaying DELETE', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const failure = responseError(
      'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
      '草稿已更新',
      409,
    )
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [serverFile()],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.deleteCurrentDraft()).rejects.toBe(failure)

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.revisionConflict).toBe(true)
    expect(store.mutationError).toContain('重新删除')
  })

  it('reloads an expired authoritative draft after any draft deletion failure', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const failure = responseError(
      'REIMBURSEMENT_DRAFT_EXPIRED',
      '草稿已过期',
      409,
    )
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockResolvedValue({
      ...draft('draft-1', 2), status: 'EXPIRED',
    })
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [serverFile('authoritative-file')],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile('stale-file')]

    await expect(store.deleteCurrentDraft()).rejects.toBe(failure)

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.currentDraft?.status).toBe('EXPIRED')
    expect(store.files.map((file) => file.id)).toEqual(['authoritative-file'])
    expect(store.revisionConflict).toBe(false)
    expect(store.mutationError).toContain('草稿已过期')
    expect(store.mutationError).toContain('已同步报销内容最新状态')
  })

  it('refreshes a non-current summary after any draft deletion failure', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const failure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '删除结果确认失败',
      500,
    )
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockResolvedValue({
      ...draft('draft-list', 2), status: 'EXPIRED',
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-list')]
    store.draftListTotal = 2
    store.files = [serverFile('current-file')]

    await expect(store.deleteDraft('draft-list', 1)).rejects.toBe(failure)

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledWith(
      'draft-list',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(listReimbursementDraftFiles).not.toHaveBeenCalled()
    expect(store.drafts.find((item) => item.id === 'draft-list')).toMatchObject({
      revision: 2,
      status: 'EXPIRED',
    })
    expect(store.currentDraft?.id).toBe('draft-current')
    expect(store.currentDraft?.revision).toBe(1)
    expect(store.files.map((file) => file.id)).toEqual(['current-file'])
    expect(store.mutationError).toContain('删除结果确认失败')
    expect(store.mutationError).toContain('已同步报销内容最新状态')
  })

  it('tombstones a draft opened after its non-current deletion began', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const deletion = deferred<Awaited<ReturnType<typeof deleteReimbursementDraft>>>()
    const failure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '删除结果确认失败',
      500,
    )
    vi.mocked(deleteReimbursementDraft).mockReturnValue(deletion.promise)
    vi.mocked(getReimbursementDraft)
      .mockResolvedValueOnce(draft('draft-target'))
      .mockRejectedValueOnce(responseError(
        'REIMBURSEMENT_DRAFT_NOT_FOUND',
        '草稿不存在',
        404,
      ))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: 'draft-target', revision: 1, items: [serverFile('target-file')],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-target')]
    store.draftListTotal = 2

    const deleting = store.deleteDraft('draft-target', 1)
    await store.loadDraft('draft-target')
    expect(store.currentDraft?.id).toBe('draft-target')
    deletion.reject(failure)

    await expect(deleting).rejects.toBe(failure)
    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
  })

  it('removes a non-current summary confirmed missing without accepting a stale list', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const failure = responseError(
      'REIMBURSEMENT_DRAFT_NOT_FOUND',
      '草稿不存在',
      404,
    )
    const staleList = deferred<Awaited<ReturnType<typeof listReimbursementDrafts>>>()
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockRejectedValue(responseError(
      'REIMBURSEMENT_DRAFT_NOT_FOUND',
      '草稿不存在',
      404,
    ))
    vi.mocked(listReimbursementDrafts).mockReturnValue(staleList.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-list')]
    store.draftListTotal = 2

    const listing = store.loadDrafts()
    await expect(store.deleteDraft('draft-list', 1)).rejects.toBe(failure)
    staleList.resolve({
      items: [draft('draft-current'), draft('draft-list')],
      offset: 0,
      limit: 50,
      total: 2,
    })
    await listing

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).not.toHaveBeenCalled()
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
    expect(store.currentDraft?.id).toBe('draft-current')
    expect(store.loadingDrafts).toBe(false)
  })

  it('does not discard a current save when an unrelated summary is tombstoned', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const saved = deferred<ReimbursementDraft>()
    const deletionFailure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '删除结果确认失败',
      500,
    )
    vi.mocked(updateReimbursementDraft).mockReturnValue(saved.promise)
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(deletionFailure)
    vi.mocked(getReimbursementDraft).mockRejectedValue(responseError(
      'REIMBURSEMENT_DRAFT_NOT_FOUND',
      '草稿不存在',
      404,
    ))
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-list')]
    store.draftListTotal = 2

    const saving = store.saveDraft(input)
    await expect(store.deleteDraft('draft-list', 1)).rejects.toBe(deletionFailure)
    saved.resolve(draft('draft-current', 2))
    await saving

    expect(store.currentDraft?.id).toBe('draft-current')
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
  })

  it('does not resurrect a tombstoned summary from an older failure refresh', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft('draft-current'))
    const staleSummary = deferred<ReimbursementDraft>()
    const deletionFailure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '删除结果确认失败',
      500,
    )
    vi.mocked(deleteReimbursementDraft)
      .mockRejectedValueOnce(deletionFailure)
      .mockResolvedValueOnce({ deletedDraftId: 'draft-list' })
    vi.mocked(getReimbursementDraft).mockReturnValue(staleSummary.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.drafts = [draft('draft-current'), draft('draft-list')]
    store.draftListTotal = 2

    const staleDeletion = store.deleteDraft('draft-list', 1)
    await vi.waitFor(() => expect(getReimbursementDraft).toHaveBeenCalledOnce())
    await store.deleteDraft('draft-list', 1)
    staleSummary.resolve(draft('draft-list', 1))
    await expect(staleDeletion).rejects.toBe(deletionFailure)

    expect(store.drafts.map((item) => item.id)).toEqual(['draft-current'])
    expect(store.draftListTotal).toBe(1)
    expect(store.currentDraft?.id).toBe('draft-current')
  })

  it('tombstones a missing current draft without accepting stale list or detail reads', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const failure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '删除结果确认失败',
      500,
    )
    const staleList = deferred<Awaited<ReturnType<typeof listReimbursementDrafts>>>()
    const staleDraft = deferred<ReimbursementDraft>()
    const staleFiles = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    vi.mocked(deleteReimbursementDraft).mockRejectedValue(failure)
    vi.mocked(listReimbursementDrafts).mockReturnValue(staleList.promise)
    vi.mocked(getReimbursementDraft)
      .mockReturnValueOnce(staleDraft.promise)
      .mockRejectedValueOnce(responseError(
        'REIMBURSEMENT_DRAFT_NOT_FOUND',
        '草稿不存在',
        404,
      ))
    vi.mocked(listReimbursementDraftFiles)
      .mockReturnValueOnce(staleFiles.promise)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 1, items: [] })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile('local-file')]

    const listing = store.loadDrafts()
    const loading = store.loadDraft('draft-1')
    await expect(store.deleteCurrentDraft()).rejects.toBe(failure)

    expect(deleteReimbursementDraft).toHaveBeenCalledOnce()
    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts).toEqual([])
    expect(store.draftListTotal).toBe(0)

    staleList.resolve({
      items: [draft()], offset: 0, limit: 50, total: 1,
    })
    staleDraft.resolve(draft('draft-1', 1))
    staleFiles.resolve({
      draftId: 'draft-1', revision: 1, items: [serverFile('stale-file')],
    })
    await Promise.all([listing, loading])

    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.drafts).toEqual([])
    expect(store.draftListTotal).toBe(0)
  })

  it('reloads a deleting file after any remove failure without replaying DELETE', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const failure = responseError(
      'REIMBURSEMENT_STORAGE_ERROR',
      '文件物理清理失败',
      500,
    )
    vi.mocked(deleteReimbursementDraftFile).mockRejectedValue(failure)
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1',
      revision: 2,
      items: [{ ...serverFile(), status: 'DELETING' }],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)
    store.files = [serverFile()]

    await expect(store.removeFile('file-1')).rejects.toBe(failure)

    expect(deleteReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files[0]?.status).toBe('DELETING')
    expect(store.revisionConflict).toBe(false)
    expect(store.mutationError).toContain('文件物理清理失败')
    expect(store.mutationError).toContain('已同步报销内容最新状态')
  })

  it('reloads authoritative state after an upload failure without replaying upload', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(uploadReimbursementDraftFile).mockRejectedValue(responseError(
      'PROCESS_RESOURCE_LIMIT',
      '票据文件处理超过资源限制',
    ))
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [serverFile('reserved-file')],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.uploadFile(new File(['bytes'], '票据.jpg'))).rejects.toBeTruthy()

    expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files.map((file) => file.id)).toEqual(['reserved-file'])
    expect(store.mutationError).toContain('票据文件处理超过资源限制')
    expect(store.mutationError).toContain('已同步报销内容最新状态')
  })

  it('reloads authoritative state after an OCR failure without replaying OCR', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(recognizeReimbursementDraftFile).mockRejectedValue(responseError(
      'OCR_TIMEOUT',
      '票据识别超时，请手工填写',
      504,
    ))
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [{
        ...serverFile(), ocrStatus: 'FAILED',
      }],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.recognizeFile('file-1', 2026)).rejects.toBeTruthy()

    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.files[0]?.ocrStatus).toBe('FAILED')
    expect(store.mutationError).toContain('票据识别超时，请手工填写')
    expect(store.mutationError).toContain('已同步报销内容最新状态')
  })

  it('reloads and retries OCR once when its draft revision changed', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(recognizeReimbursementDraftFile)
      .mockRejectedValueOnce(responseError(
        'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
        '报销内容版本已更新，请刷新后重试',
        409,
      ))
      .mockResolvedValueOnce({
        draftId: 'draft-1',
        revision: 3,
        file: { ...serverFile(), ocrStatus: 'COMPLETE' },
      })
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [serverFile()],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.recognizeFile('file-1', 2026)).resolves.toMatchObject({ revision: 3 })

    expect(recognizeReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(vi.mocked(recognizeReimbursementDraftFile).mock.calls.map((call) => call[2].expectedRevision))
      .toEqual([1, 2])
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.currentDraft?.revision).toBe(3)
    expect(store.files[0]?.ocrStatus).toBe('COMPLETE')
    expect(store.revisionConflict).toBe(false)
    expect(store.mutationError).toBe('')
  })

  it('stops after one OCR conflict retry and exposes the refreshed state', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const conflict = responseError(
      'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
      '报销内容版本已更新，请刷新后重试',
      409,
    )
    vi.mocked(recognizeReimbursementDraftFile).mockRejectedValue(conflict)
    vi.mocked(getReimbursementDraft)
      .mockResolvedValueOnce(draft('draft-1', 2))
      .mockResolvedValueOnce(draft('draft-1', 3))
    vi.mocked(listReimbursementDraftFiles)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, items: [serverFile()] })
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 3, items: [serverFile()] })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.recognizeFile('file-1', 2026)).rejects.toBe(conflict)

    expect(recognizeReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(getReimbursementDraft).toHaveBeenCalledTimes(2)
    expect(listReimbursementDraftFiles).toHaveBeenCalledTimes(2)
    expect(store.currentDraft?.revision).toBe(3)
    expect(store.revisionConflict).toBe(true)
    expect(store.mutationError).toContain('操作期间已更新')
    expect(store.mutationError).not.toContain('其他页面')
  })

  it('reset aborts in-flight upload and ignores a late successful response', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const upload = deferred<{
      draftId: string
      revision: number
      file: ReimbursementDraftFile
    }>()
    let uploadSignal: AbortSignal | undefined
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(
      (_draftId, _revision, _file, options) => {
        uploadSignal = options!.signal
        return upload.promise
      },
    )
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const pending = store.uploadFile(new File(['bytes'], '票据.jpg'))
    store.reset()
    upload.resolve({ draftId: 'draft-1', revision: 2, file: serverFile() })
    await pending

    expect(uploadSignal?.aborted).toBe(true)
    expect(store.currentDraft).toBeNull()
    expect(store.files).toEqual([])
    expect(store.pendingMutations).toBe(0)
  })

  it('does not let an upload from before reset clear a newer upload state', async () => {
    vi.mocked(createReimbursementDraft)
      .mockResolvedValueOnce(draft('draft-a'))
      .mockResolvedValueOnce(draft('draft-b'))
    const firstUpload = deferred<{
      draftId: string
      revision: number
      file: ReimbursementDraftFile
    }>()
    const secondUpload = deferred<{
      draftId: string
      revision: number
      file: ReimbursementDraftFile
    }>()
    vi.mocked(uploadReimbursementDraftFile)
      .mockReturnValueOnce(firstUpload.promise)
      .mockReturnValueOnce(secondUpload.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const stale = store.uploadFile(new File(['old'], 'old.jpg'))
    store.reset()
    await store.createDraft(input)
    const current = store.uploadFile(new File(['new'], 'new.jpg'))
    firstUpload.resolve({ draftId: 'draft-a', revision: 2, file: serverFile('old') })
    await stale

    expect(store.uploading).toBe(true)
    secondUpload.resolve({ draftId: 'draft-b', revision: 2, file: serverFile('new') })
    await current
    expect(store.uploading).toBe(false)
    expect(store.currentDraft?.id).toBe('draft-b')
    expect(store.files.map((file) => file.id)).toEqual(['new'])
  })

  it('discards a late Excel preview after reset instead of downloading it', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    const preview = deferred<{ blob: Blob; filename: string }>()
    vi.mocked(getReimbursementDraftExcelPreview).mockReturnValue(preview.promise)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    const pending = store.downloadExcelPreview()
    store.reset()
    preview.resolve({ blob: new Blob(['xlsx']), filename: 'preview.xlsx' })
    await pending

    expect(downloadBlob).not.toHaveBeenCalled()
    expect(store.downloadingPreview).toBe(false)
  })

  it('opens a mobile Excel ticket with DingTalk native APIs and no browser navigation', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(requestReimbursementDraftExcelPreviewTicket).mockResolvedValue({
      downloadUrl: '/api/reimbursements/drafts/draft-1/excel-preview/native',
      downloadToken: 'signed-ticket',
      fileType: 'xlsx',
    })
    vi.mocked(downloadAndOpenDingTalkDocument).mockResolvedValue(undefined)
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await store.openExcelPreviewInDingTalk()

    expect(requestReimbursementDraftExcelPreviewTicket).toHaveBeenCalledWith(
      'draft-1',
      1,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    )
    expect(downloadAndOpenDingTalkDocument).toHaveBeenCalledWith({
      url: 'http://localhost/api/reimbursements/drafts/draft-1/excel-preview/native',
      headers: { 'X-Reimbursement-Download-Token': 'signed-ticket' },
      fileType: 'xlsx',
    })
  })

  it('refreshes a conflicted preview without replaying its POST', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(getReimbursementDraftExcelPreview).mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          error: {
            code: 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
            message: '草稿已更新',
          },
        },
      },
    })
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1', revision: 2, items: [serverFile()],
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await expect(store.downloadExcelPreview()).rejects.toBeTruthy()

    expect(getReimbursementDraftExcelPreview).toHaveBeenCalledOnce()
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(store.currentDraft?.revision).toBe(2)
    expect(store.revisionConflict).toBe(true)
  })

  it('sends each operation with the latest adopted revision', async () => {
    vi.mocked(createReimbursementDraft).mockResolvedValue(draft())
    vi.mocked(replaceReimbursementRelatedApprovals).mockResolvedValue(draft('draft-1', 2))
    vi.mocked(markReimbursementDraftReviewReady).mockResolvedValue({
      ...draft('draft-1', 3), status: 'REVIEW_READY',
    })
    vi.mocked(updateReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1', revision: 4, file: serverFile(),
    })
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1', revision: 5, file: {
        ...serverFile(), ocrStatus: 'COMPLETE',
      },
    })
    vi.mocked(deleteReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1', revision: 6, deletedFileId: 'file-1',
    })
    const store = useReimbursementDraftStore()
    await store.createDraft(input)

    await store.saveRelatedApprovals([])
    await store.markReviewReady()
    await store.updateFile('file-1', { role: 'ATTACHMENT_ONLY' })
    await store.recognizeFile('file-1', 2026)
    await store.removeFile('file-1')

    expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith(
      'draft-1', 1, [], expect.anything(),
    )
    expect(markReimbursementDraftReviewReady).toHaveBeenCalledWith(
      'draft-1', 2, expect.anything(),
    )
    expect(updateReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      'file-1',
      { expectedRevision: 3, role: 'ATTACHMENT_ONLY' },
      expect.anything(),
    )
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      'file-1',
      { expectedRevision: 4, tripYear: 2026 },
      expect.anything(),
    )
    expect(deleteReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1', 'file-1', 5, expect.anything(),
    )
    expect(store.currentDraft?.revision).toBe(6)
    expect(store.files).toEqual([])
  })

  it('overlaps one batch upload with one initial OCR and merges an older different-file result without lowering revision', async () => {
    const store = useReimbursementDraftStore()
    store.currentDraft = draft()
    const pipeline = store.beginFilePipeline()
    const uploadB = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const ocrA = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, file: serverFile('a') })
      .mockReturnValueOnce(uploadB.promise)
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(ocrA.promise)
    await store.uploadFile(new File(['a'], 'a.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    const recognizing = store.recognizeFile('a', 2026, pipeline)
    const uploading = store.uploadFile(new File(['b'], 'b.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    expect(store.pendingMutations).toBe(2)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledWith('draft-1', 'a', {
      expectedRevision: 2, tripYear: 2026, allowUploadOverlap: true,
    }, expect.any(Object))
    await expect(store.recognizeFile('a', 2026, pipeline)).rejects.toThrow('逐份识别')
    await expect(store.uploadFile(new File(['c'], 'c.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)).rejects.toThrow('正在上传')
    await expect(store.saveDraft(input)).rejects.toThrow('本批文件')
    await expect(store.removeFile('a')).rejects.toThrow('本批文件')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    uploadB.resolve({ draftId: 'draft-1', revision: 3, file: serverFile('b') })
    await uploading
    ocrA.resolve({ draftId: 'draft-1', revision: 2, file: { ...serverFile('a'), ocrStatus: 'COMPLETE' } })
    await recognizing
    expect(store.currentDraft!.revision).toBe(3)
    expect(store.files.find((file) => file.id === 'a')!.ocrStatus).toBe('COMPLETE')
    expect(store.files.map((file) => file.id)).toEqual(['a', 'b'])
    expect(store.busy).toBe(true)
    await expect(store.recognizeFile('a', 2026, pipeline)).rejects.toThrow('新文件')
    await expect(store.finishFilePipeline(pipeline)).resolves.toBe(true)
    expect(store.busy).toBe(false)
  })

  it('refreshes only revision after failed batch upload and reloads files only after both lanes settle', async () => {
    const store = useReimbursementDraftStore()
    store.currentDraft = draft()
    const pipeline = store.beginFilePipeline()
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, file: serverFile('a') })
      .mockRejectedValueOnce(new Error('上传中断'))
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 4, file: serverFile('c') })
    const ocrA = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(ocrA.promise)
    vi.mocked(getReimbursementDraft).mockResolvedValueOnce(draft('draft-1', 3)).mockResolvedValueOnce(draft('draft-1', 4))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({ draftId: 'draft-1', revision: 4,
      items: [{ ...serverFile('a'), ocrStatus: 'FAILED' }, serverFile('c')] })
    await store.uploadFile(new File(['a'], 'a.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    const recognizing = store.recognizeFile('a', undefined, pipeline).catch(() => undefined)
    await expect(store.uploadFile(new File(['b'], 'b.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)).rejects.toThrow('上传中断')
    expect(store.currentDraft!.revision).toBe(3)
    expect(listReimbursementDraftFiles).not.toHaveBeenCalled()
    await store.uploadFile(new File(['c'], 'c.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    expect(vi.mocked(uploadReimbursementDraftFile).mock.calls[2]![1]).toBe(3)
    ocrA.reject(new Error('识别失败'))
    await recognizing
    expect(listReimbursementDraftFiles).not.toHaveBeenCalled()
    await expect(store.finishFilePipeline(pipeline)).resolves.toBe(true)
    expect(listReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(store.files.find((file) => file.id === 'a')!.ocrStatus).toBe('FAILED')
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(3)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
  })

  it('aborts both batch lanes on cancellation and ignores late results even after returning to the same draft', async () => {
    const store = useReimbursementDraftStore()
    store.currentDraft = draft()
    const pipeline = store.beginFilePipeline()
    const uploadB = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const ocrA = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, file: serverFile('a') })
      .mockReturnValueOnce(uploadB.promise)
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(ocrA.promise)
    await store.uploadFile(new File(['a'], 'a.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    const recognizing = store.recognizeFile('a', undefined, pipeline)
    const uploading = store.uploadFile(new File(['b'], 'b.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)
    store.cancelFilePipeline(pipeline)
    expect(vi.mocked(uploadReimbursementDraftFile).mock.calls[1]![3]!.signal!.aborted).toBe(true)
    expect(vi.mocked(recognizeReimbursementDraftFile).mock.calls[0]![3]!.signal!.aborted).toBe(true)
    store.currentDraft = draft('draft-1', 7)
    store.files = [serverFile('new-session')]
    uploadB.resolve({ draftId: 'draft-1', revision: 8, file: serverFile('b') })
    ocrA.resolve({ draftId: 'draft-1', revision: 8, file: { ...serverFile('a'), ocrStatus: 'COMPLETE' } })
    await Promise.all([recognizing, uploading])
    expect(store.currentDraft!.revision).toBe(7)
    expect(store.files.map((file) => file.id)).toEqual(['new-session'])
    expect(store.pendingMutations).toBe(0)
    await expect(store.finishFilePipeline(pipeline)).resolves.toBe(false)
  })

  it('stops the batch instead of using a revision refresh to accept changed form input', async () => {
    const store = useReimbursementDraftStore()
    store.currentDraft = draft()
    const pipeline = store.beginFilePipeline()
    vi.mocked(uploadReimbursementDraftFile).mockRejectedValueOnce(new Error('网络中断'))
    vi.mocked(getReimbursementDraft).mockResolvedValueOnce({ ...draft('draft-1', 2), input: { ...input, companyValue: '另一公司' } })
    await expect(store.uploadFile(new File(['a'], 'a.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)).rejects.toThrow('网络中断')
    expect(store.isFilePipelineActive(pipeline)).toBe(false)
    expect(store.currentDraft!.input.companyValue).toBe('北京')
    expect(store.mutationError).toContain('报销内容已发生变化')
    await expect(store.uploadFile(new File(['b'], 'b.jpg'), 'ATTACHMENT_ONLY', 'other', true, pipeline)).rejects.toThrow('已取消')
    expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
  })
})

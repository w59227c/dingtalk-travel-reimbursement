import { beforeEach, describe, expect, it, vi } from 'vitest'

import { apiErrorCode } from '@/api/errors'
import { http } from '@/api/http'
import {
  createReimbursementDraft,
  deleteReimbursementDraft,
  deleteReimbursementDraftFile,
  getOaReimbursementSubmission,
  getOaReimbursementSubmissionForDraft,
  getReimbursementDraft,
  getReimbursementFileContent,
  getReimbursementDraftExcelPreview,
  getOaReimbursementOptions,
  listOaTravelApprovals,
  listReimbursementDraftFiles,
  listReimbursementDrafts,
  markReimbursementDraftReviewReady,
  recognizeReimbursementDraftFile,
  reimbursementDraftExcelPreviewUrl,
  replaceReimbursementRelatedApprovals,
  submitOaReimbursement,
  updateReimbursementDraft,
  updateReimbursementDraftFile,
  uploadReimbursementDraftFile,
} from '@/api/reimbursements'
import type { ReimbursementDraftInput } from '@/types/reimbursements'

vi.mock('@/api/http', () => ({
  http: {
    delete: vi.fn(),
    get: vi.fn(),
    patch: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

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

describe('persistent reimbursement API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('builds an encoded same-origin Excel preview URL for a native browser download', () => {
    expect(reimbursementDraftExcelPreviewUrl('draft/一', 9)).toBe(
      '/api/reimbursements/drafts/draft%2F%E4%B8%80/excel-preview?expectedRevision=9',
    )
  })

  it('fetches an original attachment as a blob through the authenticated API client', async () => {
    const blob = new Blob(['pdf'], { type: 'application/pdf' })
    const signal = new AbortController().signal
    vi.mocked(http.get).mockResolvedValue({ data: blob })
    await expect(getReimbursementFileContent('draft/1', 'file 2', { signal })).resolves.toBe(blob)
    expect(http.get).toHaveBeenCalledWith('/reimbursements/drafts/draft%2F1/files/file%202/content', {
      responseType: 'blob', signal, timeout: 60_000,
    })
  })

  it('loads strongly typed OA options and travel approvals with cancellation', async () => {
    const signal = new AbortController().signal
    const options = {
      templateConfigVersion: 12,
      reimbursementProcessCode: 'PROC-REIMBURSEMENT',
      companyOptions: [{ value: '北京', label: '北京公司', key: 'beijing' }],
      budgetCodeOptions: [{ value: '26007', label: 'MES 项目', key: null }],
      travelProfiles: [{
        profileKey: 'business',
        displayName: '商务出差',
        processCode: 'PROC-TRAVEL',
        schemaFingerprint: 'a'.repeat(64),
        travelTypeOption: { value: 'business', label: '商务出差', key: null },
      }],
    }
    const approvals = {
      templateConfigVersion: 12,
      queryWindow: { from: '2026-08-01', to: '2026-09-04' },
      items: [{
        processInstanceId: 'instance-1',
        profileKey: 'business',
        profileDisplayName: '商务出差',
        sourceProcessCode: 'PROC-TRAVEL',
        travelTypeOption: { value: 'business', label: '商务出差', key: null },
        title: '合肥出差',
        businessId: 'BIZ-1',
        startDate: '2026-08-10',
        endDate: '2026-08-12',
        createdAt: '2026-08-01T08:00:00+08:00',
        finishedAt: '2026-08-02T08:00:00+08:00',
      }],
    }
    vi.mocked(http.get)
      .mockResolvedValueOnce({ data: { data: options } })
      .mockResolvedValueOnce({ data: { data: approvals } })

    await expect(getOaReimbursementOptions({ signal })).resolves.toBe(options)
    await expect(listOaTravelApprovals({
      from: '2026-08-01',
      to: '2026-09-04',
      query: '合肥',
      signal,
    })).resolves.toBe(approvals)

    expect(http.get).toHaveBeenNthCalledWith(1, '/oa/reimbursements/options', { signal })
    expect(http.get).toHaveBeenNthCalledWith(2, '/oa/travel-approvals', {
      params: { from: '2026-08-01', to: '2026-09-04', q: '合肥' },
      signal,
      timeout: 60_000,
    })
  })

  it('normalizes create, list, detail and revision-checked draft responses', async () => {
    const controller = new AbortController()
    const created = { id: 'draft/一', revision: 1 }
    vi.mocked(http.post).mockResolvedValue({ data: { data: created } })
    vi.mocked(http.get)
      .mockResolvedValueOnce({ data: { data: { items: [], offset: 10, limit: 20, total: 0 } } })
      .mockResolvedValueOnce({ data: { data: created } })
    vi.mocked(http.put).mockResolvedValue({ data: { data: { ...created, revision: 2 } } })

    await expect(createReimbursementDraft(input, { signal: controller.signal }))
      .resolves.toBe(created)
    await listReimbursementDrafts({ offset: 10, limit: 20, signal: controller.signal })
    await getReimbursementDraft('draft/一', { signal: controller.signal })
    await updateReimbursementDraft('draft/一', 1, input, { signal: controller.signal })

    expect(http.post).toHaveBeenCalledWith(
      '/reimbursements/drafts',
      { expectedRevision: 0, input },
      { signal: controller.signal },
    )
    expect(http.get).toHaveBeenNthCalledWith(1, '/reimbursements/drafts', {
      params: { offset: 10, limit: 20 },
      signal: controller.signal,
    })
    expect(http.get).toHaveBeenNthCalledWith(
      2,
      '/reimbursements/drafts/draft%2F%E4%B8%80',
      { signal: controller.signal },
    )
    expect(http.put).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft%2F%E4%B8%80',
      { expectedRevision: 1, input },
      { signal: controller.signal },
    )
  })

  it('sends only verified-selection identifiers and the original query window', async () => {
    vi.mocked(http.put).mockResolvedValue({ data: { data: { id: 'draft-1', revision: 4 } } })
    const selections = [{
      processInstanceId: 'instance-1',
      profileKey: 'business',
      queryWindow: { from: '2026-08-01', to: '2026-09-04' },
    }]

    await replaceReimbursementRelatedApprovals('draft-1', 3, selections)

    expect(http.put).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft-1/related-approvals',
      { expectedRevision: 3, selections },
      { signal: undefined, timeout: 60_000 },
    )
  })

  it('marks exactly one expected draft revision ready for review', async () => {
    vi.mocked(http.post).mockResolvedValue({
      data: { data: { id: 'draft-1', revision: 8, status: 'REVIEW_READY' } },
    })

    await markReimbursementDraftReviewReady('draft-1', 7)

    expect(http.post).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft-1/review',
      { expectedRevision: 7 },
      { signal: undefined },
    )
  })

  it('deletes exactly one expected draft revision', async () => {
    const signal = new AbortController().signal
    const deleted = { deletedDraftId: 'draft/一' }
    vi.mocked(http.delete).mockResolvedValue({ data: { data: deleted } })

    await expect(deleteReimbursementDraft('draft/一', 7, { signal })).resolves.toBe(deleted)

    expect(http.delete).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft%2F%E4%B8%80',
      { params: { expectedRevision: 7 }, signal },
    )
  })

  it('uploads one transient File without manually setting multipart content type', async () => {
    vi.mocked(http.post).mockResolvedValue({
      data: { data: { draftId: 'draft-1', revision: 2, file: { id: 'server-file-1' } } },
    })
    const file = new File(['image'], '票据.jpg', { type: 'image/jpeg' })
    const onProgress = vi.fn()

    await uploadReimbursementDraftFile('draft-1', 1, file, {
      role: 'ATTACHMENT_ONLY',
      onProgress,
    })

    const call = vi.mocked(http.post).mock.calls[0]!
    expect(call[0]).toBe('/reimbursements/drafts/draft-1/files')
    expect(call[1]).toBeInstanceOf(FormData)
    expect((call[1] as FormData).getAll('files[]')).toEqual([file])
    expect(call[2]).toMatchObject({
      params: { expectedRevision: 1, role: 'ATTACHMENT_ONLY', attachmentKind: 'other' },
      timeout: 120_000,
    })
    expect(call[2]).not.toHaveProperty('headers.Content-Type')
    call[2]!.onUploadProgress!({ loaded: 1, total: 2 } as never)
    expect(onProgress).toHaveBeenCalledWith(50)
  })

  it('normalizes file list, patch, delete and OCR endpoints', async () => {
    const signal = new AbortController().signal
    vi.mocked(http.get).mockResolvedValue({
      data: { data: { draftId: 'draft/1', revision: 3, items: [] } },
    })
    vi.mocked(http.patch).mockResolvedValue({
      data: { data: { draftId: 'draft/1', revision: 4, file: { id: 'file/1' } } },
    })
    vi.mocked(http.delete).mockResolvedValue({
      data: { data: { draftId: 'draft/1', revision: 5, deletedFileId: 'file/1' } },
    })
    vi.mocked(http.post).mockResolvedValue({
      data: { data: { draftId: 'draft/1', revision: 6, file: { id: 'file/1' } } },
    })

    await listReimbursementDraftFiles('draft/1', { signal })
    await updateReimbursementDraftFile(
      'draft/1',
      'file/1',
      { expectedRevision: 3, role: 'EXPENSE_SOURCE' },
      { signal },
    )
    await deleteReimbursementDraftFile('draft/1', 'file/1', 4, { signal })
    await recognizeReimbursementDraftFile(
      'draft/1',
      'file/1',
      { expectedRevision: 5, tripYear: 2026 },
      { signal },
    )

    const encodedFileUrl = '/reimbursements/drafts/draft%2F1/files/file%2F1'
    expect(http.get).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft%2F1/files',
      { signal },
    )
    expect(http.patch).toHaveBeenCalledWith(
      encodedFileUrl,
      { expectedRevision: 3, role: 'EXPENSE_SOURCE' },
      { signal },
    )
    expect(http.delete).toHaveBeenCalledWith(encodedFileUrl, {
      params: { expectedRevision: 4 },
      signal,
    })
    expect(http.post).toHaveBeenCalledWith(
      `${encodedFileUrl}/ocr`,
      { expectedRevision: 5, tripYear: 2026 },
      { signal, timeout: 135_000 },
    )
  })

  it('returns an Excel preview blob with the safe server filename', async () => {
    const blob = new Blob(['xlsx'])
    vi.mocked(http.post).mockResolvedValue({
      data: blob,
      headers: {
        'content-disposition':
          `attachment; filename*=UTF-8''${encodeURIComponent('差旅费报销单-预览.xlsx')}`,
      },
    })

    await expect(getReimbursementDraftExcelPreview('draft-1', 9)).resolves.toEqual({
      blob,
      filename: '差旅费报销单-预览.xlsx',
    })
    expect(http.post).toHaveBeenCalledWith(
      '/reimbursements/drafts/draft-1/excel-preview',
      { expectedRevision: 9 },
      {
        responseType: 'blob',
        signal: undefined,
        timeout: 60_000,
      },
    )
  })

  it('normalizes a blob-wrapped preview error back into the API envelope', async () => {
    const conflict = Object.assign(new Error('Request failed with status code 409'), {
      isAxiosError: true,
      response: {
        status: 409,
        data: new Blob([JSON.stringify({
          error: {
            code: 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT',
            message: '草稿已更新',
          },
        })], { type: 'application/json' }),
      },
    })
    vi.mocked(http.post).mockRejectedValue(conflict)

    const caught = await getReimbursementDraftExcelPreview('draft-1', 9)
      .catch((error: unknown) => error)

    expect(caught).toBe(conflict)
    expect(apiErrorCode(caught)).toBe('REIMBURSEMENT_DRAFT_REVISION_CONFLICT')
  })

  it('submits one exact draft revision with a UUID key and reads its task', async () => {
    const signal = new AbortController().signal
    const queued = {
      submissionId: 'submission/一',
      draftId: 'draft/一',
      status: 'QUEUED',
      statusVersion: 1,
      attemptCount: 0,
      processInstanceId: null,
      businessId: null,
      approvalUrl: null,
      error: null,
      pollAfterMs: 1_500,
      createdAt: '2026-09-04T00:00:00Z',
      updatedAt: '2026-09-04T00:00:00Z',
      submittedAt: null,
    } as const
    vi.mocked(http.post).mockResolvedValue({ data: { data: queued } })
    vi.mocked(http.get).mockResolvedValue({ data: { data: queued } })

    await expect(submitOaReimbursement(
      'draft/一',
      7,
      '123e4567-e89b-42d3-a456-426614174000',
      { signal },
    )).resolves.toBe(queued)
    await expect(getOaReimbursementSubmission('submission/一', { signal }))
      .resolves.toBe(queued)
    await expect(getOaReimbursementSubmissionForDraft('draft/一', { signal }))
      .resolves.toBe(queued)

    expect(http.post).toHaveBeenCalledWith(
      '/oa/reimbursements/draft%2F%E4%B8%80/submit',
      { expectedRevision: 7 },
      {
        headers: { 'Idempotency-Key': '123e4567-e89b-42d3-a456-426614174000' },
        signal,
      },
    )
    expect(http.get).toHaveBeenCalledWith(
      '/oa/reimbursements/submissions/submission%2F%E4%B8%80',
      { signal },
    )
    expect(http.get).toHaveBeenCalledWith(
      '/oa/reimbursements/drafts/draft%2F%E4%B8%80/submission',
      { signal },
    )
  })
})

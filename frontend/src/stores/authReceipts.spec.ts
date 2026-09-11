import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getMe,
  getPublicConfig,
  logout as logoutRequest,
  selectDepartment as selectDepartmentRequest,
  selectDepartmentFromTravelApproval as selectDepartmentFromTravelApprovalRequest,
} from '@/api/auth'
import { getOaReimbursementOptions } from '@/api/reimbursements'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'

const callbacks = vi.hoisted(() => ({ unauthorized: null as (() => void) | null }))

vi.mock('@/api/http', () => ({
  setCsrfToken: vi.fn(),
  setUnauthorizedHandler: vi.fn((handler: () => void) => {
    callbacks.unauthorized = handler
  }),
}))

vi.mock('@/api/auth', () => ({
  getMe: vi.fn(),
  getPublicConfig: vi.fn(),
  loginWithDingTalk: vi.fn(),
  loginWithMock: vi.fn(),
  logout: vi.fn().mockResolvedValue(undefined),
  selectDepartment: vi.fn(),
  selectDepartmentFromTravelApproval: vi.fn(),
}))

vi.mock('@/api/expenses', () => ({
  calculateTotals: vi.fn(),
  getExpenseCategories: vi.fn(),
}))

vi.mock('@/api/receipts', () => ({
  deleteReceiptFile: vi.fn(),
  recognizeReceiptFile: vi.fn(),
  uploadReceiptFile: vi.fn(),
}))

vi.mock('@/api/reimbursements', () => ({
  createReimbursementDraft: vi.fn(),
  deleteReimbursementDraft: vi.fn(),
  deleteReimbursementDraftFile: vi.fn(),
  getOaReimbursementOptions: vi.fn(),
  getOaReimbursementSubmission: vi.fn(),
  getReimbursementDraft: vi.fn(),
  getReimbursementDraftExcelPreview: vi.fn(),
  listOaTravelApprovals: vi.fn(),
  listReimbursementDraftFiles: vi.fn(),
  listReimbursementDrafts: vi.fn(),
  markReimbursementDraftReviewReady: vi.fn(),
  recognizeReimbursementDraftFile: vi.fn(),
  replaceReimbursementRelatedApprovals: vi.fn(),
  submitOaReimbursement: vi.fn(),
  updateReimbursementDraft: vi.fn(),
  updateReimbursementDraftFile: vi.fn(),
  uploadReimbursementDraftFile: vi.fn(),
}))

function seedReceiptMemory(): ReturnType<typeof useExpenseStore> {
  const expense = useExpenseStore()
  expense.receiptFiles.push({
    localId: 'local-a',
    tempId: 'temp-a',
    file: new File(['receipt'], 'a.jpg', { type: 'image/jpeg' }),
    name: 'a.jpg',
    size: 7,
    uploadProgress: 100,
    status: 'done',
    ocrItemId: 'ocr-temp-a',
  })
  expense.items.push({
    id: 'ocr-temp-a',
    category: 'other',
    date: '2026-06-30',
    displayDate: '2026-06-30',
    description: '票据',
    amount: '1.00',
    receiptCount: 1,
    source: 'ocr',
  })
  return expense
}

function session(userId = 'user-a', departmentId = '100', csrfToken = 'csrf-a') {
  return {
    user: { userId, name: `用户 ${userId}` },
    departments: [
      { id: '100', name: '测试部门' },
      { id: '200', name: '另一个部门' },
    ],
    selectedDepartment: { id: departmentId, name: `部门 ${departmentId}` },
    isAdmin: false,
    csrfToken,
  }
}

function seedDraftMemory(): ReturnType<typeof useReimbursementDraftStore> {
  const drafts = useReimbursementDraftStore()
  drafts.drafts = [{
    id: 'draft-a',
    status: 'DRAFT',
    revision: 1,
    department: { id: '100', name: '测试部门' },
    templateConfigVersion: 12,
    relatedApprovalCount: 0,
    expiresAt: '2026-10-04T00:00:00Z',
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
    lockedAt: null,
  }]
  drafts.reimbursementOptions = {
    templateConfigVersion: 12,
    reimbursementProcessCode: 'PROC-REIMBURSEMENT',
    companyOptions: [],
    budgetCodeOptions: [],
    travelProfiles: [],
  }
  return drafts
}

describe('authentication clears scoped client memory', () => {
  beforeEach(() => {
    callbacks.unauthorized = null
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(logoutRequest).mockResolvedValue(undefined)
  })

  it('aborts and clears receipt and reimbursement memory on HTTP 401', () => {
    useAuthStore()
    const expense = seedReceiptMemory()
    const drafts = seedDraftMemory()
    const submission = useReimbursementSubmissionStore()
    submission.submission = {
      submissionId: 'submission-a',
      draftId: 'draft-a',
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
    }
    let signal: AbortSignal | undefined
    vi.mocked(getOaReimbursementOptions).mockImplementation((options) => {
      signal = options?.signal
      return new Promise(() => undefined)
    })
    void drafts.loadReimbursementOptions()

    callbacks.unauthorized?.()

    expect(signal?.aborted).toBe(true)
    expect(expense.receiptFiles).toEqual([])
    expect(expense.items).toEqual([])
    expect(drafts.drafts).toEqual([])
    expect(drafts.reimbursementOptions).toBeNull()
    expect(submission.submission).toBeNull()
  })

  it('loads and refreshes OA submission availability independently of authentication', async () => {
    const config = {
      corpId: 'corp', clientId: 'client', authMockEnabled: false, oaSubmissionEnabled: false,
      uploadLimits: { maxFiles: 10, maxFileBytes: 1_000_000, maxSessionBytes: 10_000_000 },
      expenseLimits: { maxItems: 100 },
    }
    vi.mocked(getPublicConfig).mockResolvedValue(config)
    vi.mocked(getMe).mockResolvedValue(session())
    const auth = useAuthStore()
    await auth.bootstrap()
    expect(auth.status).toBe('authenticated')
    expect(useReimbursementSubmissionStore().oaSubmissionEnabled).toBe(false)
    vi.mocked(getPublicConfig).mockResolvedValue({ ...config, oaSubmissionEnabled: true })
    await auth.refreshPublicConfig()
    expect(useReimbursementSubmissionStore().oaSubmissionEnabled).toBe(true)
    expect(auth.status).toBe('authenticated')
  })

  it('ends the authenticated calculation scope while logout is still in flight', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    let finishLogout!: () => void
    vi.mocked(logoutRequest).mockReturnValue(new Promise<void>((resolve) => { finishLogout = resolve }))
    const pending = auth.logout()
    expect(auth.status).not.toBe('authenticated')
    finishLogout()
    await pending
    expect(auth.status).toBe('unauthorized')
    expect(auth.session).toBeNull()
  })

  it('clears files, OCR candidates and reimbursement state after logout', async () => {
    const auth = useAuthStore()
    const expense = seedReceiptMemory()
    const drafts = seedDraftMemory()

    await auth.logout()

    expect(logoutRequest).toHaveBeenCalledOnce()
    expect(expense.receiptFiles).toEqual([])
    expect(expense.items).toEqual([])
    expect(drafts.drafts).toEqual([])
    expect(drafts.reimbursementOptions).toBeNull()
  })

  it('clears client state even when the logout request fails', async () => {
    const auth = useAuthStore()
    const expense = seedReceiptMemory()
    const drafts = seedDraftMemory()
    vi.mocked(logoutRequest).mockRejectedValue(new Error('network unavailable'))

    await expect(auth.logout()).rejects.toThrow('network unavailable')

    expect(expense.receiptFiles).toEqual([])
    expect(drafts.drafts).toEqual([])
    expect(auth.session).toBeNull()
    expect(auth.status).toBe('unauthorized')
  })

  it('resets scoped stores before switching department', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    const expense = seedReceiptMemory()
    const drafts = seedDraftMemory()
    vi.mocked(selectDepartmentRequest).mockResolvedValue({
      id: '200', name: '另一个部门',
    })

    await auth.selectDepartment('200')

    expect(selectDepartmentRequest).toHaveBeenCalledWith('200')
    expect(auth.session?.selectedDepartment?.id).toBe('200')
    expect(expense.receiptFiles).toEqual([])
    expect(drafts.drafts).toEqual([])
  })

  it('binds reimbursement scope from a travel approval without manual department input', async () => {
    const auth = useAuthStore()
    auth.session = { ...session(), selectedDepartment: null }
    auth.status = 'department_required'
    seedReceiptMemory()
    const drafts = seedDraftMemory()
    const selection = {
      processInstanceId: 'travel-instance-20',
      profileKey: 'domestic',
      queryWindow: { from: '2026-07-01', to: '2026-07-31' },
    }
    vi.mocked(selectDepartmentFromTravelApprovalRequest).mockResolvedValue({
      id: '200', name: '工业物联二部',
    })

    await auth.selectDepartmentFromTravelApproval(selection)

    expect(selectDepartmentFromTravelApprovalRequest).toHaveBeenCalledWith(selection)
    expect(auth.status).toBe('authenticated')
    expect(auth.session?.selectedDepartment).toEqual({ id: '200', name: '工业物联二部' })
    expect(auth.session?.departments[0]).toEqual({ id: '200', name: '工业物联二部' })
    expect(drafts.drafts).toEqual([])
  })

  it('clears reimbursement state when refresh discovers a different user', async () => {
    const auth = useAuthStore()
    auth.session = session('user-a', '100', 'old-csrf')
    auth.status = 'authenticated'
    const drafts = seedDraftMemory()
    vi.mocked(getMe).mockResolvedValue(session('user-b', '100', 'new-csrf'))

    await auth.refreshMe()

    expect(auth.session?.user.userId).toBe('user-b')
    expect(drafts.drafts).toEqual([])
  })

  it('preserves reimbursement state for a CSRF-only session refresh', async () => {
    const auth = useAuthStore()
    auth.session = session('user-a', '100', 'old-csrf')
    auth.status = 'authenticated'
    const drafts = seedDraftMemory()
    vi.mocked(getMe).mockResolvedValue(session('user-a', '100', 'rotated-csrf'))

    await auth.refreshMe()

    expect(auth.session?.csrfToken).toBe('rotated-csrf')
    expect(drafts.drafts.map((draft) => draft.id)).toEqual(['draft-a'])
  })
})

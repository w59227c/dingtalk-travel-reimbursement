import savedExpenseReadback from './savedExpenseReadback.json'
import deletedReceiptReadback from './deletedReceiptReadback.json'
import type { AuthSession } from '@/types/auth'
import type {
  ReimbursementDraft, ReimbursementDraftFile, ReimbursementDraftFileList,
  ReimbursementDraftInput, ReimbursementSubmission,
} from '@/types/reimbursements'

/** Synthetic JSON fixtures. Nested overrides replace that whole field and are copied. */
function copyFixture<T>(value: T): T { return structuredClone(value) }

export function receiptExpenseInputFixture(): ReimbursementDraftInput {
  return {
    ocrDispositionVersion: 1, companyValue: '北京', budgetCodeValue: 'DEMO-001',
    project: { mode: 'manual', text: '示例项目' }, trip: null, dismissedOcrFileIds: [],
    items: [{ sourceFileId: 'file-1', category: 'local_transport', date: '2026-09-01',
      displayDate: '2026-09-01', description: '机场至酒店', amount: '44.89', receiptCount: 1 }],
  }
}

export function manualExpenseInputFixture(): ReimbursementDraftInput {
  return {
    ocrDispositionVersion: 1, companyValue: '北京', budgetCodeValue: 'DEMO-001',
    project: { mode: 'manual', text: '示例项目' }, trip: null, dismissedOcrFileIds: [],
    items: [{ category: 'other', date: '2026-09-01', displayDate: '2026-09-01',
      description: '打车费', amount: '44.89', receiptCount: 1 }],
  }
}

function noSubsidyEditingState(): NonNullable<ReimbursementDraftInput['editingState']> {
  return { includeSubsidy: false, trip: {
    tripType: 'business', startDate: '', startTime: '09:00', endDate: '', endTime: '18:00',
    policyConfirmed: false, confirmedEffectiveDays: '', noSubsidyException: false,
  }, trips: [] }
}

export function noSubsidyReceiptInputFixture(): ReimbursementDraftInput {
  const input = receiptExpenseInputFixture()
  return { ...input, editingState: noSubsidyEditingState(), items: input.items.map(item => ({
    ...item, itineraryFileIds: [], itineraryAutoMatchDisabled: false, paymentProofFileIds: [],
    hotelBillFileIds: [], railType: 'unknown', requiresItinerary: false,
    cnyAmountConfirmed: false, requiresCnyConfirmation: false,
  })) }
}

export function emptyTravelInputFixture(): ReimbursementDraftInput {
  return { ocrDispositionVersion: 1, companyValue: '', budgetCodeValue: '', trip: null,
    trips: [], editingState: noSubsidyEditingState(), items: [], dismissedOcrFileIds: [] }
}

export function reimbursementDraftFixture(overrides: Partial<ReimbursementDraft> = {}): ReimbursementDraft {
  return copyFixture({
    id: 'draft-1', status: 'DRAFT', revision: 1,
    department: { id: '100', name: '测试部门' }, templateConfigVersion: 12, relatedApprovalCount: 0,
    expiresAt: '2026-10-04T00:00:00Z', createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z', lockedAt: null,
    template: { processCode: 'PROC-REIMBURSEMENT', configVersion: 12, schemaFingerprint: 'a'.repeat(64) },
    input: receiptExpenseInputFixture(), relatedApprovals: [], relatedApprovalSummary: null,
    totals: { expenseTotal: '44.89', subsidyTotal: '0.00', totalAmount: '44.89', receiptCount: 1,
      uppercaseAmount: '肆拾肆元捌角玖分', subsidy: null },
    ...overrides,
  })
}

export function reimbursementFileFixture(overrides: Partial<ReimbursementDraftFile> = {}): ReimbursementDraftFile {
  return copyFixture({ id: 'file-1', name: '票据.jpg', role: 'EXPENSE_SOURCE', attachmentKind: 'other',
    sortOrder: 0, status: 'ACTIVE', mediaType: 'image/jpeg', sizeBytes: 5,
    ocrStatus: 'NOT_REQUESTED', ocrResult: null, ...overrides })
}

export function draftFileListFixture(draft: Pick<ReimbursementDraft, 'id' | 'revision'>,
  items: ReimbursementDraftFile[], pendingUploads?: ReimbursementDraftFileList['pendingUploads']): ReimbursementDraftFileList {
  return copyFixture({ draftId: draft.id, revision: draft.revision, items,
    ...(pendingUploads === undefined ? {} : { pendingUploads }) })
}

export function authSessionFixture(overrides: Partial<AuthSession> = {}): AuthSession {
  return copyFixture({ user: { userId: 'synthetic-user', name: '测试用户' },
    departments: [{ id: '100', name: '测试部门' }], selectedDepartment: { id: '100', name: '测试部门' },
    isAdmin: false, csrfToken: 'synthetic-csrf', ...overrides })
}

export function reimbursementSubmissionFixture(overrides: Partial<ReimbursementSubmission> = {}): ReimbursementSubmission {
  return copyFixture({ submissionId: 'submission-1', draftId: 'draft-1', status: 'QUEUED',
    statusVersion: 1, attemptCount: 0, processInstanceId: null, businessId: null, approvalUrl: null,
    error: null, pollAfterMs: 1500, createdAt: '2026-09-04T00:00:00Z', updatedAt: '2026-09-04T00:00:00Z',
    submittedAt: null, ...overrides })
}

/**
 * Checked-in wire samples come from backend _canonical_input_data(ReimbursementDraftInput
 * .model_validate(...)), the serializer used by draft_data, including nullable editing IDs
 * and omitted empty receipt references.
 * They stay independent of the frontend comparison code and are never derived from a request at runtime.
 */
export function savedExpenseReadbackFixture() {
  const request = noSubsidyReceiptInputFixture()
  request.items[0] = { ...request.items[0]!, amount: '44.8', description: '合成员工已保存的修改',
    receiptAttachmentFileIds: [] }
  // Wire editing IDs include null; the editable TripInput type models omission instead.
  const readback = copyFixture(savedExpenseReadback) as unknown as ReimbursementDraftInput
  return { request, readback }
}

/** The backend omits a deleted last receipt reference; remaining references stay ordered. */
export function deletedReceiptReadbackFixture(remaining: readonly string[]) {
  const before = copyFixture(deletedReceiptReadback.before) as ReimbursementDraftInput
  before.items[0]!.receiptAttachmentFileIds = ['file-1', ...remaining]
  const readback = copyFixture(deletedReceiptReadback.readback) as ReimbursementDraftInput
  if (remaining.length) readback.items[0]!.receiptAttachmentFileIds = [...remaining]
  else delete readback.items[0]!.receiptAttachmentFileIds
  return { before, readback }
}

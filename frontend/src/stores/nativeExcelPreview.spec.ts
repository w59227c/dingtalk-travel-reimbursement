import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { requestReimbursementDraftExcelPreviewTicket } from '@/api/reimbursements'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { ReimbursementDraft, ReimbursementDraftInput } from '@/types/reimbursements'

const bridge = vi.hoisted(() => ({}) as Record<string, unknown>)
vi.mock('dingtalk-jsapi', () => ({ default: bridge }))
vi.mock('@/api/reimbursements', async (original) => ({
  ...await original<typeof import('@/api/reimbursements')>(),
  requestReimbursementDraftExcelPreviewTicket: vi.fn(),
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

describe('native preview session isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    for (const key of Object.keys(bridge)) delete bridge[key]
  })
  it('cancels the real native bridge when the originating session is reset', async () => {
    const store = useReimbursementDraftStore()
    store.currentDraft = draft('old-session-draft')
    vi.mocked(requestReimbursementDraftExcelPreviewTicket).mockResolvedValue({
      downloadUrl: '/api/native/old-session', downloadToken: 'synthetic-ticket', fileType: 'xlsx',
    } as never)
    let downloaded!: (value: { filePath: string }) => void
    let started!: () => void
    const ready = new Promise<void>((resolve) => { started = resolve })
    bridge.ready = (callback: () => void) => callback()
    bridge.downloadFile = vi.fn((input: { success: (result: { filePath: string }) => void }) => {
      downloaded = input.success
      started()
    })
    bridge.openDocument = vi.fn((input: { success: (result: Record<string, never>) => void }) => input.success({}))
    const opening = store.openExcelPreviewInDingTalk()
    const rejected = expect(opening).rejects.toMatchObject({ name: 'AbortError' })
    await ready
    store.reset()
    store.currentDraft = draft('replacement-session-draft')
    downloaded({ filePath: 'file:///synthetic-old-session.xlsx' })
    await rejected
    expect(bridge.openDocument).not.toHaveBeenCalled()
    expect(store.currentDraft.id).toBe('replacement-session-draft')
  })
})

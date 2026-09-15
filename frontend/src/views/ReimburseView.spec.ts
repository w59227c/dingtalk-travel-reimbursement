/* eslint-disable vue/one-component-per-file */
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick } from 'vue'

import { getPublicConfig, selectDepartmentFromTravelApproval } from '@/api/auth'
import { calculateTotals } from '@/api/expenses'
import { fetchReadiness } from '@/api/health'
import {
  createReimbursementDraft,
  deleteReimbursementDraft,
  deleteReimbursementDraftFile,
  clearReimbursementDraftFiles,
  getOaReimbursementOptions,
  getOaReimbursementSubmission,
  getOaReimbursementSubmissionForDraft,
  getReimbursementDraft,
  listOaTravelApprovals,
  listReimbursementDraftFiles,
  listReimbursementDrafts,
  markReimbursementDraftReviewReady,
  replaceReimbursementRelatedApprovals,
  submitOaReimbursement,
  updateReimbursementDraft,
} from '@/api/reimbursements'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'
import type {
  ReimbursementDraft,
  ReimbursementDraftFile,
  ReimbursementDraftInput,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
  ReimbursementSubmission,
} from '@/types/reimbursements'
import ReimburseView from './ReimburseView.vue'

vi.mock('@/api/health', () => ({ fetchReadiness: vi.fn() }))
vi.mock('@/api/auth', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/auth')>(),
  getPublicConfig: vi.fn(),
  selectDepartmentFromTravelApproval: vi.fn(),
}))
vi.mock('@/api/expenses', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/expenses')>()
  return { ...actual, calculateTotals: vi.fn() }
})
vi.mock('@/api/reimbursements', () => ({
  createReimbursementDraft: vi.fn(),
  deleteReimbursementDraft: vi.fn(),
  deleteReimbursementDraftFile: vi.fn(),
  clearReimbursementDraftFiles: vi.fn(),
  getOaReimbursementOptions: vi.fn(),
  getOaReimbursementSubmission: vi.fn(),
  getOaReimbursementSubmissionForDraft: vi.fn(),
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

const options = {
  templateConfigVersion: 12,
  reimbursementProcessCode: 'PROC-REIMBURSEMENT',
  companyOptions: [{ value: '北京', label: '北京分公司', key: 'beijing' }],
  budgetCodeOptions: [{ value: '26007', label: '26007 · MES 项目', key: null }],
  travelProfiles: [{
    profileKey: 'business',
    displayName: '境内出差',
    processCode: 'PROC-TRAVEL',
    schemaFingerprint: 'b'.repeat(64),
    travelTypeOption: { value: 'business', label: '境内出差', key: null },
  }],
}

const baseInput: ReimbursementDraftInput = {
  ocrDispositionVersion: 1,
  companyValue: '北京',
  budgetCodeValue: '26007',
  project: { mode: 'manual', text: '合肥示例前道 MES 项目' },
  trip: null,
  dismissedOcrFileIds: [],
  items: [{
    sourceFileId: 'file-1',
    category: 'local_transport',
    date: '2026-09-01',
    displayDate: '2026-09-01',
    description: '机场至酒店',
    amount: '44.89',
    receiptCount: 1,
  }],
}

const linkedApproval: ReimbursementRelatedApproval = {
  processInstanceId: 'travel-instance-1',
  profileKey: 'business',
  sourceProcessCode: 'PROC-TRAVEL',
  title: '合肥出差申请',
  businessId: 'TRAVEL-20260901',
  startDate: '2026-08-31',
  endDate: '2026-09-02',
  queryWindow: {
    startTimeMs: Date.parse('2026-08-01T00:00:00+08:00'),
    endTimeMs: Date.parse('2026-09-04T23:59:59.999+08:00'),
  },
  verifiedAt: '2026-09-04T01:00:00Z',
}

const selection: ReimbursementRelatedApprovalSelection = {
  processInstanceId: 'travel-instance-1',
  profileKey: 'business',
  queryWindow: { from: '2026-08-01', to: '2026-09-04' },
}

const activeFile: ReimbursementDraftFile = {
  id: 'file-1',
  name: '打车发票.pdf',
  role: 'EXPENSE_SOURCE',
  attachmentKind: 'other',
  sortOrder: 0,
  status: 'ACTIVE',
  mediaType: 'application/pdf',
  sizeBytes: 128,
  ocrStatus: 'COMPLETE',
  ocrResult: null,
}

function makeDraft(overrides: Partial<ReimbursementDraft> = {}): ReimbursementDraft {
  return {
    id: 'draft-1',
    status: 'DRAFT',
    revision: 1,
    department: { id: '100', name: '测试部门' },
    templateConfigVersion: 12,
    relatedApprovalCount: 0,
    expiresAt: '2026-10-04T00:00:00Z',
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
    lockedAt: null,
    template: {
      processCode: 'PROC-REIMBURSEMENT',
      configVersion: 12,
      schemaFingerprint: 'a'.repeat(64),
    },
    input: structuredClone(baseInput),
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
    ...overrides,
  }
}

function submissionResult(
  overrides: Partial<ReimbursementSubmission> = {},
): ReimbursementSubmission {
  return {
    submissionId: 'submission-1',
    draftId: 'draft-1',
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
    ...overrides,
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

const ExpenseItemsCardStub = defineComponent({
  name: 'ExpenseItemsCard',
  props: {
    durable: { type: Boolean, default: false },
    mobile: { type: Boolean, default: false },
    readonly: { type: Boolean, default: false },
  },
  template: `<section
    data-testid="expense-items"
    :data-durable="String(durable)"
    :data-mobile="String(mobile)"
    :data-readonly="String(readonly)"
  >费用明细</section>`,
})
const TripSubsidyCardStub = defineComponent({
  name: 'TripSubsidyCard',
  props: {
    mobile: { type: Boolean, default: false },
    readonly: { type: Boolean, default: false },
  },
  template: '<section :data-mobile="String(mobile)" :data-readonly="String(readonly)">出差补助</section>',
})
const ExpenseSummaryCardStub = defineComponent({
  name: 'ExpenseSummaryCard',
  props: {
    previewDisabledReason: { type: String, default: '' },
    beforePreview: { type: Function, default: undefined },
  },
  template: '<section data-testid="expense-summary">费用合计</section>',
})
const TravelApprovalSelectorStub = defineComponent({
  name: 'TravelApprovalSelector',
  props: {
    mobile: { type: Boolean, default: false },
    modelValue: { type: Array, required: true },
    linkedApprovals: { type: Array, default: () => [] },
    readonly: { type: Boolean, default: false },
    requiredStartDate: { type: String, default: '' },
    requiredEndDate: { type: String, default: '' },
    single: { type: Boolean, default: false },
  },
  emits: ['update:modelValue'],
  template: '<section data-testid="travel-selector">关联出差审批</section>',
})

let serverDraft: ReimbursementDraft
let serverFiles: ReimbursementDraftFile[]

function installServerMocks(): void {
  vi.mocked(getPublicConfig).mockResolvedValue({
    corpId: 'corp', clientId: 'client', authMockEnabled: true, oaSubmissionEnabled: true,
    uploadLimits: { maxFiles: 10, maxFileBytes: 1_000_000, maxSessionBytes: 10_000_000 },
    expenseLimits: { maxItems: 100 },
  })
  vi.mocked(fetchReadiness).mockResolvedValue({
    status: 'ready',
    checks: { database: 'ok', excelTemplate: 'ok', tempStorage: 'ok', ocr: 'disabled' },
  })
  vi.mocked(selectDepartmentFromTravelApproval).mockResolvedValue({
    selectedDepartment: { id: '100', name: '测试部门' },
    selectionRequired: false,
    departments: [{ id: '100', name: '测试部门' }],
  })
  vi.mocked(calculateTotals).mockResolvedValue(serverDraft.totals)
  vi.mocked(getOaReimbursementOptions).mockResolvedValue(options)
  vi.mocked(listReimbursementDrafts).mockImplementation(async () => ({
    items: [serverDraft],
    offset: 0,
    limit: 50,
    total: 1,
  }))
  vi.mocked(getReimbursementDraft).mockImplementation(async () => serverDraft)
  vi.mocked(listReimbursementDraftFiles).mockImplementation(async () => ({
    draftId: serverDraft.id,
    revision: serverDraft.revision,
    items: serverFiles,
  }))
  vi.mocked(listOaTravelApprovals).mockResolvedValue({
    templateConfigVersion: 12,
    queryWindow: selection.queryWindow,
    items: [],
  })
  vi.mocked(updateReimbursementDraft).mockImplementation(
    async (_draftId, expectedRevision, input) => {
      serverDraft = {
        ...serverDraft,
        status: 'DRAFT',
        revision: expectedRevision + 1,
        input: structuredClone(input),
        updatedAt: '2026-09-04T00:01:00Z',
      }
      return serverDraft
    },
  )
  vi.mocked(replaceReimbursementRelatedApprovals).mockImplementation(
    async (_draftId, expectedRevision, selections) => {
      const hasSelection = selections.length > 0
      serverDraft = {
        ...serverDraft,
        status: 'DRAFT',
        revision: expectedRevision + 1,
        relatedApprovalCount: selections.length,
        input: { ...serverDraft.input, companyValue: hasSelection ? '北京' : '', budgetCodeValue: hasSelection ? '26007' : '', accountingSourceVerified: hasSelection },
        relatedApprovals: hasSelection ? [linkedApproval] : [],
        relatedApprovalSummary: hasSelection
          ? { count: 1, startDate: linkedApproval.startDate, endDate: linkedApproval.endDate }
          : null,
      }
      return serverDraft
    },
  )
  vi.mocked(markReimbursementDraftReviewReady).mockImplementation(
    async (_draftId, expectedRevision) => {
      serverDraft = {
        ...serverDraft,
        status: 'REVIEW_READY',
        revision: expectedRevision + 1,
      }
      return serverDraft
    },
  )
  vi.mocked(createReimbursementDraft).mockImplementation(async (input) => {
    serverDraft = makeDraft({ id: 'draft-created', input, revision: 1 })
    serverFiles = []
    return serverDraft
  })
  vi.mocked(submitOaReimbursement).mockResolvedValue(submissionResult())
}

async function mountView(setup?: (auth: ReturnType<typeof useAuthStore>) => void, realMaterials = false, mobile = false): Promise<{
  wrapper: VueWrapper
  expense: ReturnType<typeof useExpenseStore>
  drafts: ReturnType<typeof useReimbursementDraftStore>
}> {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.status = 'authenticated'
  auth.session = {
    user: { userId: 'synthetic-user', name: '测试用户' },
    departments: [{ id: '100', name: '测试部门' }],
    selectedDepartment: { id: '100', name: '测试部门' },
    isAdmin: true,
    csrfToken: 'synthetic-csrf',
  }
  useReimbursementSubmissionStore().oaSubmissionEnabled = true
  setup?.(auth)
  const expense = useExpenseStore()
  expense.categories = [
    { id: 'local_transport', name: '市内交通费', order: 1, manualSelectable: true },
  ]
  const wrapper = mount(ReimburseView, {
    attachTo: '#test-app',
    props: { mobile },
    global: {
      plugins: [pinia, ElementPlus],
      stubs: {
        ExpenseItemsCard: realMaterials ? false : ExpenseItemsCardStub,
        ExpenseSummaryCard: ExpenseSummaryCardStub,
        RouterLink: {
          props: ['to'],
          template: '<a :data-to="to"><slot /></a>',
        },
        TravelApprovalSelector: TravelApprovalSelectorStub,
        TripSubsidyCard: TripSubsidyCardStub,
      },
    },
  })
  await flushPromises()
  return { wrapper, expense, drafts: useReimbursementDraftStore() }
}

function visibleButton(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  if (!button) throw new Error(`Missing button: ${label}`)
  return button
}

describe('ReimburseView single-form OA flow', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="test-app"></div>'
    window.sessionStorage.clear()
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    window.requestAnimationFrame = (callback: FrameRequestCallback) => { callback(0); return 0 }
    window.cancelAnimationFrame = () => undefined
    vi.clearAllMocks()
    serverDraft = makeDraft()
    serverFiles = [activeFile]
    installServerMocks()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    window.sessionStorage.clear()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  async function saveCurrent(wrapper: VueWrapper): Promise<void> {
    await wrapper.findComponent(ExpenseSummaryCardStub).props('beforePreview')!()
    await flushPromises()
  }

  it('shows the current userId unobtrusively in the page header', async () => {
    const { wrapper } = await mountView()

    expect(wrapper.find('.current-user-id').text()).toContain('synthetic-user')
    wrapper.unmount()
  })

  it('renders the /m presentation as four mobile steps while reusing the same expense editor', async () => {
    const { wrapper } = await mountView(undefined, false, true)

    expect(wrapper.find('.current-user-id').exists()).toBe(false)
    expect(wrapper.get('.hero-actions').text()).toContain('系统设置')
    expect(wrapper.get('.hero-actions a').attributes('data-to')).toBe('/m/settings')
    expect(wrapper.findAll('.mobile-step-nav button').map((button) => button.text())).toEqual([
      '1关联审批', '2范围补助', '3费用材料', '4核对提交',
    ])
    expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(true)
    expect(wrapper.get('[data-testid="expense-items"]').attributes('data-mobile')).toBe('true')
    expect(wrapper.findComponent(TravelApprovalSelectorStub).props('mobile')).toBe(true)
    expect(wrapper.findComponent(TripSubsidyCardStub).props('mobile')).toBe(true)

    await wrapper.findAll('.mobile-step-nav button')[2]!.trigger('click')

    expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(false)
    expect(wrapper.get('[data-testid="mobile-material-step"]').isVisible()).toBe(true)
    expect(wrapper.find('.mobile-step-footer').text()).toContain('¥44.89')
    wrapper.unmount()
  })

  it('reserves the measured height of the fixed mobile footer', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ height: 128 } as DOMRect)
    const { wrapper } = await mountView(undefined, false, true)

    expect(wrapper.get('.page-shell--mobile').attributes('style')).toContain('--mobile-footer-height: 128px')
    expect(wrapper.get('.mobile-step-footer-dock').find('.mobile-step-footer').exists()).toBe(true)
    wrapper.unmount()
  })

  it.each(['success', 'failure'] as const)('clears slow uploaded files without autosaving deleted references (%s)', async (outcome) => {
    vi.useFakeTimers()
    serverFiles = [activeFile, { ...activeFile, id: 'file-2', name: '第二张发票.pdf' }]
    serverDraft.input.items.push({ ...baseInput.items[0]!, sourceFileId: 'file-2' })
    const lastDeletion = deferred<void>()
    const invalidReferences: string[] = []
    const errorMessage = '费用明细引用的票据文件无效，请刷新后重试'
    vi.mocked(updateReimbursementDraft).mockImplementation(async (_id, revision, input) => {
      const live = new Set(serverFiles.map((file) => file.id))
      const references = [...input.items.flatMap((item) => [item.sourceFileId,
        ...(item.itineraryFileIds ?? []), ...(item.paymentProofFileIds ?? [])]), ...(input.dismissedOcrFileIds ?? [])]
      const missing = references.filter((id): id is string => Boolean(id) && !live.has(id!))
      if (missing.length) {
        invalidReferences.push(...missing)
        throw new Error(errorMessage)
      }
      serverDraft = { ...serverDraft, revision: revision + 1, input: structuredClone(input) }
      return serverDraft
    })
    vi.mocked(clearReimbursementDraftFiles).mockImplementation(async (draftId, revision) => {
      serverFiles = serverFiles.filter((file) => file.id === 'file-2').map((file) => ({ ...file, status: 'DELETING' }))
      serverDraft = { ...serverDraft, revision: revision + 1, input: {
        ...serverDraft.input, items: [],
      } }
      await lastDeletion.promise
      serverFiles = []
      return { draftId, deletedFileIds: ['file-1', 'file-2'], revision: revision + 1 }
    })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const { wrapper, drafts, expense } = await mountView(undefined, true)
    try {
      await visibleButton(wrapper, '清空文件').trigger('click')
      await flushPromises()
      expect(clearReimbursementDraftFiles).toHaveBeenCalledOnce()
      expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
      expect(drafts.processingFiles).toBe(true)
      // The save timer fires while the last DELETE still owns the mutation queue.
      await vi.advanceTimersByTimeAsync(650)
      expect(updateReimbursementDraft).not.toHaveBeenCalled()
      await expect(saveCurrent(wrapper)).rejects.toThrow('请等待材料处理完成')
      if (outcome === 'success') lastDeletion.resolve()
      else lastDeletion.reject(new Error('删除连接中断'))
      await flushPromises()
      await vi.advanceTimersByTimeAsync(650)
      await flushPromises()
      expect(drafts.files.map((file) => file.id)).toEqual(outcome === 'success' ? [] : ['file-2'])
      expect(expense.items).toEqual([])
      expect(invalidReferences).toEqual([])
      expect(drafts.mutationError).toBe('')
      expect(wrapper.text()).not.toContain(errorMessage)
      expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    } finally { lastDeletion.resolve(); wrapper.unmount() }
  })

  it.each(['mock_required', 'department_required'] as const)(
    'does not calculate before authentication and department selection (%s), then calculates after login',
    async (status) => {
      vi.useFakeTimers()
      const { wrapper, expense } = await mountView((auth) => {
        auth.status = status
        if (status === 'mock_required') auth.session = null
        else auth.session!.selectedDepartment = null
      })
      expense.reset()
      await nextTick()
      await vi.advanceTimersByTimeAsync(300)
      expect(calculateTotals).not.toHaveBeenCalled()
      const auth = useAuthStore()
      auth.session = {
        user: { userId: 'synthetic-user', name: '测试用户' },
        departments: [{ id: '100', name: '测试部门' }],
        selectedDepartment: { id: '100', name: '测试部门' },
        isAdmin: true, csrfToken: 'new-csrf',
      }
      auth.status = 'authenticated'
      await flushPromises()
      await vi.advanceTimersByTimeAsync(300)
      expect(calculateTotals).toHaveBeenCalledOnce()
      wrapper.unmount()
    },
  )

  it('derives a multi-department user scope from the selected travel approval', async () => {
    const { wrapper } = await mountView((auth) => {
      auth.status = 'department_required'
      auth.session!.selectedDepartment = null
      auth.session!.departments = [
        { id: '100', name: '技术管理中心' },
        { id: '200', name: '工业物联二部' },
      ]
    })
    vi.mocked(selectDepartmentFromTravelApproval).mockResolvedValue({
      selectedDepartment: { id: '200', name: '工业物联二部' },
      selectionRequired: false,
      departments: [
        { id: '100', name: '技术管理中心' },
        { id: '200', name: '工业物联二部' },
      ],
    })

    expect(wrapper.text()).not.toContain('选择本次报销部门')
    expect(wrapper.text()).not.toContain('确认部门')
    const selector = wrapper.findComponent(TravelApprovalSelectorStub)
    expect(selector.props('single')).toBe(true)
    selector.vm.$emit('update:modelValue', [selection])
    await flushPromises()

    expect(selectDepartmentFromTravelApproval).toHaveBeenCalledWith(selection, undefined)
    expect(useAuthStore().session?.selectedDepartment).toEqual({
      id: '200', name: '工业物联二部',
    })
    expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith(
      'draft-1',
      expect.any(Number),
      [selection],
      { signal: expect.any(AbortSignal) },
    )
    wrapper.unmount()
  })

  it.each([false, true])(
    'asks for a filtered current department when the approval department is historical (mobile=%s)',
    async (mobile) => {
      const { wrapper } = await mountView((auth) => {
        auth.status = 'department_required'
        auth.session!.selectedDepartment = null
        auth.session!.departments = [
          { id: 'other', name: '其他临时部门' },
          { id: '100', name: '技术管理中心' },
          { id: '300', name: '技术管理中心' },
          { id: '400', name: '产品开发部' },
        ]
      }, false, mobile)
      vi.mocked(selectDepartmentFromTravelApproval).mockImplementation(
        async (_selection, selectedDepartmentId) => selectedDepartmentId
          ? {
              selectedDepartment: { id: '400', name: '产品开发部' },
              selectionRequired: false,
              departments: [
                { id: '100', name: '技术管理中心' },
                { id: '400', name: '产品开发部' },
              ],
            }
          : {
              selectedDepartment: null,
              selectionRequired: true,
              departments: [
                { id: 'other', name: '其他临时部门' },
                { id: '100', name: '技术管理中心' },
                { id: '300', name: '技术管理中心' },
                { id: '400', name: '产品开发部' },
              ],
            },
      )

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit(
        'update:modelValue',
        [selection],
      )
      await flushPromises()

      const dialog = wrapper.findComponent({ name: 'ElDialog' })
      expect(dialog.props('modelValue')).toBe(true)
      expect(wrapper.findAllComponents({ name: 'ElOption' }).map(
        (option) => option.props('label'),
      )).toEqual(['技术管理中心', '产品开发部'])
      wrapper.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', '400')
      await nextTick()
      await visibleButton(wrapper, '确认部门').trigger('click')
      await flushPromises()

      expect(selectDepartmentFromTravelApproval).toHaveBeenNthCalledWith(
        1,
        selection,
        undefined,
      )
      expect(selectDepartmentFromTravelApproval).toHaveBeenNthCalledWith(
        2,
        selection,
        '400',
      )
      expect(useAuthStore().session?.selectedDepartment).toEqual({
        id: '400',
        name: '产品开发部',
      })
      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith(
        'draft-1',
        expect.any(Number),
        [selection],
        { signal: expect.any(AbortSignal) },
      )
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'switches to another current department before linking its approval (mobile=%s)',
    async (mobile) => {
      const { wrapper, drafts } = await mountView((auth) => {
        auth.session!.departments = [
          { id: '100', name: '技术管理中心' },
          { id: '200', name: '产品开发部' },
        ]
        auth.session!.selectedDepartment = { id: '100', name: '技术管理中心' }
      }, false, mobile)
      drafts.travelApprovals = [{
        ...linkedApproval,
        originatorDepartmentId: '200',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '境内出差', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      serverDraft = makeDraft({
        department: { id: '200', name: '产品开发部' },
      })
      vi.mocked(selectDepartmentFromTravelApproval).mockResolvedValue({
        selectedDepartment: { id: '200', name: '产品开发部' },
        selectionRequired: false,
        departments: [
          { id: '100', name: '技术管理中心' },
          { id: '200', name: '产品开发部' },
        ],
      })

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit(
        'update:modelValue',
        [selection],
      )
      await flushPromises()

      expect(selectDepartmentFromTravelApproval).toHaveBeenCalledWith(
        selection,
        undefined,
      )
      expect(useAuthStore().session?.selectedDepartment).toEqual({
        id: '200',
        name: '产品开发部',
      })
      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith(
        'draft-1',
        expect.any(Number),
        [selection],
        { signal: expect.any(AbortSignal) },
      )
      wrapper.unmount()
    },
  )

  it('cancels a pending calculation when authentication ends', async () => {
    vi.useFakeTimers()
    const { wrapper, expense } = await mountView()
    await vi.advanceTimersByTimeAsync(300)
    vi.mocked(calculateTotals).mockClear()
    expense.items[0]!.amount = '45.00'
    await nextTick()
    useAuthStore().session = null
    useAuthStore().status = 'unauthorized'
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)
    expect(calculateTotals).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('disables OA submission while keeping editing, saving and preview available when the worker is disabled', async () => {
    const { wrapper, expense } = await mountView(() => {
      useReimbursementSubmissionStore().oaSubmissionEnabled = false
    })
    expect(wrapper.text()).toContain('OA提交服务未开启，可继续填写')
    expect(wrapper.text()).toContain('基础服务已就绪')
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toBe('')
    expense.items[0]!.amount = '46.00'
    await nextTick()
    await saveCurrent(wrapper)
    expect(updateReimbursementDraft).toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    await visibleButton(wrapper, '刷新服务状态').trigger('click')
    await flushPromises()
    expect(getPublicConfig).toHaveBeenCalledOnce()
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('shows a per-expense material checklist and keeps preview/editing available until proofs are attached', async () => {
    const { wrapper, expense, drafts } = await mountView()
    expense.items[0]!.amount = '600.00'
    await nextTick()
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('还有 1 笔费用需补材料')
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('付款凭证')
    expect(visibleButton(wrapper, '去补齐').exists()).toBe(true)
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toBe('')
    drafts.files.push({ ...activeFile, id: 'payment-1', role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof', status: 'WRITING' })
    expense.items[0]!.paymentProofFileIds = ['payment-1']
    await nextTick()
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    drafts.files[1]!.status = 'ACTIVE'
    await nextTick()
    expect(wrapper.find('[data-testid="material-checklist"]').exists()).toBe(false)
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeUndefined()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('blocks unconfirmed material purposes without treating them as invoices or disabling preview', async () => {
    const { wrapper, expense, drafts } = await mountView()
    drafts.files.push({ ...activeFile, id: 'unknown-1', role: 'ATTACHMENT_ONLY', attachmentKind: 'other',
      materialClassification: { status: 'needs_confirmation', kind: 'unknown', reason: '请确认用途', pageCount: 1 } })
    await nextTick()
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('1 份材料需处理后才能提交 OA')
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('材料用途待确认')
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toBe('')
    expect(expense.items).toHaveLength(1)
    drafts.files[1]!.materialClassification!.status = 'confirmed'
    await nextTick()
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('disables submission and lists a terminal OCR source that has no disposition', async () => {
    const { wrapper, drafts } = await mountView()
    drafts.files.push({
      ...activeFile,
      id: 'unresolved-1',
      name: '待处理票据.pdf',
      ocrStatus: 'FAILED',
    })
    await nextTick()

    const checklist = wrapper.get('[data-testid="material-checklist"]')
    expect(checklist.text()).toContain('1 份材料需处理后才能提交 OA')
    expect(checklist.text()).toContain('待处理票据.pdf')
    expect(checklist.text()).toContain('票据尚未加入费用明细')
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('restores queued work without polling a disabled worker and refreshes safely after service recovery', async () => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult())
    vi.mocked(getOaReimbursementSubmission).mockResolvedValue(submissionResult())
    const { wrapper } = await mountView(() => {
      useReimbursementSubmissionStore().oaSubmissionEnabled = false
    })
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('等待 OA 提交服务开启，尚未发起审批')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已排队，内容已锁定')
    expect(wrapper.text()).not.toContain('再报销一笔')
    expect(wrapper.text()).not.toContain('重新填写')
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getOaReimbursementSubmission).not.toHaveBeenCalled()
    await visibleButton(wrapper, '刷新提交进度').trigger('click')
    await flushPromises()
    expect(getPublicConfig).toHaveBeenCalledOnce()
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1_500)
    expect(getOaReimbursementSubmission).toHaveBeenCalledTimes(2)
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('loads DingTalk budget choices and restores one form without project or draft management', async () => {
    const { wrapper, expense, drafts } = await mountView()
    expect(getOaReimbursementOptions).toHaveBeenCalledOnce()
    expect(drafts.reimbursementOptions).toEqual(options)
    expect(expense.manualProjectText).toBe('26007 · MES 项目')
    expect(expense.items).toHaveLength(1)
    expect(wrapper.text()).toContain('预算代码 / 项目')
    expect(wrapper.text()).not.toContain('草稿')
    expect(wrapper.text()).not.toContain('内部项目')
    expect(wrapper.text()).not.toContain('项目管理')
    const basics = wrapper.get('[data-testid="reimbursement-basics-card"]')
    expect(basics.text()).toContain('北京分公司')
    expect(basics.text()).toContain('26007 · MES 项目')
    expect(wrapper.findAllComponents({ name: 'ElSelect' })).toHaveLength(0)
    wrapper.unmount()
  })

  it('automatically provisions an empty form before company, budget or receipts are entered', async () => {
    vi.mocked(listReimbursementDrafts).mockResolvedValue({ items: [], offset: 0, limit: 50, total: 0 })
    const { wrapper, drafts } = await mountView()
    expect(createReimbursementDraft).toHaveBeenCalledWith(
      expect.objectContaining({
        companyValue: '', budgetCodeValue: '', items: [], trip: null,
        editingState: expect.objectContaining({ includeSubsidy: false }),
      }),
      { signal: expect.any(AbortSignal) },
    )
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.text()).not.toContain('创建')
    wrapper.unmount()
  })

  it('derives read-only accounting from approvals and clears it without losing expenses or subsidy edits', async () => {
    serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
    const { wrapper, expense } = await mountView()
    const selector = wrapper.findComponent(TravelApprovalSelectorStub)
    const basics = wrapper.get('[data-testid="reimbursement-basics-card"]')
    expect(basics.findComponent(TravelApprovalSelectorStub).exists()).toBe(true)
    expect(basics.text().indexOf('基本信息')).toBeLessThan(basics.text().indexOf('关联出差审批'))
    selector.vm.$emit('update:modelValue', [selection])
    await vi.waitFor(() => expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledTimes(1), { timeout: 2000 })
    await flushPromises()
    expect(wrapper.get('[data-testid="derived-accounting-summary"]').text()).toContain('北京分公司')
    expect(wrapper.get('[data-testid="derived-accounting-summary"]').text()).toContain('26007 · MES 项目')
    const items = JSON.parse(JSON.stringify(expense.items))
    expense.includeSubsidy = true
    expense.trip.startDate = '2026-09-01'
    expense.trip.endDate = ''
    selector.vm.$emit('update:modelValue', [])
    await vi.waitFor(() => expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledTimes(2), { timeout: 2000 })
    await flushPromises()
    expect(wrapper.get('[data-testid="derived-accounting-summary"]').text()).toContain('选择出差审批后自动填入')
    expect(expense.items).toEqual(items)
    expect(expense.includeSubsidy).toBe(true)
    expect(expense.trip.startDate).toBe('2026-09-01')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it.each([
    { processCode: 'PROC-OLD', configVersion: 12 },
    { processCode: 'PROC-REIMBURSEMENT', configVersion: 11 },
  ])('requires confirmation before replacing an outdated unlocked form (%j)', async (template) => {
    serverDraft.template = { ...serverDraft.template, ...template }
    const oldDraft = serverDraft
    const oldFiles = [...serverFiles]
    const confirm = vi.spyOn(ElMessageBox, 'confirm')
      .mockRejectedValueOnce('cancel')
      .mockResolvedValueOnce(undefined as never)
    const { wrapper, drafts } = await mountView()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(visibleButton(wrapper, '提交 OA').attributes()).toHaveProperty('disabled')
    await visibleButton(wrapper, '按新表单重新填写').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(drafts.currentDraft?.id).toBe('draft-1')
    await visibleButton(wrapper, '按新表单重新填写').trigger('click')
    await flushPromises()
    expect(createReimbursementDraft).toHaveBeenCalledOnce()
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(oldDraft.input.items).toEqual(baseInput.items)
    expect(oldFiles).toEqual([activeFile])
    expect(deleteReimbursementDraft).not.toHaveBeenCalled()
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each(['locked', 'tracked'] as const)('cancels template replacement if the old form becomes %s during confirmation', async (state) => {
    serverDraft.template.configVersion = 11
    const confirmation = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    vi.spyOn(ElMessageBox, 'confirm').mockReturnValueOnce(confirmation.promise)
    const { wrapper, drafts } = await mountView()
    await visibleButton(wrapper, '按新表单重新填写').trigger('click')
    await vi.waitFor(() => expect(ElMessageBox.confirm).toHaveBeenCalledOnce())
    if (state === 'locked') drafts.currentDraft = { ...drafts.currentDraft!, status: 'LOCKED' }
    else {
      const submission = useReimbursementSubmissionStore()
      submission.activeDraftId = 'draft-1'
      submission.idempotencyKey = 'pending-submit-key'
    }
    confirmation.resolve(undefined as never)
    await flushPromises()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(drafts.currentDraft?.id).toBe('draft-1')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    wrapper.unmount()
  })

  it.each(['outdated template', 'definitive failure'] as const)(
    'retains the previous form when creating its replacement fails (%s)',
    async (reason) => {
      serverDraft.relatedApprovals = [linkedApproval]
      serverDraft.relatedApprovalCount = 1
      if (reason === 'outdated template') serverDraft.template.configVersion = 11
      else {
        serverDraft.status = 'LOCKED'
        vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
          status: 'FAILED_FINAL', pollAfterMs: 0,
        }))
      }
      const pending = deferred<ReimbursementDraft>()
      vi.mocked(createReimbursementDraft).mockReturnValueOnce(pending.promise)
      vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
      const { wrapper, drafts, expense } = await mountView()
      const previousItems = JSON.parse(JSON.stringify(expense.items))
      const previousTrip = { ...expense.trip }
      const previousSelections = wrapper.findComponent(TravelApprovalSelectorStub).props('modelValue')
      await visibleButton(wrapper, reason === 'outdated template' ? '按新表单重新填写' : '重新填写').trigger('click')
      await vi.waitFor(() => expect(createReimbursementDraft).toHaveBeenCalledOnce())
      expect(expense.items).toEqual(previousItems)
      expect(createReimbursementDraft).toHaveBeenCalledWith(expect.objectContaining({
        companyValue: '', budgetCodeValue: '', items: [], trip: null,
      }), { signal: expect.any(AbortSignal) })
      pending.reject(new Error('创建连接失败'))
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('draft-1')
      expect(drafts.files).toEqual([activeFile])
      expect(expense.items).toEqual(previousItems)
      expect(expense.trip).toEqual(previousTrip)
      expect(wrapper.get('[data-testid="derived-accounting-summary"]').text()).toContain('北京分公司')
      expect(wrapper.get('[data-testid="derived-accounting-summary"]').text()).toContain('26007 · MES 项目')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('modelValue')).toEqual(previousSelections)
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )

  it('automatically saves incomplete subsidy dates and unfinished OCR amounts', async () => {
    const { wrapper, expense } = await mountView()
    vi.mocked(updateReimbursementDraft).mockClear()
    expense.includeSubsidy = true
    expense.trip.startDate = '2026-09-01'
    expense.trip.endDate = ''
    expense.items[0]!.amount = ''
    await nextTick()
    await vi.waitFor(() => expect(updateReimbursementDraft).toHaveBeenCalledOnce(), { timeout: 2000 })
    const sent = vi.mocked(updateReimbursementDraft).mock.calls[0]![2]
    expect(sent.trip).toBeNull()
    expect(sent.editingState).toEqual(expect.objectContaining({
      includeSubsidy: true, trip: expect.objectContaining({ startDate: '2026-09-01', endDate: '' }),
    }))
    expect(sent.items[0]?.amount).toBeNull()
    expect(sent).not.toHaveProperty('project')
    await flushPromises()
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it('restores incomplete editing state after reload without turning the subsidy switch off', async () => {
    serverDraft.input = {
      ...serverDraft.input,
      trip: null,
      editingState: {
        includeSubsidy: true,
        trip: { tripType: 'project', startDate: '2026-09-01', startTime: '09:00', endDate: '', endTime: '18:00' },
      },
      items: [{ ...baseInput.items[0]!, amount: null }],
    }
    const { wrapper, expense } = await mountView()
    expect(expense.includeSubsidy).toBe(true)
    expect(expense.trip.startDate).toBe('2026-09-01')
    expect(expense.trip.endDate).toBe('')
    expect(expense.items[0]?.amount).toBe('')
    wrapper.unmount()
  })

  it('keeps edits enabled during a slow autosave and persists the newer edit next', async () => {
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValueOnce(pending.promise)
    const { wrapper, expense } = await mountView()
    expense.items[0]!.description = '请求中的说明'
    const saving = saveCurrent(wrapper)
    await vi.waitFor(() => expect(updateReimbursementDraft).toHaveBeenCalledOnce())
    const input = vi.mocked(updateReimbursementDraft).mock.calls[0]![2]
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expense.items[0]!.description = '请求发出后的新说明'
    pending.resolve(makeDraft({ revision: 2, input }))
    await saving
    expect(expense.items[0]?.description).toBe('请求发出后的新说明')
    expect(updateReimbursementDraft).toHaveBeenCalledTimes(2)
    expect(vi.mocked(updateReimbursementDraft).mock.calls[1]?.[1]).toBe(2)
    expect(vi.mocked(updateReimbursementDraft).mock.calls[1]?.[2].items[0]?.description).toBe('请求发出后的新说明')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it('does not revive an OCR row deleted while a save is in flight', async () => {
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValueOnce(pending.promise)
    const { wrapper, expense } = await mountView()
    const saving = saveCurrent(wrapper)
    await vi.waitFor(() => expect(updateReimbursementDraft).toHaveBeenCalledOnce())
    const input = vi.mocked(updateReimbursementDraft).mock.calls[0]![2]
    expense.removeItem('ocr-file-1')
    pending.resolve(makeDraft({ revision: 2, input }))
    await saving
    expect(expense.items).toEqual([])
    expect(vi.mocked(updateReimbursementDraft).mock.lastCall?.[2]).toEqual(expect.objectContaining({
      items: [], dismissedOcrFileIds: ['file-1'],
    }))
    wrapper.unmount()
  })

  it('shows save failure and retries without losing the current edit', async () => {
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('连接暂时中断'))
    const { wrapper, expense } = await mountView()
    expense.items[0]!.description = '本页的新内容'
    await expect(saveCurrent(wrapper)).rejects.toThrow('连接暂时中断')
    await nextTick()
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toContain('保存失败')
    expect(expense.items[0]?.description).toBe('本页的新内容')
    await visibleButton(wrapper, '重试保存').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    expect(updateReimbursementDraft).toHaveBeenCalledTimes(2)
    wrapper.unmount()
  })

  it('offers an in-page retry when locked-submission discovery fails before an idempotency key exists', async () => {
    const { wrapper } = await mountView()
    const submission = useReimbursementSubmissionStore()
    submission.activeDraftId = 'draft-1'
    submission.idempotencyKey = null
    submission.submission = null
    submission.requestError = '提交记录恢复失败，请稍后重试'
    submission.requestAction = 'discover'
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult())
    await nextTick()

    await visibleButton(wrapper, '重新查找提交记录').trigger('click')
    await flushPromises()

    expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith(
      'draft-1',
      { signal: expect.any(AbortSignal) },
    )
    expect((submission.submission as ReimbursementSubmission | null)?.submissionId).toBe('submission-1')
    wrapper.unmount()
  })

  it('does not turn an ordinary failed save into an endless automatic retry loop', async () => {
    vi.useFakeTimers()
    const { wrapper, expense } = await mountView()
    vi.mocked(updateReimbursementDraft).mockRejectedValue(new Error('连接暂时中断'))
    expense.items[0]!.description = '需要保留的修改'
    await nextTick()
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toContain('保存失败')
    wrapper.unmount()
  })

  it('saves input and travel selections before review and submits only once on double click', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    const { wrapper } = await mountView()
    vi.mocked(updateReimbursementDraft).mockClear()
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await nextTick()
    const submit = visibleButton(wrapper, '提交 OA')
    await Promise.all([submit.trigger('click'), submit.trigger('click')])
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(updateReimbursementDraft).toHaveBeenCalled()
    expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith('draft-1', expect.any(Number), [selection], { signal: expect.any(AbortSignal) })
    expect(markReimbursementDraftReviewReady).toHaveBeenCalledWith('draft-1', expect.any(Number), { signal: expect.any(AbortSignal) })
    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    expect(vi.mocked(submitOaReimbursement).mock.calls[0]?.[0]).toBe('draft-1')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    wrapper.unmount()
  })

  it('derives one subsidy period directly from each selected approval', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    const { wrapper, expense, drafts } = await mountView()
    drafts.travelApprovals = [{
      ...linkedApproval,
      profileDisplayName: '境内出差',
      travelTypeOption: { value: 'business', label: '境内出差', key: null },
      companyOption: options.companyOptions[0]!,
      budgetCodeOption: options.budgetCodeOptions[0]!,
      unavailableReason: null,
      createdAt: '2026-08-30T00:00:00Z',
      finishedAt: '2026-08-31T00:00:00Z',
    }]
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    expense.includeSubsidy = true
    await nextTick()

    expect(wrapper.findComponent(TravelApprovalSelectorStub).props()).toMatchObject({
      requiredStartDate: '',
      requiredEndDate: '',
    })
    expect(expense.subsidyTrips).toEqual([expect.objectContaining({
      relatedApprovalId: 'travel-instance-1',
      startDate: '2026-08-31',
      endDate: '2026-09-02',
    })])
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('renders multiple travel periods as separate non-breaking rows', async () => {
    const { wrapper, expense } = await mountView()
    expense.subsidyTrips = [
      {
        relatedApprovalId: 'travel-instance-1', tripType: 'business',
        startDate: '2026-06-30', startTime: '09:00', endDate: '2026-07-03', endTime: '18:00',
      },
      {
        relatedApprovalId: 'travel-instance-2', tripType: 'business',
        startDate: '2026-07-04', startTime: '09:00', endDate: '2026-07-07', endTime: '18:00',
      },
    ]
    await nextTick()

    const periods = wrapper.get('[data-testid="travel-periods"]')
    expect(periods.text()).toContain('共 2 个时间段')
    expect(periods.findAll('.travel-periods__item')).toHaveLength(2)
    expect(periods.findAll('.travel-periods__item')[0]?.text()).toBe('2026-06-30—2026-07-03')
    expect(periods.findAll('.travel-periods__item')[1]?.text()).toBe('2026-07-04—2026-07-07')
    wrapper.unmount()
  })

  it('recalculates restored per-approval subsidies after workspace initialization', async () => {
    vi.useFakeTimers()
    const restoredTrip = {
      relatedApprovalId: linkedApproval.processInstanceId,
      tripType: 'business' as const,
      startDate: linkedApproval.startDate,
      startTime: '09:00',
      endDate: linkedApproval.endDate,
      endTime: '18:00',
      policyConfirmed: false,
    }
    serverDraft = makeDraft({
      input: {
        ...structuredClone(baseInput),
        trip: null,
        trips: [],
        editingState: {
          includeSubsidy: true,
          trip: { ...restoredTrip },
          trips: [{ ...restoredTrip }],
        },
      },
      relatedApprovalCount: 1,
      relatedApprovals: [linkedApproval],
    })
    vi.mocked(calculateTotals).mockResolvedValue({
      ...serverDraft.totals,
      subsidyTotal: '300.00',
      totalAmount: '344.89',
      subsidy: {
        relatedApprovalId: linkedApproval.processInstanceId,
        tripType: 'business', calendarDays: 3, effectiveDays: '3.0',
        dailyRate: '100.00', total: '300.00',
      },
      subsidies: [{
        relatedApprovalId: linkedApproval.processInstanceId,
        tripType: 'business', calendarDays: 3, effectiveDays: '3.0',
        dailyRate: '100.00', total: '300.00',
      }],
    })

    const { wrapper, expense } = await mountView()
    vi.mocked(calculateTotals).mockClear()
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()

    expect(calculateTotals).toHaveBeenCalledWith(
      [expect.objectContaining({ relatedApprovalId: linkedApproval.processInstanceId })],
      expect.any(Array),
    )
    expect(expense.totals?.subsidyTotal).toBe('300.00')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('recalculates when a per-approval departure or return period changes', async () => {
    vi.useFakeTimers()
    serverDraft = makeDraft({
      relatedApprovalCount: 1,
      relatedApprovals: [linkedApproval],
    })
    const { wrapper, expense } = await mountView()
    expense.setSubsidyIncluded(true)
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()
    vi.mocked(calculateTotals).mockClear()

    expense.subsidyTrips[0]!.startTime = '18:00'
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)
    await flushPromises()

    expect(calculateTotals).toHaveBeenCalledWith(
      [expect.objectContaining({
        relatedApprovalId: linkedApproval.processInstanceId,
        startTime: '18:00',
      })],
      expect.any(Array),
    )
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('blocks ride-hailing without a linked itinerary and foreign expenses without RMB confirmation', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    const { wrapper, expense } = await mountView()
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    expense.items[0]!.requiresItinerary = true
    await nextTick()
    await visibleButton(wrapper, '提交 OA').trigger('click')
    expect(confirm).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expense.items[0]!.requiresItinerary = false
    expense.items[0]!.originalCurrency = 'VND'
    expense.items[0]!.originalAmount = '97600000'
    expense.items[0]!.cnyAmountConfirmed = false
    await nextTick()
    await visibleButton(wrapper, '提交 OA').trigger('click')
    expect(confirm).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([
    { amount: '500.00', railType: 'unknown', kind: 'other', linked: false, blocked: false },
    { amount: '500.01', railType: 'unknown', kind: 'other', linked: false, blocked: true },
    { amount: '900.00', railType: 'high_speed', kind: 'other', linked: false, blocked: false },
    { amount: '900.00', railType: 'emu', kind: 'other', linked: false, blocked: true },
    { amount: '900.00', railType: 'regular', kind: 'itinerary', linked: true, blocked: true },
    { amount: '900.00', railType: 'regular', kind: 'payment_proof', linked: true, blocked: false },
  ] as const)('checks payment evidence before review, independent of receipt count: %j', async ({ amount, railType, kind, linked, blocked }) => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    const { wrapper, expense } = await mountView()
    const drafts = useReimbursementDraftStore()
    expense.categories.push({ id: 'rail_fare', name: '火车票', order: 2, manualSelectable: true })
    Object.assign(expense.items[0]!, { category: 'rail_fare', amount, railType, receiptCount: 10, paymentProofFileIds: linked ? ['proof-1'] : [] })
    drafts.files.push({ ...activeFile, id: 'proof-1', role: 'ATTACHMENT_ONLY', attachmentKind: kind, ocrResult: null })
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await nextTick()
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(blocked ? 0 : 1)
    expect(markReimbursementDraftReviewReady).toHaveBeenCalledTimes(blocked ? 0 : 1)
    expect(submitOaReimbursement).toHaveBeenCalledTimes(blocked ? 0 : 1)
    wrapper.unmount()
  })

  it('autosaves payment and itinerary associations through the existing save queue', async () => {
    vi.useFakeTimers()
    const { wrapper, expense } = await mountView()
    expense.items[0]!.paymentProofFileIds = ['payment-1']
    expense.items[0]!.itineraryFileIds = ['itinerary-1']
    expense.items[0]!.railType = 'unknown'
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(vi.mocked(updateReimbursementDraft).mock.calls[0]?.[2].items[0]).toMatchObject({
      paymentProofFileIds: ['payment-1'], itineraryFileIds: ['itinerary-1'], railType: 'unknown',
    })
    wrapper.unmount()
  })

  it('keeps interactions locked during confirmation and reopens editing after cancellation', async () => {
    const confirmation = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    vi.spyOn(ElMessageBox, 'confirm').mockReturnValueOnce(confirmation.promise)
    const { wrapper } = await mountView()
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await nextTick()
    void visibleButton(wrapper, '提交 OA').trigger('click')
    await vi.waitFor(() => expect(ElMessageBox.confirm).toHaveBeenCalledOnce())
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(true)
    confirmation.reject('cancel')
    await flushPromises()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('restores completed OA without another POST and lets the employee start another reimbursement', async () => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'SUBMITTED', statusVersion: 8, processInstanceId: 'oa-process-1',
      businessId: 'OA-20260904001', approvalUrl: 'dingtalk://dingtalkclient/action/openapp?process=oa-process-1',
      pollAfterMs: 0, submittedAt: '2026-09-04T00:03:00Z',
    }))
    const { wrapper, drafts } = await mountView()
    expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith('draft-1', { signal: expect.any(AbortSignal) })
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('OA-20260904001')
    expect(wrapper.get('[data-testid="approval-link"]').attributes('href')).toContain('oa-process-1')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('OA 已成功发起')
    expect(wrapper.findComponent(TripSubsidyCardStub).props('readonly')).toBe(true)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    await visibleButton(wrapper, '再报销一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('restores a locked OA submission even when the current template options are unavailable', async () => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
    vi.mocked(getOaReimbursementOptions).mockRejectedValue(new Error('当前 OA 模板尚未确认'))
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'SUBMITTED', statusVersion: 8, processInstanceId: 'oa-process-1',
      businessId: 'OA-20260904001', approvalUrl: 'dingtalk://approval/oa-process-1',
      pollAfterMs: 0, submittedAt: '2026-09-04T00:03:00Z',
    }))

    const { wrapper } = await mountView()

    expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith(
      'draft-1', { signal: expect.any(AbortSignal) },
    )
    expect(wrapper.text()).toContain('OA-20260904001')
    expect(wrapper.get('[data-testid="approval-link"]').attributes('href')).toContain('oa-process-1')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(calculateTotals).not.toHaveBeenCalled()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('hides the unsupported OA deep link in the mobile presentation', async () => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'SUBMITTED', statusVersion: 8, processInstanceId: 'oa-process-1',
      businessId: 'OA-20260904001', approvalUrl: 'dingtalk://dingtalkclient/action/openapp?process=oa-process-1',
      pollAfterMs: 0, submittedAt: '2026-09-04T00:03:00Z',
    }))

    const { wrapper } = await mountView(undefined, false, true)

    expect(wrapper.text()).toContain('OA-20260904001')
    expect(wrapper.find('[data-testid="approval-link"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('打开钉钉 OA')
    await wrapper.findAll('.mobile-step-nav button')[3]!.trigger('click')
    expect(wrapper.get('[data-testid="mobile-review-step"]').isVisible()).toBe(true)

    await visibleButton(wrapper, '再报销一笔').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(true)
    expect(wrapper.findAll('.mobile-step-nav button')[0]!.attributes('aria-current')).toBe('step')
    wrapper.unmount()
  })

  it('preserves a definitive failure but lets the employee start a fresh reimbursement', async () => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'FAILED_FINAL', statusVersion: 8,
      error: { code: 'OA_CREATE_REJECTED', message: '钉钉拒绝发起审批' }, pollAfterMs: 0,
    }))
    const failedDraft = serverDraft
    const failedFiles = [...serverFiles]
    const { wrapper, drafts } = await mountView()
    expect(wrapper.text()).toContain('钉钉拒绝发起审批')
    expect(wrapper.find('[data-testid="approval-link"]').exists()).toBe(false)
    expect(wrapper.find('.editor-fieldset').attributes()).toHaveProperty('disabled')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    await visibleButton(wrapper, '重新填写').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(failedDraft.status).toBe('LOCKED')
    expect(failedDraft.input.items).toEqual(baseInput.items)
    expect(failedFiles).toEqual([activeFile])
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each(['MANUAL_REVIEW', 'OA_CREATING', 'VERIFYING'] as const)(
    'does not offer another reimbursement while the OA result is uncertain (%s)',
    async (status) => {
      serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
      serverDraft.template.configVersion = 11
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status }))
      const { wrapper } = await mountView()
      expect(wrapper.text()).not.toContain('重新填写')
      expect(wrapper.text()).not.toContain('再报销一笔')
      expect(wrapper.text()).not.toContain('按新表单重新填写')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(createReimbursementDraft).not.toHaveBeenCalled()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )
})

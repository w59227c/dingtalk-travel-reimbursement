/* eslint-disable vue/one-component-per-file */
import ElementPlus, { ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, nextTick } from 'vue'
import { createMemoryHistory, createRouter, RouterView, type Router } from 'vue-router'

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
  listReimbursementSubmissionHistory,
  listWorkspaceReimbursements,
  recoverReimbursementDraft,
  markReimbursementDraftReviewReady,
  replaceReimbursementRelatedApprovals,
  submitOaReimbursement,
  updateReimbursementDraft,
} from '@/api/reimbursements'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'
import { groupOverlappingSubsidyTrips } from '@/utils/subsidyTripGroups'
import { updateSubsidyWindow } from '@/utils/subsidyWindow'
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
  listReimbursementSubmissionHistory: vi.fn(),
  listWorkspaceReimbursements: vi.fn(),
  recoverReimbursementDraft: vi.fn(),
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

function canonicalNoSubsidyInput(): ReimbursementDraftInput {
  return {
    ...structuredClone(baseInput),
    editingState: {
      includeSubsidy: false,
      trip: {
        tripType: 'business',
        startDate: '',
        startTime: '09:00',
        endDate: '',
        endTime: '18:00',
        policyConfirmed: false,
        confirmedEffectiveDays: '',
        noSubsidyException: false,
      },
      trips: [],
    },
    items: baseInput.items.map((item) => ({
      ...structuredClone(item),
      itineraryFileIds: [],
      itineraryAutoMatchDisabled: false,
      paymentProofFileIds: [],
      hotelBillFileIds: [],
      railType: 'unknown',
      requiresItinerary: false,
      cnyAmountConfirmed: false,
      requiresCnyConfirmation: false,
    })),
  }
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
    refreshingMaterialStatus: { type: Boolean, default: false },
  },
  emits: ['refresh-draft', 'refresh-material-status'],
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
    approvalTripType: { type: String, default: null },
  },
  template: '<section :data-mobile="String(mobile)" :data-readonly="String(readonly)">出差补助</section>',
})
const ExpenseSummaryCardStub = defineComponent({
  name: 'ExpenseSummaryCard',
  props: {
    previewDisabledReason: { type: String, default: '' },
    beforePreview: { type: Function, default: undefined },
  },
  template: '<section data-testid="expense-summary">费用合计<slot name="submission" /></section>',
})
const TravelApprovalSelectorStub = defineComponent({
  name: 'TravelApprovalSelector',
  props: {
    mobile: { type: Boolean, default: false },
    modelValue: { type: Array, required: true },
    linkedApprovals: { type: Array, default: () => [] },
    readonly: { type: Boolean, default: false },
    busy: { type: Boolean, default: false },
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
  vi.mocked(listReimbursementSubmissionHistory).mockResolvedValue([])
  vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => {
    const list = await vi.mocked(listReimbursementDrafts).mock.results.at(-1)?.value
    return { items: list?.items.slice(0, 1) ?? [], total: list?.items.length ? 1 : 0 }
  })
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
    serverDraft = makeDraft({ id: 'draft-created', input, revision: 1, department: serverDraft.department })
    serverFiles = []
    return serverDraft
  })
  vi.mocked(submitOaReimbursement).mockResolvedValue(submissionResult())
}

async function mountView(setup?: (auth: ReturnType<typeof useAuthStore>) => void, realMaterials = false, mobile = false, realSummary = false): Promise<{
  wrapper: VueWrapper
  expense: ReturnType<typeof useExpenseStore>
  drafts: ReturnType<typeof useReimbursementDraftStore>
  router: Router
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
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: ReimburseView },
      { path: '/m', component: ReimburseView, props: { mobile: true } },
      { path: '/admin/settings', component: defineComponent({ template: '<div>设置</div>' }) },
      { path: '/m/settings', component: defineComponent({ template: '<div>设置</div>' }) },
    ],
  })
  await router.push(mobile ? '/m' : '/')
  await router.isReady()
  const wrapper = mount(RouterView, {
    attachTo: '#test-app',
    global: {
      plugins: [pinia, ElementPlus, router],
      stubs: {
        ExpenseItemsCard: realMaterials ? false : ExpenseItemsCardStub,
        ExpenseSummaryCard: realSummary ? false : ExpenseSummaryCardStub,
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
  return { wrapper, expense, drafts: useReimbursementDraftStore(), router }
}

let restoreObservedScroll: (() => void) | undefined
function observeScrollIntoView() {
  const previous = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollIntoView')
  const scroll = vi.fn()
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scroll })
  restoreObservedScroll = () => {
    if (previous) Object.defineProperty(Element.prototype, 'scrollIntoView', previous)
    else Reflect.deleteProperty(Element.prototype, 'scrollIntoView')
  }
  return scroll
}

function visibleButton(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  if (!button) throw new Error(`Missing button: ${label}`)
  return button
}

async function openReviewStep(wrapper: VueWrapper, mobile: boolean): Promise<void> {
  if (mobile) await wrapper.findAll('.mobile-step-nav button')[3]!.trigger('click')
}

describe('ReimburseView single-form OA flow', () => {
  it.each([false, true])('shows settings only to administrators and links the correct presentation (mobile=%s)', async (mobile) => {
    let authority!: ReturnType<typeof useAuthStore>
    const { wrapper } = await mountView((auth) => { authority = auth; auth.session!.isAdmin = false }, false, mobile)
    expect(wrapper.find('.hero-links a').exists()).toBe(false)
    expect(wrapper.get('.hero-links').findAll('button, a').map((item) => item.text()))
      .toEqual(['报销记录', '新开一笔'])
    authority.session!.isAdmin = true; await nextTick()
    expect(wrapper.get('.hero-links').findAll('button, a').map((item) => item.text()))
      .toEqual(['系统设置', '报销记录', '新开一笔'])
    const settings = wrapper.get('.hero-links a.hero-link')
    expect(settings.attributes('data-to')).toBe(mobile ? '/m/settings' : '/admin/settings')
    const history = wrapper.get('.hero-links button[aria-haspopup="dialog"]')
    expect(history.text()).toBe('报销记录')
    expect(history.attributes('aria-haspopup')).toBe('dialog')
    wrapper.unmount()
  })

  it.each([false, true])('prepares a blank form when the expired current was removed by cleanup, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'EXPIRED', input: canonicalNoSubsidyInput() })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    vi.mocked(createReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('报销已被清理'), {
      isAxiosError: true, response: { status: 404, data: { error: { code: 'REIMBURSEMENT_DRAFT_NOT_FOUND', message: '报销已被清理' } } },
    }))
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(expense.items).toEqual([])
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(createReimbursementDraft).toHaveBeenCalledTimes(2)
    expect(vi.mocked(createReimbursementDraft).mock.calls[1]![1]).toEqual(expect.objectContaining({ reuseExisting: true }))
    expect(vi.mocked(createReimbursementDraft).mock.calls[1]![1]).not.toHaveProperty('replaceCurrent')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })
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
    restoreObservedScroll?.(); restoreObservedScroll = undefined
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
    expect(wrapper.text()).not.toContain('基础服务已就绪')
    expect(wrapper.get('.hero-links').text()).toContain('系统设置')
    expect(wrapper.get('.hero-links a').attributes('data-to')).toBe('/m/settings')
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

  it('switches all four mobile steps without forcing the heading to the top', async () => {
    const { wrapper } = await mountView(undefined, false, true)
    const scrollIntoView = vi.fn()
    for (const heading of wrapper.findAll('.mobile-step-heading')) {
      Object.defineProperty(heading.element, 'scrollIntoView', { value: scrollIntoView })
    }
    const buttons = wrapper.findAll('.mobile-step-nav button')
    for (const index of [1, 2, 3, 0, 0]) {
      await buttons[index]!.trigger('click')
      await flushPromises()
      expect(buttons[index]!.attributes('aria-current')).toBe('step')
      expect(wrapper.get(`#reimbursement-step-${index}`).isVisible()).toBe(true)
      expect(scrollIntoView).not.toHaveBeenCalled()
    }
    await buttons[1]!.trigger('click')
    await visibleButton(wrapper, '下一步：费用材料').trigger('click')
    expect(buttons[2]!.attributes('aria-current')).toBe('step')
    await visibleButton(wrapper, '上一步').trigger('click')
    expect(buttons[1]!.attributes('aria-current')).toBe('step')
    expect(scrollIntoView).not.toHaveBeenCalled()
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

  it.each([false, true])(
    'derives a multi-department user scope from the selected travel approval without showing a submission-stage state (mobile=%s)',
    async (mobile) => {
    const { wrapper } = await mountView((auth) => {
      auth.status = 'department_required'
      auth.session!.selectedDepartment = null
      auth.session!.departments = [
        { id: '100', name: '技术管理中心' },
        { id: '200', name: '工业物联二部' },
      ]
    }, false, mobile)
    const departmentResolution = deferred<Awaited<ReturnType<typeof selectDepartmentFromTravelApproval>>>()
    vi.mocked(selectDepartmentFromTravelApproval).mockReturnValue(departmentResolution.promise)
    const resolvedDepartment = {
      selectedDepartment: { id: '200', name: '工业物联二部' },
      selectionRequired: false,
      departments: [
        { id: '100', name: '技术管理中心' },
        { id: '200', name: '工业物联二部' },
      ],
    }

    expect(wrapper.text()).not.toContain('选择本次报销部门')
    expect(wrapper.text()).not.toContain('确认部门')
    const selector = wrapper.findComponent(TravelApprovalSelectorStub)
    expect(selector.props('single')).toBe(true)
    selector.vm.$emit('update:modelValue', [selection])
    await nextTick()
    await vi.waitFor(() => expect(selectDepartmentFromTravelApproval).toHaveBeenCalledOnce())

    expect(selector.props('busy')).toBe(true)
    expect(selector.props('readonly')).toBe(false)

    departmentResolution.resolve(resolvedDepartment)
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
    },
  )

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

      const dialog = wrapper.findAllComponents({ name: 'ElDialog' }).find(
        (component) => component.props('title') === '确认本次报销部门',
      )!
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
      const departmentResolution = deferred<Awaited<ReturnType<typeof selectDepartmentFromTravelApproval>>>()
      vi.mocked(selectDepartmentFromTravelApproval).mockReturnValue(departmentResolution.promise)
      const resolvedDepartment = {
        selectedDepartment: { id: '200', name: '产品开发部' },
        selectionRequired: false,
        departments: [
          { id: '100', name: '技术管理中心' },
          { id: '200', name: '产品开发部' },
        ],
      }

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit(
        'update:modelValue',
        [selection],
      )
      await nextTick()
      await vi.waitFor(() => expect(selectDepartmentFromTravelApproval).toHaveBeenCalledOnce())

      const summary = wrapper.get('[data-testid="derived-accounting-summary"]')
      expect(summary.text()).toContain('北京分公司')
      expect(summary.text()).toContain('26007 · MES 项目')
      expect(summary.text()).toContain('境内出差')
      expect(summary.text()).toContain('2026-08-31')
      expect(summary.text()).toContain('2026-09-02')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(true)
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(true)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(replaceReimbursementRelatedApprovals).not.toHaveBeenCalled()

      departmentResolution.resolve(resolvedDepartment)
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
    expect(wrapper.text()).not.toContain('基础服务已就绪')
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

  it.each([false, true])('blocks preview and submission for unconfirmed material purposes, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    await openReviewStep(wrapper, mobile)
    drafts.files.push({ ...activeFile, id: 'unknown-1', role: 'ATTACHMENT_ONLY', attachmentKind: 'other',
      materialClassification: { status: 'needs_confirmation', kind: 'unknown', reason: '请确认用途', pageCount: 1 } })
    await nextTick()
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('1 份材料需处理后才能提交 OA')
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('材料用途待确认')
    expect(wrapper.get('[data-testid="material-checklist"]').text()).toContain('请先处理上面列出的材料，再提交 OA。')
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeDefined()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('请先处理全部待确认或未完成的材料')
    expect(expense.items).toHaveLength(1)
    drafts.files[1]!.materialClassification!.status = 'confirmed'
    await nextTick()
    expect(visibleButton(wrapper, '提交 OA').attributes('disabled')).toBeUndefined()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toBe('')
    wrapper.unmount()
  })

  it.each([false, true])('blocks preview and submission for a terminal OCR source with no disposition, mobile=%s', async (mobile) => {
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    await openReviewStep(wrapper, mobile)
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
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('请先处理全部待确认或未完成的材料')
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
    expect(wrapper.find('[data-testid="autosave-status"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('返回报销')
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
      { signal: expect.any(AbortSignal), reuseExisting: true, expectedDepartment: { id: '100', name: '测试部门' } },
    )
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.text()).not.toContain('创建')
    wrapper.unmount()
  })

  it.each([false, true])('keeps approval selection editable after a canonical server save of an unbound trip, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(updateReimbursementDraft).mockImplementation(async (_id, revision, input) => {
      // Pydantic emits null for the optional relatedApprovalId in editingState.trip.
      const canonicalInput = JSON.parse(JSON.stringify({
        ...input,
        editingState: {
          ...input.editingState,
          trip: { ...input.editingState!.trip, relatedApprovalId: input.editingState!.trip.relatedApprovalId ?? null },
        },
      })) as ReimbursementDraftInput
      serverDraft = { ...serverDraft, revision: revision + 1, input: canonicalInput }
      return serverDraft
    })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '选择审批前已保存的说明'
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalled()
    const selector = wrapper.findComponent(TravelApprovalSelectorStub)
    for (const selections of [[selection], [], [selection]]) {
      selector.vm.$emit('update:modelValue', selections)
      await flushPromises()
      await vi.advanceTimersByTimeAsync(650)
      await flushPromises()
      expect(wrapper.text()).not.toContain('报销内容已更新，请核对最新报销内容')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(expense.items[0]?.description).toBe('选择审批前已保存的说明')
    }
    expect(serverDraft.relatedApprovals.map((approval) => approval.processInstanceId)).toEqual([selection.processInstanceId])
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => [
    { mobile, entered: '100', canonical: '100.00' },
    { mobile, entered: '12.3', canonical: '12.30' },
    { mobile, entered: '999999999999999.9', canonical: '999999999999999.90' },
  ]))('accepts canonical decimal precision in saved original currency, $entered mobile=$mobile', async ({ mobile, entered, canonical }) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(updateReimbursementDraft).mockImplementation(async (_id, revision, input) => {
      const canonicalInput = structuredClone(input)
      canonicalInput.items[0]!.originalAmount = canonical
      serverDraft = { ...serverDraft, revision: revision + 1, input: canonicalInput }
      return serverDraft
    })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.upsertManualItem({ ...expense.items[0]!, originalCurrency: 'USD', originalAmount: entered,
      originalDetailsEdited: true, cnyAmountConfirmed: true })
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(serverDraft.input.items[0]?.originalAmount).toBe(canonical)
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()
    expect(wrapper.text()).not.toContain('报销内容已更新，请核对最新报销内容')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(expense.items[0]?.originalAmount).toBe(entered)
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['100.000', '0100.00'].map((invalid) => ({ mobile, invalid }))))(
    'does not skip server validation for unsupported original amount format, $invalid mobile=$mobile', async ({ mobile, invalid }) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    Object.assign(serverDraft.input.items[0]!, { originalCurrency: 'USD', originalAmount: '100.00',
      originalDetailsEdited: true, cnyAmountConfirmed: true, requiresCnyConfirmation: true })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    vi.mocked(updateReimbursementDraft).mockClear()
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('原币金额格式不符合要求'))
    expense.upsertManualItem({ ...expense.items[0]!, originalAmount: invalid })
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('原币金额格式不符合要求')
    expect(expense.items[0]?.originalAmount).toBe(invalid)
    wrapper.unmount()
  })

  it.each([false, true])('keeps editing after completed manual receipt clearing loses its response, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    delete serverDraft.input.items[0]!.sourceFileId
    serverDraft.input.items[0]!.receiptAttachmentFileIds = ['file-1']
    serverFiles = [{ ...activeFile, role: 'ATTACHMENT_ONLY', ocrStatus: 'NOT_REQUESTED' }]
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    vi.mocked(clearReimbursementDraftFiles).mockImplementationOnce(async () => {
      const input = structuredClone(serverDraft.input)
      delete input.items[0]!.receiptAttachmentFileIds
      serverDraft = { ...serverDraft, revision: serverDraft.revision + 1, input }
      serverFiles = []
      throw new Error('删除响应连接中断')
    })
    const { wrapper, expense, drafts } = await mountView(undefined, true, mobile)
    if (mobile) await wrapper.findAll('.mobile-step-nav button')[2]!.trigger('click')
    await visibleButton(wrapper, '清空文件').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(clearReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(drafts.revisionConflict).toBe(false)
    expect(wrapper.text()).not.toContain('报销内容已更新，请核对最新报销内容')
    expect(visibleButton(wrapper, '手动添加').attributes('disabled')).toBeUndefined()
    expect(expense.items[0]?.receiptAttachmentFileIds ?? []).toEqual([])
    expect(serverDraft.input.items[0]?.amount).toBe('44.89')
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
    await saveCurrent(wrapper)
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it.each([false, true])(
    'shows all approval-derived fields immediately while the server verifies them (mobile=%s)',
    async (mobile) => {
      vi.useFakeTimers()
      serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      drafts.travelApprovals = [{
        ...linkedApproval,
        originatorDepartmentId: '100',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '境内出差', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      const pendingSave = deferred<ReimbursementDraft>()
      vi.mocked(replaceReimbursementRelatedApprovals).mockReturnValueOnce(pendingSave.promise)

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await nextTick()
      await Promise.resolve()

      const summary = wrapper.get('[data-testid="derived-accounting-summary"]')
      expect(summary.text()).toContain('北京分公司')
      expect(summary.text()).toContain('26007 · MES 项目')
      expect(summary.text()).toContain('境内出差')
      expect(summary.text()).toContain('2026-08-31')
      expect(summary.text()).toContain('2026-09-02')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(true)
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(false)
      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledOnce()

      serverDraft = makeDraft({
        revision: 2,
        relatedApprovalCount: 1,
        input: {
          ...serverDraft.input,
          companyValue: '北京',
          budgetCodeValue: '26007',
          accountingSourceVerified: true,
        },
        relatedApprovals: [linkedApproval],
        relatedApprovalSummary: {
          count: 1,
          startDate: linkedApproval.startDate,
          endDate: linkedApproval.endDate,
        },
      })
      pendingSave.resolve(serverDraft)
      await flushPromises()

      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      expect(summary.find('[data-testid="related-approval-verification-status"]').exists()).toBe(false)
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'restores the authoritative approval fields when immediate verification fails (mobile=%s)',
    async (mobile) => {
      serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      drafts.travelApprovals = [{
        ...linkedApproval,
        originatorDepartmentId: '100',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '境内出差', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      vi.mocked(replaceReimbursementRelatedApprovals).mockRejectedValueOnce(
        new Error('网络中断'),
      )

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()

      const summary = wrapper.get('[data-testid="derived-accounting-summary"]')
      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledOnce()
      expect(drafts.currentDraft?.relatedApprovals).toEqual([])
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('modelValue')).toEqual([])
      expect(summary.text()).not.toContain('北京分公司')
      expect(summary.text()).not.toContain('26007 · MES 项目')
      expect(summary.text()).toContain('网络中断')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'switches all approval-derived fields to authoritative values after verification (mobile=%s)',
    async (mobile) => {
      vi.useFakeTimers()
      serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      drafts.reimbursementOptions = {
        ...options,
        travelProfiles: [{
          ...options.travelProfiles[0]!,
          travelTypeMappings: {
            internal: { value: 'internal', label: '公司内部出差', key: null },
          },
          subsidyTripTypeMappings: { internal: 'internal' },
        }],
      }
      drafts.travelApprovals = [{
        ...linkedApproval,
        startDate: '2026-08-31',
        endDate: '2026-09-02',
        originatorDepartmentId: '100',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '查询时的出差类别', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      const pendingSave = deferred<ReimbursementDraft>()
      vi.mocked(replaceReimbursementRelatedApprovals).mockReturnValueOnce(pendingSave.promise)

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await nextTick()

      const summary = wrapper.get('[data-testid="derived-accounting-summary"]')
      expect(summary.text()).toContain('查询时的出差类别')
      expect(summary.text()).toContain('2026-08-31')
      expect(summary.text()).toContain('2026-09-02')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(true)
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(false)

      const authoritativeApproval: ReimbursementRelatedApproval = {
        ...linkedApproval,
        sourceTravelTypeValue: 'internal',
        startDate: '2026-09-05',
        endDate: '2026-09-06',
      }
      serverDraft = makeDraft({
        revision: 2,
        relatedApprovalCount: 1,
        input: {
          ...serverDraft.input,
          companyValue: '北京',
          budgetCodeValue: '26007',
          accountingSourceVerified: true,
        },
        relatedApprovals: [authoritativeApproval],
        relatedApprovalSummary: {
          count: 1,
          startDate: authoritativeApproval.startDate,
          endDate: authoritativeApproval.endDate,
        },
      })
      pendingSave.resolve(serverDraft)
      await flushPromises()

      expect(summary.text()).toContain('公司内部出差')
      expect(summary.text()).toContain('2026-09-05')
      expect(summary.text()).toContain('2026-09-06')
      expect(summary.text()).not.toContain('查询时的出差类别')
      expect(summary.text()).not.toContain('2026-08-31')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'accepts the reloaded server result when approval verification succeeded but its response was lost (mobile=%s)',
    async (mobile) => {
      serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      drafts.travelApprovals = [{
        ...linkedApproval,
        originatorDepartmentId: '100',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '境内出差', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      serverDraft = makeDraft({
        revision: 2,
        relatedApprovalCount: 1,
        input: {
          ...serverDraft.input,
          companyValue: '北京',
          budgetCodeValue: '26007',
          accountingSourceVerified: true,
        },
        relatedApprovals: [linkedApproval],
        relatedApprovalSummary: {
          count: 1,
          startDate: linkedApproval.startDate,
          endDate: linkedApproval.endDate,
        },
      })
      vi.mocked(replaceReimbursementRelatedApprovals).mockRejectedValueOnce(
        new Error('响应丢失'),
      )

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()

      const summary = wrapper.get('[data-testid="derived-accounting-summary"]')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('modelValue')).toEqual([selection])
      expect(summary.text()).toContain('北京分公司')
      expect(summary.text()).toContain('26007 · MES 项目')
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      expect(summary.text()).not.toContain('响应丢失')
      expect(wrapper.get('[data-testid="autosave-status"]').text()).not.toContain('保存失败')
      expect(drafts.mutationError).toBe('')
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'reconciles a lost response from explicit approval reconfirmation (mobile=%s)',
    async (mobile) => {
      serverDraft = makeDraft({
        relatedApprovalCount: 1,
        input: { ...serverDraft.input, accountingSourceVerified: false },
        relatedApprovals: [linkedApproval],
        relatedApprovalSummary: {
          count: 1,
          startDate: linkedApproval.startDate,
          endDate: linkedApproval.endDate,
        },
      })
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      await openReviewStep(wrapper, mobile)
      const recovery = wrapper.get('[data-testid="accounting-options-recovery"]')
      expect(recovery.isVisible()).toBe(true)
      expect(visibleButton(wrapper, '提交 OA').attributes()).toHaveProperty('disabled')
      serverDraft = makeDraft({
        revision: 2,
        relatedApprovalCount: 1,
        input: { ...serverDraft.input, accountingSourceVerified: true },
        relatedApprovals: [linkedApproval],
        relatedApprovalSummary: {
          count: 1,
          startDate: linkedApproval.startDate,
          endDate: linkedApproval.endDate,
        },
      })
      vi.mocked(replaceReimbursementRelatedApprovals).mockRejectedValueOnce(
        new Error('重新核验响应丢失'),
      )

      await visibleButton(wrapper, '重新确认出差审批').trigger('click')
      await flushPromises()

      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledOnce()
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      expect(wrapper.text()).not.toContain('重新核验响应丢失')
      expect(drafts.mutationError).toBe('')
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'keeps a later ordinary-input save failure visible after approval verification (mobile=%s)',
    async (mobile) => {
      vi.useFakeTimers()
      serverDraft.input = { ...serverDraft.input, companyValue: '', budgetCodeValue: '' }
      const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
      drafts.travelApprovals = [{
        ...linkedApproval,
        originatorDepartmentId: '100',
        profileDisplayName: '境内出差',
        travelTypeOption: { value: 'business', label: '境内出差', key: null },
        companyOption: options.companyOptions[0]!,
        budgetCodeOption: options.budgetCodeOptions[0]!,
        unavailableReason: null,
        createdAt: '2026-08-30T00:00:00Z',
        finishedAt: '2026-08-31T00:00:00Z',
      }]
      expense.items[0]!.description = '尚未保存的说明'
      vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(
        new Error('普通输入保存失败'),
      )

      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()

      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledOnce()
      expect(updateReimbursementDraft).not.toHaveBeenCalled()
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)

      await vi.advanceTimersByTimeAsync(650)
      await flushPromises()

      expect(updateReimbursementDraft).toHaveBeenCalledOnce()
      await vi.waitFor(() => expect(drafts.mutationError).toContain('普通输入保存失败'))
      expect(wrapper.get('[data-testid="autosave-error"]').text()).toContain('保存失败')
      wrapper.unmount()
    },
  )

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

  it.each([false, true])(
    'treats omitted and explicit input defaults as the same saved form (mobile=%s)',
    async (mobile) => {
      vi.useFakeTimers()
      serverDraft.input = canonicalNoSubsidyInput()
      const { wrapper } = await mountView(undefined, false, mobile)
      await vi.advanceTimersByTimeAsync(1_000)
      await flushPromises()

      expect(updateReimbursementDraft).not.toHaveBeenCalled()
      const indicator = wrapper.get('.hero-title [data-testid="autosave-status"]')
      expect(indicator.text()).toBe('已保存')
      expect(indicator.attributes('data-state')).toBe('saved')
      wrapper.unmount()
    },
  )

  it.each([false, true])(
    'shows template replacement beside submission instead of an endless save wait (mobile=%s)',
    async (mobile) => {
      vi.useFakeTimers()
      serverDraft.input = canonicalNoSubsidyInput()
      serverDraft.template.configVersion = 11
      const { wrapper } = await mountView(undefined, false, mobile)
      await vi.advanceTimersByTimeAsync(1_000)
      await flushPromises()

      expect(wrapper.find('[data-testid="autosave-status"]').exists()).toBe(false)
      const recovery = wrapper.get('[data-testid="template-recovery-panel"]')
      expect(recovery.isVisible()).toBe(true)
      expect(recovery.text()).toContain('OA 表单已更新')
      expect(recovery.text()).toContain('当前记录不能继续保存或提交')
      expect(recovery.find('button').text()).toBe('按新表单重新填写')

      if (mobile) {
        await wrapper.findAll('.mobile-step-nav button')[3]!.trigger('click')
        expect(wrapper.get('.mobile-step-footer .el-button--primary').text())
          .toBe('按新表单重新填写')
      }
      wrapper.unmount()
    },
  )

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

  it.each([false, true])('restores the actual submission if another page locks the outdated form, mobile=%s', async (mobile) => {
    serverDraft.template.configVersion = 11
    vi.spyOn(ElMessageBox, 'confirm').mockImplementationOnce(async () => {
      serverDraft = makeDraft({ status: 'LOCKED', revision: 5, template: serverDraft.template })
      return 'confirm' as never
    })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'VERIFYING' }))
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    await visibleButton(wrapper, '按新表单重新填写').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.status).toBe('LOCKED')
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('审批已发起，正在核验')
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each(['outdated template'] as const)(
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
      }), { signal: expect.any(AbortSignal), reuseExisting: true, replaceCurrent: { id: 'draft-1', revision: 1 }, expectedDepartment: { id: '100', name: '测试部门' } })
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

  it.each([false, true])('autosaves exclusions and retains unfinished ranges across reload and subsidy toggles, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.includeSubsidy = true
    Object.assign(expense.trip, {
      startDate: '2026-09-01', endDate: '2026-09-03', excludedPeriods: [{
        startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-02', endTime: '18:00',
      }],
    })
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    vi.mocked(updateReimbursementDraft).mockClear()
    expense.trip.excludedPeriods[0]!.endDate = '2026-09-03'
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(vi.mocked(updateReimbursementDraft).mock.calls[0]![2].trip?.excludedPeriods?.[0]?.endDate)
      .toBe('2026-09-03')
    vi.mocked(updateReimbursementDraft).mockClear()
    expense.trip.excludedPeriods[0]!.endDate = ''
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const sent = vi.mocked(updateReimbursementDraft).mock.calls[0]![2]
    expect(sent.trip).toBeNull()
    expect(sent.editingState?.trip.excludedPeriods?.[0]?.endDate).toBe('')
    wrapper.unmount()
    const restored = await mountView(undefined, false, mobile)
    expect(restored.expense.includeSubsidy).toBe(true)
    expect(restored.expense.trip.excludedPeriods[0]?.endDate).toBe('')
    expect(restored.expense.policyInputError).toContain('请补齐每个非补助时段')
    restored.expense.setSubsidyIncluded(false)
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const disabled = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(disabled.trip).toBeNull()
    expect(disabled.editingState?.includeSubsidy).toBe(false)
    expect(disabled.editingState?.trip.excludedPeriods?.[0]?.endDate).toBe('')
    restored.wrapper.unmount()
    const reopened = await mountView(undefined, false, mobile)
    expect(reopened.expense.includeSubsidy).toBe(false)
    expect(reopened.expense.trip.excludedPeriods[0]?.startDate).toBe('2026-09-01')
    expect(reopened.expense.trip.excludedPeriods[0]?.endDate).toBe('')
    reopened.expense.setSubsidyIncluded(true)
    expect(reopened.expense.policyInputError).toContain('请补齐每个非补助时段')
    reopened.wrapper.unmount()
  })

  it.each([false, true])('persists matching half-day times for restored per-approval subsidies, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId,
      tripType: 'business' as const,
      startDate: '2026-09-01', startTime: '08:00', endDate: '2026-09-03', endTime: '21:00',
      excludedPeriods: [{ startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-03', endTime: '18:00' }],
    }
    serverDraft = makeDraft({
      input: {
        ...baseInput, trips: [trip],
        editingState: { includeSubsidy: true, trip, trips: [trip] },
      },
      relatedApprovalCount: 1,
      relatedApprovals: [{ ...linkedApproval, startDate: trip.startDate, endDate: trip.endDate }],
    })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    await vi.advanceTimersByTimeAsync(650)
    const sent = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(sent.trips?.[0]).toMatchObject({ startTime: '09:00', endTime: '18:00', excludedPeriods: trip.excludedPeriods })
    expect(sent.editingState?.trips?.[0]).toMatchObject({ startTime: '09:00', endTime: '18:00' })
    expense.subsidyTrips[0]!.excludedPeriods![0]!.endDate = ''
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const incomplete = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(incomplete.trips).toEqual([])
    expect(incomplete.editingState?.trips?.[0]).toMatchObject({
      startTime: '08:00', excludedPeriods: [expect.objectContaining({ endDate: '' })],
    })
    wrapper.unmount()
  })

  it.each([false, true])('waits for exclusion edits to save before opening settings and restores them, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense, router } = await mountView(undefined, false, mobile)
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValueOnce(pending.promise)
    expense.includeSubsidy = true
    expense.trip.excludedPeriods.push({ startDate: '2026-09-01', startTime: '09:00', endDate: '', endTime: '18:00' })
    await nextTick()
    const home = mobile ? '/m' : '/'
    const leaving = router.push(mobile ? '/m/settings' : '/admin/settings')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe(home)
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    const input = vi.mocked(updateReimbursementDraft).mock.calls[0]![2]
    serverDraft = makeDraft({ revision: 2, input })
    pending.resolve(serverDraft)
    await leaving
    expect(router.currentRoute.value.path).toContain('/settings')
    await router.push(home)
    await flushPromises()
    expect(expense.trip.excludedPeriods[0]?.endDate).toBe('')
    expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    wrapper.unmount()
  })

  it.each([false, true])('retains edits on the form when saving before navigation fails or materials are busy, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense, drafts, router } = await mountView(undefined, false, mobile)
    expense.trip.excludedPeriods.push({ startDate: '', startTime: '09:00', endDate: '', endTime: '18:00' })
    await nextTick()
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('连接暂时中断'))
    const settings = mobile ? '/m/settings' : '/admin/settings'
    const home = mobile ? '/m' : '/'
    await router.push(settings)
    expect(router.currentRoute.value.path).toBe(home)
    expect(wrapper.text()).toContain('连接暂时中断')
    expect(expense.trip.excludedPeriods).toHaveLength(1)
    vi.mocked(updateReimbursementDraft).mockClear()
    drafts.processingFiles = true
    await router.push(settings)
    expect(router.currentRoute.value.path).toBe(home)
    expect(wrapper.text()).toContain('请等待材料处理完成后重试')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    drafts.processingFiles = false
    await router.push(settings)
    expect(router.currentRoute.value.path).toBe(settings)
    wrapper.unmount()
  })

  it.each([false, true])('opens settings from a locked draft without attempting to rewrite it, mobile=%s', async (mobile) => {
    serverDraft.status = 'LOCKED'
    const { wrapper, router } = await mountView(undefined, false, mobile)
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    expect(router.currentRoute.value.path).toContain('/settings')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('previews frozen locked subsidies with historical overlaps without saving, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId, tripType: 'business' as const,
      startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-03', endTime: '18:00',
      excludedPeriods: [
        { startDate: '2026-09-01', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00' },
        { startDate: '2026-09-02', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00' },
      ],
    }
    serverDraft = makeDraft({
      status: 'LOCKED', lockedAt: '2026-09-04T00:01:00Z', relatedApprovalCount: 1,
      relatedApprovals: [{ ...linkedApproval, startDate: trip.startDate, endDate: trip.endDate }],
      input: {
        ...baseInput, trips: [trip],
        editingState: { includeSubsidy: true, trip, trips: [trip] },
      },
      totals: { ...makeDraft().totals, subsidyTotal: '200.00', totalAmount: '244.89' },
    })
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile, true)
    const preview = vi.spyOn(drafts, mobile ? 'openExcelPreviewInDingTalk' : 'downloadExcelPreview')
      .mockResolvedValue()
    await vi.advanceTimersByTimeAsync(650)
    expect(expense.tripPayloads()).toEqual([])
    expect(wrapper.findComponent({ name: 'ExpenseSummaryCard' }).props('previewDisabledReason')).toBe('')
    expect(wrapper.findComponent({ name: 'ExpenseSummaryCard' }).props('beforePreview')).toBeUndefined()
    await visibleButton(wrapper, mobile ? '打开 Excel' : '预览 Excel').trigger('click')
    await flushPromises()
    expect(preview).toHaveBeenCalledOnce()
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    expect(calculateTotals).not.toHaveBeenCalled()
    expect(expense.displaySubsidyTotal).toBe('200.00')
    wrapper.unmount()
  })

  it.each([false, true])('previews locked attendance dates without reformatting, recalculating or saving, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId, tripType: 'same_city_project' as const,
      startDate: linkedApproval.startDate, startTime: '09:00', endDate: linkedApproval.endDate, endTime: '18:00',
      attendanceDates: [linkedApproval.startDate, linkedApproval.endDate],
    }
    serverDraft = makeDraft({
      status: 'LOCKED', lockedAt: '2026-09-04T00:01:00Z', relatedApprovalCount: 1,
      relatedApprovals: [linkedApproval],
      input: { ...structuredClone(baseInput), trip: null, trips: [trip], editingState: { includeSubsidy: true, trip, trips: [trip] } },
      totals: { ...makeDraft().totals, subsidyTotal: '100.00', totalAmount: '144.89' },
    })
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile, true)
    const preview = vi.spyOn(drafts, mobile ? 'openExcelPreviewInDingTalk' : 'downloadExcelPreview').mockResolvedValue()
    await vi.advanceTimersByTimeAsync(650)
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(trip.attendanceDates)
    expect(wrapper.findComponent({ name: 'ExpenseSummaryCard' }).props('previewDisabledReason')).toBe('')
    expect(wrapper.findComponent({ name: 'ExpenseSummaryCard' }).props('beforePreview')).toBeUndefined()
    await visibleButton(wrapper, mobile ? '打开 Excel' : '预览 Excel').trigger('click')
    await flushPromises()
    expect(preview).toHaveBeenCalledOnce()
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    expect(calculateTotals).not.toHaveBeenCalled()
    expect(expense.displaySubsidyTotal).toBe('100.00')
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(trip.attendanceDates)
    wrapper.unmount()
  })

  it.each([false, true])('autosaves and restores overlapping exclusion input for correction, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.includeSubsidy = true
    Object.assign(expense.trip, {
      startDate: '2026-09-01', endDate: '2026-09-03', excludedPeriods: [
        { startDate: '2026-09-01', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00' },
        { startDate: '2026-09-02', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00' },
      ],
    })
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const sent = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(sent.trip).toBeNull()
    expect(sent.editingState?.trip.excludedPeriods).toHaveLength(2)
    wrapper.unmount()
    const restored = await mountView(undefined, false, mobile)
    expect(restored.expense.trip.excludedPeriods).toHaveLength(2)
    expect(restored.expense.policyInputError).toContain('非补助时段 1 与时段 2 重叠')
    restored.expense.trip.excludedPeriods.pop()
    expect(restored.expense.policyInputError).toBe('')
    restored.wrapper.unmount()
  })

  it.each([false, true])('explains invalid subsidy ranges beside Excel preview until repaired, mobile=%s', async (mobile) => {
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.includeSubsidy = true
    Object.assign(expense.trip, {
      startDate: '2026-09-01', endDate: '2026-09-03', excludedPeriods: [{
        startDate: '2026-09-01', startTime: '09:00', endDate: '', endTime: '18:00',
      }],
    })
    await nextTick()
    const summary = wrapper.findComponent(ExpenseSummaryCardStub)
    expect(summary.props('previewDisabledReason')).toBe('请补齐每个非补助时段的日期和出发时段')
    expense.trip.excludedPeriods[0]!.endDate = '2026-09-04'
    await nextTick()
    expect(summary.props('previewDisabledReason')).toBe('非补助时段必须在本项出差时段内')
    expense.trip.excludedPeriods[0]!.endDate = '2026-09-03'
    await nextTick()
    expect(summary.props('previewDisabledReason')).not.toContain('非补助时段')
    expect(expense.tripPayload()?.excludedPeriods).toHaveLength(1)
    expense.trip.excludedPeriods.push({
      startDate: '2026-09-02', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00',
    })
    await nextTick()
    expect(summary.props('previewDisabledReason')).toContain('非补助时段 1 与时段 2 重叠')
    expect(expense.tripPayloads()).toEqual([])
    expense.trip.excludedPeriods.pop()
    await nextTick()
    expect(summary.props('previewDisabledReason')).not.toContain('重叠')
    wrapper.unmount()
  })

  it.each([false, true])('autosaves an updated subsidy window with retained conflicting absences and restores them for manual repair, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const absence = { startDate: linkedApproval.startDate, startTime: '09:00', endDate: linkedApproval.startDate, endTime: '09:00' }
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId, tripType: 'business' as const,
      startDate: linkedApproval.startDate, startTime: '09:00',
      endDate: linkedApproval.endDate, endTime: '18:00', excludedPeriods: [absence],
    }
    serverDraft = makeDraft({
      relatedApprovalCount: 1, relatedApprovals: [linkedApproval],
      input: {
        ...structuredClone(baseInput), trip: null, trips: [trip],
        editingState: { includeSubsidy: true, trip, trips: [trip] },
      },
    })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    const group = groupOverlappingSubsidyTrips(expense.subsidyTrips)[0]!
    expect(updateSubsidyWindow(group, 'startDate', linkedApproval.endDate)).toBe('')
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const sent = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(sent.trips).toEqual([])
    expect(sent.editingState?.trips?.[0]?.excludedPeriods).toEqual([
      absence,
      { type: 'late_departure', endDate: linkedApproval.endDate, endTime: '09:00' },
    ])
    expect(expense.policyInputError).not.toBe('')
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).not.toBe('')
    wrapper.unmount()
    const restored = await mountView(undefined, false, mobile)
    const saved = restored.expense.subsidyTrips[0]!
    expect(saved.excludedPeriods).toEqual(sent.editingState!.trips![0]!.excludedPeriods)
    expect(restored.expense.policyInputError).not.toBe('')
    saved.excludedPeriods![0]!.startDate = linkedApproval.endDate
    saved.excludedPeriods![0]!.endDate = linkedApproval.endDate
    await nextTick()
    expect(restored.expense.policyInputError).toBe('')
    expect(restored.expense.tripPayloads()).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(650)
    expect(vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2].trips).toHaveLength(1)
    restored.wrapper.unmount()
  })

  it.each([false, true])('autosaves and restores exact attendance dates after changing only a day selection, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const cityOptions = { ...structuredClone(options), attendanceCalendar: { years: [2026], days: { [linkedApproval.startDate]: { working: false, name: "合成休息日" } } } }
    cityOptions.travelProfiles[0]!.travelTypeOption = { value: 'city', label: '境内市内项目出差（长/短期）', key: null }
    vi.mocked(getOaReimbursementOptions).mockResolvedValue(cityOptions)
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId, tripType: 'same_city_project' as const,
      startDate: linkedApproval.startDate, startTime: '09:00', endDate: linkedApproval.endDate, endTime: '18:00',
      attendanceDates: [linkedApproval.startDate, linkedApproval.endDate],
    }
    serverDraft = makeDraft({ relatedApprovalCount: 1, relatedApprovals: [linkedApproval], input: {
      ...structuredClone(baseInput), trip: null, trips: [trip], editingState: { includeSubsidy: true, trip, trips: [trip] },
    } })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expect(expense.attendanceCalendar).toEqual(cityOptions.attendanceCalendar)
    expect(expense.subsidyTrips[0]!.attendanceDates).toContain(linkedApproval.startDate)
    expense.subsidyTrips[0]!.attendanceDates = [linkedApproval.endDate]
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    const sent = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)![2]
    expect(sent.trips![0]!.attendanceDates).toEqual([linkedApproval.endDate])
    expect(sent.editingState!.trips![0]!.attendanceDates).toEqual([linkedApproval.endDate])
    wrapper.unmount()
    const restored = await mountView(undefined, false, mobile)
    expect(restored.expense.subsidyTrips[0]!.attendanceDates).toEqual([linkedApproval.endDate])
    restored.wrapper.unmount()
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

  it.each([false, true])('keeps autosave errors visible without scrolling or losing edits, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    const scroll = observeScrollIntoView()
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('自动保存暂时中断'))
    expense.items[0]!.description = '失败后仍在编辑的内容'
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(wrapper.get('[data-testid="reimbursement-attention"]').text()).toContain('保存失败')
    expect(scroll).not.toHaveBeenCalled()
    expect(expense.items[0]?.description).toBe('失败后仍在编辑的内容')
    await visibleButton(wrapper, '查看并处理').trigger('click')
    await flushPromises()
    expect(scroll.mock.contexts.at(-1)).toBe(wrapper.get('[data-attention-target="save"]').element)
    await visibleButton(wrapper, '重试保存').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="reimbursement-attention"]').exists()).toBe(false)
    expect(expense.items[0]?.description).toBe('失败后仍在编辑的内容')
    wrapper.unmount()
  })

  it.each([false, true])('announces background submission failures without interrupting the page, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    const scroll = observeScrollIntoView()
    const submission = useReimbursementSubmissionStore()
    submission.activeDraftId = 'another-draft'
    submission.submission = submissionResult({ draftId: 'another-draft', status: 'FAILED_FINAL', pollAfterMs: 0 })
    await flushPromises()
    expect(wrapper.find('[data-testid="reimbursement-attention"]').exists()).toBe(false)
    drafts.currentDraft = { ...drafts.currentDraft!, status: 'LOCKED', revision: 5 }
    submission.activeDraftId = drafts.currentDraft.id
    submission.submission = submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 })
    await flushPromises()
    expect(wrapper.get('[data-testid="reimbursement-attention"]').text()).toContain('审批未发起')
    expect(scroll).not.toHaveBeenCalled()
    await visibleButton(wrapper, '查看并处理').trigger('click')
    await flushPromises()
    expect(scroll.mock.contexts.at(-1)).toBe(wrapper.get('[data-attention-target="submission"]').element)
    wrapper.unmount()
  })

  it.each([false, true])('locates an explicit submission validation failure, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: true }, relatedApprovalCount: 1, relatedApprovals: [linkedApproval] })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    vi.mocked(markReimbursementDraftReviewReady).mockRejectedValueOnce(new Error('服务器复核暂时失败'))
    const { wrapper } = await mountView(undefined, false, mobile)
    const scroll = observeScrollIntoView()
    await openReviewStep(wrapper, mobile)
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(markReimbursementDraftReviewReady).toHaveBeenCalledOnce()
    expect(wrapper.get('[data-attention-target="workspace"]').text()).toContain('服务器复核暂时失败')
    expect(scroll.mock.contexts.at(-1)).toBe(wrapper.get('[data-attention-target="workspace"]').element)
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(createReimbursementDraft).toHaveBeenCalledOnce()
    expect(wrapper.find('[data-attention-target="workspace"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="reimbursement-attention"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('locates incomplete fee input instead of leaving the user on review, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: true }, relatedApprovalCount: 1, relatedApprovals: [linkedApproval] })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    const scroll = observeScrollIntoView()
    expense.items[0]!.amount = ''
    await openReviewStep(wrapper, mobile)
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(scroll).toHaveBeenCalled()
    expect((scroll.mock.contexts.at(-1) as HTMLElement).closest('#reimbursement-step-2')).not.toBeNull()
    if (mobile) expect(wrapper.findAll('.mobile-step-nav button')[2]!.attributes('aria-current')).toBe('step')
    expect(expense.items[0]?.amount).toBe('')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows save failure and retries without losing the current edit', async () => {
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('连接暂时中断'))
    const { wrapper, expense } = await mountView()
    expense.items[0]!.description = '本页的新内容'
    await expect(saveCurrent(wrapper)).rejects.toThrow('连接暂时中断')
    await nextTick()
    const error = wrapper.get('[data-testid="autosave-error"]')
    expect(error.text()).toContain('保存失败')
    expect(error.text()).toContain('连接暂时中断')
    expect(wrapper.find('[data-testid="autosave-status"]').exists()).toBe(false)
    expect(expense.items[0]?.description).toBe('本页的新内容')
    await error.get('button').trigger('click')
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
    expect(wrapper.get('[data-testid="autosave-error"]').text()).toContain('保存失败')
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => [false, true].map((withMaterials) => ({ mobile, withMaterials }))))(
    'shows and submits the correct files for a subsidy-only claim, mobile=$mobile, materials=$withMaterials',
    async ({ mobile, withMaterials }) => {
      const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
      const restoredTrip = {
        relatedApprovalId: linkedApproval.processInstanceId, tripType: 'business' as const,
        startDate: linkedApproval.startDate, startTime: '09:00',
        endDate: linkedApproval.endDate, endTime: '18:00', policyConfirmed: false,
      }
      const totals = {
        expenseTotal: '0.00', subsidyTotal: '300.00', totalAmount: '300.00', receiptCount: 0,
        uppercaseAmount: '叁佰元整', subsidy: null, subsidies: [{
          relatedApprovalId: linkedApproval.processInstanceId, tripType: 'business' as const,
          calendarDays: 3, effectiveDays: '3.0', dailyRate: '100.00', total: '300.00',
        }],
      }
      serverDraft = makeDraft({
        input: {
          ...structuredClone(baseInput), accountingSourceVerified: true, items: [], trip: null, trips: [restoredTrip],
          editingState: { includeSubsidy: true, trip: restoredTrip, trips: [restoredTrip] },
        },
        totals, relatedApprovalCount: 1, relatedApprovals: [linkedApproval],
      })
      serverFiles = withMaterials ? [{ ...activeFile, role: 'ATTACHMENT_ONLY', ocrStatus: 'NOT_REQUESTED' }] : []
      vi.mocked(calculateTotals).mockResolvedValue(totals)
      const { wrapper, expense, router, drafts } = await mountView(undefined, false, mobile)
      await expense.refreshCalculations()
      await flushPromises()
      await openReviewStep(wrapper, mobile)
      expect(expense.items).toHaveLength(0)
      const attachments = wrapper.get('[data-testid="submission-attachments"]')
      expect(attachments.findAll('li').map((row) => row.text()))
        .toEqual(withMaterials ? ['票据汇总.pdf', '报销单.xlsx'] : ['报销单.xlsx'])
      drafts.pendingUploads = [{ id: 'uploading', name: '演示材料.pdf', status: 'WRITING' }]
      await nextTick()
      expect(attachments.text()).toContain('材料上传完成后会显示附件清单。')
      expect(attachments.findAll('li')).toHaveLength(0)
      drafts.pendingUploads = []
      await nextTick()
      expense.categoryLoadError = '费用类别暂不可用'
      expense.categories = []
      await visibleButton(wrapper, '提交 OA').trigger('click')
      await flushPromises()
      expect(confirm).toHaveBeenCalledWith(
        expect.stringContaining(withMaterials ? '生成票据汇总 PDF 和报销单 Excel' : '生成报销单 Excel'),
        '提交报销申请', expect.anything(),
      )
      expect(markReimbursementDraftReviewReady).toHaveBeenCalledOnce()
      expect(submitOaReimbursement).toHaveBeenCalledOnce()
      expect(router.currentRoute.value.path).toBe(mobile ? '/m' : '/')
      wrapper.unmount()
    },
  )

  it.each([false, true])('rejects an empty zero-subsidy submission, mobile=%s', async (mobile) => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    serverDraft = makeDraft({
      input: { ...structuredClone(baseInput), items: [] },
      relatedApprovalCount: 1, relatedApprovals: [linkedApproval],
      totals: { ...makeDraft().totals, expenseTotal: '0.00', totalAmount: '0.00', receiptCount: 0 },
    })
    serverFiles = []
    const { wrapper } = await mountView(undefined, false, mobile)
    await openReviewStep(wrapper, mobile)
    const attachments = wrapper.get('[data-testid="submission-attachments"]')
    expect(attachments.findAll('li')).toHaveLength(0)
    expect(attachments.text()).toContain('填写费用明细或核对出差补助')
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(confirm).not.toHaveBeenCalled()
    expect(markReimbursementDraftReviewReady).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('saves input and travel selections before review and submits only once on double click', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    const { wrapper } = await mountView()
    vi.mocked(updateReimbursementDraft).mockClear()
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()
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
    await flushPromises()

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

  it.each([false, true])(
    'renders multiple travel periods as separate responsive rows (mobile=%s)',
    async (mobile) => {
      const { wrapper, expense } = await mountView(undefined, false, mobile)
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
      expect(periods.findAll('.travel-periods__item')[0]?.text())
        .toBe(mobile ? '2026-06-30至2026-07-03' : '2026-06-30—2026-07-03')
      expect(periods.findAll('.travel-periods__item')[1]?.text())
        .toBe(mobile ? '2026-07-04至2026-07-07' : '2026-07-04—2026-07-07')
      expect(periods.findAll('.travel-periods__item')[1]?.attributes('aria-label'))
        .toBe('2026-07-04 至 2026-07-07')
      expect(wrapper.find('[data-testid="derived-accounting-mobile-list"]').exists()).toBe(mobile)
      wrapper.unmount()
    },
  )

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
    await flushPromises()
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

  it.each([false, true])('autosaves receipt-only edits and restores manual attachments (mobile=%s)', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    delete serverDraft.input.items[0]!.sourceFileId
    serverFiles = [{ ...activeFile, role: 'ATTACHMENT_ONLY', attachmentKind: 'other', ocrStatus: 'NOT_REQUESTED', ocrResult: null }]
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    await vi.advanceTimersByTimeAsync(650)
    vi.mocked(updateReimbursementDraft).mockClear()
    expense.items[0]!.receiptAttachmentFileIds = ['file-1']
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(serverDraft.input.items[0]?.receiptAttachmentFileIds).toEqual(['file-1'])
    expect(serverDraft.input.items[0]?.amount).toBe('44.89')
    wrapper.unmount()
    const restored = await mountView(undefined, false, mobile)
    expect(restored.expense.items[0]?.receiptAttachmentFileIds).toEqual(['file-1'])
    vi.mocked(updateReimbursementDraft).mockClear()
    restored.expense.items[0]!.receiptAttachmentFileIds = []
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(serverDraft.input.items[0]?.receiptAttachmentFileIds ?? []).toEqual([])
    restored.wrapper.unmount()
  })

  it('keeps interactions locked during confirmation and reopens editing after cancellation', async () => {
    const confirmation = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    vi.spyOn(ElMessageBox, 'confirm').mockReturnValueOnce(confirmation.promise)
    const { wrapper } = await mountView()
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()
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

  it.each([false, true].flatMap((mobile) => (['excel_only', 'bundle_and_excel'] as const)
    .map((attachmentMode) => ({ mobile, attachmentMode }))))(
    'restores the frozen attachment summary after cleanup, mobile=$mobile, mode=$attachmentMode',
    async ({ mobile, attachmentMode }) => {
      const trip = {
        relatedApprovalId: linkedApproval.processInstanceId, tripType: 'same_city_project' as const,
        startDate: linkedApproval.startDate, startTime: '09:00',
        endDate: linkedApproval.endDate, endTime: '18:00', policyConfirmed: false,
        attendanceDates: [linkedApproval.startDate],
      }
      serverDraft = makeDraft({
        status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z', attachmentMode,
        input: {
          ...structuredClone(baseInput), items: [], trip: null, trips: [trip],
          editingState: { includeSubsidy: true, trip, trips: [trip] },
        },
        totals: {
          expenseTotal: '0.00', subsidyTotal: '300.00', totalAmount: '300.00', receiptCount: 0,
          uppercaseAmount: '叁佰元整', subsidy: null, subsidies: [],
        },
        relatedApprovalCount: 1, relatedApprovals: [linkedApproval],
      })
      serverFiles = attachmentMode === 'bundle_and_excel' ? [{ ...activeFile, status: 'PURGED' }] : []
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
        status: 'SUBMITTED', statusVersion: 8, processInstanceId: 'oa-process-1', pollAfterMs: 0,
      }))
      const { wrapper } = await mountView(undefined, false, mobile, true)
      expect(wrapper.get('.submitted-receipt').text()).toContain('¥300.00')
      expect(wrapper.get('.receipt-attachment-summary').text()).toBe(attachmentMode === 'excel_only'
        ? 'OA 附件：报销单.xlsx'
        : 'OA 附件：票据汇总.pdf、报销单.xlsx')
      const actions = wrapper.get('.submitted-receipt-actions')
      expect(actions.findAll('button').map((button) => button.text()))
        .toEqual(mobile ? ['打开 Excel'] : ['预览 Excel'])
      expect(wrapper.get('.hero-links').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(true)
      expect(wrapper.findAll('button').filter((button) => button.text() === '新开一笔')).toHaveLength(1)
      expect(wrapper.get('.submitted-receipt-toolbar').find('.receipt-attachment-summary').exists()).toBe(!mobile)
      expect(wrapper.findComponent(TripSubsidyCardStub).props('approvalTripType')).toBe('same_city_project')
      expect(calculateTotals).not.toHaveBeenCalled()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )

  it.each([false, true])('loads and retries the locked receipt before enabling Excel, mobile=%s', async (mobile) => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue(undefined as never)
    serverDraft = makeDraft({
      input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: true },
      relatedApprovals: [linkedApproval], relatedApprovalCount: 1,
    })
    vi.mocked(submitOaReimbursement).mockImplementationOnce(async (_draftId, revision) => {
      serverDraft = {
        ...serverDraft, status: 'LOCKED', revision: revision + 1, attachmentMode: 'bundle_and_excel',
        totals: { ...serverDraft.totals, subsidyTotal: '100.00', totalAmount: '144.89' },
        submittedDetails: { employeeName: '提交时姓名', companyLabel: '提交时公司',
          budgetLabel: '提交时预算', travelTypeLabel: '提交时出差类别' },
      }
      vi.mocked(getReimbursementDraft).mockRejectedValueOnce(new Error('回执暂时读取失败'))
      return submissionResult({ status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0 })
    })
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile, true)
    await openReviewStep(wrapper, mobile)
    await visibleButton(wrapper, '提交 OA').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="submitted-receipt-loading"]').text()).toContain('回执暂时读取失败')
    expect(wrapper.find('.submitted-receipt').exists()).toBe(false)
    expect(wrapper.find('.editor-fieldset').exists()).toBe(false)
    await visibleButton(wrapper, '重新读取回执').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.revision).toBe(serverDraft.revision)
    expect(expense.totals?.subsidyTotal).toBe('100.00')
    const receipt = wrapper.get('.submitted-receipt')
    for (const value of ['¥144.89', '提交时姓名', '提交时公司', '提交时预算', '提交时出差类别']) {
      expect(receipt.text()).toContain(value)
    }
    expect(visibleButton(wrapper, mobile ? '打开 Excel' : '预览 Excel').attributes('disabled')).toBeUndefined()
    wrapper.findComponent(ExpenseItemsCardStub).vm.$emit('refresh-draft')
    await flushPromises()
    expect(getReimbursementDraft).toHaveBeenCalledTimes(4)
    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([false, true])('refreshes upload status without discarding edits and flags a changed revision, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '本页未保存的费用说明'
    drafts.pendingUploads = [{ id: 'pending', name: '演示材料.pdf', status: 'WRITING' }]
    await nextTick()
    const materials = wrapper.findComponent(ExpenseItemsCardStub)
    const refreshed = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    const readCount = vi.mocked(listReimbursementDraftFiles).mock.calls.length
    vi.mocked(listReimbursementDraftFiles).mockReturnValueOnce(refreshed.promise)
    materials.vm.$emit('refresh-material-status')
    materials.vm.$emit('refresh-material-status')
    await nextTick()
    expect(materials.props('refreshingMaterialStatus')).toBe(true)
    expect(listReimbursementDraftFiles).toHaveBeenCalledTimes(readCount + 1)
    refreshed.resolve({ draftId: serverDraft.id, revision: serverDraft.revision, items: [] })
    await flushPromises()
    expect(materials.props('refreshingMaterialStatus')).toBe(false)
    expect(drafts.pendingUploads).toHaveLength(0)
    expect(expense.items[0]?.description).toBe('本页未保存的费用说明')
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({
      draftId: serverDraft.id, revision: serverDraft.revision + 1, items: [activeFile],
    })
    materials.vm.$emit('refresh-material-status')
    await flushPromises()
    expect(wrapper.get('[data-testid="autosave-error"]').text()).toContain('核对最新报销内容')
    expect(expense.items[0]?.description).toBe('本页未保存的费用说明')
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([false, true])('defers a newer material revision while a fee editor is open, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, expense, drafts } = await mountView(undefined, true, mobile)
    await vi.advanceTimersByTimeAsync(650)
    if (mobile) await wrapper.findAll('.mobile-step-nav button')[2]!.trigger('click')
    drafts.pendingUploads = [{ id: 'pending', name: '演示材料.pdf', status: 'WRITING' }]
    await nextTick()
    const refreshed = deferred<Awaited<ReturnType<typeof listReimbursementDraftFiles>>>()
    vi.mocked(listReimbursementDraftFiles).mockReturnValueOnce(refreshed.promise)
    await visibleButton(wrapper, '刷新材料状态').trigger('click')
    const row = wrapper.get(mobile ? '.expense-mobile-card' : '.el-table__row')
    await row.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const editor = wrapper.get('.el-dialog textarea')
    await editor.setValue('刷新期间填写的费用说明')

    serverDraft = { ...serverDraft, revision: serverDraft.revision + 1 }
    refreshed.resolve({ draftId: serverDraft.id, revision: serverDraft.revision, items: serverFiles })
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('.el-dialog textarea').element.value).toBe('刷新期间填写的费用说明')
    expect(drafts.currentDraft?.revision).toBe(serverDraft.revision - 1)
    await visibleButton(wrapper, '保存').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(650)
    expect(wrapper.get('[data-testid="autosave-error"]').text()).toContain('核对最新报销内容')
    expect(expense.items[0]?.description).toBe('刷新期间填写的费用说明')
    expect(serverDraft.input.items[0]?.description).not.toBe('刷新期间填写的费用说明')
    wrapper.unmount()
  })

  it.each([false, true])('reads a newer revision when background OCR finishes, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    serverFiles = [{ ...activeFile, ocrStatus: 'RUNNING' }]
    const { wrapper, drafts } = await mountView(undefined, true, mobile)
    await vi.advanceTimersByTimeAsync(650)
    serverDraft = { ...serverDraft, revision: serverDraft.revision + 1 }
    serverFiles = [{ ...activeFile, ocrStatus: 'COMPLETE' }]

    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(drafts.currentDraft?.revision).toBe(serverDraft.revision)
    expect(drafts.files[0]?.ocrStatus).toBe('COMPLETE')
    expect(wrapper.find('[data-testid="autosave-error"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('offers save retry when changing department cannot save local edits, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, drafts, expense } = await mountView((auth) => {
      auth.session!.departments.push({ id: '200', name: '另一演示部门' })
    }, false, mobile)
    drafts.travelApprovals = [{
      ...linkedApproval, originatorDepartmentId: '200', profileDisplayName: '境内出差',
      travelTypeOption: { value: 'business', label: '境内出差', key: null },
      companyOption: options.companyOptions[0]!, budgetCodeOption: options.budgetCodeOptions[0]!,
      unavailableReason: null, createdAt: '2026-08-30T00:00:00Z', finishedAt: '2026-08-31T00:00:00Z',
    }]
    expense.items[0]!.description = '切换部门前的本页输入'
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(new Error('保存连接中断'))
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()

    expect(selectDepartmentFromTravelApproval).not.toHaveBeenCalled()
    expect(wrapper.findAll('button').some((button) => button.text() === '重新加载')).toBe(false)
    expect(expense.items[0]?.description).toBe('切换部门前的本页输入')
    await visibleButton(wrapper, '重试保存').trigger('click')
    await flushPromises()
    expect(serverDraft.input.items[0]?.description).toBe('切换部门前的本页输入')
    wrapper.unmount()
  })

  it.each([false, true])('keeps dirty input when a template change makes the page readonly, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const { wrapper, expense, drafts, router } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '新版模板加载前的编辑'
    drafts.reimbursementOptions = { ...options, templateConfigVersion: options.templateConfigVersion + 1 }
    await nextTick()
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    expect(router.currentRoute.value.path).toBe(mobile ? '/m' : '/')
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(expense.items[0]?.description).toBe('新版模板加载前的编辑')
    wrapper.unmount()
  })

  it.each([false, true])('lets an administrator enter settings to repair unavailable OA templates after confirming unsaved input, mobile=%s', async (mobile) => {
    const unavailable = Object.assign(new Error('请先确认 OA 表单'), {
      isAxiosError: true,
      response: { data: { error: { code: 'OA_TEMPLATE_CONFIRMATION_REQUIRED', message: '请先确认 OA 表单' } } },
    })
    vi.mocked(getOaReimbursementOptions).mockRejectedValue(unavailable)
    const { wrapper, expense, router } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '表单不可用时仍保留的本页说明'
    await nextTick()
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValueOnce(undefined as never)
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    expect(confirm).toHaveBeenCalledOnce()
    expect(router.currentRoute.value.path).toBe(mobile ? '/m/settings' : '/admin/settings')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    expect(serverDraft.input.items[0]?.description).not.toBe('表单不可用时仍保留的本页说明')
    wrapper.unmount()
  })

  it.each([false, true])('opens template settings from a blocked restored draft without treating normalization as an edit, mobile=%s', async (mobile) => {
    const unavailable = Object.assign(new Error('请先确认 OA 表单'), {
      isAxiosError: true,
      response: { data: { error: { code: 'OA_TEMPLATE_CONFIRMATION_REQUIRED', message: '请先确认 OA 表单' } } },
    })
    vi.mocked(getOaReimbursementOptions).mockRejectedValue(unavailable)
    const { wrapper, router } = await mountView(undefined, false, mobile)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    const confirm = vi.spyOn(ElMessageBox, 'confirm')
    await visibleButton(wrapper, '重新检查表单').trigger('click')
    await flushPromises()
    expect(getOaReimbursementOptions).toHaveBeenCalledTimes(2)
    await visibleButton(wrapper, '前往系统设置确认表单').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe(mobile ? '/m/settings' : '/admin/settings')
    expect(router.currentRoute.value.hash).toBe('#oa-template-catalog')
    expect(confirm).not.toHaveBeenCalled()
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    vi.mocked(getOaReimbursementOptions).mockResolvedValue(options)
    await router.push(mobile ? '/m' : '/')
    await flushPromises()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('keeps real unsaved input when the administrator cancels opening template settings, mobile=%s', async (mobile) => {
    const { wrapper, expense, drafts, router } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '需要保留的本页修改'
    drafts.reimbursementOptionsError = 'OA 表单需要确认'
    drafts.reimbursementOptionsErrorCode = 'OA_TEMPLATE_CONFIRMATION_REQUIRED'
    await nextTick()
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    expect(router.currentRoute.value.path).toBe(mobile ? '/m' : '/')
    expect(expense.items[0]?.description).toBe('需要保留的本页修改')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('asks a non-administrator to contact an administrator when OA confirmation is required, mobile=%s', async (mobile) => {
    const { wrapper, drafts } = await mountView((auth) => { auth.session!.isAdmin = false }, false, mobile)
    drafts.reimbursementOptionsError = 'OA 表单需要确认'
    drafts.reimbursementOptionsErrorCode = 'OA_TEMPLATE_CONFIRMATION_REQUIRED'
    await nextTick()
    expect(wrapper.text()).toContain('请联系管理员确认 OA 表单')
    expect(wrapper.findAll('button').some((button) => button.text() === '前往系统设置确认表单')).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('makes invalid accounting options actionable from the review step, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({
      input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: true },
      relatedApprovals: [linkedApproval], relatedApprovalCount: 1,
    })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    await openReviewStep(wrapper, mobile)
    drafts.reimbursementOptions = { ...options, budgetCodeOptions: [] }
    await nextTick()
    const recovery = wrapper.get('[data-testid="accounting-options-recovery"]')
    expect(recovery.isVisible()).toBe(true)
    expect(visibleButton(wrapper, '提交 OA').attributes()).toHaveProperty('disabled')
    await recovery.get('button').trigger('click')
    await flushPromises()
    expect(getOaReimbursementOptions).toHaveBeenCalledTimes(2)
    if (mobile) expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(true)
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
    expect(wrapper.find('[data-testid="autosave-status"]').exists()).toBe(false)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(wrapper.findComponent(ExpenseItemsCardStub).isVisible()).toBe(true)
    expect(wrapper.find('[data-testid="reimbursement-basics-card"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('查看上笔明细')
    expect(wrapper.get('.submitted-receipt').text()).toContain('¥44.89')
    await visibleButton(wrapper, '新开一笔').trigger('click')
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
    expect(wrapper.find('[data-testid="mobile-review-step"]').exists()).toBe(false)
    expect(wrapper.find('.mobile-step-nav').exists()).toBe(false)
    expect(wrapper.findComponent(ExpenseItemsCardStub).isVisible()).toBe(true)
    expect(visibleButton(wrapper, '新开一笔').isVisible()).toBe(true)

    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(true)
    expect(wrapper.findAll('.mobile-step-nav button')[0]!.attributes('aria-current')).toBe('step')
    wrapper.unmount()
  })

  it.each([false, true])('refreshes manual review after administrator recovery without resubmitting, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'MANUAL_REVIEW', statusVersion: 6, pollAfterMs: 0,
    }))
    vi.mocked(getOaReimbursementSubmission).mockResolvedValue(submissionResult({
      status: 'SUBMITTED', statusVersion: 7, processInstanceId: 'oa-process-1', pollAfterMs: 0,
    }))
    const { wrapper } = await mountView(undefined, false, mobile)
    const refresh = visibleButton(wrapper, '刷新提交进度')
    const panel = wrapper.get('.manual-review-panel')
    expect(panel.get('h2').text()).toBe('提交结果待人工核对')
    expect(panel.get('dd').text()).toBe('submission-1')
    expect(panel.get('.manual-review-actions').element.contains(refresh.element)).toBe(true)
    expect(refresh.isVisible()).toBe(true)
    expect(wrapper.text()).not.toContain('返回报销')
    await refresh.trigger('click')
    await flushPromises()
    expect(visibleButton(wrapper, '新开一笔').isVisible()).toBe(true)
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('offers a read-only retry after a remembered submission request fails, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    window.sessionStorage.setItem('dingtalk-travel-reimbursement.reimbursement-submissions.v1', JSON.stringify([{
      draftId: serverDraft.id, expectedRevision: 4, idempotencyKey: 'e5037db9-3909-4fc7-b846-6661d4cfd6c0',
      submissionId: 'submission-1',
    }]))
    vi.mocked(getOaReimbursementSubmission)
      .mockRejectedValueOnce(new Error('提交读取暂时失败'))
      .mockResolvedValueOnce(submissionResult({ status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0 }))
    const { wrapper } = await mountView(undefined, false, mobile)
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('提交读取暂时失败')
    await visibleButton(wrapper, '重新查找提交记录').trigger('click')
    await flushPromises()
    expect(visibleButton(wrapper, '新开一笔').isVisible()).toBe(true)
    expect(getOaReimbursementSubmission).toHaveBeenCalledTimes(2)
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('offers a new form when an open draft expires, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '过期时还没保存的费用说明'
    drafts.currentDraft = { ...drafts.currentDraft!, status: 'EXPIRED' }
    await nextTick()
    const confirm = vi.spyOn(ElMessageBox, 'confirm')
      .mockRejectedValueOnce('cancel')
      .mockResolvedValueOnce(undefined as never)
    const restart = visibleButton(wrapper, '重新填写')
    expect(restart.isVisible()).toBe(true)
    await restart.trigger('click')
    await flushPromises()
    expect(expense.items[0]?.description).toBe('过期时还没保存的费用说明')
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    await restart.trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(wrapper.get('[data-testid="mobile-approval-step"]').isVisible()).toBe(true)
    expect(deleteReimbursementDraft).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('keeps a newly selected approval when the target department has a completed reimbursement, mobile=%s', async (mobile) => {
    const original = { id: '100', name: '技术管理中心' }
    const target = { id: '200', name: '产品开发部' }
    serverDraft = makeDraft({ department: original, input: canonicalNoSubsidyInput() })
    const { wrapper, drafts } = await mountView((auth) => {
      auth.session!.departments = [original, target]
      auth.session!.selectedDepartment = original
    }, false, mobile)
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, department: target })
    vi.mocked(selectDepartmentFromTravelApproval).mockResolvedValue({
      selectedDepartment: target, selectionRequired: false, departments: [original, target],
    })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'SUBMITTED', statusVersion: 8, processInstanceId: 'oa-process-1', pollAfterMs: 0,
    }))
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('打开报销表单后可继续处理该审批')
    expect(replaceReimbursementRelatedApprovals).not.toHaveBeenCalled()
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledWith(
      'draft-created', expect.any(Number), [selection], { signal: expect.any(AbortSignal) },
    )
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('refreshes unavailable options without replacing the draft, mobile=%s', async (mobile) => {
    vi.mocked(getOaReimbursementOptions).mockRejectedValueOnce(new Error('报销选项暂不可用'))
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(true)
    const refresh = visibleButton(wrapper, '刷新表单与审批')
    expect(refresh.isVisible()).toBe(true)
    await refresh.trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-1')
    expect(getReimbursementDraft).toHaveBeenCalledOnce()
    expect(getOaReimbursementOptions).toHaveBeenCalledTimes(2)
    expect(wrapper.findComponent(TravelApprovalSelectorStub).props('readonly')).toBe(false)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => (['amount', 'originalAmount', 'relatedApprovalId'] as const)
    .map((field) => ({ mobile, field }))))('pauses autosave when an approval timeout reads another tab’s $field edits, mobile=$mobile', async ({ mobile, field }) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    if (field === 'originalAmount') Object.assign(serverDraft.input.items[0]!, {
      originalCurrency: 'USD', originalAmount: '999999999999999.90',
    })
    if (field === 'relatedApprovalId') serverDraft.input.editingState!.trip.relatedApprovalId = 'saved-approval'
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '本页尚未保存的说明'
    const concurrent = structuredClone(serverDraft.input)
    if (field === 'amount') concurrent.items[0]!.amount = '88.88'
    else if (field === 'originalAmount') concurrent.items[0]!.originalAmount = '999999999999999.91'
    else concurrent.editingState!.trip.relatedApprovalId = 'other-approval'
    serverDraft = { ...serverDraft, revision: 2, input: concurrent }
    vi.mocked(replaceReimbursementRelatedApprovals).mockRejectedValueOnce(new Error('审批保存响应超时'))
    wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1200)
    expect(drafts.currentDraft?.revision).toBe(2)
    expect(expense.items[0]?.description).toBe('本页尚未保存的说明')
    expect(visibleButton(wrapper, '核对最新报销内容').isVisible()).toBe(true)
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    expect(serverDraft.input).toEqual(concurrent)
    wrapper.unmount()
  })

  it.each([false, true])('keeps saved subsidy exclusions when approval removal fails, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const trip = {
      relatedApprovalId: linkedApproval.processInstanceId, tripType: 'business' as const,
      startDate: linkedApproval.startDate, endDate: linkedApproval.endDate, startTime: '09:00', endTime: '18:00',
      excludedPeriods: [{ startDate: linkedApproval.startDate, endDate: linkedApproval.startDate, startTime: '09:00', endTime: '18:00' }],
    }
    serverDraft = makeDraft({
      input: { ...baseInput, trip: null, trips: [trip], editingState: { includeSubsidy: true, trip, trips: [trip] } },
      relatedApprovals: [linkedApproval], relatedApprovalCount: 1,
    })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    await vi.advanceTimersByTimeAsync(650)
    vi.mocked(updateReimbursementDraft).mockClear()
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(replaceReimbursementRelatedApprovals).mockReturnValueOnce(pending.promise)
    const approvalSelector = wrapper.findComponent(TravelApprovalSelectorStub)
    approvalSelector.vm.$emit('update:modelValue', [])
    await flushPromises()
    expect(expense.subsidyTrips[0]?.excludedPeriods).toMatchObject(trip.excludedPeriods)
    pending.reject(new Error('审批保存响应超时'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1200)
    expect(approvalSelector.props('modelValue')).toHaveLength(1)
    expect(expense.subsidyTrips[0]?.excludedPeriods).toMatchObject(trip.excludedPeriods)
    expect(serverDraft.input.editingState?.trips?.[0]?.excludedPeriods).toMatchObject(trip.excludedPeriods)
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('does not replay conflicting edits before an explicit reload, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const conflict = Object.assign(new Error('报销内容版本已更新'), {
      isAxiosError: true,
      response: { data: { error: { code: 'REIMBURSEMENT_DRAFT_REVISION_CONFLICT' } } },
    })
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(conflict)
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    const remoteInput = structuredClone(baseInput)
    remoteInput.items[0]!.description = '服务器已有的修改'
    serverDraft = makeDraft({ revision: 2, input: remoteInput })
    expense.items[0]!.description = '本页尚未保存的修改'
    await nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(expense.items[0]?.description).toBe('本页尚未保存的修改')
    expect(wrapper.find('[data-testid="autosave-status"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="autosave-error"]').text()).not.toContain('重试保存')
    expect(visibleButton(wrapper, '核对最新报销内容').isVisible()).toBe(true)
    await vi.advanceTimersByTimeAsync(1200)
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
      .mockResolvedValueOnce('confirm' as never)
    await visibleButton(wrapper, '核对最新报销内容').trigger('click')
    await flushPromises()
    expect(expense.items[0]?.description).toBe('本页尚未保存的修改')
    await visibleButton(wrapper, '核对最新报销内容').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(expense.items[0]?.description).toBe('服务器已有的修改')
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([false, true])('shows successful submissions and returns to the original draft, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    const unused = makeDraft({ id: 'other-draft' })
    const expired = makeDraft({ id: 'expired-draft', status: 'EXPIRED' })
    const submitted = makeDraft({ id: 'older-submission', status: 'LOCKED', revision: 5 })
    vi.mocked(listReimbursementDrafts).mockResolvedValue({
      items: [serverDraft, unused, expired], offset: 0, limit: 50, total: 3,
    })
    vi.mocked(listReimbursementSubmissionHistory).mockResolvedValue([
      { id: submitted.id, lockedAt: '2026-09-04T00:03:00Z', relatedApprovalCount: 1 },
    ])
    vi.mocked(getReimbursementDraft).mockImplementation(async (id) => id === submitted.id ? submitted : serverDraft)
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async (id) => id === submitted.id
      ? { draftId: id, revision: submitted.revision, items: [] }
      : { draftId: id, revision: serverDraft.revision, items: [activeFile] })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      draftId: submitted.id, status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0,
    }))
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const originalId = serverDraft.id
    expense.items[0]!.description = '查看记录前编辑的费用'
    await nextTick()
    if (mobile) await wrapper.findAll('.mobile-step-nav button')[2]!.trigger('click')
    expect(listReimbursementSubmissionHistory).not.toHaveBeenCalled()
    await visibleButton(wrapper, '报销记录').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('最近 10 笔成功提交')
    const list = wrapper.get('[data-testid="reimbursement-history"]')
    expect(list.text()).not.toContain('草稿')
    expect(list.text()).not.toContain('已过期')
    expect(list.findAll('button')).toHaveLength(1)
    await visibleButton(wrapper, '查看提交').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(submitted.id)
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('审批已成功发起')
    expect(visibleButton(wrapper, '返回当前报销').element.closest('.hero-meta')).not.toBeNull()
    expect(wrapper.text()).not.toContain('再报销一笔')
    expect(wrapper.text()).not.toContain('重新填写')
    await visibleButton(wrapper, '返回当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(originalId)
    expect(expense.items[0]?.description).toBe('查看记录前编辑的费用')
    expect(drafts.files).toEqual([activeFile])
    if (mobile) expect(wrapper.findAll('.mobile-step-nav button')[2]!.attributes('aria-current')).toBe('step')
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('allows retrying the history list and explains an empty result, mobile=%s', async (mobile) => {
    vi.mocked(listReimbursementSubmissionHistory).mockRejectedValueOnce(new Error('记录读取失败'))
    const { wrapper } = await mountView(undefined, false, mobile)
    await visibleButton(wrapper, '报销记录').trigger('click')
    await flushPromises()
    await visibleButton(wrapper, '重新加载记录').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('当前部门还没有成功提交 OA 的报销记录')
    expect(listReimbursementSubmissionHistory).toHaveBeenCalledTimes(2)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('lists OA attachments inside the totals card, mobile=%s', async (mobile) => {
    const { wrapper, drafts } = await mountView(undefined, false, mobile, true)
    await openReviewStep(wrapper, mobile)
    const review = wrapper.get('[data-testid="mobile-review-step"]')
    const summary = review.get('.summary-card')
    expect(summary.find('.totals-grid').exists()).toBe(true)
    const attachments = summary.get('[data-testid="submission-attachments"]')
    expect(attachments.findAll('li').map((row) => row.text())).toEqual(['票据汇总.pdf', '报销单.xlsx'])
    expect(attachments.text()).not.toContain('行程单')
    expect(review.findAll('.content-card')).toHaveLength(1)
    expect(visibleButton(wrapper, '提交 OA').isVisible()).toBe(true)
    drafts.files = []
    await nextTick()
    expect(attachments.findAll('li')).toHaveLength(0)
    expect(attachments.text()).toContain('请先上传报销材料')
    wrapper.unmount()
  })

  it.each([false, true])('resumes a saved draft with its materials after returning from the current submission, mobile=%s', async (mobile) => {
    const editable = makeDraft({ id: 'editable-draft', revision: 4, input: canonicalNoSubsidyInput() })
    const submitted = makeDraft({ id: 'older-submission', status: 'LOCKED', revision: 5 })
    const pendingUpload = { id: 'upload-1', name: '演示材料.pdf', status: 'WRITING' } as const
    vi.mocked(listReimbursementDrafts).mockResolvedValue({
      items: [submitted, editable], offset: 0, limit: 50, total: 2,
    })
    vi.mocked(getReimbursementDraft).mockImplementation(async (id) => id === submitted.id ? submitted : editable)
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async (id) => id === submitted.id
      ? { draftId: id, revision: submitted.revision, items: [] }
      : { draftId: id, revision: editable.revision, items: [activeFile], pendingUploads: [pendingUpload] })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      draftId: submitted.id, status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0,
    }))
    const opened = deferred<ReimbursementDraft>()
    vi.mocked(createReimbursementDraft).mockReturnValueOnce(opened.promise)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    expect(drafts.currentDraft?.id).toBe(submitted.id)
    const resume = visibleButton(wrapper, '新开一笔')
    await Promise.all([resume.trigger('click'), resume.trigger('click')])
    opened.resolve(editable)
    await flushPromises()
    expect(createReimbursementDraft).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ items: [] }), { signal: expect.any(AbortSignal), reuseExisting: true, replaceCurrent: { id: submitted.id, revision: submitted.revision }, expectedDepartment: { id: '100', name: '测试部门' } },
    )
    expect(drafts.currentDraft?.id).toBe(editable.id)
    expect(drafts.currentDraft?.revision).toBe(editable.revision)
    expect(drafts.files).toEqual([activeFile])
    expect(drafts.pendingUploads).toEqual([pendingUpload])
    expect(expense.items).toHaveLength(1)
    expect(drafts.drafts.map((draft) => draft.id).sort()).toEqual([editable.id, submitted.id].sort())
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('keeps the failed form when cancelling new from the header, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 }))
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    const originalId = drafts.currentDraft!.id
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(originalId)
    expect(visibleButton(wrapper, '保留内容重新处理').isVisible()).toBe(true)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(recoverReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('keeps new in the header and disables it while recovering, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 }))
    const restoring = deferred<ReimbursementDraft>()
    vi.mocked(recoverReimbursementDraft).mockReturnValueOnce(restoring.promise)
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    expect(visibleButton(wrapper, '保留内容重新处理').classes()).toContain('is-loading')
    expect(visibleButton(wrapper, '新开一笔').attributes()).toHaveProperty('disabled')
    expect(wrapper.get('.submission-feedback-actions').findAll('button')).toHaveLength(1)
    expect(wrapper.get('.hero-links').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(true)
    expect(wrapper.get('.hero-links').findAll('button, a').at(-1)!.text()).toBe('新开一笔')
    serverDraft = makeDraft({ id: 'restored-form', recoverySourceDraftId: 'draft-1' })
    restoring.resolve(serverDraft)
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(serverDraft.id)
    expect(wrapper.get('.hero-links').text()).toContain('新开一笔')
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => [false, true].map((admin) => ({ mobile, admin }))))(
    'keeps header actions in place while preparing and after opening a blank reimbursement, mobile=$mobile admin=$admin', async ({ mobile, admin }) => {
      serverDraft.input = canonicalNoSubsidyInput()
      vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
      const { wrapper, drafts, expense } = await mountView((auth) => { auth.session!.isAdmin = admin }, false, mobile)
      try {
        const labels = admin ? ['系统设置', '报销记录', '新开一笔'] : ['报销记录', '新开一笔']
        const actions = () => wrapper.get('.hero-links').findAll('button, a').map((item) => item.text())
        const history = visibleButton(wrapper, '报销记录').element
        const newButton = visibleButton(wrapper, '新开一笔').element
        const pending = deferred<ReimbursementDraft>()
        vi.mocked(createReimbursementDraft).mockReturnValueOnce(pending.promise)
        await visibleButton(wrapper, '新开一笔').trigger('click')
        await flushPromises()
        expect(actions()).toEqual(labels)
        expect(visibleButton(wrapper, '报销记录').element).toBe(history)
        expect(visibleButton(wrapper, '新开一笔').element).toBe(newButton)
        expect(visibleButton(wrapper, '报销记录').attributes()).toHaveProperty('disabled')
        expect(visibleButton(wrapper, '新开一笔').attributes()).toHaveProperty('disabled')
        serverDraft = makeDraft({ id: 'blank-current', input: structuredClone(vi.mocked(createReimbursementDraft).mock.calls[0]![0]) })
        serverFiles = []
        pending.resolve(serverDraft)
        await flushPromises()
        expect(drafts.currentDraft?.id).toBe('blank-current')
        expect(actions()).toEqual(labels)
        expect(visibleButton(wrapper, '报销记录').element).toBe(history)
        expect(visibleButton(wrapper, '新开一笔').element).toBe(newButton)
        expect(visibleButton(wrapper, '报销记录').attributes('disabled')).toBeUndefined()
        expect(visibleButton(wrapper, '新开一笔').attributes()).toHaveProperty('disabled')
        await visibleButton(wrapper, '新开一笔').trigger('click')
        await flushPromises()
        expect(createReimbursementDraft).toHaveBeenCalledOnce()
        expect(ElMessageBox.confirm).toHaveBeenCalledOnce()
        expense.items = [{ id: 'manual-new', source: 'manual', category: 'local_transport',
          date: '2026-09-01', displayDate: '2026-09-01', description: '新录入交通费', amount: '44.89', receiptCount: 1 }]
        await nextTick()
        expect(visibleButton(wrapper, '新开一笔').attributes('disabled')).toBeUndefined()
        expect(actions()).toEqual(labels)
      } finally { wrapper.unmount() }
    },
  )

  it.each([false, true])('opens a blank form only after confirming new, mobile=%s', async (mobile) => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const original = drafts.currentDraft!.id
    expect(wrapper.get('.hero-links').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(true)
    expect(wrapper.get('.hero-links').findAll('button, a').at(-1)!.text()).toBe('新开一笔')
    expect(wrapper.find('.new-reimbursement-action').exists()).toBe(false)
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(original)
    expect(expense.items).toHaveLength(1)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(createReimbursementDraft).toHaveBeenCalledWith(expect.objectContaining({ items: [] }),
      expect.objectContaining({ replaceCurrent: { id: original, revision: 1 } }))
    expect(drafts.currentDraft?.id).toBe('draft-created')
    expect(expense.items).toHaveLength(0)
    expect(wrapper.text()).not.toContain('待处理报销')
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['lost-response', 'concurrent-replacement'].map((reason) => ({ mobile, reason }))))('resolves a server-committed replacement ($reason), mobile=$mobile', async ({ mobile, reason }) => {
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'draft-created-before-disconnect', input: { ...canonicalNoSubsidyInput(), items: [] } })
      throw new Error(reason === 'lost-response' ? '模拟：新单已创建但响应丢失' : '模拟：另一页已新开，版本冲突')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    try {
      expect(drafts.currentDraft?.id).toBe('draft-created-before-disconnect')
      expect(drafts.mutationError).toBe('')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(createReimbursementDraft).toHaveBeenCalledOnce()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['user', 'department'].map((changed) => ({ mobile, changed }))))('discards old-session recovery errors after $changed changes, mobile=$mobile', async ({ mobile, changed }) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 }))
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(recoverReimbursementDraft).mockReturnValueOnce(pending.promise)
    let authority!: ReturnType<typeof useAuthStore>
    const { wrapper, drafts } = await mountView((auth) => { authority = auth }, false, mobile)
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    await vi.waitFor(() => expect(recoverReimbursementDraft).toHaveBeenCalledOnce())
    serverDraft = makeDraft({ id: 'next-session-draft', input: canonicalNoSubsidyInput() })
    if (changed === 'user') authority.session!.user.userId = 'next-synthetic-user'
    else {
      const department = { id: '200', name: '另一个合成部门' }
      serverDraft.department = department
      authority.session!.departments = [department]
      authority.session!.selectedDepartment = department
    }
    await flushPromises()
    await vi.waitFor(() => expect(drafts.currentDraft?.id).toBe('next-session-draft'))
    pending.reject(new Error('仅属于旧会话的恢复失败'))
    await flushPromises()
    try {
      expect(wrapper.text()).not.toContain('仅属于旧会话的恢复失败')
    } finally { wrapper.unmount() }
  })


  it.each([false, true].flatMap((mobile) => ['workspace', 'detail'].flatMap((unavailable) => [false, true].map((sourceLocked) => ({ mobile, unavailable, sourceLocked })))))('can confirm an uncertain new result after $unavailable read fails, mobile=$mobile locked=$sourceLocked', async ({ mobile, unavailable, sourceLocked }) => {
    serverDraft = makeDraft({ status: sourceLocked ? 'LOCKED' : 'DRAFT', input: canonicalNoSubsidyInput() })
    if (sourceLocked) vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 }))
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const previousId = drafts.currentDraft!.id
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'actual-new-form', input: { ...canonicalNoSubsidyInput(), items: [] } })
      serverFiles = []
      if (unavailable === 'workspace') vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('工作区暂不可读'))
      else vi.mocked(getReimbursementDraft).mockRejectedValueOnce(new Error('新单暂不可读'))
      throw new Error('模拟：创建完成后断线')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(previousId)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('核对当前报销')
    expect(visibleButton(wrapper, '新开一笔').attributes()).toHaveProperty('disabled')
    if (sourceLocked) expect(visibleButton(wrapper, '保留内容重新处理').attributes()).toHaveProperty('disabled')
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('actual-new-form')
    expect(expense.items).toEqual([])
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(wrapper.find('[data-attention-target="workspace"]').exists()).toBe(false)
    expect(createReimbursementDraft).toHaveBeenCalledOnce()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('preserves local edits if new was not created and readback initially failed, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const previousId = drafts.currentDraft!.id
    expense.items[0]!.description = '新开确实失败后仍须保留的输入'
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('暂时无法核对'))
      throw new Error('创建被拒绝，未生成新单')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(previousId)
    expect(expense.items[0]!.description).toBe('新开确实失败后仍须保留的输入')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(createReimbursementDraft).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['LOCKED', 'EXPIRED', 'recovering'].map((state) => ({ mobile, state }))))('keeps a concurrently changed same-id form readonly after confirmation, mobile=$mobile state=$state', async ({ mobile, state }) => {
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('回查暂时中断'))
      throw new Error('新开请求未成功')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    serverDraft = { ...serverDraft, status: state === 'recovering' ? 'DRAFT' : state as 'LOCKED' | 'EXPIRED', revision: 5, recoveryPending: state === 'recovering', recoverySourceDraftId: state === 'recovering' ? 'failed-source' : undefined, lockedAt: state === 'LOCKED' ? '2026-09-04T00:02:00Z' : null }
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'VERIFYING', pollAfterMs: 0 }))
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    try {
      expect(drafts.currentDraft?.status).toBe(state === 'recovering' ? 'DRAFT' : state)
      expect(drafts.currentDraft?.revision).toBe(5)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    } finally { wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['user', 'department', 'return-to-user'].map((change) => ({ mobile, change }))))('ignores an old confirmation continuation after a locked restore and $change switch, mobile=$mobile', async ({ mobile, change }) => {
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    let authority!: ReturnType<typeof useAuthStore>
    const { wrapper, drafts } = await mountView((auth) => { authority = auth }, false, mobile)
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'locked-replacement', status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
      vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('回查暂时中断'))
      throw new Error('新单成功但响应丢失')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    const lockedRestore = deferred<ReimbursementSubmission>()
    vi.mocked(getOaReimbursementSubmissionForDraft).mockReturnValueOnce(lockedRestore.promise)
    void visibleButton(wrapper, '核对当前报销').trigger('click')
    await vi.waitFor(() => expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith('locked-replacement', expect.anything()))
    serverDraft = makeDraft({ id: 'new-session-current', input: canonicalNoSubsidyInput() })
    if (change === 'department') {
      const department = { id: '200', name: '下一合成部门' }
      serverDraft.department = department
      authority.session!.departments = [department]
      authority.session!.selectedDepartment = department
    } else authority.session!.user.userId = 'next-confirmation-user'
    await flushPromises()
    await vi.waitFor(() => expect(drafts.currentDraft?.id).toBe('new-session-current'))
    if (change === 'return-to-user') {
      serverDraft = makeDraft({ id: 'locked-replacement', status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ draftId: 'locked-replacement', status: 'FAILED_FINAL', pollAfterMs: 0 }))
      authority.session!.user.userId = 'synthetic-user'
      await flushPromises()
      await vi.waitFor(() => expect(drafts.currentDraft?.id).toBe('locked-replacement'))
    }
    vi.mocked(createReimbursementDraft).mockRejectedValueOnce(new Error('当前会话自己的新开错误'))
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('当前会话自己的新开错误')
    await openReviewStep(wrapper, mobile)
    lockedRestore.reject(new Error('旧会话的状态恢复失败'))
    await flushPromises()
    try {
      expect(wrapper.text()).toContain('当前会话自己的新开错误')
      if (mobile) expect(wrapper.findAll('.mobile-step-nav button')[3]!.attributes('aria-current')).toBe('step')
    } finally { wrapper.unmount() }
  })


  it.each([false, true].flatMap((mobile) => ['LOCKED', 'DRAFT'].map((status) => ({ mobile, status }))))('preserves unsaved input until explicit readback when same-id $status changes, mobile=$mobile', async ({ mobile, status }) => {
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValueOnce('confirm' as never).mockRejectedValueOnce('cancel').mockResolvedValueOnce('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '本页未保存的合成说明'
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('回查中断'))
      throw new Error('未新开')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    serverDraft = { ...serverDraft, status: status as 'DRAFT' | 'LOCKED', revision: 5,
      input: { ...serverDraft.input, items: [{ ...serverDraft.input.items[0]!, description: '服务器中的合成说明' }] } }
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'VERIFYING', pollAfterMs: 0 }))
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    expect(expense.items[0]!.description).toBe('本页未保存的合成说明')
    expect(drafts.currentDraft?.revision).toBe(1)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(wrapper.get('[data-attention-target="save"]').text()).toContain('本页修改已保留')
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    await visibleButton(wrapper, '核对最新报销内容').trigger('click')
    await flushPromises()
    expect(expense.items[0]!.description).toBe('本页未保存的合成说明')
    await visibleButton(wrapper, '核对最新报销内容').trigger('click')
    await flushPromises()
    expect(expense.items[0]!.description).toBe('服务器中的合成说明')
    expect(drafts.currentDraft?.revision).toBe(5)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(status === 'LOCKED')
    expect(createReimbursementDraft).toHaveBeenCalledOnce()
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('uses a newer compatible revision while retaining local input after confirmation, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '继续保存本页输入'
    vi.mocked(createReimbursementDraft).mockImplementationOnce(async () => {
      vi.mocked(listWorkspaceReimbursements).mockRejectedValueOnce(new Error('回查中断'))
      throw new Error('未新开')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    serverDraft = { ...serverDraft, revision: 5 }
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.revision).toBe(5)
    expect(expense.items[0]!.description).toBe('继续保存本页输入')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledWith('draft-1', 5,
      expect.objectContaining({ items: [expect.objectContaining({ description: '继续保存本页输入' })] }), expect.anything())
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })


  it.each([false, true])('resumes autosave after a slow rejected new request and compatible automatic readback, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '慢请求结束后继续保存'
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(createReimbursementDraft).mockReturnValueOnce(pending.promise)
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(650)
    serverDraft = { ...serverDraft, revision: 5 }
    pending.reject(new Error('新开未成功'))
    await flushPromises()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledWith('draft-1', 5, expect.objectContaining({
      items: [expect.objectContaining({ description: '慢请求结束后继续保存' })],
    }), expect.anything())
    wrapper.unmount()
  })


  it.each([false, true].flatMap((mobile) => [false, true].map((committed) => ({ mobile, committed }))))('can recover a failed empty-workspace preparation (committed=$committed), mobile=$mobile', async ({ mobile, committed }) => {
    serverDraft = makeDraft({ status: 'EXPIRED', input: canonicalNoSubsidyInput() })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const originalId = drafts.currentDraft!.id
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(createReimbursementDraft).mockRejectedValueOnce(new Error('旧单已清理')).mockImplementationOnce(async (input) => {
      if (committed) {
        serverDraft = makeDraft({ id: 'prepared-before-disconnect', input })
        serverFiles = []
        vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [serverDraft], total: 1 })
      }
      throw new Error('准备新单连接中断')
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(originalId)
    expect(expense.items).toHaveLength(1)
    await visibleButton(wrapper, '核对当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(committed ? 'prepared-before-disconnect' : 'draft-created')
    expect(expense.items).toEqual([])
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(createReimbursementDraft).toHaveBeenCalledTimes(committed ? 2 : 3)
    for (const call of vi.mocked(createReimbursementDraft).mock.calls.slice(1)) {
      expect(call[1]).not.toHaveProperty('replaceCurrent')
      expect(call[1]).toEqual(expect.objectContaining({ reuseExisting: true }))
    }
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('reuses another page current created after an empty workspace read, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'EXPIRED', input: canonicalNoSubsidyInput() })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(createReimbursementDraft).mockRejectedValueOnce(new Error('旧单已清理')).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'other-page-current', input: canonicalNoSubsidyInput() })
      serverDraft.input.items[0]!.description = '另一页已保存的内容'
      return serverDraft
    })
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('other-page-current')
    expect(expense.items[0]!.description).toBe('另一页已保存的内容')
    expect(vi.mocked(createReimbursementDraft).mock.calls[1]![1]).toEqual(expect.objectContaining({ reuseExisting: true }))
    expect(vi.mocked(createReimbursementDraft).mock.calls[1]![1]).not.toHaveProperty('replaceCurrent')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('keeps the current form on settings return despite an older pending task, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    const editing = serverDraft
    const pending = makeDraft({ id: 'older-pending', status: 'LOCKED', revision: 5 })
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [editing], total: 1 })
    vi.mocked(listReimbursementDrafts).mockResolvedValue({ items: [pending, editing], offset: 0, limit: 50, total: 2 })
    const { wrapper, drafts, router } = await mountView(undefined, false, mobile)
    expect(drafts.currentDraft?.id).toBe(editing.id)
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    await flushPromises()
    await router.push(mobile ? '/m' : '/')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(editing.id)
    expect(wrapper.text()).not.toContain('提交结果待人工核对')
    expect(wrapper.text()).toContain('报销记录')
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('replaces expired material recovery with an actionable new form, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ status: 'EXPIRED', recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    try {
      expect(wrapper.text()).toContain('本次填写内容已过期')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(wrapper.findAll('button').some((button) => button.text().trim() === '继续恢复材料')).toBe(false)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('过期')
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(9000)
      expect(getReimbursementDraft).not.toHaveBeenCalled()
      await visibleButton(wrapper, '重新填写').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('draft-created')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('stops polling when material recovery expires on the server, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const { wrapper } = await mountView(undefined, false, mobile)
    try {
      expect(visibleButton(wrapper, '继续恢复材料').isVisible()).toBe(true)
      serverDraft = { ...serverDraft, status: 'EXPIRED' }
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(wrapper.text()).toContain('本次填写内容已过期')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(visibleButton(wrapper, '重新填写').attributes('disabled')).toBeUndefined()
      expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('过期')
      expect(getReimbursementDraft).toHaveBeenCalledOnce()
      await vi.advanceTimersByTimeAsync(9000)
      expect(getReimbursementDraft).toHaveBeenCalledOnce()
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('shows expiration when continuing material recovery is rejected as expired, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    vi.mocked(recoverReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('保留的材料已过期，请新开报销'), {
      isAxiosError: true, response: { status: 409, data: { error: { code: 'REIMBURSEMENT_DRAFT_EXPIRED', message: '保留的材料已过期，请新开报销' } } },
    }))
    const { wrapper } = await mountView(undefined, false, mobile)
    try {
      await visibleButton(wrapper, '继续恢复材料').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('本次填写内容已过期')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(visibleButton(wrapper, '新开一笔').attributes('disabled')).toBeUndefined()
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(9000)
      expect(getReimbursementDraft).not.toHaveBeenCalled()
      expect(recoverReimbursementDraft).toHaveBeenCalledOnce()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('does not resume material recovery after expiration during its source lookup, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    try {
      const source = deferred<ReimbursementDraft>()
      vi.mocked(getReimbursementDraft).mockReturnValueOnce(source.promise)
      await visibleButton(wrapper, '继续恢复材料').trigger('click')
      drafts.currentDraft = { ...drafts.currentDraft!, status: 'EXPIRED' }
      source.resolve(makeDraft({ id: 'failed-source', status: 'LOCKED' }))
      await flushPromises()
      expect(wrapper.text()).toContain('本次填写内容已过期')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(visibleButton(wrapper, '新开一笔').attributes('disabled')).toBeUndefined()
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(9000)
      expect(getReimbursementDraft).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('stops waiting when cleanup removed a pending recovery and can prepare one new form, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    try {
      const unavailable = Object.assign(new Error('报销记录不存在'), {
        isAxiosError: true, response: { status: 404, data: { error: { code: 'REIMBURSEMENT_DRAFT_NOT_FOUND', message: '报销记录不存在' } } },
      })
      vi.mocked(getReimbursementDraft).mockClear()
      vi.mocked(getReimbursementDraft).mockRejectedValueOnce(unavailable)
      const scroll = observeScrollIntoView()
      await vi.advanceTimersByTimeAsync(9000)
      await flushPromises()
      expect(getReimbursementDraft).toHaveBeenCalledOnce()
      expect(scroll).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('当前报销记录已不可用')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(wrapper.text()).not.toContain('本次填写内容已过期')
      expect(expense.items[0]!.description).toBe('机场至酒店')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('不可用')
      expect(visibleButton(wrapper, '新开一笔').attributes('disabled')).toBeUndefined()
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
      vi.mocked(createReimbursementDraft).mockRejectedValueOnce(unavailable)
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
      await visibleButton(wrapper, '新开一笔').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('draft-created')
      expect(wrapper.text()).not.toContain('当前报销记录已不可用')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(expense.items).toEqual([])
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('keeps recovery retryable after a transient status-read timeout, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    try {
      vi.mocked(getReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('读取超时'), { code: 'ECONNABORTED' }))
      await vi.advanceTimersByTimeAsync(3000)
      expect(wrapper.text()).not.toContain('当前报销记录已不可用')
      expect(visibleButton(wrapper, '继续恢复材料').isVisible()).toBe(true)
      serverDraft = { ...serverDraft, recoveryPending: false, revision: 2 }
      await vi.advanceTimersByTimeAsync(3000)
      await flushPromises()
      expect(drafts.currentDraft?.recoveryPending).toBe(false)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('stops recovery when its source record is no longer available, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const { wrapper, expense } = await mountView(undefined, false, mobile)
    try {
      vi.mocked(getReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('报销记录不存在'), {
        isAxiosError: true, response: { status: 404, data: { error: { code: 'REIMBURSEMENT_DRAFT_NOT_FOUND', message: '报销记录不存在' } } },
      }))
      await visibleButton(wrapper, '继续恢复材料').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('当前报销记录已不可用')
      expect(wrapper.text()).not.toContain('材料尚在恢复')
      expect(expense.items[0]!.description).toBe('机场至酒店')
      expect(visibleButton(wrapper, '新开一笔').attributes('disabled')).toBeUndefined()
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(9000)
      expect(getReimbursementDraft).not.toHaveBeenCalled()
      expect(recoverReimbursementDraft).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['missing', 'expired'].map((reply) => ({ mobile, reply }))))('ignores an old recovery poll after returning to the same account and draft ($reply), mobile=$mobile', async ({ mobile, reply }) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const original = structuredClone(serverDraft)
    let authority!: ReturnType<typeof useAuthStore>
    const { wrapper, drafts } = await mountView((auth) => { authority = auth }, false, mobile)
    try {
      const old = deferred<ReimbursementDraft>()
      vi.mocked(getReimbursementDraft).mockReturnValueOnce(old.promise)
      await vi.advanceTimersByTimeAsync(3000)
      serverDraft = makeDraft({ id: 'next-account-draft', input: canonicalNoSubsidyInput() })
      authority.session!.user.userId = 'next-account'
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('next-account-draft')
      serverDraft = original
      authority.session!.user.userId = 'synthetic-user'
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(original.id)
      if (reply === 'missing') old.reject(Object.assign(new Error('旧会话的记录不存在'), {
        isAxiosError: true, response: { status: 404, data: { error: { code: 'REIMBURSEMENT_DRAFT_NOT_FOUND' } } },
      }))
      else old.resolve({ ...original, status: 'EXPIRED' })
      await flushPromises()
      expect(wrapper.text()).not.toContain('当前报销记录已不可用')
      expect(wrapper.text()).not.toContain('本次填写内容已过期')
      expect(visibleButton(wrapper, '继续恢复材料').isVisible()).toBe(true)
      vi.mocked(getReimbursementDraft).mockClear()
      await vi.advanceTimersByTimeAsync(3000)
      expect(getReimbursementDraft).toHaveBeenCalledOnce()
    } finally { wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['background', 'immediate', 'preserve-other-error'].map((completion) => ({ mobile, completion }))))('clears only the resolved recovery error ($completion), mobile=$mobile', async ({ mobile, completion }) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status: 'FAILED_FINAL', pollAfterMs: 0 }))
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    const timeout = '合成：恢复请求响应超时'
    try {
      vi.mocked(recoverReimbursementDraft).mockImplementationOnce(async () => {
        serverDraft = makeDraft({ id: 'pending-copy', recoverySourceDraftId: 'draft-1', recoveryPending: completion !== 'immediate', input: canonicalNoSubsidyInput() })
        throw new Error(timeout)
      })
      await visibleButton(wrapper, '保留内容重新处理').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('pending-copy')
      if (completion !== 'immediate') {
        expect(wrapper.text()).toContain(timeout)
        if (completion === 'preserve-other-error') {
          vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as never)
          vi.mocked(createReimbursementDraft).mockRejectedValueOnce(new Error(timeout))
          await visibleButton(wrapper, '新开一笔').trigger('click')
          await flushPromises()
          expect(drafts.currentDraft?.id).toBe('pending-copy')
        }
        serverDraft = { ...serverDraft, recoveryPending: false, revision: 2 }
        await vi.advanceTimersByTimeAsync(3000)
        await flushPromises()
      }
      expect(drafts.currentDraft?.recoveryPending).toBe(false)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      if (completion === 'preserve-other-error') expect(wrapper.text()).toContain(timeout)
      else {
        expect(wrapper.text()).not.toContain(timeout)
        expect(wrapper.find('[data-testid="reimbursement-attention"]').exists()).toBe(false)
      }
      expect(recoverReimbursementDraft).toHaveBeenCalledOnce()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('refreshes a recovery completed in another page, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft = makeDraft({ recoverySourceDraftId: 'failed-source', recoveryPending: true, input: canonicalNoSubsidyInput() })
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(wrapper.findComponent(ExpenseSummaryCardStub).props('previewDisabledReason')).toContain('材料恢复完成')
    serverDraft = { ...serverDraft, recoveryPending: false, revision: 2,
      input: { ...serverDraft.input, items: serverDraft.input.items.map((item) => ({ ...item, description: '另一页恢复后保存的内容' })) } }
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(drafts.currentDraft?.recoveryPending).toBe(false)
    expect(expense.items[0]!.description).toBe('另一页恢复后保存的内容')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(recoverReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('opens the existing submission when a recovered form was submitted in another page, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockImplementation(async (id) => submissionResult({
      draftId: id, status: id === 'already-recovered' ? 'SUBMITTED' : 'FAILED_FINAL', pollAfterMs: 0,
    }))
    vi.mocked(recoverReimbursementDraft).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'already-recovered', status: 'LOCKED', revision: 5, recoverySourceDraftId: 'draft-1' })
      return serverDraft
    })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('already-recovered')
    expect(wrapper.get('[data-testid="submission-status"]').text()).toContain('审批已成功发起')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('restores a definitive failure with its saved contents, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'FAILED_FINAL', statusVersion: 8,
      error: { code: 'OA_CREATE_REJECTED', message: '钉钉拒绝发起审批' }, pollAfterMs: 0,
    }))
    const failedDraft = serverDraft
    const failedFiles = [...serverFiles]
    vi.mocked(recoverReimbursementDraft).mockImplementationOnce(async () => {
      serverDraft = makeDraft({ id: 'draft-recovered', revision: 2, recoverySourceDraftId: failedDraft.id })
      return serverDraft
    })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    const panel = wrapper.get('[data-testid="submission-status"]')
    expect(panel.get('h2').text()).toBe('审批未发起')
    expect(panel.text()).toContain('钉钉拒绝发起审批')
    expect(panel.find('.el-alert').exists()).toBe(false)
    expect(panel.get('.submission-feedback-actions').findAll('button').map((button) => button.text()))
      .toEqual(['保留内容重新处理'])
    expect(visibleButton(wrapper, '保留内容重新处理').classes()).toContain('el-button--primary')
    expect(visibleButton(wrapper, '新开一笔').classes()).not.toContain('el-button--primary')
    expect(wrapper.get('.hero-links').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(true)
    expect(wrapper.get('.hero-links').findAll('button, a').at(-1)!.text()).toBe('新开一笔')
    expect(wrapper.find('.new-reimbursement-action').exists()).toBe(false)
    expect(wrapper.find('[data-testid="approval-link"]').exists()).toBe(false)
    expect(wrapper.find('.editor-fieldset').attributes()).toHaveProperty('disabled')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('draft-recovered')
    expect(drafts.currentDraft?.input.items).toEqual(failedDraft.input.items)
    expect(drafts.files).toEqual(failedFiles)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(failedDraft.status).toBe('LOCKED')
    expect(failedDraft.input.items).toEqual(baseInput.items)
    expect(failedFiles).toEqual([activeFile])
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('freezes a replaced workspace without discarding local edits, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    await vi.advanceTimersByTimeAsync(650)
    const previous = serverDraft
    serverDraft = makeDraft({ id: 'current-in-other-tab', input: canonicalNoSubsidyInput() })
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('已新开另一笔报销，请返回当前报销'), {
      isAxiosError: true,
      response: { status: 409, data: { error: { code: 'REIMBURSEMENT_DRAFT_REPLACED', message: '已新开另一笔报销，请返回当前报销' } } },
    }))
    expense.items[0]!.description = '旧页尚未保存的说明'
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(previous.id)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    expect(expense.items[0]?.description).toBe('旧页尚未保存的说明')
    expect(wrapper.findAll('button').some((button) => button.text() === '重试保存')).toBe(false)
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    await visibleButton(wrapper, '打开当前报销').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    expect(drafts.currentDraft?.id).toBe(previous.id)
    expect(expense.items[0]?.description).toBe('旧页尚未保存的说明')
    confirm.mockResolvedValueOnce(undefined as never)
    await visibleButton(wrapper, '打开当前报销').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(serverDraft.id)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('protects genuine edits after another tab has locked the draft, mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, expense, drafts, router } = await mountView(undefined, false, mobile)
    vi.mocked(updateReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('报销已锁定'), {
      isAxiosError: true, response: { status: 409, data: { error: { code: 'REIMBURSEMENT_DRAFT_LOCKED', message: '报销已锁定' } } },
    }))
    expense.items[0]!.description = '锁定前本页未保存的说明'
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()
    expect(drafts.currentDraft?.status).toBe('LOCKED')
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    await router.push(mobile ? '/m/settings' : '/admin/settings')
    expect(router.currentRoute.value.path).toBe(mobile ? '/m' : '/')
    expect(expense.items[0]?.description).toBe('锁定前本页未保存的说明')
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValueOnce(submissionResult({
      status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0,
    }))
    await visibleButton(wrapper, '重新查找提交记录').trigger('click')
    await flushPromises()
    expect(expense.items[0]?.description).toBe('锁定前本页未保存的说明')
    expect(wrapper.get('[data-testid="autosave-error"]').text()).toContain('本页未保存的修改仍保留')
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    await visibleButton(wrapper, '新开一笔').trigger('click')
    await flushPromises()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(expense.items[0]?.description).toBe('锁定前本页未保存的说明')
    confirm.mockRejectedValueOnce('cancel')
    await visibleButton(wrapper, '核对最新报销内容').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(2)
    expect(expense.items[0]?.description).toBe('锁定前本页未保存的说明')
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['pending', 'expired'].flatMap((state) =>
    ['missingEditingState', 'legacyClockTimes'].flatMap((format) =>
      ['settings', 'history'].map((destination) => ({ mobile, state, format, destination }))),
  )))('opens $destination from saved readonly $state/$format without saving normalization, mobile=$mobile', async ({ mobile, state, format, destination }) => {
    const input = canonicalNoSubsidyInput()
    if (format === 'missingEditingState') delete input.editingState
    else {
      const trip = { tripType: 'business' as const, startDate: '2026-09-01', startTime: '08:30', endDate: '2026-09-02', endTime: '19:00' }
      input.trip = trip
      input.editingState = { includeSubsidy: true, trip, trips: [] }
    }
    serverDraft = makeDraft({ input, ...(state === 'pending'
      ? { recoverySourceDraftId: 'failed-source', recoveryPending: true } : { status: 'EXPIRED' as const }) })
    const submitted = makeDraft({ id: 'older-submission', status: 'LOCKED', revision: 5 })
    vi.mocked(listReimbursementSubmissionHistory).mockResolvedValue([
      { id: submitted.id, lockedAt: '2026-09-04T00:03:00Z', relatedApprovalCount: 1 },
    ])
    vi.mocked(getReimbursementDraft).mockImplementation(async (id) => id === submitted.id ? submitted : serverDraft)
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async (id) => ({
      draftId: id, revision: id === submitted.id ? submitted.revision : serverDraft.revision, items: [],
    }))
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      draftId: submitted.id, status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0,
    }))
    const { wrapper, router, drafts } = await mountView(undefined, false, mobile)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    const confirm = vi.spyOn(ElMessageBox, 'confirm')
    if (destination === 'settings') {
      await router.push(mobile ? '/m/settings' : '/admin/settings')
      expect(router.currentRoute.value.path).toBe(mobile ? '/m/settings' : '/admin/settings')
    } else {
      await visibleButton(wrapper, '报销记录').trigger('click')
      await flushPromises()
      await visibleButton(wrapper, '查看提交').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(submitted.id)
      await visibleButton(wrapper, '返回当前报销').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(serverDraft.id)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
    }
    expect(confirm).not.toHaveBeenCalled()
    expect(updateReimbursementDraft).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => (
    ['QUEUED', 'UPLOADING', 'FAILED_RETRYABLE', 'MANUAL_REVIEW', 'OA_CREATING', 'RECONCILING', 'VERIFYING'] as const
  ).map((status) => ({ mobile, status }))))(
    'restores unfinished submission on the current page without another reimbursement (%j)',
    async ({ mobile, status }) => {
      serverDraft = makeDraft({ status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z' })
      serverDraft.template.configVersion = 11
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({ status }))
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      expect(drafts.currentDraft?.id).toBe(serverDraft.id)
      const panel = wrapper.get('[data-testid="submission-status"]')
      expect(panel.get('.submission-feedback-actions').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(false)
      expect(wrapper.get('.hero-links').element.contains(visibleButton(wrapper, '新开一笔').element)).toBe(true)
      expect(wrapper.get('.hero-links').findAll('button, a').at(-1)!.text()).toBe('新开一笔')
      expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith(serverDraft.id, expect.anything())
      expect(listReimbursementSubmissionHistory).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('报销记录')
      expect(wrapper.text()).not.toContain('重新填写')
      expect(wrapper.text()).not.toContain('返回报销')
      expect(wrapper.text()).not.toContain('按新表单重新填写')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(createReimbursementDraft).not.toHaveBeenCalled()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )
  it.each([false, true])('ignores a late workspace initialization after department returns mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const staleWorkspace = deferred<{ items: ReimbursementDraft[]; total: number }>()
    const oldA = makeDraft({ id: 'old-a', workspaceRetired: true, input: canonicalNoSubsidyInput() })
    const currentA = makeDraft({ id: 'current-a', input: canonicalNoSubsidyInput() })
    const currentB = makeDraft({ id: 'current-b', department: { id: '200', name: '另一部门' }, input: canonicalNoSubsidyInput() })
    vi.mocked(listWorkspaceReimbursements).mockReturnValueOnce(staleWorkspace.promise)
      .mockResolvedValueOnce({ items: [currentB], total: 1 })
      .mockResolvedValueOnce({ items: [currentA], total: 1 })
    vi.mocked(getReimbursementDraft).mockImplementation(async (id) => id === oldA.id ? oldA : id === currentB.id ? currentB : currentA)
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async (id) => ({ draftId: id, revision: 1, items: [] }))
    const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
    const auth = useAuthStore()
    try {
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentB.department
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(currentB.id)
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentA.department
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(currentA.id)
      expense.items[0]!.description = '返回原部门后，用户正在填写的新内容'
      await nextTick()
      staleWorkspace.resolve({ items: [oldA], total: 1 })
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(currentA.id)
      expect(expense.items[0]?.description).toBe('返回原部门后，用户正在填写的新内容')
    } finally { staleWorkspace.resolve({ items: [oldA], total: 1 }); wrapper.unmount() }
  })

  it.each([false, true])('ignores a late autosave failure after a department roundtrip mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const save = deferred<ReimbursementDraft>()
    serverDraft.input = canonicalNoSubsidyInput()
    const currentA = structuredClone(serverDraft)
    const currentB = makeDraft({ id: 'b-draft', department: { id: '200', name: '另一部门' }, input: canonicalNoSubsidyInput() })
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    const auth = useAuthStore()
    try {
      await vi.advanceTimersByTimeAsync(650)
      vi.mocked(updateReimbursementDraft).mockReturnValueOnce(save.promise)
      expense.items[0]!.description = '旧会话里的输入'
      await vi.advanceTimersByTimeAsync(650)
      expect(updateReimbursementDraft).toHaveBeenCalledOnce()
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [currentB], total: 1 })
      serverDraft = currentB
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentB.department
      await flushPromises()
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [currentA], total: 1 })
      serverDraft = currentA
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentA.department
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(currentA.id)
      expect(expense.items[0]?.description).toBe(currentA.input.items[0]?.description)
      const newSave = deferred<ReimbursementDraft>()
      vi.mocked(updateReimbursementDraft).mockReturnValueOnce(newSave.promise)
      expense.items[0]!.description = '新会话正在保存的内容'
      await vi.advanceTimersByTimeAsync(650)
      expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('正在保存…')
      save.reject(new Error('旧会话的保存响应失败'))
      await flushPromises()
      expect(wrapper.find('[data-testid="autosave-error"]').exists()).toBe(false)
      expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('正在保存…')
      newSave.resolve(makeDraft({ ...currentA, revision: 2, input: {
        ...currentA.input, items: [{ ...currentA.input.items[0]!, description: '新会话正在保存的内容' }],
      } }))
      await flushPromises()
      expect(wrapper.get('[data-testid="autosave-status"]').text()).toBe('已保存')
    } finally { save.reject(new Error('end')); wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['confirmation', 'review'].map((stage) => ({ mobile, stage }))))(
    'does not continue an old OA $stage after a department roundtrip, mobile=$mobile', async ({ mobile, stage }) => {
      vi.useFakeTimers()
      const confirmation = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
      const review = deferred<ReimbursementDraft>()
      const confirm = vi.spyOn(ElMessageBox, 'confirm')
      if (stage === 'confirmation') confirm.mockReturnValueOnce(confirmation.promise)
      else confirm.mockResolvedValueOnce(undefined as never)
      const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()
      const currentA = structuredClone(serverDraft)
      if (stage === 'review') vi.mocked(markReimbursementDraftReviewReady).mockReturnValueOnce(review.promise)
      if (mobile) await wrapper.findAll('.mobile-step-nav button')[3]!.trigger('click')
      await visibleButton(wrapper, '提交 OA').trigger('click')
      await flushPromises()
      expect(confirm).toHaveBeenCalledOnce()
      if (stage === 'review') expect(markReimbursementDraftReviewReady).toHaveBeenCalledOnce()
      const currentB = makeDraft({ id: 'other-department', department: { id: '200', name: '另一部门' }, input: canonicalNoSubsidyInput() })
      const auth = useAuthStore()
      for (const current of [currentB, currentA]) {
        serverDraft = current
        vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [current], total: 1 })
        drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
        auth.session!.selectedDepartment = current.department
        await flushPromises()
        expect(drafts.currentDraft?.id).toBe(current.id)
      }
      confirmation.resolve(undefined as never)
      review.resolve({ ...currentA, status: 'REVIEW_READY', revision: currentA.revision + 1 })
      await flushPromises()
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      if (stage === 'confirmation') expect(markReimbursementDraftReviewReady).not.toHaveBeenCalled()
      expect(drafts.currentDraft?.id).toBe(currentA.id)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      wrapper.unmount()
    },
  )

  it.each([false, true])('offers the current workspace when failed recovery discovers a replacement, mobile=%s', async (mobile) => {
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5 })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'FAILED_FINAL', statusVersion: 8, pollAfterMs: 0,
    }))
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    const previous = serverDraft
    serverDraft = makeDraft({ id: 'another-current', input: canonicalNoSubsidyInput() })
    vi.mocked(recoverReimbursementDraft).mockRejectedValueOnce(Object.assign(new Error('已放弃这笔报销，请返回当前报销'), {
      isAxiosError: true, response: { status: 409, data: { error: { code: 'REIMBURSEMENT_DRAFT_REPLACED', message: '已放弃这笔报销，请返回当前报销' } } },
    }))
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(previous.id)
    expect(wrapper.findAll('button').some((button) => button.text() === '打开当前报销')).toBe(true)
    expect(wrapper.findAll('button').some((button) => button.text() === '保留内容重新处理')).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('never attaches an old queued approval to a replacement workspace mobile=%s', async (mobile) => {
    vi.useFakeTimers()
    const save = deferred<ReimbursementDraft>()
    serverDraft.input = canonicalNoSubsidyInput()
    const currentA = makeDraft({ id: 'new-current-a', input: canonicalNoSubsidyInput() })
    const currentB = makeDraft({ id: 'b-draft', department: { id: '200', name: '另一部门' }, input: canonicalNoSubsidyInput() })
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    const auth = useAuthStore()
    try {
      await vi.advanceTimersByTimeAsync(650)
      vi.mocked(updateReimbursementDraft).mockReturnValueOnce(save.promise)
      expense.items[0]!.description = '旧会话里的输入'
      await vi.advanceTimersByTimeAsync(650)
      expect(updateReimbursementDraft).toHaveBeenCalledOnce()
      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()
      expect(replaceReimbursementRelatedApprovals).not.toHaveBeenCalled()
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [currentB], total: 1 })
      serverDraft = currentB
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentB.department
      await flushPromises()
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [currentA], total: 1 })
      serverDraft = currentA
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      auth.session!.selectedDepartment = currentA.department
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(currentA.id)
      expect(drafts.currentDraft?.relatedApprovals).toEqual([])
      save.reject(new Error('旧会话的保存响应失败'))
      await flushPromises()
      expect(replaceReimbursementRelatedApprovals).not.toHaveBeenCalled()
      expect(drafts.currentDraft?.relatedApprovals).toEqual([])
    } finally { save.reject(new Error('end')); wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['cancel', 'restore'].map((choice) => ({ mobile, choice }))))('protects live edits before recovery ($choice), mobile=$mobile', async ({ mobile, choice }) => {
    vi.useFakeTimers()
    vi.mocked(recoverReimbursementDraft).mockReset()
    serverDraft = makeDraft({ input: canonicalNoSubsidyInput() })
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(replaceReimbursementRelatedApprovals).mockReturnValueOnce(pending.promise)
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    const confirm = vi.spyOn(ElMessageBox, 'confirm')
    if (choice === 'cancel') confirm.mockRejectedValueOnce('cancel')
    else confirm.mockResolvedValueOnce(undefined as never)
    try {
      await wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()
      expect(replaceReimbursementRelatedApprovals).toHaveBeenCalledOnce()
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expense.items[0]!.description = '用户在原页尚未保存的真实说明'
      await nextTick()
      serverDraft = makeDraft({
        status: 'LOCKED', revision: 5, lockedAt: '2026-09-04T00:02:00Z',
        input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: true },
        relatedApprovals: [linkedApproval], relatedApprovalCount: 1,
      })
      pending.reject(new Error('关联审批保存响应丢失'))
      await flushPromises()
      expect(drafts.currentDraft?.status).toBe('LOCKED')
      expect(drafts.currentDraft?.revision).toBe(5)
      expect(expense.items[0]?.description).toBe('用户在原页尚未保存的真实说明')
      const beforeRecoveryUnload = new Event('beforeunload', { cancelable: true })
      window.dispatchEvent(beforeRecoveryUnload)
      expect(beforeRecoveryUnload.defaultPrevented).toBe(true)
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValueOnce(submissionResult({
        status: 'FAILED_FINAL', statusVersion: 8,
        error: { code: 'OA_CREATE_REJECTED', message: '钉钉明确拒绝创建' }, pollAfterMs: 0,
      }))
      await visibleButton(wrapper, '重新查找提交记录').trigger('click')
      await flushPromises()
      expect(expense.items[0]?.description).toBe('用户在原页尚未保存的真实说明')
      vi.mocked(recoverReimbursementDraft).mockImplementationOnce(async (_id, revision) => {
        expect(revision).toBe(serverDraft.revision)
        serverDraft = makeDraft({
          id: 'recovery-target', recoverySourceDraftId: 'draft-1', revision: 2,
          input: { ...canonicalNoSubsidyInput(), accountingSourceVerified: false },
          relatedApprovals: [linkedApproval], relatedApprovalCount: 1,
        })
        return serverDraft
      })
      await visibleButton(wrapper, '保留内容重新处理').trigger('click')
      await flushPromises()
      expect(confirm).toHaveBeenCalledOnce()
      expect(confirm.mock.calls[0]?.[0]).toContain('本页未保存的修改')
      if (choice === 'cancel') {
        expect(recoverReimbursementDraft).not.toHaveBeenCalled()
        expect(drafts.currentDraft?.id).toBe('draft-1')
        expect(expense.items[0]?.description).toBe('用户在原页尚未保存的真实说明')
      } else {
        expect(recoverReimbursementDraft).toHaveBeenCalledOnce()
        expect(drafts.currentDraft?.id).toBe('recovery-target')
        expect(expense.items[0]?.description).toBe(baseInput.items[0]?.description)
      }
    } finally { wrapper.unmount() }
  })
  it.each([false, true])('discards an old recovery confirmation after the session changes, mobile=%s', async (mobile) => {
    vi.mocked(recoverReimbursementDraft).mockReset()
    serverDraft = makeDraft({ status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
      status: 'FAILED_FINAL', statusVersion: 8, pollAfterMs: 0,
    }))
    const { wrapper, expense, drafts } = await mountView(undefined, false, mobile)
    expense.items[0]!.description = '原会话尚未保存的说明'
    await nextTick()
    const confirmation = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockReturnValueOnce(confirmation.promise)
    await visibleButton(wrapper, '保留内容重新处理').trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledOnce()
    serverDraft = makeDraft({ id: 'new-session-current', input: canonicalNoSubsidyInput() })
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [serverDraft], total: 1 })
    drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
    useAuthStore().session!.user.userId = 'new-session-user'
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe(serverDraft.id)
    confirmation.resolve(undefined as never)
    await flushPromises()
    expect(recoverReimbursementDraft).not.toHaveBeenCalled()
    expect(drafts.currentDraft?.id).toBe(serverDraft.id)
    expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['success', 'lost-response'].map((response) => ({ mobile, response }))))(
    'recovers the latest failed reimbursement after an earlier recovery ($response), mobile=$mobile', async ({ mobile, response }) => {
      vi.mocked(recoverReimbursementDraft).mockReset()
      vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [serverDraft], total: 1 }))
      const original = makeDraft({ id: 'failed-a', status: 'LOCKED', revision: 5 })
      serverDraft = makeDraft({ id: 'failed-b', status: 'LOCKED', revision: 9, recoverySourceDraftId: original.id,
        input: { ...canonicalNoSubsidyInput(), items: [{ ...baseInput.items[0]!, description: '第二次提交前保存的说明' }] } })
      const failedAgain = serverDraft
      vi.mocked(getReimbursementDraft).mockImplementation(async (id) => id === original.id ? original : serverDraft)
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
        draftId: failedAgain.id, status: 'FAILED_FINAL', statusVersion: 8, pollAfterMs: 0,
      }))
      vi.mocked(recoverReimbursementDraft).mockImplementation(async (sourceId, revision) => {
        if (sourceId === failedAgain.id) {
          expect(revision).toBe(failedAgain.revision)
          serverDraft = makeDraft({ id: 'recovered-c', recoverySourceDraftId: failedAgain.id, input: failedAgain.input })
          if (response === 'lost-response') throw new Error('恢复响应丢失')
        }
        return serverDraft
      })
      const { wrapper, drafts, expense } = await mountView(undefined, false, mobile)
      await visibleButton(wrapper, '保留内容重新处理').trigger('click')
      await flushPromises()
      expect(vi.mocked(recoverReimbursementDraft).mock.calls[0]?.[0]).toBe(failedAgain.id)
      expect(drafts.currentDraft?.id).toBe('recovered-c')
      expect(drafts.currentDraft?.status).toBe('DRAFT')
      expect(expense.items[0]?.description).toBe('第二次提交前保存的说明')
      expect(wrapper.text()).not.toContain('恢复响应丢失')
      expect(submitOaReimbursement).not.toHaveBeenCalled()
      wrapper.unmount()
    },
  )


  it.each([false, true])('rechecks the workspace instead of reusing cached detail after returning from settings, mobile=%s', async (mobile) => {
    serverDraft.input = canonicalNoSubsidyInput()
    const { wrapper, drafts, router } = await mountView(undefined, false, mobile)
    try {
      const original = structuredClone(serverDraft)
      await router.push(mobile ? '/m/settings' : '/admin/settings')
      await flushPromises()
      serverDraft = { ...original, workspaceRetired: true }
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [original], total: 1 })
      await router.push(mobile ? '/m' : '/')
      await flushPromises()
      expect(wrapper.text()).toContain('报销内容加载失败')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      serverDraft = makeDraft({ id: 'new-current-after-settings', input: canonicalNoSubsidyInput() })
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [serverDraft], total: 1 })
      await visibleButton(wrapper, '重新加载').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(serverDraft.id)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(createReimbursementDraft).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('does not restore a workspace retired during initialization, mobile=%s', async (mobile) => {
    const retired = makeDraft({ id: 'retired-during-read', workspaceRetired: true, input: canonicalNoSubsidyInput() })
    serverDraft = retired
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [{ ...retired, workspaceRetired: false }], total: 1 })
    const { wrapper, drafts } = await mountView(undefined, false, mobile)
    try {
      expect(drafts.currentDraft).toBeNull()
      expect(wrapper.text()).toContain('报销内容加载失败')
      serverDraft = makeDraft({ id: 'live-after-retry', input: canonicalNoSubsidyInput() })
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [serverDraft], total: 1 })
      await visibleButton(wrapper, '重新加载').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('live-after-retry')
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(createReimbursementDraft).not.toHaveBeenCalled()
    } finally { wrapper.unmount() }
  })

  it.each([false, true].flatMap((mobile) => ['replaced', 'empty', 'network', 'empty-retired'].map((state) => ({ mobile, state }))))(
    'returns to the authoritative current reimbursement $state mobile=$mobile', async ({ mobile, state }) => {
      serverDraft.input = canonicalNoSubsidyInput()
      const origin = serverDraft
      const historical = makeDraft({ id: 'historical-success', status: 'LOCKED', revision: 5, input: canonicalNoSubsidyInput() })
      const replacement = makeDraft({ id: 'actual-current-b', input: { ...canonicalNoSubsidyInput(), items: [] } })
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [origin], total: 1 })
      vi.mocked(listReimbursementSubmissionHistory).mockResolvedValue([
        { id: historical.id, lockedAt: '2026-09-04T00:03:00Z', relatedApprovalCount: 1 },
      ])
      vi.mocked(getReimbursementDraft).mockImplementation(async (id) => {
        if (id === historical.id) return historical
        if (id === origin.id) {
          if (state === 'empty' && origin.workspaceRetired) throw Object.assign(new Error('已清理'), {
            response: { data: { error: { code: 'REIMBURSEMENT_DRAFT_NOT_FOUND' } } },
          })
          return origin
        }
        return serverDraft
      })
      vi.mocked(listReimbursementDraftFiles).mockImplementation(async (id) => ({
        draftId: id, revision: id === historical.id ? historical.revision : id === origin.id ? origin.revision : serverDraft.revision,
        items: id === origin.id ? [activeFile] : [],
      }))
      vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(submissionResult({
        draftId: historical.id, status: 'SUBMITTED', statusVersion: 8, pollAfterMs: 0,
      }))
      const { wrapper, drafts } = await mountView(undefined, false, mobile)
      await visibleButton(wrapper, '报销记录').trigger('click')
      await flushPromises()
      await visibleButton(wrapper, '查看提交').trigger('click')
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe(historical.id)
      const workspaceReads = vi.mocked(listWorkspaceReimbursements).mock.calls.length
      origin.workspaceRetired = true
      if (state === 'replaced') {
        serverDraft = replacement
        vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [replacement], total: 1 })
      } else if (state === 'empty' || state === 'empty-retired') {
        vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
        if (state === 'empty-retired') {
          vi.mocked(createReimbursementDraft).mockImplementationOnce(async (input) => {
            const prepared = makeDraft({ id: 'prepared-b', input })
            serverDraft = { ...prepared, workspaceRetired: true }
            return prepared
          })
        }
      } else {
        vi.mocked(listWorkspaceReimbursements).mockRejectedValue(new Error('工作区网络读取失败'))
      }
      await visibleButton(wrapper, '返回当前报销').trigger('click')
      await flushPromises()
      const observed = {
        id: drafts.currentDraft?.id,
        workspaceReads: vi.mocked(listWorkspaceReimbursements).mock.calls.length - workspaceReads,
        creates: vi.mocked(createReimbursementDraft).mock.calls.length,
        error: wrapper.text().includes('工作区网络读取失败') || wrapper.text().includes('当前报销读取失败'),
      }
      if (state === 'empty-retired') {
        expect(drafts.currentDraft?.id).toBe(historical.id)
        expect(wrapper.text()).toContain('当前报销已变化')
        serverDraft = replacement
        vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [replacement], total: 1 })
        await visibleButton(wrapper, '返回当前报销').trigger('click')
        await flushPromises()
        expect(drafts.currentDraft?.id).toBe(replacement.id)
        expect(createReimbursementDraft).toHaveBeenCalledOnce()
      }
      wrapper.unmount()
      expect(observed).toMatchObject(state === 'replaced'
        ? { id: replacement.id, workspaceReads: 1, creates: 0 }
        : state === 'empty' ? { id: 'draft-created', workspaceReads: 1, creates: 1 }
          : { id: historical.id, workspaceReads: 1, creates: state === 'empty-retired' ? 1 : 0 })
      expect(submitOaReimbursement).not.toHaveBeenCalled()
    },
  )


  it('cancels a desktop preview waiting for a save when the session changes', async () => {
    vi.useFakeTimers()
    const { wrapper, drafts, expense } = await mountView(undefined, false, false, true)
    const auth = useAuthStore()
    const pendingSave = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValueOnce(pendingSave.promise)
    const downloaded: string[] = []
    const download = vi.spyOn(drafts, 'downloadExcelPreview').mockImplementation(async () => {
      downloaded.push(drafts.currentDraft!.id)
    })
    expense.items[0]!.amount = '45.00'
    await nextTick()
    const preview = wrapper.findAll('button').find((candidate) => candidate.text().trim() === '预览 Excel')!
    expect(preview.attributes('disabled')).toBeUndefined()
    await preview.trigger('click')
    await flushPromises()
    expect(updateReimbursementDraft).toHaveBeenCalledOnce()
    expect(download).not.toHaveBeenCalled()
    const oldInput = structuredClone(vi.mocked(updateReimbursementDraft).mock.calls[0]![2])
    drafts.reset()
    expense.reset()
    useReimbursementSubmissionStore().reset()
    serverDraft = makeDraft({ id: 'replacement-user-draft', input: canonicalNoSubsidyInput() })
    auth.session = { ...auth.session!, user: { userId: 'replacement-user', name: 'Synthetic B' } }
    await nextTick()
    await flushPromises()
    expect(drafts.currentDraft?.id).toBe('replacement-user-draft')
    pendingSave.resolve(makeDraft({ revision: 2, input: oldInput }))
    await flushPromises()
    expect(downloaded).toEqual([])
    wrapper.unmount()
  })

  it.each([false, true].flatMap((mobile) => ['saving', 'resolving'].map((stage) => ({ mobile, stage }))))(
    'cancels old approval department work after an identity switch ($stage), mobile=$mobile', async ({ mobile, stage }) => {
      vi.useFakeTimers()
      serverDraft.input = canonicalNoSubsidyInput()
      const pendingSave = deferred<ReimbursementDraft>()
      const pendingDepartment = deferred<{ selectedDepartment: { id: string; name: string }; selectionRequired: boolean; departments: { id: string; name: string }[] }>()
      const { wrapper, drafts, expense } = await mountView((auth) => {
        auth.session!.departments.push({ id: '200', name: '另一部门' })
      }, false, mobile)
      if (stage === 'saving') {
        vi.mocked(updateReimbursementDraft).mockReturnValueOnce(pendingSave.promise)
        expense.items[0]!.description = '旧部门尚未保存的输入'
      } else vi.mocked(selectDepartmentFromTravelApproval).mockReturnValueOnce(pendingDepartment.promise)
      wrapper.findComponent(TravelApprovalSelectorStub).vm.$emit('update:modelValue', [selection])
      await flushPromises()
      const oldInput = vi.mocked(updateReimbursementDraft).mock.calls.at(-1)?.[2]
      expect(selectDepartmentFromTravelApproval).toHaveBeenCalledTimes(stage === 'resolving' ? 1 : 0)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(true)
      expect(visibleButton(wrapper, '新开一笔').attributes()).toHaveProperty('disabled')
      serverDraft = makeDraft({ id: 'new-identity-current', input: canonicalNoSubsidyInput() })
      vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [serverDraft], total: 1 })
      drafts.reset(); expense.reset(); useReimbursementSubmissionStore().reset()
      const auth = useAuthStore()
      auth.session!.user.userId = 'replacement-user'
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('new-identity-current')
      if (stage === 'saving') pendingSave.resolve(makeDraft({ revision: 2, input: oldInput! }))
      else pendingDepartment.resolve({ selectedDepartment: { id: '200', name: '另一部门' }, selectionRequired: false, departments: [{ id: '200', name: '另一部门' }] })
      await flushPromises()
      expect(drafts.currentDraft?.id).toBe('new-identity-current')
      expect(auth.session?.selectedDepartment?.id).toBe('100')
      expect(replaceReimbursementRelatedApprovals).not.toHaveBeenCalled()
      expect(wrapper.findComponent(TravelApprovalSelectorStub).props('busy')).toBe(false)
      expect(wrapper.findComponent(ExpenseItemsCardStub).props('readonly')).toBe(false)
      expect(wrapper.text()).not.toContain('登录或部门状态已变化')
      wrapper.unmount()
    },
  )

})

import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { h, ref } from 'vue'

import {
  deleteReimbursementDraftFile,
  clearReimbursementDraftFiles,
  getReimbursementDraft,
  getReimbursementFileContent,
  listReimbursementDraftFiles,
  recognizeReimbursementDraftFile,
  updateReimbursementDraft,
  updateReimbursementDraftFile,
  uploadReimbursementDraftFile,
} from '@/api/reimbursements'
import { logout as logoutRequest } from '@/api/auth'
import { useExpenseStore } from '@/stores/expense'
import { useAuthStore } from '@/stores/auth'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { OcrReceiptCandidate, ItineraryOcrResult } from '@/types/receipts'
import { receiptOcrResult } from '@/types/reimbursements'
import type {
  ReimbursementDraft,
  ReimbursementDraftFile,
} from '@/types/reimbursements'
import ExpenseItemsCard from './ExpenseItemsCard.vue'

vi.mock('@/api/auth', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/auth')>()
  return { ...actual, logout: vi.fn() }
})

vi.mock('@/api/reimbursements', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/reimbursements')>()
  return {
    ...actual,
    deleteReimbursementDraftFile: vi.fn(),
    clearReimbursementDraftFiles: vi.fn(),
    getReimbursementDraft: vi.fn(),
    getReimbursementFileContent: vi.fn(),
    listReimbursementDraftFiles: vi.fn(),
    recognizeReimbursementDraftFile: vi.fn(),
    updateReimbursementDraft: vi.fn(),
    updateReimbursementDraftFile: vi.fn(),
    uploadReimbursementDraftFile: vi.fn(),
  }
})

function draft(revision = 1, id = 'draft-1'): ReimbursementDraft {
  return {
    id,
    status: 'DRAFT',
    revision,
    department: { id: '100', name: '测试部门' },
    templateConfigVersion: 3,
    relatedApprovalCount: 0,
    expiresAt: '2026-10-04T00:00:00Z',
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:01:00Z',
    lockedAt: null,
    template: {
      processCode: 'PROC-REIMBURSEMENT',
      configVersion: 3,
      schemaFingerprint: 'a'.repeat(64),
    },
    input: {
      ocrDispositionVersion: 1,
      companyValue: '北京',
      budgetCodeValue: '26007',
      project: { mode: 'manual', text: '测试项目' },
      trip: null,
      items: [],
      dismissedOcrFileIds: [],
    },
    totals: {
      expenseTotal: '0.00',
      subsidyTotal: '0.00',
      totalAmount: '0.00',
      receiptCount: 0,
      uppercaseAmount: '零元整',
      subsidy: null,
    },
    relatedApprovals: [],
    relatedApprovalSummary: null,
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

function serverFile(
  id: string,
  name: string,
  role: ReimbursementDraftFile['role'],
  overrides: Partial<ReimbursementDraftFile> = {},
): ReimbursementDraftFile {
  return {
    id,
    name,
    role,
    attachmentKind: 'other',
    sortOrder: Number(id.replace(/\D/g, '')) || 0,
    status: 'ACTIVE',
    mediaType: name.endsWith('.pdf') ? 'application/pdf' : 'image/jpeg',
    sizeBytes: 7,
    ocrStatus: 'NOT_REQUESTED',
    ocrResult: null,
    ...overrides,
  }
}

function recognizedFile(
  id: string,
  name: string,
  amount = '454.00',
): ReimbursementDraftFile & { ocrResult: OcrReceiptCandidate } {
  return { ...serverFile(id, name, 'EXPENSE_SOURCE'),
    ocrStatus: 'COMPLETE',
    ocrResult: {
      fileId: id,
      type: 'train',
      categoryId: 'rail_fare',
      categoryName: '火车票',
      date: '2026-09-01',
      description: `${name} 的行程`,
      amount,
      receiptCount: 1,
      source: 'ocr',
      confidence: '0.93',
      warnings: [],
      status: 'recognized',
      error: null,
    },
  }
}

function selectFiles(wrapper: ReturnType<typeof mount>, testId: string, files: File[]) {
  const input = wrapper.get<HTMLInputElement>(`[data-testid="${testId}"]`)
  Object.defineProperty(input.element, 'files', { configurable: true, value: files })
  return input.trigger('change')
}

function recognizedItinerary(id = 'proof-1'): ReimbursementDraftFile {
  const ocrResult: ItineraryOcrResult = {
    fileId: id, version: 1, kind: 'itinerary', status: 'recognized', source: 'pdf_text',
    pageCount: 1, processedPageCount: 1, complete: true, warnings: [], error: null,
    summary: { currency: 'CNY', amount: '60.00', startDate: '2026-09-01', endDate: '2026-09-01', invoiceNumbers: ['INV-001'], orderNumbers: [] },
    trips: [{ page: 1, row: 1, date: '2026-09-01', amount: '60.00', origin: '合肥机场', destination: '滨湖酒店', invoiceNumbers: [], orderNumbers: [] }],
  }
  return serverFile(id, '行程.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'itinerary', ocrStatus: 'COMPLETE', ocrResult })
}

function taxiInvoice(): ReimbursementDraftFile & { ocrResult: OcrReceiptCandidate } {
  const file = recognizedFile('invoice-1', '打车发票.pdf', '60.00')
  Object.assign(file.ocrResult, { categoryId: 'local_transport', type: 'ride_hailing', transportType: 'ride_hailing', invoiceNumbers: ['INV-001'], requiresItinerary: true })
  return file
}

describe('ExpenseItemsCard durable files', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.clearAllMocks()
    vi.mocked(logoutRequest).mockResolvedValue()
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
  })

  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('shows unreadable itinerary rows as needing recognition instead of zero trips', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const file = recognizedItinerary()
    Object.assign(file.ocrResult!, { trips: [], complete: false, warnings: ['ITINERARY_ROWS_INCOMPLETE'] })
    drafts.files = [file]
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain('行程明细未识别')
    expect(wrapper.text()).not.toContain('0 次行程')
    wrapper.unmount()
  })

  it('keeps submitted invoice and itinerary metadata visible after file bodies are purged', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    const source = taxiInvoice()
    const itinerary = recognizedItinerary()
    source.status = 'PURGED'
    itinerary.status = 'PURGED'
    drafts.currentDraft = {
      ...draft(),
      status: 'LOCKED',
      lockedAt: '2026-09-08T08:00:00Z',
      input: {
        ...draft().input,
        items: [{
          sourceFileId: source.id,
          category: 'local_transport',
          date: '2026-09-01',
          displayDate: '2026-09-01',
          description: '合肥机场至滨湖酒店',
          amount: '60.00',
          receiptCount: 1,
          requiresItinerary: true,
          transportType: 'ride_hailing',
          itineraryFileIds: [itinerary.id],
        }],
      },
    }
    drafts.files = [source, itinerary]
    expense.hydrateFromDraft(drafts.currentDraft, drafts.files)

    const wrapper = mount(ExpenseItemsCard, {
      props: { readonly: true },
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const row = wrapper.find('.el-table__row')
    expect(row.text()).toContain('打车发票.pdf')
    expect(row.text()).toContain('行程.pdf')
    expect(row.text()).not.toContain('待补行程单')
    expect(row.text()).not.toContain('已随 OA 提交')
    wrapper.unmount()
  })

  it('warns about zero amounts on desktop and mobile and clears the hint after correction', async () => {
    const expense = useExpenseStore()
    expense.items = [{ id: 'zero', source: 'manual', category: 'local_transport',
      date: '2026-09-01', displayDate: '2026-09-01', description: '出租车车费',
      amount: '0.00', receiptCount: 1 }]
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    for (const amount of ['0.00', '0', '0.0']) {
      expense.items[0]!.amount = amount
      await flushPromises()
      expect(wrapper.find('.el-table__row').text()).toContain('金额为 0，请核实原票据')
      expect(wrapper.find('.expense-mobile-card').text()).toContain('金额为 0，请核实原票据')
    }
    for (const amount of ['0.01', '25.00', '']) {
      expense.items[0]!.amount = amount
      await flushPromises()
      expect(wrapper.findAll('[data-testid="zero-amount-warning"]')).toHaveLength(0)
    }
    wrapper.unmount()
  })

  it('offers one mixed-material upload entry and keeps unknown material out of expense totals', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const pending = serverFile('unknown-1', '材料.pdf', 'ATTACHMENT_ONLY', {
      materialClassification: { status: 'pending', kind: 'unknown', reason: null, pageCount: 1 },
    })
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: pending })
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 3, file: {
      ...pending, materialClassification: { status: 'needs_confirmation', kind: 'unknown', reason: '用途不确定，请选择', pageCount: 1 },
    } })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    const header = wrapper.find('.receipt-header-actions')
    expect(header.text()).toContain('上传报销材料')
    expect(header.text()).not.toContain('添加行程单')
    expect(header.text()).not.toContain('添加付款凭证')
    await selectFiles(wrapper, 'durable-expense-input', [new File(['pdf'], '材料.pdf', { type: 'application/pdf' })])
    await flushPromises()
    expect(expense.items).toHaveLength(0)
    expect(wrapper.text()).toContain('确认用途')
    expect(wrapper.text()).toContain('用途不确定，请选择')
    expect(uploadReimbursementDraftFile).toHaveBeenCalledWith('draft-1', 1, expect.any(File), expect.objectContaining({ autoClassify: true, role: 'ATTACHMENT_ONLY' }))
    wrapper.unmount()
  })

  it('splits the mobile upload entry into the file manager and image picker', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const wrapper = mount(ExpenseItemsCard, {
      props: { mobile: true },
      global: { plugins: [pinia, ElementPlus] },
    })
    const fileInput = wrapper.get('[data-testid="durable-expense-input"]')
    const imageInput = wrapper.get('[data-testid="durable-image-input"]')
    const fileClick = vi.spyOn(fileInput.element as HTMLInputElement, 'click')
    const imageClick = vi.spyOn(imageInput.element as HTMLInputElement, 'click')

    expect(fileInput.attributes('accept')).toBeUndefined()
    expect(imageInput.attributes('accept')).not.toContain('.pdf')
    expect(imageInput.attributes('accept')).toContain('image/jpeg')
    await wrapper.get('[data-testid="mobile-file-upload-button"]').trigger('click')
    await wrapper.get('[data-testid="mobile-image-upload-button"]').trigger('click')

    expect(fileClick).toHaveBeenCalledOnce()
    expect(imageClick).toHaveBeenCalledOnce()
    expect(wrapper.find('.receipt-header-actions').text()).toContain('清空文件')
    expect(wrapper.find('.receipt-header-actions').text()).not.toContain('更多')
    wrapper.unmount()
  })

  it('lets desktop and mobile source rows correct a misclassified invoice, with explicit consequences and harmless cancellation', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '误归类材料.pdf')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    const desktop = wrapper.find('.el-table__row')
    const mobile = wrapper.find('.expense-mobile-card')
    expect(desktop.findAll('button').some((button) => button.text() === '修改用途')).toBe(true)
    await mobile.findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    await flushPromises()
    const dialog = wrapper.findAllComponents({ name: 'ElDialog' }).find((entry) => entry.props('title') === '修改材料用途')!
    dialog.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'payment_proof')
    await flushPromises()
    expect(dialog.text()).toContain('移除对应的费用明细，但会保留原文件')
    await dialog.findAll('button').find((button) => button.text() === '取消')!.trigger('click')
    expect(updateReimbursementDraftFile).not.toHaveBeenCalled()
    expect(expense.items).toHaveLength(1)
    expect(drafts.files).toEqual([source])
    await desktop.findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    dialog.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'payment_proof')
    vi.mocked(updateReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2,
      file: { ...source, role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof', ocrResult: null } })
    await flushPromises()
    await dialog.findAll('button').find((button) => button.text() === '保存用途')!.trigger('click')
    await flushPromises()
    expect(expense.items).toHaveLength(0)
    expect(drafts.files[0]).toMatchObject({ id: 'file-1', role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof' })
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('confirming an existing invoice purpose preserves employee edits without rerunning recognition', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '已人工核对.pdf')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    Object.assign(expense.items[0]!, { amount: '455.00', description: '人工修正的说明', itineraryAutoMatchDisabled: true })
    const expected = JSON.parse(JSON.stringify(expense.items[0]))
    vi.mocked(updateReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: source })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.find('.el-table__row').findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    await flushPromises()
    const dialog = wrapper.findAllComponents({ name: 'ElDialog' }).find((entry) => entry.props('title') === '修改材料用途')!
    await dialog.findAll('button').find((button) => button.text() === '保存用途')!.trigger('click')
    await flushPromises()
    expect(expense.items[0]).toMatchObject(expected)
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('changing an attachment to an invoice respects edits made while OCR is pending (%s)', async (editedDuringRecognition) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [serverFile('file-1', '需修正用途.pdf', 'ATTACHMENT_ONLY')]
    const classified = { ...drafts.files[0]!, role: 'EXPENSE_SOURCE' as const }
    const recognized = recognizedFile('file-1', '需修正用途.pdf')
    const pending = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(updateReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: classified })
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    const dialog = wrapper.findAllComponents({ name: 'ElDialog' }).find((entry) => entry.props('title') === '修改材料用途')!
    dialog.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'expense')
    await flushPromises()
    await dialog.findAll('button').find((button) => button.text() === '保存用途')!.trigger('click')
    await vi.waitFor(() => expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce())
    if (editedDuringRecognition) {
      expense.upsertDraftOcrItem(recognized)
      Object.assign(expense.items[0]!, { amount: '455.00', description: '员工刚才修正' })
    }
    pending.resolve({ draftId: 'draft-1', revision: 3, file: recognized })
    await flushPromises()
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]?.amount).toBe(editedDuringRecognition ? '455.00' : '454.00')
    if (editedDuringRecognition) expect(expense.items[0]?.description).toBe('员工刚才修正')
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('retries an unclassified material and inserts its recovered invoice without another upload', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [serverFile('file-1', '模糊材料.pdf', 'ATTACHMENT_ONLY', {
      materialClassification: { status: 'needs_confirmation', kind: 'unknown', reason: null, pageCount: 1 },
    })]
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: recognizedFile('file-1', '模糊材料.pdf') })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '重新识别')!.trigger('click')
    await flushPromises()
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]?.sourceFileId).toBe('file-1')
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('uploads a payment proof from the expense row and links only after upload succeeds', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'hotel', name: '住宿费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    expense.upsertManualItem({ category: 'hotel', description: '酒店住宿', date: '2026-09-01', displayDate: '2026-09-01', amount: '600.00', receiptCount: 1 })
    const proof = serverFile('payment-1', '付款截图.jpg', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' })
    const pending = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加付款凭证'))!.trigger('click')
    await selectFiles(wrapper, 'durable-payment-proof-input', [new File(['image'], proof.name, { type: 'image/jpeg' })])
    expect(expense.items[0]?.paymentProofFileIds ?? []).toEqual([])
    expect(wrapper.text()).toContain('待补付款凭证')
    pending.resolve({ draftId: 'draft-1', revision: 2, file: proof })
    await flushPromises()
    expect(expense.items[0]?.paymentProofFileIds).toEqual(['payment-1'])
    expect(wrapper.text()).toContain('已附凭证')
    expect(wrapper.text()).not.toContain('待补付款凭证')
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledWith('draft-1', 1, expect.any(File), expect.objectContaining({ role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof' }))
    await wrapper.findAll('button').find((button) => button.text() === '移除关联')!.trigger('click')
    expect(expense.items[0]?.paymentProofFileIds).toEqual([])
    expect(drafts.files.map((file) => file.id)).toContain('payment-1')
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('待补付款凭证')
    wrapper.unmount()
  })

  function paymentExpenseFixture(details: ReimbursementDraftFile['paymentDetails'] = {
    amount: '50.00', date: '2026-07-09', description: '制卡费', categoryId: 'hotel',
  }) {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'other', name: '其他费用', order: 1, manualSelectable: true }]
    vi.spyOn(expense, 'refreshCalculations').mockResolvedValue()
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const proof = serverFile('payment-card-fee', '银行付款凭证.jpg', 'ATTACHMENT_ONLY', {
      attachmentKind: 'payment_proof', paymentDetails: details,
      materialClassification: { status: 'classified', kind: 'payment_proof', reason: null, pageCount: 1 },
      ocrStatus: 'COMPLETE',
    })
    drafts.files = [proof]
    const readonly = ref(false)
    const wrapper = mount({ render: () => h(ExpenseItemsCard, { readonly: readonly.value }) },
      { global: { plugins: [pinia, ElementPlus] } })
    const open = async () => {
      await wrapper.get('[data-testid="record-payment-expense"]').trigger('click')
      await flushPromises()
      return wrapper.findAllComponents({ name: 'ElDialog' })
        .find((entry) => entry.props('title') === '根据付款凭证录入费用')!
    }
    return { wrapper, expense, drafts, proof, open, readonly }
  }

  it('records a below-500 payment as a manual expense only after category and amount confirmation, then restores its proof', async () => {
    const { wrapper, expense, drafts, proof, open } = paymentExpenseFixture()
    const dialog = await open()
    const form = (label: string) => dialog.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === label)!
    expect(form('费用类别').findComponent({ name: 'ElSelect' }).props('modelValue')).toBe('')
    expect(form('发生日期').findComponent({ name: 'ElDatePicker' }).props('modelValue')).toBe('2026-07-09')
    expect(form('说明').findComponent({ name: 'ElInput' }).props('modelValue')).toBe('制卡费')
    expect(form('金额（元）').findComponent({ name: 'ElInput' }).props('modelValue')).toBe('50.00')
    expect(form('付款凭证').findComponent({ name: 'ElSelect' }).props('modelValue')).toEqual([proof.id])
    expect(expense.items).toHaveLength(0)
    const confirm = dialog.findAll('button').find((button) => button.text() === '确认并录入费用')!
    const error = vi.spyOn(ElMessage, 'error')
    await confirm.trigger('click')
    expect(expense.items).toHaveLength(0)
    expect(error).toHaveBeenCalledWith('请选择费用类别，并核对人民币金额后确认录入')
    form('费用类别').findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'other')
    form('金额（元）').findComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '52.00')
    await confirm.trigger('click')
    await confirm.trigger('click')
    await flushPromises()
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]).toMatchObject({
      source: 'manual', category: 'other', date: '2026-07-09', amount: '52.00',
      description: '制卡费', receiptCount: 1, paymentProofFileIds: [proof.id],
    })
    expect(expense.items[0]!.sourceFileId).toBeUndefined()
    expect(wrapper.text()).toContain('已附凭证')
    expect(wrapper.find('[data-testid="record-payment-expense"]').exists()).toBe(false)
    const saved = { ...draft(2), input: { ...draft().input, items: expense.buildDraftExpenseItems() } }
    expect(saved.input.items[0]!.sourceFileId).toBeUndefined()
    expect(saved.input.items[0]!.paymentProofFileIds).toEqual([proof.id])
    expense.hydrateFromDraft(saved, drafts.files)
    await flushPromises()
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]).toMatchObject({ source: 'manual', amount: '52.00', paymentProofFileIds: [proof.id] })
    expect(wrapper.text()).toContain('已附凭证')
    expect(wrapper.find('[data-testid="record-payment-expense"]').exists()).toBe(false)
    expect(drafts.files[0]).toMatchObject({ role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof', ocrResult: null })
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('cancels payment-based entry without creating expenses or changing proof links', async () => {
    const { wrapper, expense, drafts, open } = paymentExpenseFixture()
    const before = JSON.stringify(drafts.files)
    const dialog = await open()
    await dialog.findAll('button').find((button) => button.text() === '取消')!.trigger('click')
    expect(expense.items).toHaveLength(0)
    expect(expense.refreshCalculations).not.toHaveBeenCalled()
    expect(JSON.stringify(drafts.files)).toBe(before)
    expect(wrapper.find('[data-testid="record-payment-expense"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('allows manual payment-based entry with no extracted details and never guesses the category or amount', async () => {
    const { wrapper, expense, open, proof } = paymentExpenseFixture(null)
    const dialog = await open()
    const form = (label: string) => dialog.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === label)!
    for (const label of ['说明', '金额（元）']) expect(form(label).findComponent({ name: 'ElInput' }).props('modelValue')).toBe('')
    expect(form('发生日期').findComponent({ name: 'ElDatePicker' }).props('modelValue')).toBe('')
    form('费用类别').findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'other')
    form('发生日期').findComponent({ name: 'ElDatePicker' }).vm.$emit('update:modelValue', '2026-07-09')
    form('说明').findComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '制卡费')
    form('金额（元）').findComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '50.00')
    await dialog.findAll('button').find((button) => button.text() === '确认并录入费用')!.trigger('click')
    expect(expense.items[0]).toMatchObject({ source: 'manual', amount: '50.00', paymentProofFileIds: [proof.id] })
    wrapper.unmount()
  })

  it.each(['deleted', 'reclassified', 'linked', 'running', 'unknown'] as const)('refuses payment-based entry if the proof becomes %s while editing', async (change) => {
    const { wrapper, expense, drafts, proof, open } = paymentExpenseFixture()
    const dialog = await open()
    dialog.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === '费用类别')!
      .findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'other')
    if (change === 'deleted') drafts.files = []
    else if (change === 'reclassified') drafts.files[0]!.attachmentKind = 'other'
    else if (change === 'running') drafts.files[0]!.ocrStatus = 'RUNNING'
    else if (change === 'unknown') drafts.files[0]!.materialClassification!.status = 'needs_confirmation'
    else expense.upsertManualItem({ category: 'other', description: '已录入的制卡费', date: '2026-07-09', displayDate: '2026-07-09', amount: '50.00', receiptCount: 1, paymentProofFileIds: [proof.id] })
    await dialog.findAll('button').find((button) => button.text() === '确认并录入费用')!.trigger('click')
    expect(expense.items).toHaveLength(change === 'linked' ? 1 : 0)
    expect(expense.refreshCalculations).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each(['readonly', 'locked', 'draft'] as const)('closes payment-based entry if its editing context changes to %s', async (change) => {
    const { wrapper, expense, drafts, open, readonly } = paymentExpenseFixture()
    const dialog = await open()
    const confirm = dialog.findAllComponents({ name: 'ElButton' }).find((button) => button.text() === '确认并录入费用')!
    if (change === 'readonly') readonly.value = true
    else if (change === 'locked') drafts.currentDraft!.status = 'LOCKED'
    else drafts.currentDraft = draft(1, 'another-draft')
    await flushPromises()
    confirm.vm.$emit('click')
    expect(dialog.props('modelValue')).toBe(false)
    expect(expense.items).toHaveLength(0)
    expect(expense.refreshCalculations).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('disables payment-based creation in readonly mode and rejects removal of the originating proof before confirmation', async () => {
    const { wrapper, expense, open, readonly } = paymentExpenseFixture()
    readonly.value = true
    await flushPromises()
    expect(wrapper.get<HTMLButtonElement>('[data-testid="record-payment-expense"]').element.disabled).toBe(true)
    readonly.value = false
    await flushPromises()
    const dialog = await open()
    const form = (label: string) => dialog.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === label)!
    form('费用类别').findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'other')
    form('付款凭证').findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', [])
    await dialog.findAll('button').find((button) => button.text() === '确认并录入费用')!.trigger('click')
    expect(expense.items).toHaveLength(0)
    wrapper.unmount()
  })

  it('uploads multiple hotel bills, reuses one for another stay and keeps manual expenses when unlinked', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'lodging', name: '住宿费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    for (const description of ['住宿一', '住宿二']) expense.upsertManualItem({ category: 'lodging', description, date: '2026-09-01', displayDate: '2026-09-01', amount: '100.00', receiptCount: 1 })
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(async (draftId, revision, file) => ({
      draftId, revision: revision + 1,
      file: serverFile(file.name, file.name, 'ATTACHMENT_ONLY', { attachmentKind: 'hotel_bill' }),
    }))
    vi.mocked(recognizeReimbursementDraftFile).mockImplementation(async (draftId, fileId, input) => ({
      draftId, revision: input.expectedRevision + 1,
      file: serverFile(fileId, fileId, 'ATTACHMENT_ONLY', { attachmentKind: 'hotel_bill', ocrStatus: 'FAILED', hotelBillDetails: { warnings: ['HOTEL_BILL_INCOMPLETE'] } }),
    }))
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加住宿明细'))!.trigger('click')
    await selectFiles(wrapper, 'durable-hotel-bill-input', [new File(['a'], 'hotel-a.pdf', { type: 'application/pdf' }), new File(['b'], 'hotel-b.pdf', { type: 'application/pdf' })])
    await flushPromises()
    expect(expense.items).toHaveLength(2)
    expect(expense.items[0]!.hotelBillFileIds).toEqual(['hotel-a.pdf', 'hotel-b.pdf'])
    expect(wrapper.text()).not.toContain('住宿信息识别不完整')
    await wrapper.findAll('button').find((button) => button.text() === '从已上传材料选择')!.trigger('click')
    const picker = wrapper.findAllComponents({ name: 'ElDialog' }).find((dialog) => dialog.props('title') === '选择已上传的住宿明细')!
    picker.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', ['hotel-a.pdf'])
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '确认关联')!.trigger('click')
    expect(expense.items[1]!.hotelBillFileIds).toEqual(['hotel-a.pdf'])
    expense.removeDraftFileAssociation('hotel-a.pdf')
    expect(expense.items.map((item) => item.amount)).toEqual(['100.00', '100.00'])
    expect(expense.items.map((item) => item.hotelBillFileIds)).toEqual([['hotel-b.pdf'], []])
    wrapper.unmount()
  })

  it('reuses a shared payment proof and changing the amount does not delete its link', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'hotel', name: '住宿费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [serverFile('payment-1', '合并付款.png', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' })]
    for (const description of ['住宿一', '住宿二']) expense.upsertManualItem({ category: 'hotel', description, date: '2026-09-01', displayDate: '2026-09-01', amount: '600.00', receiptCount: 1 })
    expense.items[1]!.paymentProofFileIds = ['payment-1']
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '从已上传材料选择')!.trigger('click')
    const picker = wrapper.findAllComponents({ name: 'ElDialog' }).find((dialog) => dialog.props('title') === '选择已上传的付款凭证')!
    picker.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', ['payment-1'])
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '确认关联')!.trigger('click')
    expect(expense.items.map((item) => item.paymentProofFileIds)).toEqual([['payment-1'], ['payment-1']])
    expense.items[0]!.amount = '100.00'
    await flushPromises()
    expect(expense.items[0]?.paymentProofFileIds).toEqual(['payment-1'])
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('retains an old payment proof if replacement upload fails and never deletes the file', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'hotel', name: '住宿费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [serverFile('payment-1', '原付款.png', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' })]
    expense.upsertManualItem({ category: 'hotel', description: '住宿', date: '2026-09-01', displayDate: '2026-09-01', amount: '600.00', receiptCount: 1 })
    expense.items[0]!.paymentProofFileIds = ['payment-1']
    vi.spyOn(drafts, 'uploadFile').mockRejectedValue(new Error('网络中断，请重试'))
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '更换')!.trigger('click')
    await selectFiles(wrapper, 'durable-payment-proof-input', [new File(['img'], '新付款.png', { type: 'image/png' })])
    await flushPromises()
    expect(expense.items[0]?.paymentProofFileIds).toEqual(['payment-1'])
    expect(wrapper.text()).toContain('网络中断，请重试')
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('cancels a row proof upload when the file picker returns to a different reimbursement', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'hotel', name: '住宿费', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    expense.upsertManualItem({ category: 'hotel', description: '住宿', date: '2026-09-01', displayDate: '2026-09-01', amount: '600.00', receiptCount: 1 })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().includes('添加付款凭证'))!.trigger('click')
    drafts.currentDraft = draft(1, 'draft-2')
    expense.reset()
    await flushPromises()
    await selectFiles(wrapper, 'durable-payment-proof-input', [new File(['img'], '付款.png', { type: 'image/png' })])
    await flushPromises()
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(drafts.files).toEqual([])
    wrapper.unmount()
  })

  it('shows ambiguous taxi evidence as a proposal until explicitly confirmed', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    Object.assign(invoice.ocrResult, { type: 'invoice', transportType: 'other', requiresItinerary: false, description: '合肥机场→滨湖酒店', invoiceNumbers: [] })
    const itinerary = recognizedItinerary()
    drafts.files = [invoice, itinerary]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(expense.items[0]?.itineraryFileIds).toEqual([])
    expect(wrapper.text()).toContain('疑似对应打车行程')
    await wrapper.findAll('button').find((button) => button.text() === '确认关联')!.trigger('click')
    expect(expense.items[0]).toMatchObject({ transportType: 'ride_hailing', requiresItinerary: true, itineraryFileIds: ['proof-1'] })
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('confirms an amount-only proposal with one click and preserves edits (%s)', async (edited) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    Object.assign(invoice.ocrResult, { type: 'invoice', categoryName: '市内交通', description: null,
      invoiceNumbers: [], warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'MANUAL_REVIEW_REQUIRED'] })
    const proof = recognizedItinerary()
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.startDate = result.summary.endDate = result.trips[0]!.date = '2026-08-13'
    drafts.files = [invoice, proof]
    expense.upsertDraftOcrItem(invoice)
    if (edited) Object.assign(expense.items[0]!, { date: '2026-08-12', displayDate: '2026-08-12', description: '手动核对过的路线' })
    const before = JSON.stringify(expense.items)
    const originalOcr = JSON.stringify(invoice.ocrResult)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(JSON.stringify(expense.items)).toBe(before)
    // The same recommendation is available in the desktop row and mobile card.
    expect(wrapper.findAll('.itinerary-suggestion')).toHaveLength(2)
    expect(wrapper.text()).toContain('仅金额相同，请核对是否为同一笔')
    expect(wrapper.text()).not.toContain('待补行程单 · 网约车费用')
    expect(wrapper.text().includes('确认后补全：乘车日期、路线说明')).toBe(!edited)
    await wrapper.findAll('button').find((button) => button.text() === '确认关联')!.trigger('click')
    await flushPromises()
    expect(expense.items[0]).toMatchObject({
      itineraryFileIds: ['proof-1'], itineraryAutoMatchDisabled: true, amount: '60.00',
      date: edited ? '2026-08-12' : '2026-08-13', displayDate: edited ? '2026-08-12' : '2026-08-13',
      description: edited ? '手动核对过的路线' : '合肥机场 → 滨湖酒店',
    })
    if (!edited) {
      expect(expense.items[0]!.warnings).not.toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
      expect(expense.items[0]!.warnings).not.toContain('MISSING_DESCRIPTION')
    }
    expect(JSON.stringify(invoice.ocrResult)).toBe(originalOcr)
    expect(wrapper.findAll('.itinerary-suggestion')).toHaveLength(0)
    const saved = { ...draft(), input: { ...draft().input, items: expense.buildDraftExpenseItems() } }
    expense.hydrateFromDraft(saved, drafts.files)
    expect(expense.items[0]!.itineraryFileIds).toEqual(['proof-1'])
    expect(expense.items[0]!.description).toBe(edited ? '手动核对过的路线' : '合肥机场 → 滨湖酒店')
    if (!edited) {
      expect(expense.items[0]!.warnings).not.toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
      expect(expense.items[0]!.warnings).not.toContain('MISSING_DESCRIPTION')
    }
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('rechecks a suggestion at click time and does not bind a removed proof or a locked expense', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    Object.assign(invoice.ocrResult, { type: 'invoice', description: null, invoiceNumbers: [] })
    drafts.files = [invoice, recognizedItinerary()]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    const suggestion = wrapper.findComponent({ name: 'ExpenseItinerarySuggestion' })
    expect(suggestion.exists()).toBe(true)
    drafts.currentDraft!.status = 'LOCKED'
    await flushPromises()
    suggestion.vm.$emit('confirm')
    expect(expense.items[0]!.itineraryFileIds).toEqual([])
    drafts.currentDraft!.status = 'DRAFT'
    drafts.files = [invoice]
    suggestion.vm.$emit('confirm')
    expect(expense.items[0]!.itineraryFileIds).toEqual([])
    wrapper.unmount()
  })

  it('shows itinerary amounts, routes, associations and defaults to one file without dropping existing multiple links', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    drafts.files = [invoice, recognizedItinerary(), recognizedItinerary('proof-2')]
    expense.upsertDraftOcrItem(invoice)
    expense.items[0]!.itineraryAutoMatchDisabled = true
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    await flushPromises()
    const field = wrapper.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === '对应行程单')!
    expect(field.findComponent({ name: 'ElSelect' }).props('multiple')).toBe(false)
    expect(field.findAllComponents({ name: 'ElOption' })[0]!.text()).toContain('合计 ¥60.00')
    expect(field.findAllComponents({ name: 'ElOption' })[0]!.text()).toContain('合肥机场 → 滨湖酒店')
    await field.findAll('button').find((button) => button.text().includes('补充行程单文件'))!.trigger('click')
    field.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', ['proof-1', 'proof-2'])
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    expect(expense.items[0]?.itineraryFileIds).toEqual(['proof-1', 'proof-2'])
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    expect(field.findComponent({ name: 'ElSelect' }).props('multiple')).toBe(true)
    wrapper.unmount()
  })

  it.each(['invoice_first', 'itinerary_first'])('matches itinerary OCR without creating an expense in %s order and persists through hydration', async (order) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    const proof = recognizedItinerary()
    if (order === 'invoice_first') { drafts.files = [invoice]; expense.upsertDraftOcrItem(invoice) }
    else drafts.files = [proof]
    const fileToUpload = order === 'invoice_first' ? proof : invoice
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: { ...fileToUpload, ocrResult: null, ocrStatus: 'NOT_REQUESTED' } })
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 3, file: fileToUpload })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, order === 'invoice_first' ? 'durable-itinerary-input' : 'durable-expense-input', [new File(['pdf'], fileToUpload.name, { type: 'application/pdf' })])
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledWith('draft-1', 1, expect.any(File), expect.objectContaining(order === 'invoice_first'
      ? { attachmentKind: 'itinerary', role: 'ATTACHMENT_ONLY' }
      : { attachmentKind: 'other', role: 'ATTACHMENT_ONLY', autoClassify: true }))
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]).toMatchObject({ sourceFileId: 'invoice-1', itineraryFileIds: ['proof-1'], receiptCount: 1 })
    const persisted = expense.buildDraftExpenseItems()
    expense.hydrateFromDraft({ ...draft(4), input: { ...draft().input, items: persisted } }, drafts.files)
    expect(expense.items[0]?.itineraryFileIds).toEqual(['proof-1'])
    expect(expense.buildDraftExpenseItems()).toEqual(persisted)
    wrapper.unmount()
  })

  it('uploads payment proofs without OCR or receipt-count changes', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    useReimbursementDraftStore().currentDraft = draft()
    const file = serverFile('payment-1', '转账.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' })
    vi.mocked(uploadReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-payment-proof-input', [new File(['pdf'], file.name, { type: 'application/pdf' })])
    await flushPromises()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(expense.items).toEqual([])
    expect(expense.buildDraftExpenseItems()).toEqual([])
    expect(wrapper.text()).toContain('付款凭证')
    wrapper.unmount()
  })

  it('lets the employee confirm an unclassified OCR city-transport invoice as taxi and match its itinerary on save', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    Object.assign(invoice.ocrResult, { transportType: 'other', requiresItinerary: false })
    drafts.files = [invoice, recognizedItinerary()]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(expense.items[0]?.itineraryFileIds).toEqual([])
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const transportField = wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === '市内交通类型')
    expect(transportField).toBeDefined()
    transportField!.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'taxi')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((field) => field.props('label') === '对应行程单')).toBe(true)
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]).toMatchObject({ transportType: 'taxi', requiresItinerary: false, itineraryFileIds: ['proof-1'], itineraryAutoMatchDisabled: false })
    wrapper.unmount()
  })

  it('keeps insufficient evidence manual and retries matching after the employee corrects amount, date and route', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    Object.assign(invoice.ocrResult, { transportType: 'other', requiresItinerary: false, invoiceNumbers: [], amount: '70.00', date: '2026-09-02', description: '交通费用' })
    drafts.files = [invoice, recognizedItinerary()]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const formItem = (label: string) => wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === label)!
    formItem('市内交通类型').findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'taxi')
    await flushPromises()
    expect(formItem('对应行程单').findAllComponents({ name: 'ElOption' }).map((option) => option.props('value'))).toEqual(['proof-1'])
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(expense.items[0]?.itineraryFileIds).toEqual([])
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    formItem('发生日期').findComponent({ name: 'ElDatePicker' }).vm.$emit('update:modelValue', '2026-09-01')
    formItem('金额（元）').findComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '60.00')
    formItem('说明').findComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '合肥机场至滨湖酒店')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]?.itineraryFileIds).toEqual(['proof-1'])
    wrapper.unmount()
  })

  it('does not let an OCR-confirmed ride-hailing invoice cancel its required itinerary', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    drafts.files = [invoice]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((field) => field.props('label') === '市内交通类型')).toBe(false)
    expect(wrapper.text()).toContain('网约车费用必须有对应行程单')
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]).toMatchObject({ transportType: 'ride_hailing', requiresItinerary: true })
    wrapper.unmount()
  })

  it('keeps a manually cleared itinerary after autosave and refresh until explicit automatic matching is requested', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const invoice = taxiInvoice()
    drafts.files = [invoice, recognizedItinerary()]
    expense.upsertDraftOcrItem(invoice)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(expense.items[0]?.itineraryFileIds).toEqual(['proof-1'])
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const itinerary = wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === '对应行程单')!
    itinerary.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', [])
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
    const input = { ...draft().input, items: expense.buildDraftExpenseItems() }
    expect(input.items[0]?.itineraryFileIds).toEqual([])
    expect(input.items[0]).toMatchObject({ itineraryAutoMatchDisabled: true })
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(updateReimbursementDraft).mockReturnValue(pending.promise)
    const saving = drafts.saveDraft(input)
    await vi.waitFor(() => expect(updateReimbursementDraft).toHaveBeenCalledOnce())
    pending.resolve({ ...draft(2), input })
    await saving
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]?.itineraryFileIds).toEqual([])
    wrapper.unmount()
    expense.hydrateFromDraft({ ...draft(2), input }, drafts.files)
    const restored = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]).toMatchObject({ itineraryFileIds: [], itineraryAutoMatchDisabled: true })
    await restored.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    await restored.findAll('button').find((button) => button.text() === '重新自动匹配')!.trigger('click')
    await flushPromises()
    expect(expense.buildDraftExpenseItems()[0]).toMatchObject({ itineraryFileIds: ['proof-1'], itineraryAutoMatchDisabled: false })
    restored.unmount()
  })

  it.each(['other', 'local_transport'])('allows unknown rail evidence but explains known non-rail evidence when category is corrected from %s', async (categoryId) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '待确认.pdf', '600.00')
    Object.assign(source.ocrResult, { categoryId, status: categoryId === 'other' ? 'failed' : 'recognized', railType: null })
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    expense.items[0]!.category = 'rail_fare'
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const railField = wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === '铁路票种')
    expect(Boolean(railField)).toBe(categoryId === 'other')
    if (railField) {
      railField.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'high_speed')
      await flushPromises()
      expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((field) => field.props('label') === '付款凭证')).toBe(false)
      await wrapper.findAll('button').find((button) => button.text() === '保存')!.trigger('click')
      expect(expense.items[0]?.railType).toBe('high_speed')
    } else {
      expect(wrapper.text()).toContain('修改类别不会获得高铁付款凭证豁免')
      expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((field) => field.props('label') === '付款凭证')).toBe(true)
    }
    wrapper.unmount()
  })

  it('discards a late purpose-change response after switching drafts without OCR or relinking', async () => {
    const pending = deferred<Awaited<ReturnType<typeof updateReimbursementDraftFile>>>()
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [serverFile('proof-1', '原材料.pdf', 'ATTACHMENT_ONLY')]
    vi.mocked(updateReimbursementDraftFile).mockReturnValue(pending.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    await flushPromises()
    const dialog = wrapper.findAllComponents({ name: 'ElDialog' }).find((entry) => entry.props('title') === '修改材料用途')!
    dialog.findComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'itinerary')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存用途')!.trigger('click')
    await vi.waitFor(() => expect(updateReimbursementDraftFile).toHaveBeenCalledOnce())
    drafts.currentDraft = draft(1, 'draft-2')
    drafts.files = []
    expense.reset()
    pending.resolve({ draftId: 'draft-1', revision: 2, file: recognizedItinerary() })
    await flushPromises()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(drafts.files).toEqual([])
    expect(expense.items).toEqual([])
    expect(dialog.props('modelValue')).toBe(false)
    wrapper.unmount()
  })

  it('changes an existing material purpose, clears incompatible proof links and recognizes without reupload', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = taxiInvoice()
    const proof = recognizedItinerary()
    drafts.files = [source, { ...proof, attachmentKind: 'payment_proof', ocrResult: null }]
    expense.upsertDraftOcrItem(source)
    expense.items[0]!.paymentProofFileIds = [proof.id]
    vi.mocked(updateReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 2, file: { ...proof, ocrResult: null } })
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({ draftId: 'draft-1', revision: 3, file: proof })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    // A reused proof is deliberately not deleted when unlinked, then its purpose can be changed.
    await wrapper.findAll('button').find((button) => button.text() === '移除关联')!.trigger('click')
    await wrapper.findAll('button').find((button) => button.text() === '修改用途')!.trigger('click')
    await flushPromises()
    const selector = wrapper.findAllComponents({ name: 'ElDialog' }).find((entry) => entry.props('title') === '修改材料用途')!.findComponent({ name: 'ElSelect' })
    selector.vm.$emit('update:modelValue', 'itinerary')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '保存用途')!.trigger('click')
    await flushPromises()
    expect(updateReimbursementDraftFile).toHaveBeenCalledWith('draft-1', proof.id, { role: 'ATTACHMENT_ONLY', attachmentKind: 'itinerary', expectedRevision: 1 }, expect.any(Object))
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(expense.items[0]).toMatchObject({ itineraryFileIds: [proof.id], paymentProofFileIds: [] })
    expect(expense.items).toHaveLength(1)
    wrapper.unmount()
  })

  it.each([
    { amount: '500.00', category: 'rail_fare', railType: 'unknown', expected: false },
    { amount: '500.01', category: 'rail_fare', railType: 'unknown', expected: true },
    { amount: '600.00', category: 'rail_fare', railType: 'high_speed', expected: false },
    { amount: '600.00', category: 'rail_fare', railType: 'emu', expected: true },
    { amount: '600.00', category: 'local_transport', railType: null, expected: true },
  ] as const)('shows payment candidates for the confirmed row amount and supported rail evidence %j', async ({ amount, category, railType, expected }) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: category, name: '费用类别', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '费用.pdf', amount)
    Object.assign(source.ocrResult, { categoryId: category, railType })
    drafts.files = [source, recognizedItinerary(), serverFile('payment-1', '付款.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' }), serverFile('other-1', '其他.pdf', 'ATTACHMENT_ONLY')]
    expense.upsertDraftOcrItem(source)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    const paymentField = wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === '付款凭证')
    expect(Boolean(paymentField)).toBe(expected)
    if (paymentField) expect(paymentField.findAllComponents({ name: 'ElOption' }).map((option) => option.props('value'))).toEqual(['payment-1'])
    const railField = wrapper.findAllComponents({ name: 'ElFormItem' }).find((field) => field.props('label') === '铁路票种')
    expect(Boolean(railField)).toBe(category === 'rail_fare')
    if (railField) expect(railField.findComponent({ name: 'ElSelect' }).props('disabled')).toBe(railType !== 'unknown')
    wrapper.unmount()
  })

  it('pipelines automatic classification while explicit other attachments upload without OCR', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(
      async (_draftId, revision, file, options) => ({
        draftId: 'draft-1',
        revision: revision + 1,
        file: serverFile(
          `file-${revision}`,
          file.name,
          options?.role ?? 'EXPENSE_SOURCE',
        ),
      }),
    )
    vi.mocked(recognizeReimbursementDraftFile).mockImplementation(
      async (_draftId, fileId, input) => {
        const uploaded = drafts.files.find((file) => file.id === fileId)!
        return {
          draftId: 'draft-1',
          revision: input.expectedRevision,
          file: recognizedFile(fileId, uploaded.name),
        }
      },
    )
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    await selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '发票一.pdf', { type: 'application/pdf' }),
      new File(['b'], '发票二.jpg', { type: 'image/jpeg' }),
    ])
    await flushPromises()
    await selectFiles(wrapper, 'durable-attachment-input', [
      new File(['c'], '行程单.pdf', { type: 'application/pdf' }),
    ])
    await flushPromises()

    expect(vi.mocked(uploadReimbursementDraftFile).mock.calls.map((call) => [
      call[1], call[2].name, call[3]?.role,
    ])).toEqual([
      [1, '发票一.pdf', 'ATTACHMENT_ONLY'],
      [2, '发票二.jpg', 'ATTACHMENT_ONLY'],
      [3, '行程单.pdf', 'ATTACHMENT_ONLY'],
    ])
    expect(vi.mocked(recognizeReimbursementDraftFile).mock.calls.map((call) => [
      call[1], call[2].expectedRevision,
    ])).toEqual([
      ['file-1', 2],
      ['file-2', 3],
    ])
    expect(expense.items.map((item) => item.id)).toEqual(['ocr-file-1', 'ocr-file-2'])
    expect(drafts.currentDraft?.revision).toBe(4)
    expect(wrapper.text()).toContain('行程单.pdf')
    expect(wrapper.text()).toContain('其他材料')
    expect(wrapper.text()).toContain('发票一.pdf 的行程')
    expect(wrapper.find('[aria-label="预览票据 发票一.pdf"]').exists()).toBe(true)
    expect(wrapper.findAll('.receipt-row').some((row) => row.text().includes('发票一.pdf'))).toBe(false)

    wrapper.unmount()
  })

  it.each([
    ['hotel', 'hotel', false], ['rail_fare', 'rail', false],
    ['local_transport', 'other', false], ['local_transport', 'taxi', true],
    ['local_transport', 'ride_hailing', true],
  ] as const)('shows itinerary editing only for taxi expenses (%s/%s)', async (category, transportType, visible) => {
    const expense = useExpenseStore()
    expense.categories = [{ id: category, name: category, order: 1, manualSelectable: true }]
    expense.items = [{
      id: 'manual', category, transportType, date: '2026-09-01', displayDate: '2026-09-01',
      description: '本次费用', amount: '10.00', receiptCount: 1, source: 'manual',
    }]
    useReimbursementDraftStore().currentDraft = draft()
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((item) =>
      String(item.props('label')).includes('对应行程单'),
    )).toBe(visible)
    wrapper.unmount()
  })

  it('keeps a source invoice fixed at one receipt and permits manual aggregate receipt counts', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '单张发票.pdf')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElInputNumber' })).toHaveLength(0)
    await wrapper.findAllComponents({ name: 'ElButton' }).find((button) => button.text() === '取消')!.trigger('click')
    await wrapper.findAll('button').find((button) => button.text() === '手动添加')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElInputNumber' })).toHaveLength(1)
    expect(wrapper.findAllComponents({ name: 'ElFormItem' }).find((item) => item.props('label') === '票据张数')!.text())
      .toContain('手工汇总多张票据时填写；行程单和证明材料不计入')
    wrapper.unmount()
  })

  it('keeps every selected placeholder stable and publishes OCR rows together at batch completion', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const calculate = vi.spyOn(expense, 'refreshCalculations').mockResolvedValue()
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const upload = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const recognition = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile).mockReturnValueOnce(upload.promise)
      .mockImplementation(async (draftId, revision, file) => ({
        draftId, revision: revision + 1, file: serverFile('file-2', file.name, 'EXPENSE_SOURCE'),
      }))
    vi.mocked(recognizeReimbursementDraftFile)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 4, file: recognizedFile('file-1', '第一张.pdf') })
      .mockReturnValueOnce(recognition.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '第一张.pdf', { type: 'application/pdf' }),
      new File(['b'], '第二张.pdf', { type: 'application/pdf' }),
    ])
    expect(wrapper.findAll('[data-testid="batch-file"]')).toHaveLength(2)
    const placeholders = wrapper.findAll('[data-testid="batch-file"]').map((row) => row.element)
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    upload.resolve({ draftId: 'draft-1', revision: 2, file: serverFile('file-1', '第一张.pdf', 'EXPENSE_SOURCE') })
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(wrapper.findAll('[data-testid="batch-file"]').map((row) => row.element)).toEqual(placeholders)
    expect(expense.items).toHaveLength(0)
    expect(calculate).not.toHaveBeenCalled()
    recognition.resolve({ draftId: 'draft-1', revision: 5, file: recognizedFile('file-2', '第二张.pdf') })
    await flushPromises()
    expect(expense.items).toHaveLength(2)
    expect(wrapper.get('[data-testid="batch-progress"]').text()).toContain('本批 2 个文件已处理完成')
    expect(wrapper.findAll('[data-testid="batch-file"]')).toHaveLength(0)
    expect(wrapper.get('[data-testid="batch-progress"]').text()).not.toContain('第一张.pdf')
    expect(calculate).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('continues the batch after individual upload and OCR failures without replaying either request', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    let revision = 1
    let failedUploadAttempts = 0
    const retained: ReimbursementDraftFile[] = []
    vi.mocked(getReimbursementDraft).mockImplementation(async () => draft(revision))
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async () => ({ draftId: 'draft-1', revision, items: [...retained] }))
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(async (draftId, expected, file) => {
      expect(expected).toBe(revision)
      if (file.name === '上传失败.pdf' && failedUploadAttempts++ === 0) throw new Error('单张上传失败')
      const uploaded = serverFile(`file-${revision}`, file.name, 'EXPENSE_SOURCE')
      retained.push(uploaded)
      return { draftId, revision: ++revision, file: uploaded }
    })
    vi.mocked(recognizeReimbursementDraftFile).mockImplementation(async (draftId, fileId, input) => {
      expect(input.expectedRevision).toBeLessThanOrEqual(revision)
      const uploaded = retained.find((file) => file.id === fileId)!
      if (uploaded.name === '识别失败.pdf') throw new Error('单张识别失败')
      const result = recognizedFile(fileId, uploaded.name)
      retained[retained.indexOf(uploaded)] = result
      return { draftId, revision, file: result }
    })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-expense-input', ['上传失败.pdf', '识别失败.pdf', '成功一.pdf', '成功二.pdf'].map((name) =>
      new File(['pdf'], name, { type: 'application/pdf' }),
    ))
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(4)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledTimes(3)
    expect(expense.items.map((item) => item.description)).toEqual(['成功一.pdf 的行程', '成功二.pdf 的行程'])
    expect(wrapper.get('[data-testid="batch-progress"]').text()).toContain('单张上传失败')
    expect(wrapper.get('[data-testid="batch-progress"]').text()).toContain('单张识别失败')
    const failedUploadRow = wrapper.findAll('[data-testid="batch-file"]')
      .find((row) => row.text().includes('上传失败.pdf'))
    expect(failedUploadRow).toBeDefined()
    expect(failedUploadRow!.text()).toContain('重试上传')
    expect(failedUploadRow!.text()).toContain('移除')
    await failedUploadRow!.findAll('button').find((button) => button.text() === '重试上传')!.trigger('click')
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(5)
    expect(expense.items.map((item) => item.description)).toContain('上传失败.pdf 的行程')
    expect(wrapper.findAll('[data-testid="batch-file"]')
      .some((row) => row.text().includes('上传失败.pdf'))).toBe(false)
    expect(drafts.processingFiles).toBe(false)
    wrapper.unmount()
  })

  it('uploads B while recognizing A, keeps one OCR lane, and publishes all rows only after the last result', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const calculate = vi.spyOn(expense, 'refreshCalculations').mockResolvedValue()
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const uploadB = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const ocrA = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    const ocrB = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, file: serverFile('a', 'A.pdf', 'ATTACHMENT_ONLY') })
      .mockReturnValueOnce(uploadB.promise)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 4, file: serverFile('c', 'C.pdf', 'ATTACHMENT_ONLY') })
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(ocrA.promise).mockReturnValueOnce(ocrB.promise)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 4, file: recognizedFile('c', 'C.pdf') })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-expense-input', ['A.pdf', 'B.pdf', 'C.pdf'].map((name) => new File(['pdf'], name, { type: 'application/pdf' })))
    await flushPromises()
    const placeholders = wrapper.findAll('[data-testid="batch-file"]').map((row) => row.element)
    expect(placeholders).toHaveLength(3)
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(drafts.pendingMutations).toBe(2)
    expect(wrapper.get('[data-testid="batch-lane-counts"]').text()).toContain('已上传 1/3')
    expect(wrapper.get('[data-testid="batch-lane-counts"]').text()).toContain('已识别 0/3')
    expect(wrapper.findAll('[data-testid="batch-file"]').map((row) => row.text())).toEqual(['A.pdf识别中', 'B.pdf上传中', 'C.pdf等待上传'])
    expect(wrapper.find('.expense-table').exists()).toBe(false)
    expect(wrapper.text()).toContain('还没有费用明细')
    expect(wrapper.findAll('button').find((button) => button.text() === '手动添加')!.attributes('disabled')).toBeDefined()
    await selectFiles(wrapper, 'durable-expense-input', [new File(['pdf'], '重复点击.pdf')])
    uploadB.resolve({ draftId: 'draft-1', revision: 3, file: serverFile('b', 'B.pdf', 'ATTACHMENT_ONLY') })
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(3)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(expense.items).toHaveLength(0)
    expect(calculate).not.toHaveBeenCalled()
    expect(wrapper.findAll('[data-testid="batch-file"]').map((row) => row.element)).toEqual(placeholders)
    ocrA.resolve({ draftId: 'draft-1', revision: 2, file: recognizedFile('a', 'A.pdf') })
    await flushPromises()
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="batch-lane-counts"]').text()).toContain('已识别 1/3')
    expect(drafts.currentDraft!.revision).toBe(4)
    expect(expense.items).toHaveLength(0)
    expect(wrapper.find('.expense-table').exists()).toBe(false)
    expect(wrapper.text()).toContain('还没有费用明细')
    ocrB.resolve({ draftId: 'draft-1', revision: 4, file: recognizedFile('b', 'B.pdf') })
    await flushPromises()
    expect(expense.items.map((item) => item.id)).toEqual(['ocr-a', 'ocr-b', 'ocr-c'])
    expect(wrapper.find('.expense-table').exists()).toBe(true)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(3)
    expect(wrapper.get('[data-testid="batch-lane-counts"]').text()).toContain('已上传 3/3')
    expect(wrapper.get('[data-testid="batch-lane-counts"]').text()).toContain('已识别 3/3')
    expect(drafts.processingFiles).toBe(false)
    expect(calculate).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('cancels both in-flight lanes on reimbursement switch without starting queued files or publishing stale rows', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const uploadB = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const ocrA = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    vi.mocked(uploadReimbursementDraftFile)
      .mockResolvedValueOnce({ draftId: 'draft-1', revision: 2, file: serverFile('a', 'A.pdf', 'ATTACHMENT_ONLY') })
      .mockReturnValueOnce(uploadB.promise)
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(ocrA.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-expense-input', ['A.pdf', 'B.pdf', 'C.pdf'].map((name) => new File(['pdf'], name)))
    await flushPromises()
    drafts.currentDraft = draft(1, 'draft-2')
    drafts.files = []
    expect(vi.mocked(uploadReimbursementDraftFile).mock.calls[1]![3]!.signal!.aborted).toBe(true)
    expect(vi.mocked(recognizeReimbursementDraftFile).mock.calls[0]![3]!.signal!.aborted).toBe(true)
    uploadB.resolve({ draftId: 'draft-1', revision: 3, file: serverFile('b', 'B.pdf', 'ATTACHMENT_ONLY') })
    ocrA.resolve({ draftId: 'draft-1', revision: 3, file: recognizedFile('a', 'A.pdf') })
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(expense.items).toHaveLength(0)
    expect(drafts.files).toEqual([])
    expect(drafts.currentDraft!.id).toBe('draft-2')
    expect(drafts.processingFiles).toBe(false)
    wrapper.unmount()
  })

  it('does not start a batch when the file picker is cancelled', async () => {
    useReimbursementDraftStore().currentDraft = draft()
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await selectFiles(wrapper, 'durable-expense-input', [])
    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(wrapper.find('[data-testid="batch-progress"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('lets a manual city-transport row explicitly choose taxi or ride-hailing and preserves the requirement', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'local_transport', name: '市内交通', order: 1, manualSelectable: true }]
    expense.upsertManualItem({ category: 'local_transport', date: '2026-09-01', displayDate: '2026-09-01', description: '市内交通', amount: '20.00', receiptCount: 3 })
    useReimbursementDraftStore().currentDraft = draft()
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    for (const transportType of ['taxi', 'ride_hailing'] as const) {
      await wrapper.findAll('button').find((button) => button.text() === '编辑')!.trigger('click')
      await flushPromises()
      const selector = wrapper.findAllComponents({ name: 'ElFormItem' }).find((item) => item.props('label') === '市内交通类型')!
        .findComponent({ name: 'ElSelect' })
      selector.vm.$emit('update:modelValue', transportType)
      await flushPromises()
      expect(wrapper.findAllComponents({ name: 'ElFormItem' }).some((item) => String(item.props('label')).includes('对应行程单'))).toBe(true)
      expect(wrapper.findAllComponents({ name: 'ElFormItem' }).find((item) => item.props('label') === '票据张数')!.text()).toContain('付款凭证按本行金额判断')
      await wrapper.findAllComponents({ name: 'ElButton' }).find((button) => button.text() === '保存')!.trigger('click')
      expect(expense.items[0]).toMatchObject({ transportType, requiresItinerary: transportType === 'ride_hailing', receiptCount: 3 })
    }
    wrapper.unmount()
  })

  it('allows durable upload, OCR retry, and deletion while the draft is review ready', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = { ...draft(8), status: 'REVIEW_READY' }
    const failed = recognizedFile('file-1', '待重试发票.pdf', '10.00')
    failed.ocrStatus = 'FAILED'
    failed.ocrResult = {
      ...failed.ocrResult!,
      status: 'failed',
      error: { code: 'OCR_FAILED', message: '识别失败，请重试' },
    }
    drafts.files = [failed]
    expense.upsertDraftOcrItem(failed)
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 9,
      file: recognizedFile('file-1', '待重试发票.pdf', '20.00'),
    })
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(
      async (draftId, revision, file, options) => ({
        draftId,
        revision: revision + 1,
        file: serverFile('file-2', file.name, options?.role ?? 'EXPENSE_SOURCE'),
      }),
    )
    vi.mocked(deleteReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 11,
      deletedFileId: 'file-1',
    })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const retry = wrapper.findAll('button').find((button) =>
      button.text().trim() === '重新识别',
    )
    expect(retry?.attributes('disabled')).toBeUndefined()
    await retry!.trigger('click')
    await flushPromises()
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      'file-1',
      expect.objectContaining({ expectedRevision: 8 }),
      expect.any(Object),
    )

    drafts.currentDraft!.status = 'REVIEW_READY'
    await selectFiles(wrapper, 'durable-attachment-input', [
      new File(['a'], '行程单.pdf', { type: 'application/pdf' }),
    ])
    await flushPromises()
    expect(uploadReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      9,
      expect.objectContaining({ name: '行程单.pdf' }),
      expect.objectContaining({ role: 'ATTACHMENT_ONLY' }),
    )

    drafts.currentDraft!.status = 'REVIEW_READY'
    const sourceRow = wrapper.findAll('.el-table__row').find((row) =>
      row.text().includes('待重试发票.pdf'),
    )
    const remove = sourceRow!.findAll('button').find((button) =>
      button.text().trim() === '删除',
    )
    expect(remove?.attributes('disabled')).toBeUndefined()
    await remove!.trigger('click')
    await flushPromises()
    expect(deleteReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      'file-1',
      10,
      expect.any(Object),
    )

    wrapper.unmount()
  })

  it.each([
    ['LOCKED', '当前报销已进入提交处理，不能再修改附件'],
    ['EXPIRED', '当前报销已过期，不能再修改附件'],
  ] as const)(
    'disables every durable file mutation when the draft is %s',
    async (status, reason) => {
      const expense = useExpenseStore()
      expense.categories = [
        { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
      ]
      const drafts = useReimbursementDraftStore()
      drafts.currentDraft = { ...draft(8), status }
      const failed = recognizedFile('file-1', '只读发票.pdf', '10.00')
      failed.ocrStatus = 'FAILED'
      drafts.files = [failed]
      expense.upsertDraftOcrItem(failed)
      const wrapper = mount(ExpenseItemsCard, {
        global: { plugins: [pinia, ElementPlus] },
      })

      const uploadButtons = wrapper.findAll('button').filter((button) =>
        ['上传报销材料'].includes(button.text().trim()),
      )
      expect(uploadButtons).toHaveLength(1)
      for (const button of uploadButtons) {
        expect(button.attributes('disabled')).toBeDefined()
        expect(button.attributes('title')).toBe(reason)
      }
      expect(wrapper.get('[data-testid="durable-expense-input"]').attributes('disabled')).toBeDefined()
      expect(wrapper.get('[data-testid="durable-attachment-input"]').attributes('disabled')).toBeDefined()

      await flushPromises()
      const sourceRow = wrapper.findAll('.el-table__row').find((row) =>
        row.text().includes('只读发票.pdf'),
      )!
      const mutationButtons = sourceRow.findAll('button').filter((button) => ['重新识别', '删除'].includes(button.text().trim()))
      expect(mutationButtons.map((button) => button.text().trim())).toEqual([
        '重新识别',
        '删除',
      ])
      for (const button of mutationButtons) {
        expect(button.attributes('disabled')).toBeDefined()
        expect(button.attributes('title')).toBe(reason)
      }

      await selectFiles(wrapper, 'durable-expense-input', [
        new File(['a'], '禁止上传.pdf', { type: 'application/pdf' }),
      ])
      await mutationButtons[0]!.trigger('click')
      await mutationButtons[1]!.trigger('click')
      await flushPromises()
      expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
      expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
      expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()

      wrapper.unmount()
    },
  )

  it('blocks every durable item and file mutation while the parent operation is read only', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const failed = recognizedFile('file-1', '操作中只读发票.pdf', '10.00')
    failed.ocrStatus = 'FAILED'
    failed.ocrResult = {
      ...failed.ocrResult!,
      status: 'failed',
      error: { code: 'OCR_FAILED', message: '识别失败，请重试' },
    }
    drafts.files = [failed]
    expense.upsertDraftOcrItem(failed)
    const wrapper = mount(ExpenseItemsCard, {
      props: { readonly: true },
      global: { plugins: [pinia, ElementPlus] },
    })

    const mutationLabels = new Set([
      '手动添加',
      '上传报销材料',
      '修改用途',
      '重新识别',
      '删除文件',
      '编辑',
      '删除',
    ])
    const mutationButtons = wrapper.findAll('button').filter((button) =>
      mutationLabels.has(button.text().trim()),
    )
    expect(mutationButtons.length).toBeGreaterThanOrEqual(5)
    for (const button of mutationButtons) {
      expect(button.attributes()).toHaveProperty('disabled')
    }
    expect(wrapper.get('[data-testid="durable-expense-input"]').attributes())
      .toHaveProperty('disabled')
    expect(wrapper.get('[data-testid="durable-attachment-input"]').attributes())
      .toHaveProperty('disabled')

    await selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '禁止上传.pdf', { type: 'application/pdf' }),
    ])
    for (const button of mutationButtons) await button.trigger('click')
    await flushPromises()

    expect(uploadReimbursementDraftFile).not.toHaveBeenCalled()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(expense.items).toEqual([
      expect.objectContaining({ id: 'ocr-file-1', amount: '10.00' }),
    ])

    wrapper.unmount()
  })

  it('stops the previous upload before OCR or remaining files when another draft is opened', async () => {
    const pending = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    vi.mocked(uploadReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const operation = selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '前一笔一.pdf', { type: 'application/pdf' }),
      new File(['b'], '前一笔二.pdf', { type: 'application/pdf' }),
    ])
    await vi.waitFor(() => expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce())
    drafts.currentDraft = draft(1, 'draft-2')
    drafts.files = []
    pending.resolve({
      draftId: 'draft-1',
      revision: 2,
      file: serverFile('file-previous', '前一笔一.pdf', 'EXPENSE_SOURCE'),
    })
    await operation
    await flushPromises()

    expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
    expect(expense.items).toEqual([])
    expect(drafts.currentDraft?.id).toBe('draft-2')

    wrapper.unmount()
  })

  it('does not insert a previous OCR result or continue its batch after switching drafts', async () => {
    const pending = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    vi.mocked(uploadReimbursementDraftFile).mockImplementation(
      async (draftId, revision, file, options) => ({
        draftId,
        revision: revision + 1,
        file: serverFile(`file-${revision}`, file.name, options?.role ?? 'EXPENSE_SOURCE'),
      }),
    )
    vi.mocked(recognizeReimbursementDraftFile)
      .mockReturnValueOnce(pending.promise)
      .mockImplementation(async (draftId, fileId, input) => ({
        draftId,
        revision: input.expectedRevision + 1,
        file: recognizedFile(fileId, '不应上传.pdf'),
      }))
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const operation = selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '前一笔一.pdf', { type: 'application/pdf' }),
      new File(['b'], '前一笔二.pdf', { type: 'application/pdf' }),
    ])
    await vi.waitFor(() => expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce())
    const current = draft(1, 'draft-2')
    current.input.items = [{
      category: 'rail_fare',
      date: '2026-09-02',
      displayDate: '2026-09-02',
      description: '新草稿已有明细',
      amount: '1.00',
      receiptCount: 1,
    }]
    drafts.currentDraft = current
    drafts.files = []
    expense.hydrateFromDraft(current, [])
    pending.resolve({
      draftId: 'draft-1',
      revision: 3,
      file: recognizedFile('file-1', '前一笔一.pdf'),
    })
    await operation
    await flushPromises()

    expect(uploadReimbursementDraftFile).toHaveBeenCalledTimes(2)
    expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(expense.items).toEqual([
      expect.objectContaining({ description: '新草稿已有明细', amount: '1.00' }),
    ])
    expect(expense.items.some((item) => item.id === 'ocr-file-1')).toBe(false)

    wrapper.unmount()
  })

  it.each(['reset', 'logout'] as const)(
    'silently stops an in-flight batch after %s clears the draft scope',
    async (ending) => {
      const pending = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
      const drafts = useReimbursementDraftStore()
      drafts.currentDraft = draft()
      vi.mocked(uploadReimbursementDraftFile).mockReturnValueOnce(pending.promise)
      const message = vi.spyOn(ElMessage, 'error')
      const wrapper = mount(ExpenseItemsCard, {
        global: { plugins: [pinia, ElementPlus] },
      })

      const operation = selectFiles(wrapper, 'durable-expense-input', [
        new File(['a'], '旧会话一.pdf', { type: 'application/pdf' }),
        new File(['b'], '旧会话二.pdf', { type: 'application/pdf' }),
      ])
      await vi.waitFor(() => expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce())
      if (ending === 'logout') await useAuthStore().logout()
      else drafts.reset()
      pending.resolve({
        draftId: 'draft-1',
        revision: 2,
        file: serverFile('file-old', '旧会话一.pdf', 'EXPENSE_SOURCE'),
      })
      await operation
      await flushPromises()

      expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
      expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
      expect(message).not.toHaveBeenCalled()

      wrapper.unmount()
    },
  )

  it('does not continue an in-flight batch after the component is unmounted', async () => {
    const pending = deferred<Awaited<ReturnType<typeof uploadReimbursementDraftFile>>>()
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    vi.mocked(uploadReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const operation = selectFiles(wrapper, 'durable-expense-input', [
      new File(['a'], '卸载前一.pdf', { type: 'application/pdf' }),
      new File(['b'], '卸载前二.pdf', { type: 'application/pdf' }),
    ])
    await vi.waitFor(() => expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce())
    wrapper.unmount()
    pending.resolve({
      draftId: 'draft-1',
      revision: 2,
      file: serverFile('file-old', '卸载前一.pdf', 'EXPENSE_SOURCE'),
    })
    await operation
    await flushPromises()

    expect(uploadReimbursementDraftFile).toHaveBeenCalledOnce()
    expect(recognizeReimbursementDraftFile).not.toHaveBeenCalled()
  })

  it('clears uploaded materials after one confirmation and preserves manual expenses and form data', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '发票.pdf', '10.00')
    const itinerary = recognizedItinerary('file-2')
    const payment = serverFile('file-3', '付款凭证.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' })
    drafts.files = [source, itinerary, payment]
    expense.upsertDraftOcrItem(source)
    expense.upsertManualItem({ category: 'rail_fare', description: '手工费用', date: '2026-09-01', displayDate: '2026-09-01', amount: '20.00', receiptCount: 1, itineraryFileIds: [itinerary.id], paymentProofFileIds: [payment.id] })
    const formBefore = JSON.stringify(drafts.currentDraft)
    const recalculate = vi.spyOn(expense, 'refreshCalculations').mockResolvedValue()
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    vi.mocked(clearReimbursementDraftFiles).mockResolvedValue({ draftId: 'draft-1', deletedFileIds: ['file-1', 'file-2', 'file-3'], revision: 9 })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text().trim() === '清空文件')!.trigger('click')
    await flushPromises()
    expect(ElMessageBox.confirm).toHaveBeenCalledOnce()
    expect(ElMessageBox.confirm).toHaveBeenCalledWith(expect.stringContaining('3 个已上传文件'), '清空已上传文件', expect.any(Object))
    expect(clearReimbursementDraftFiles).toHaveBeenCalledOnce()
    expect(clearReimbursementDraftFiles).toHaveBeenCalledWith('draft-1', 8, expect.any(Object))
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(drafts.files).toEqual([])
    expect(expense.items).toHaveLength(1)
    expect(expense.items[0]).toMatchObject({ description: '手工费用', amount: '20.00', itineraryFileIds: [], paymentProofFileIds: [] })
    expect({ ...drafts.currentDraft, revision: 8 }).toEqual(JSON.parse(formBefore))
    expect(drafts.processingFiles).toBe(false)
    expect(recalculate).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each(['cancel', 'switch'] as const)('does not clear uploaded files when confirmation is cancelled or scope changes: %s', async (scenario) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [recognizedFile('file-1', '发票.pdf', '10.00')]
    const pending = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    vi.spyOn(ElMessageBox, 'confirm').mockReturnValue(pending.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text().trim() === '清空文件')!.trigger('click')
    await flushPromises()
    if (scenario === 'switch') {
      drafts.currentDraft = draft(1, 'draft-2')
      pending.resolve({} as never)
    } else pending.reject('cancel')
    await flushPromises()
    expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
    expect(drafts.files).toHaveLength(1)
    expect(drafts.processingFiles).toBe(false)
    wrapper.unmount()
  })

  it('stops clearing after a deletion failure and preserves the remaining files and expenses', async () => {
    const expense = useExpenseStore()
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const files = [1, 2, 3].map((index) => recognizedFile(`file-${index}`, `发票${index}.pdf`, '10.00'))
    drafts.files = files
    files.forEach((file) => expense.upsertDraftOcrItem(file))
    vi.spyOn(expense, 'refreshCalculations').mockResolvedValue()
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const errorMessage = vi.spyOn(ElMessage, 'error')
    const remove = vi.spyOn(drafts, 'clearFiles').mockImplementation(async () => {
      drafts.files = drafts.files.filter((file) => file.id !== 'file-1')
      throw new Error('连接中断')
    })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text().trim() === '清空文件')!.trigger('click')
    await flushPromises()
    expect(remove).toHaveBeenCalledOnce()
    expect(drafts.files.map((file) => file.id)).toEqual(['file-2', 'file-3'])
    expect(expense.items.map((item) => item.sourceFileId)).toEqual(['file-2', 'file-3'])
    expect(errorMessage).toHaveBeenCalledOnce()
    expect(drafts.processingFiles).toBe(false)
    wrapper.unmount()
  })

  it('stops sending clear requests after the current reimbursement changes during deletion', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    drafts.files = [recognizedFile('file-1', '发票1.pdf', '10.00'), recognizedFile('file-2', '发票2.pdf', '20.00')]
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const pending = deferred<Awaited<ReturnType<typeof drafts.clearFiles>>>()
    const remove = vi.spyOn(drafts, 'clearFiles').mockReturnValue(pending.promise)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('button').find((button) => button.text().trim() === '清空文件')!.trigger('click')
    await flushPromises()
    expect(drafts.processingFiles).toBe(true)
    drafts.currentDraft = draft(1, 'draft-2')
    const newFile = recognizedFile('new-file', '新的报销.pdf', '30.00')
    drafts.files = [newFile]
    pending.resolve({ draftId: 'draft-1', deletedFileIds: ['file-1', 'file-2'], revision: 2 })
    await flushPromises()
    expect(remove).toHaveBeenCalledOnce()
    expect(drafts.files).toEqual([newFile])
    expect(drafts.processingFiles).toBe(false)
    wrapper.unmount()
  })

  it('disables clearing for empty, busy or submitted reimbursements', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    const button = wrapper.findAll('button').find((entry) => entry.text().trim() === '清空文件')!
    expect(button.attributes('disabled')).toBeDefined()
    drafts.files = [recognizedFile('file-1', '发票.pdf', '10.00')]
    await flushPromises()
    expect(button.attributes('disabled')).toBeUndefined()
    drafts.processingFiles = true
    await flushPromises()
    expect(button.attributes('disabled')).toBeDefined()
    drafts.processingFiles = false
    drafts.currentDraft.status = 'LOCKED'
    await flushPromises()
    expect(button.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.action-help').text()).toContain('已进入提交处理')
    wrapper.unmount()
  })

  it('does not insert a header help row while linked deletion awaits confirmation', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const source = recognizedFile('file-1', '发票.pdf', '10.00')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    const pending = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
    vi.spyOn(ElMessageBox, 'confirm').mockReturnValue(pending.promise)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })
    try {
      expect(wrapper.find('.action-help').exists()).toBe(false)
      await wrapper.findAll('button').find((button) => button.text().trim() === '删除')!.trigger('click')
      await flushPromises()
      expect(ElMessageBox.confirm).toHaveBeenCalledOnce()
      const upload = wrapper.findAll('button').find((button) => button.text().trim() === '上传报销材料')!
      expect(upload.attributes('disabled')).toBeDefined()
      expect(upload.attributes('title')).toBe('请等待当前文件操作完成')
      expect(wrapper.find('.action-help').exists()).toBe(false)
      expect(wrapper.get('.receipt-operation-status').text()).toBe('请等待当前文件操作完成')
      expect(wrapper.get('.receipt-operation-status').classes()).toContain('visually-hidden')
    } finally {
      pending.reject('cancel')
      await flushPromises()
      expect(deleteReimbursementDraftFile).not.toHaveBeenCalled()
      wrapper.unmount()
    }
  })

  it('retries persistent OCR without duplication and confirms linked deletion', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const failed = recognizedFile('file-1', '待重试发票.pdf', '10.00')
    failed.ocrStatus = 'FAILED'
    failed.ocrResult = {
      ...failed.ocrResult!,
      status: 'failed',
      error: { code: 'OCR_FAILED', message: '识别失败，请重试' },
    }
    drafts.files = [failed]
    expense.upsertDraftOcrItem(failed)
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 9,
      file: recognizedFile('file-1', '待重试发票.pdf', '20.00'),
    })
    vi.mocked(deleteReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 10,
      deletedFileId: 'file-1',
    })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const retry = wrapper.findAll('button').find((button) =>
      button.text().trim() === '重新识别',
    )
    expect(retry).toBeDefined()
    await retry!.trigger('click')
    await flushPromises()

    expect(expense.items).toEqual([
      expect.objectContaining({ id: 'ocr-file-1', amount: '20.00' }),
    ])
    const remove = wrapper.findAll('button').find((button) =>
      ['删除', '删除文件'].includes(button.text().trim()),
    )
    expect(remove).toBeDefined()
    await remove!.trigger('click')
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalledWith(
      expect.stringContaining('费用明细'),
      expect.any(String),
      expect.any(Object),
    )
    expect(deleteReimbursementDraftFile).toHaveBeenCalledWith(
      'draft-1',
      'file-1',
      9,
      expect.any(Object),
    )
    expect(expense.items).toEqual([])
    expect(expense.dismissedOcrFileIds).toEqual([])
    expect(drafts.files).toEqual([])

    wrapper.unmount()
  })

  it('previews server-held invoices and shows linked itinerary only with the expense row', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '发票.pdf', '10.00')
    const itinerary = serverFile('file-2', '行程单.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'itinerary' })
    drafts.files = [source, itinerary]
    expense.upsertDraftOcrItem(source)
    expense.items[0]!.itineraryFileIds = ['file-2']
    expense.items[0]!.requiresItinerary = true
    const blob = new Blob(['pdf'], { type: 'application/pdf' })
    vi.mocked(getReimbursementFileContent).mockResolvedValue(blob)
    const createUrl = vi.fn().mockReturnValue('blob:server-preview')
    const revokeUrl = vi.fn()
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = createUrl
      static revokeObjectURL = revokeUrl
    })
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    expect(wrapper.findAll('.receipt-row')).toHaveLength(0)
    const desktopRow = wrapper.find('.el-table__row')
    expect(desktopRow.text()).toContain('发票.pdf')
    expect(desktopRow.text()).toContain('行程单.pdf')
    expect(desktopRow.text()).not.toContain('缺少行程单')
    expect(desktopRow.findAll('button').filter((button) => button.text().trim() === '删除')).toHaveLength(1)
    await wrapper.get('[aria-label="预览票据 发票.pdf"]').trigger('click')
    await flushPromises()
    expect(getReimbursementFileContent).toHaveBeenCalledWith('draft-1', 'file-1', { signal: expect.any(AbortSignal) })
    expect(createUrl).toHaveBeenCalledWith(blob)
    expect(wrapper.get('iframe').attributes('src')).toBe('blob:server-preview')
    wrapper.unmount()
    expect(revokeUrl).toHaveBeenCalledWith('blob:server-preview')
  })

  it('allows explicit reuse of a multi-trip itinerary for another expense', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const first = recognizedFile('file-1', '发票一.pdf')
    const second = recognizedFile('file-2', '发票二.pdf')
    for (const source of [first, second]) {
      source.ocrResult = { ...source.ocrResult!, transportType: 'ride_hailing', requiresItinerary: true }
    }
    const itinerary = serverFile('file-3', '多次行程.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'itinerary' })
    drafts.files = [first, second, itinerary]
    expense.upsertDraftOcrItem(first)
    expense.upsertDraftOcrItem(second)
    expense.items[0]!.itineraryFileIds = ['file-3']
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    const secondRow = wrapper.findAll('.el-table__row').find((row) => row.text().includes('发票二.pdf'))!
    await secondRow.findAll('button').find((button) => button.text().trim() === '编辑')!.trigger('click')
    await flushPromises()
    const option = wrapper.findAllComponents({ name: 'ElOption' }).find((entry) => entry.props('value') === 'file-3')!
    expect(option.props('disabled')).toBe(false)
    const selector = wrapper.findAllComponents({ name: 'ElFormItem' }).find((entry) => entry.props('label') === '对应行程单')!.findComponent({ name: 'ElSelect' })
    expect(selector.props('multiple')).toBe(false)
    selector.vm.$emit('update:modelValue', 'file-3')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().trim() === '保存')!.trigger('click')
    expect(expense.items.map((item) => item.itineraryFileIds)).toEqual([['file-3'], ['file-3']])
    expect(expense.items[1]?.itineraryAutoMatchDisabled).toBe(true)
    expect(wrapper.findAll('button').some((button) => button.text() === '重新自动匹配')).toBe(false)
    expect(wrapper.text()).toContain('一个文件可包含多次行程')
    wrapper.unmount()
  })

  it('lets an employee confirm RMB for an unknown-currency foreign invoice without guessing its currency', async () => {
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-foreign', '海外发票.jpg')
    source.ocrResult = {
      ...source.ocrResult!, type: 'foreign_receipt', amount: null, originalCurrency: null,
      warnings: ['FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'],
    }
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    const wrapper = mount(ExpenseItemsCard, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().trim() === '编辑')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('人民币报销金额（元）')
    const amountInput = wrapper.findAllComponents({ name: 'ElInput' }).find((input) => input.props('placeholder') === '0.00')!
    amountInput.vm.$emit('update:modelValue', '700.00')
    const confirmation = wrapper.findAllComponents({ name: 'ElCheckbox' }).find((checkbox) => checkbox.text().includes('已核对原币金额'))!
    confirmation.vm.$emit('update:modelValue', true)
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text().trim() === '保存')!.trigger('click')
    expect(expense.items[0]).toMatchObject({ amount: '700.00', requiresCnyConfirmation: true, cnyAmountConfirmed: true })
    expect(expense.items[0]?.originalCurrency).toBeUndefined()
    wrapper.unmount()
  })

  it('shows an unresolved OCR blocker and requires confirmation before ignoring it', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    drafts.files = [recognizedFile('file-1', '待确认票据.pdf', '10.00')]
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    expect(wrapper.text()).toContain('1 张票据的 OCR 结果待确认')
    expect(wrapper.text()).toContain('添加到费用明细')
    const ignore = wrapper.findAll('button').find((button) =>
      button.text().trim() === '仅作为材料保留',
    )
    expect(ignore).toBeDefined()

    await ignore!.trigger('click')
    await flushPromises()

    expect(ElMessageBox.confirm).toHaveBeenCalledWith(
      expect.stringContaining('不计入费用金额'),
      '忽略此票据',
      expect.any(Object),
    )
    expect(expense.items).toEqual([])
    expect(expense.dismissedOcrFileIds).toEqual(['file-1'])
    expect(wrapper.text()).not.toContain('OCR 结果待确认')

    wrapper.unmount()
  })

  it.each(['switch', 'department', 'clear', 'unmount'] as const)(
    'drops a late ignore confirmation after draft scope %s',
    async (ending) => {
      const expense = useExpenseStore()
      expense.categories = [
        { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
      ]
      const drafts = useReimbursementDraftStore()
      const auth = useAuthStore()
      auth.status = 'authenticated'
      auth.session = {
        user: { userId: 'synthetic-user', name: '测试用户' },
        departments: [
          { id: '100', name: '测试部门' },
          { id: '200', name: '另一部门' },
        ],
        selectedDepartment: { id: '100', name: '测试部门' },
        isAdmin: false,
        csrfToken: 'synthetic-csrf',
      }
      drafts.currentDraft = draft(8)
      drafts.files = [recognizedFile('file-1', '前一笔票据.pdf', '10.00')]
      const pending = deferred<Awaited<ReturnType<typeof ElMessageBox.confirm>>>()
      vi.spyOn(ElMessageBox, 'confirm').mockReturnValue(pending.promise)
      const wrapper = mount(ExpenseItemsCard, {
        global: { plugins: [pinia, ElementPlus] },
      })
      const ignore = wrapper.findAll('button').find((button) =>
        button.text().trim() === '仅作为材料保留',
      )

      const operation = ignore!.trigger('click')
      await vi.waitFor(() => expect(ElMessageBox.confirm).toHaveBeenCalledOnce())
      if (ending === 'switch') {
        drafts.currentDraft = draft(1, 'draft-2')
        drafts.files = []
        expense.hydrateFromDraft(drafts.currentDraft, [])
      } else if (ending === 'department') {
        auth.session = {
          ...auth.session!,
          selectedDepartment: { id: '200', name: '另一部门' },
        }
      } else if (ending === 'clear') {
        drafts.currentDraft = null
        drafts.files = []
        expense.reset()
      } else {
        wrapper.unmount()
      }
      pending.resolve({} as Awaited<ReturnType<typeof ElMessageBox.confirm>>)
      await operation
      await flushPromises()

      expect(expense.dismissedOcrFileIds).toEqual([])
      if (ending !== 'unmount') wrapper.unmount()
    },
  )

  it('keeps a linked item changed by the user while an OCR retry is in flight', async () => {
    const pending = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '人工修改中的票据.pdf', '10.00')
    source.ocrStatus = 'FAILED'
    source.ocrResult = {
      ...source.ocrResult!,
      status: 'failed',
      error: { code: 'OCR_FAILED', message: '识别失败，请重试' },
    }
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const warning = vi.spyOn(ElMessage, 'warning')
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const retry = wrapper.findAll('button').find((button) =>
      button.text().trim() === '重新识别',
    )
    await retry!.trigger('click')
    await vi.waitFor(() => expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce())

    expense.upsertManualItem({
      id: 'ocr-file-1',
      category: 'rail_fare',
      date: '2026-09-02',
      displayDate: '2026-09-02',
      description: '用户在重试期间人工修改',
      amount: '99.00',
      receiptCount: 2,
      warnings: [],
    })
    pending.resolve({
      draftId: 'draft-1',
      revision: 9,
      file: recognizedFile('file-1', '人工修改中的票据.pdf', '20.00'),
    })
    await flushPromises()

    expect(expense.items).toEqual([
      expect.objectContaining({
        id: 'ocr-file-1',
        date: '2026-09-02',
        description: '用户在重试期间人工修改',
        amount: '99.00',
        receiptCount: 1,
      }),
    ])
    expect(receiptOcrResult(drafts.files[0])?.amount).toBe('20.00')
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('未自动新增或覆盖'))

    wrapper.unmount()
  })

  it.each([
    { requiresItinerary: true },
    { transportType: 'ride_hailing' as const },
    { itineraryFileIds: ['itinerary-1'] },
    { originalCurrency: 'VND' },
    { originalAmount: '97600000.00' },
    { cnyAmountConfirmed: true },
    { requiresCnyConfirmation: true },
    { paymentProofFileIds: ['payment-1'] },
    { railType: 'high_speed' as const },
    { itineraryAutoMatchDisabled: true },
  ])('preserves metadata-only edits during an OCR retry (%j)', async (changedFields) => {
    const pending = deferred<Awaited<ReturnType<typeof recognizeReimbursementDraftFile>>>()
    const expense = useExpenseStore()
    expense.categories = [{ id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true }]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '待核对票据.pdf', '10.00')
    drafts.files = [source,
      serverFile('itinerary-1', '行程单.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'itinerary' }),
      serverFile('payment-1', '付款.pdf', 'ATTACHMENT_ONLY', { attachmentKind: 'payment_proof' }),
    ]
    expense.upsertDraftOcrItem(source)
    vi.mocked(recognizeReimbursementDraftFile).mockReturnValueOnce(pending.promise)
    const warning = vi.spyOn(ElMessage, 'warning')
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    const retry = wrapper.find('.el-table__row').findAll('button').find((button) => button.text().trim() === '重新识别')
    await retry!.trigger('click')
    await vi.waitFor(() => expect(recognizeReimbursementDraftFile).toHaveBeenCalledOnce())
    Object.assign(expense.items[0]!, changedFields)
    pending.resolve({
      draftId: 'draft-1', revision: 9,
      file: recognizedFile('file-1', '待核对票据.pdf', '20.00'),
    })
    await flushPromises()
    expect(expense.items[0]).toEqual(expect.objectContaining({ ...changedFields, amount: '10.00' }))
    expect(receiptOcrResult(drafts.files[0])?.amount).toBe('20.00')
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('未自动新增或覆盖'))
    wrapper.unmount()
  })

  it('removes the linked item when a lost delete response reloads an absent file', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '已在服务端删除.pdf', '10.00')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    vi.mocked(deleteReimbursementDraftFile).mockRejectedValue(new Error('response lost'))
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft(9))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1',
      revision: 9,
      items: [],
    })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const remove = wrapper.findAll('button').find((button) =>
      ['删除', '删除文件'].includes(button.text().trim()),
    )
    await remove!.trigger('click')
    await flushPromises()

    expect(drafts.currentDraft?.revision).toBe(9)
    expect(drafts.files).toEqual([])
    expect(expense.items).toEqual([])
    expect(expense.dismissedOcrFileIds).toEqual([])

    wrapper.unmount()
  })

  it('keeps the linked item when a lost delete response reloads the existing file', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft(8)
    const source = recognizedFile('file-1', '仍在服务端.pdf', '10.00')
    drafts.files = [source]
    expense.upsertDraftOcrItem(source)
    vi.mocked(deleteReimbursementDraftFile).mockRejectedValue(new Error('response lost'))
    vi.mocked(getReimbursementDraft).mockResolvedValue(draft(8))
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({
      draftId: 'draft-1',
      revision: 8,
      items: [source],
    })
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const remove = wrapper.findAll('button').find((button) =>
      ['删除', '删除文件'].includes(button.text().trim()),
    )
    await remove!.trigger('click')
    await flushPromises()

    expect(drafts.files).toEqual([source])
    expect(expense.items).toEqual([
      expect.objectContaining({ id: 'ocr-file-1', amount: '10.00' }),
    ])
    expect(wrapper.text()).toContain('已同步报销内容最新状态')

    wrapper.unmount()
  })

  it('does not recreate a dismissed OCR item during retry without explicit adoption', async () => {
    const expense = useExpenseStore()
    expense.categories = [
      { id: 'rail_fare', name: '火车票', order: 1, manualSelectable: true },
    ]
    const current = draft(8)
    current.input.items = [{
      category: 'rail_fare',
      date: '2026-09-02',
      displayDate: '2026-09-02',
      description: '用户保存的人工修改',
      amount: '10.00',
      receiptCount: 1,
    }]
    current.input.dismissedOcrFileIds = ['file-1']
    const source = recognizedFile('file-1', '人工修改票据.pdf', '10.00')
    source.ocrStatus = 'FAILED'
    source.ocrResult = {
      ...source.ocrResult!,
      date: '2026-09-01',
      description: '旧 OCR 内容',
      status: 'failed',
      error: { code: 'OCR_FAILED', message: '识别失败，请重试' },
    }
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = current
    drafts.files = [source]
    expense.hydrateFromDraft(current, [source])
    vi.mocked(recognizeReimbursementDraftFile).mockResolvedValue({
      draftId: 'draft-1',
      revision: 9,
      file: recognizedFile('file-1', '人工修改票据.pdf', '20.00'),
    })
    const warning = vi.spyOn(ElMessage, 'warning')
    const wrapper = mount(ExpenseItemsCard, {
      global: { plugins: [pinia, ElementPlus] },
    })

    const retry = wrapper.findAll('button').find((button) =>
      button.text().trim() === '重新识别',
    )
    await retry!.trigger('click')
    await flushPromises()

    expect(expense.items).toEqual([
      expect.objectContaining({
        id: 'draft-draft-1-item-0',
        description: '用户保存的人工修改',
        amount: '10.00',
      }),
    ])
    expect(expense.items.some((item) => item.id === 'ocr-file-1')).toBe(false)
    expect(expense.dismissedOcrFileIds).toEqual(['file-1'])
    expect(receiptOcrResult(drafts.files[0])?.amount).toBe('20.00')
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('未自动新增或覆盖'))
    expect(wrapper.findAll('button').some((button) =>
      button.text().trim() === '添加到费用明细',
    )).toBe(true)

    wrapper.unmount()
  })
})

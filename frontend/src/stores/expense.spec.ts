import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { calculateTotals, getExpenseCategories } from '@/api/expenses'
import { useExpenseStore } from '@/stores/expense'
import type { OcrReceiptCandidate } from '@/types/receipts'
import type {
  ReimbursementDraft,
  ReimbursementDraftFile,
} from '@/types/reimbursements'

const MANUAL_CATEGORIES = [
  { id: 'local_transport', name: '市内交通费', order: 1, manualSelectable: true },
  { id: 'rail_fare', name: '火车票', order: 2, manualSelectable: true },
]

function durableDraft(
  items: ReimbursementDraft['input']['items'],
  dismissedOcrFileIds: string[] = [],
): ReimbursementDraft {
  return {
    id: 'draft-1',
    status: 'DRAFT',
    revision: 7,
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
      trip: {
        tripType: 'business',
        startDate: '2026-09-01',
        startTime: '09:00',
        endDate: '2026-09-02',
        endTime: '18:00',
      },
      items,
      dismissedOcrFileIds,
    },
    totals: {
      expenseTotal: '474.00',
      subsidyTotal: '200.00',
      totalAmount: '674.00',
      receiptCount: 2,
      uppercaseAmount: '陆佰柒拾肆元整',
      subsidy: {
        tripType: 'business',
        calendarDays: 2,
        effectiveDays: '2.0',
        dailyRate: '100.00',
        total: '200.00',
      },
    },
    relatedApprovals: [],
    relatedApprovalSummary: null,
  }
}

function durableFile(
  id: string,
  overrides: Partial<ReimbursementDraftFile & { ocrResult: OcrReceiptCandidate }> = {},
): ReimbursementDraftFile & { ocrResult: OcrReceiptCandidate } {
  return {
    id,
    name: `${id}.pdf`,
    role: 'EXPENSE_SOURCE',
    attachmentKind: 'other',
    sortOrder: 0,
    status: 'ACTIVE',
    mediaType: 'application/pdf',
    sizeBytes: 1024,
    ocrStatus: 'COMPLETE',
    ocrResult: {
      fileId: id,
      type: 'train',
      categoryId: 'rail_fare',
      categoryName: '火车票',
      date: '2026-09-01',
      description: '北京南-合肥南',
      amount: '454.00',
      receiptCount: 1,
      source: 'ocr',
      confidence: '0.93',
      warnings: [],
      status: 'recognized',
      error: null,
    },
    ...overrides,
  }
}

vi.mock('@/api/expenses', () => ({
  calculateTotals: vi.fn(),
  getExpenseCategories: vi.fn(),
}))

describe('expense store', () => {
  it('does not create a subsidy payload for overseas travel', () => {
    setActivePinia(createPinia())
    const store = useExpenseStore()
    store.setSubsidyIncluded(true)
    store.setTripType('overseas')
    store.trip.startDate = '2026-09-01'
    store.trip.endDate = '2026-09-03'
    expect(store.tripPayload()).toBeNull()
    expect(store.policyInputError).toContain('境外出差不申请')
    store.setSubsidyIncluded(false)
    expect(store.tripPayload()).toBeNull()
    expect(store.displaySubsidyTotal).toBe('0.00')
  })

  it('hydrates and resets an overseas trip without a manual total', () => {
    setActivePinia(createPinia())
    const store = useExpenseStore()
    const draft = durableDraft([])
    const trip = { ...draft.input.trip!, tripType: 'overseas' as const }
    draft.input.trip = trip
    store.hydrateFromDraft(draft, [])
    expect(store.trip.tripType).toBe('overseas')
    expect(store.tripPayload()).toBeNull()
    draft.input.trip = null
    draft.input.editingState = { includeSubsidy: false, trip }
    store.hydrateFromDraft(draft, [])
    expect(store.includeSubsidy).toBe(false)
    expect(store.displaySubsidyTotal).toBe('0.00')
    store.reset()
    expect(store.trip.tripType).toBe('business')
  })
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(calculateTotals).mockReset()
    vi.mocked(getExpenseCategories).mockReset()
  })

  it('round-trips payment associations and rail evidence, clears invalid purpose links, and removes deleted proofs', () => {
    const store = useExpenseStore()
    const source = durableFile('source', { ocrResult: { ...durableFile('source').ocrResult, railType: 'regular' } })
    const itinerary = durableFile('itinerary', { role: 'ATTACHMENT_ONLY', attachmentKind: 'itinerary' })
    const payment = durableFile('payment', { role: 'ATTACHMENT_ONLY', attachmentKind: 'payment_proof' })
    const row = { category: 'rail_fare', date: '2026-09-01', displayDate: '2026-09-01', description: '员工确认费用', amount: '600.00', receiptCount: 4, sourceFileId: 'source', railType: 'high_speed' as const, itineraryFileIds: ['itinerary'], paymentProofFileIds: ['payment'] }
    store.hydrateFromDraft(durableDraft([row]), [source, itinerary, payment])
    expect(store.items[0]).toMatchObject({ railType: 'regular', receiptCount: 1, itineraryFileIds: ['itinerary'], paymentProofFileIds: ['payment'] })
    expect(store.buildDraftExpenseItems()[0]).toMatchObject({ railType: 'regular', paymentProofFileIds: ['payment'] })
    store.reconcileDraftProofs([source, { ...itinerary, attachmentKind: 'payment_proof' }, payment])
    expect(store.items[0]?.itineraryFileIds).toEqual([])
    expect(store.items[0]?.paymentProofFileIds).toEqual(['payment'])
    store.removeDraftFileAssociation('payment')
    expect(store.buildDraftExpenseItems()[0]?.paymentProofFileIds).toEqual([])
  })

  it('hydrates persisted OCR lines by source file id without overwriting user edits', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const files = [
      durableFile('file-exact'),
      durableFile('file-edited', {
        sortOrder: 1,
        ocrResult: {
          ...durableFile('file-edited').ocrResult!,
          fileId: 'file-edited',
          description: 'OCR 原说明',
          amount: '20.00',
        },
      }),
    ]
    const draft = durableDraft([
      {
        sourceFileId: 'file-exact',
        category: 'rail_fare',
        date: '2026-09-01',
        displayDate: '2026-09-01',
        description: '北京南-合肥南',
        amount: '454.00',
        receiptCount: 1,
      },
      {
        sourceFileId: 'file-edited',
        category: 'rail_fare',
        date: '2026-09-02',
        displayDate: '2026-09-02',
        description: '用户修改后的说明',
        amount: '20.00',
        receiptCount: 1,
      },
    ])

    store.hydrateFromDraft(draft, files)
    store.hydrateFromDraft(draft, files)

    expect(store.manualProjectText).toBe('测试项目')
    expect(store.includeSubsidy).toBe(true)
    expect(store.trip.startDate).toBe('2026-09-01')
    expect(store.items).toHaveLength(2)
    expect(store.items[0]).toMatchObject({
      id: 'ocr-file-exact',
      source: 'ocr',
      description: '北京南-合肥南',
      confidence: '0.93',
    })
    expect(store.items[1]).toMatchObject({
      id: 'ocr-file-edited',
      source: 'ocr',
      sourceFileId: 'file-edited',
      description: '用户修改后的说明',
    })
    expect(store.items).toHaveLength(2)
    expect(store.totals?.totalAmount).toBe('674.00')
  })

  it('recovers a persisted OCR candidate that never reached the draft input', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('file-response-lost')

    store.hydrateFromDraft(durableDraft([]), [source])

    expect(store.items).toEqual([
      expect.objectContaining({
        id: 'ocr-file-response-lost',
        sourceFileId: 'file-response-lost',
        description: '北京南-合肥南',
        amount: '454.00',
      }),
    ])
    expect(store.totals).toBeNull()
    expect(store.calculationsCurrent).toBe(false)
  })


  it('does not recover a non-terminal OCR candidate', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const running = durableFile('file-running', { ocrStatus: 'RUNNING' })

    store.hydrateFromDraft(durableDraft([]), [running])

    expect(store.items).toEqual([])
  })

  it('persists an OCR-line dismissal and only revives it after explicit adoption', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('file-dismissed')
    store.upsertDraftOcrItem(source)

    store.removeItem('ocr-file-dismissed')

    expect(store.items).toEqual([])
    expect(store.dismissedOcrFileIds).toEqual(['file-dismissed'])

    store.hydrateFromDraft(durableDraft([], ['file-dismissed']), [source])
    store.hydrateFromDraft(durableDraft([], ['file-dismissed']), [source])
    expect(store.items).toEqual([])

    expect(store.upsertDraftOcrItem(source)).toBe(true)
    expect(store.items).toEqual([
      expect.objectContaining({ sourceFileId: 'file-dismissed' }),
    ])
    expect(store.dismissedOcrFileIds).toEqual([])
  })

  it('clears both an OCR item and its disposition when the source file is deleted', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const deleted = durableFile('file-deleted')
    const kept = durableFile('file-kept')
    store.upsertDraftOcrItem(deleted)
    store.upsertDraftOcrItem(kept)
    store.removeItem('ocr-file-deleted')

    store.removeDraftFileAssociation('file-deleted')

    expect(store.dismissedOcrFileIds).toEqual([])
    expect(store.items).toEqual([
      expect.objectContaining({ sourceFileId: 'file-kept' }),
    ])
  })

  it('keeps source-file provenance when an OCR line is edited and builds draft-only items', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.upsertDraftOcrItem(durableFile('file-edited'))

    store.upsertManualItem({
      id: 'ocr-file-edited',
      category: 'rail_fare',
      date: '2026-09-02',
      displayDate: '2026-09-02',
      description: '人工修改后的行程',
      amount: '455.00',
      receiptCount: 2,
    })

    expect(store.items[0]).toMatchObject({
      id: 'ocr-file-edited',
      source: 'ocr',
      sourceFileId: 'file-edited',
      description: '人工修改后的行程',
      amount: '455.00',
    })
    expect(store.buildDraftExpenseItems()).toEqual([expect.objectContaining({
      sourceFileId: 'file-edited',
      category: 'rail_fare',
      date: '2026-09-02',
      displayDate: '2026-09-02',
      description: '人工修改后的行程',
      amount: '455.00',
      receiptCount: 1,
    })])
  })

  it('keeps id and source for totals and draft persistence', async () => {
    vi.mocked(calculateTotals).mockResolvedValue({
      expenseTotal: '454.00',
      subsidyTotal: '0.00',
      totalAmount: '454.00',
      receiptCount: 1,
      uppercaseAmount: '肆佰伍拾肆元整',
      subsidy: null,
    })
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.manualProjectText = '测试项目'
    store.upsertDraftOcrItem(durableFile('file-calculation'))

    await store.refreshCalculations()

    expect(calculateTotals).toHaveBeenCalledWith(null, [expect.objectContaining({
      id: 'ocr-file-calculation',
      source: 'ocr',
      category: 'rail_fare',
      date: '2026-09-01',
      displayDate: '2026-09-01',
      description: '北京南-合肥南',
      amount: '454.00',
      receiptCount: 1,
    })])
    expect(store.buildDraftExpenseItems()[0]).toHaveProperty(
      'sourceFileId',
      'file-calculation',
    )
  })

  it('upserts durable OCR by file id so a retry never duplicates the item', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const first = durableFile('file-1')

    expect(store.upsertDraftOcrItem(first)).toBe(true)
    expect(store.upsertDraftOcrItem({
      ...first,
      ocrResult: {
        ...first.ocrResult!,
        amount: '455.00',
        confidence: '0.96',
      },
    })).toBe(true)

    expect(store.items).toEqual([
      expect.objectContaining({
        id: 'ocr-file-1',
        source: 'ocr',
        amount: '455.00',
        confidence: '0.96',
      }),
    ])
  })

  it('normalizes source invoices to one receipt while preserving manual aggregate counts', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('file-1')
    store.upsertDraftOcrItem(source)
    store.upsertManualItem({ ...store.items[0]!, receiptCount: 8 })
    expect(store.items[0]?.receiptCount).toBe(1)
    const input = store.buildDraftExpenseItems()
    input[0]!.receiptCount = 8
    store.hydrateFromDraft(durableDraft(input), [source])
    expect(store.items[0]?.receiptCount).toBe(1)
    expect(store.buildDraftExpenseItems()[0]?.receiptCount).toBe(1)
    store.upsertManualItem({
      category: 'rail_fare', date: '2026-09-01', displayDate: '2026-09-01',
      description: '手工汇总', amount: '100.00', receiptCount: 3,
    })
    expect(store.items[1]?.receiptCount).toBe(3)
    expect(store.localReceiptCount).toBe(4)
    const locked = durableDraft(input)
    locked.status = 'LOCKED'
    store.hydrateFromDraft(locked, [source])
    expect(store.items[0]?.receiptCount).toBe(8)
  })

  it('preserves manual itinerary intent across manual edits, re-recognition and draft hydration', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const file = durableFile('file-1')
    store.upsertDraftOcrItem(file)
    expect(store.items[0]?.itineraryAutoMatchDisabled).toBe(false)
    store.items[0]!.itineraryAutoMatchDisabled = true
    const item = store.items[0]!
    store.upsertManualItem({ id: item.id, category: item.category, date: item.date, displayDate: item.displayDate, description: '人工确认', amount: item.amount, receiptCount: 1 })
    expect(store.items[0]?.itineraryAutoMatchDisabled).toBe(true)
    store.upsertDraftOcrItem(file)
    expect(store.items[0]?.itineraryAutoMatchDisabled).toBe(true)
    const items = store.buildDraftExpenseItems()
    store.hydrateFromDraft(durableDraft(items), [file])
    expect(store.buildDraftExpenseItems()[0]?.itineraryAutoMatchDisabled).toBe(true)
  })

  it('keeps foreign original amounts separate from RMB and preserves explicit confirmation after reload', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('foreign-file')
    source.ocrResult = {
      ...source.ocrResult!, type: 'foreign_receipt', amount: '97600000.00',
      originalAmount: '97600000.00', originalCurrency: 'VND',
      warnings: ['FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'],
    }
    store.upsertDraftOcrItem(source)
    expect(store.items[0]).toMatchObject({ amount: '', originalAmount: '97600000.00', originalCurrency: 'VND', cnyAmountConfirmed: false, requiresCnyConfirmation: true })
    store.items[0]!.amount = '27800.00'
    store.items[0]!.cnyAmountConfirmed = true
    const input = store.buildDraftExpenseItems()
    store.hydrateFromDraft(durableDraft(input), [source])
    expect(store.items[0]).toMatchObject({ amount: '27800.00', originalAmount: '97600000.00', cnyAmountConfirmed: true })
  })

  it('keeps an unknown foreign currency pending and does not adopt its total as RMB', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('unknown-currency')
    source.ocrResult = { ...source.ocrResult!, type: 'foreign_receipt', originalCurrency: null, amount: '100.00' }
    store.upsertDraftOcrItem(source)
    expect(store.items[0]).toMatchObject({ amount: '', requiresCnyConfirmation: true, cnyAmountConfirmed: false })
    expect(store.items[0]?.warnings).toContain('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
  })

  it('preserves proof links through OCR retry and clears them when the proof file is deleted', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const source = durableFile('ride-source')
    source.ocrResult = { ...source.ocrResult!, transportType: 'ride_hailing', requiresItinerary: true }
    store.upsertDraftOcrItem(source)
    store.items[0]!.itineraryFileIds = ['itinerary-file']
    store.upsertDraftOcrItem(source)
    expect(store.buildDraftExpenseItems()[0]).toMatchObject({ requiresItinerary: true, itineraryFileIds: ['itinerary-file'] })
    store.removeDraftFileAssociation('itinerary-file')
    expect(store.items[0]?.itineraryFileIds).toEqual([])
    expect(store.items[0]?.requiresItinerary).toBe(true)
  })

  it('keeps integer-cent totals exact and supports item CRUD', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const firstId = store.upsertManualItem({
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '市内交通',
      amount: '44.89',
      receiptCount: 4,
    })
    store.upsertManualItem({
      category: 'rail_fare',
      date: '2026-06-30',
      displayDate: '2026-06-30',
      description: '北京南-合肥南',
      amount: '454',
      receiptCount: 1,
    })
    store.upsertManualItem({
      id: firstId,
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '修改后的市内交通',
      amount: '44.89',
      receiptCount: 5,
    })

    expect(store.items).toHaveLength(2)
    expect(store.displayExpenseTotal).toBe('498.89')
    expect(store.localReceiptCount).toBe(6)
    store.removeItem(firstId)
    expect(store.displayExpenseTotal).toBe('454.00')
    expect(store.localReceiptCount).toBe(1)
  })

  it('uses the server-provided technical item limit', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.setExpenseItemLimit(2)
    for (let index = 0; index < 2; index += 1) {
      store.upsertManualItem({
        category: 'local_transport',
        date: '2026-07-01',
        displayDate: '2026-07-01',
        description: `费用 ${index}`,
        amount: '1.00',
        receiptCount: 1,
      })
    }

    expect(store.maxExpenseItems).toBe(2)
    expect(() => store.upsertManualItem({
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '超限费用',
      amount: '1.00',
      receiptCount: 1,
    })).toThrow('当前最多添加 2 条票据费用明细')
  })

  it('exposes items in stable occurrence-date order without changing entry order', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    const entered = [
      ['2026-07-08', '同日先录入'],
      ['2026-06-30', '最早发生'],
      ['2026-07-08', '同日后录入'],
      ['2026-07-07', '中间发生'],
    ] as const
    for (const [date, description] of entered) {
      store.upsertManualItem({
        category: 'rail_fare',
        date,
        displayDate: date,
        description,
        amount: '1.00',
        receiptCount: 1,
      })
    }

    expect(store.items.map((item) => item.description)).toEqual(entered.map((item) => item[1]))
    expect(store.sortedItems.map((item) => item.description)).toEqual([
      '最早发生',
      '中间发生',
      '同日先录入',
      '同日后录入',
    ])
  })

  it('automatically calculates normal trips and only requires same-city confirmation', () => {
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.setSubsidyIncluded(true)
    Object.assign(store.trip, {
      startDate: '2026-06-30',
      startTime: '09:00',
      endDate: '2026-07-07',
      endTime: '18:00',
    })
    expect(store.tripPayload()).toEqual({
      tripType: 'business',
      startDate: '2026-06-30',
      startTime: '09:00',
      endDate: '2026-07-07',
      endTime: '18:00',
    })

    store.setTripType('project')
    Object.assign(store.trip, {
      startDate: '2026-06-01',
      endDate: '2026-07-01',
    })
    expect(store.projectCalendarDays).toBe(31)
    expect(store.projectPolicyType).toBe('long_term_project')
    expect(store.requiresPolicyConfirmation).toBe(false)
    expect(store.tripPayload()).toMatchObject({
      tripType: 'project',
    })

    Object.assign(store.trip, {
      startDate: '2026-06-01',
      endDate: '2026-06-30',
    })
    expect(store.projectCalendarDays).toBe(30)
    expect(store.projectPolicyType).toBe('short_term_project')
    expect(store.requiresPolicyConfirmation).toBe(false)

    store.setTripType('internal')
    expect(store.tripPayload()).toMatchObject({
      tripType: 'internal',
    })

    store.setTripType('same_city_project')
    expect(store.tripPayload()).toMatchObject({ tripType: 'same_city_project', policyConfirmed: false })
    expect(store.policyInputError).toContain('勾选')
    store.trip.policyConfirmed = true
    expect(store.tripPayload()).toMatchObject({ tripType: 'same_city_project', policyConfirmed: true })
    expect(store.policyInputError).toBe('')
  })

  it.each([
    ['11:59', '09:00', '09:00', '09:00'],
    ['18:00', '12:00', '18:00', '18:00'],
    ['11:59', '12:00', '09:00', '18:00'],
    ['12:00', '11:59', '18:00', '09:00'],
  ])('normalizes exact %s–%s to half-day times without changing editing values', (start, end, expectedStart, expectedEnd) => {
    const store = useExpenseStore()
    store.setSubsidyIncluded(true)
    Object.assign(store.trip, {
      startDate: '2026-06-30', endDate: '2026-06-30', startTime: start, endTime: end,
    })
    expect(store.tripPayload()).toMatchObject({ startTime: expectedStart, endTime: expectedEnd })
    expect(store.trip.startTime).toBe(start)
    expect(store.trip.endTime).toBe(end)
  })

  it('does not build a payload from non-exact time strings', () => {
    const store = useExpenseStore()
    store.setSubsidyIncluded(true)
    Object.assign(store.trip, {
      startDate: '2026-06-30',
      endDate: '2026-07-07',
      startTime: '9:00',
      endTime: '18:00',
    })
    expect(store.tripPayload()).toBeNull()
    store.trip.startTime = '09:00:00'
    expect(store.tripPayload()).toBeNull()
  })

  it('reconciles the summary with server values and reset removes current form data', async () => {
    vi.mocked(calculateTotals).mockResolvedValue({
      expenseTotal: '44.89',
      subsidyTotal: '800.00',
      totalAmount: '844.89',
      receiptCount: 4,
      uppercaseAmount: '捌佰肆拾肆元捌角玖分',
      subsidy: {
        tripType: 'business',
        calendarDays: 8,
        effectiveDays: '8.0',
        dailyRate: '100.00',
        total: '800.00',
      },
    })
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.setSubsidyIncluded(true)
    Object.assign(store.trip, {
      startDate: '2026-06-30',
      endDate: '2026-07-07',
    })
    store.manualProjectText = '临时项目'
    store.upsertManualItem({
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '市内交通',
      amount: '44.89',
      receiptCount: 4,
    })
    await store.refreshCalculations()
    expect(store.displayTotal).toBe('844.89')
    expect(store.displayReceiptCount).toBe(4)
    expect(store.totals?.uppercaseAmount).toBe('捌佰肆拾肆元捌角玖分')

    store.reset()
    expect(store.items).toEqual([])
    expect(store.includeSubsidy).toBe(false)
    expect(store.manualProjectText).toBe('')
    expect(store.trip.startDate).toBe('')
    expect(store.totals).toBeNull()
  })

  it('surfaces category loading failure and blocks manual items until retry succeeds', async () => {
    vi.mocked(getExpenseCategories).mockRejectedValueOnce(new Error('network failed'))
    const store = useExpenseStore()

    await expect(store.loadCategories()).resolves.toBe(false)
    expect(store.categoryLoadError).toContain('费用类别加载失败')
    expect(store.manualCategories).toEqual([])
    expect(() => store.upsertManualItem({
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '市内交通',
      amount: '44.89',
      receiptCount: 1,
    })).toThrow('费用类别不可用')

    vi.mocked(getExpenseCategories).mockResolvedValueOnce(MANUAL_CATEGORIES)
    await expect(store.loadCategories(true)).resolves.toBe(true)
    expect(store.categoryLoadError).toBe('')
    expect(store.manualCategories).toHaveLength(2)
  })


  it('keeps a completed subsidy calculation when unchanged approvals are synchronized again', async () => {
    vi.mocked(calculateTotals).mockResolvedValue({
      expenseTotal: '44.89',
      subsidyTotal: '800.00',
      totalAmount: '844.89',
      receiptCount: 1,
      uppercaseAmount: '捌佰肆拾肆元捌角玖分',
      subsidy: null,
      subsidies: [
        {
          relatedApprovalId: 'approval-1',
          tripType: 'business',
          calendarDays: 4,
          effectiveDays: '4.0',
          dailyRate: '100.00',
          total: '400.00',
        },
        {
          relatedApprovalId: 'approval-2',
          tripType: 'business',
          calendarDays: 4,
          effectiveDays: '4.0',
          dailyRate: '100.00',
          total: '400.00',
        },
      ],
    })
    const approvals = [
      { processInstanceId: 'approval-1', startDate: '2026-06-30', endDate: '2026-07-03' },
      { processInstanceId: 'approval-2', startDate: '2026-07-04', endDate: '2026-07-07' },
    ]
    const store = useExpenseStore()
    store.categories = MANUAL_CATEGORIES
    store.manualProjectText = 'P-007 测试项目'
    store.syncSubsidyApprovals(approvals)
    store.setSubsidyIncluded(true)
    store.upsertManualItem({
      category: 'local_transport',
      date: '2026-07-01',
      displayDate: '2026-07-01',
      description: '市内交通',
      amount: '44.89',
      receiptCount: 1,
    })
    await store.refreshCalculations()

    expect(store.calculationsCurrent).toBe(true)
    store.syncSubsidyApprovals(approvals.map((approval) => ({ ...approval })))

    expect(store.calculationsCurrent).toBe(true)
    expect(store.displaySubsidyTotal).toBe('800.00')
  })

})

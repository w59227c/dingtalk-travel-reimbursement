import { describe, expect, it } from 'vitest'

import {
  isRailExpense,
  linkedExpenseProofs,
  materialSubmissionBlockReason,
  missingExpenseMaterials,
  requiresPaymentProof,
} from './expenseProofs'
import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { reimbursementFileFixture } from '@/tests/fixtures/reimbursements'

describe('linked expense proofs', () => {
  it.each([
    ['itinerary', 'itineraryFileIds'], ['payment_proof', 'paymentProofFileIds'],
    ['hotel_bill', 'hotelBillFileIds'], ['waybill', 'waybillFileIds'], ['other', 'receiptAttachmentFileIds'],
  ] as const)('keeps valid %s files in display order and permits purged metadata only for history', (kind, field) => {
    const proof = (id: string, overrides: Partial<ReimbursementDraftFile> = {}) => reimbursementFileFixture({
      id, role: 'ATTACHMENT_ONLY', attachmentKind: kind, ...overrides,
    })
    const first = proof('first'), second = proof('second')
    const purged = proof('purged', { status: 'PURGED' })
    const files = [first, second, purged,
      proof('pending', { materialClassification: { status: 'needs_confirmation', kind: 'unknown', reason: null, pageCount: 1 } }),
      proof('wrong-role', { role: 'EXPENSE_SOURCE' }),
      proof('wrong-kind', { attachmentKind: kind === 'other' ? 'payment_proof' : 'other' }),
      proof('deleting', { status: 'DELETING' }), proof('unlinked')]
    const ids = ['second', 'first', 'second', 'purged', 'pending', 'wrong-role', 'wrong-kind', 'deleting', 'missing']
    const item = { [field]: ids }
    expect(linkedExpenseProofs(item, files, kind)).toEqual([first, second])
    expect(linkedExpenseProofs(item, files, kind, { allowPurged: true })).toEqual([first, second, purged])
    expect(item[field]).toEqual(ids)
    expect(linkedExpenseProofs({}, files, kind)).toEqual([])
  })
})

describe('rail expense applicability', () => {
  it.each([
    { category: 'rail_fare', transportType: 'other', general: false, expected: true },
    { category: 'rail_fare', transportType: 'other', general: true, expected: true },
    { category: 'local_transport', transportType: 'rail', general: false, expected: false },
    { category: 'local_transport', transportType: 'rail', general: true, expected: true },
    { category: 'local_transport', transportType: 'taxi', general: true, expected: false },
    { category: 'local_transport', transportType: undefined, general: true, expected: false },
    { category: 'other', transportType: 'rail', general: true, expected: false },
  ] as const)('applies railway ticket rules to $category/$transportType, general=$general: $expected', ({ category, transportType, general, expected }) => {
    expect(isRailExpense({ category, transportType }, general)).toBe(expected)
  })
})

describe('payment proof requirement', () => {
  it.each([
    ['500.00', 'hotel', 'unknown', false], ['500.01', 'hotel', 'unknown', true],
    ['600.00', 'rail_fare', 'high_speed', false], ['600.00', 'rail_fare', 'regular', true],
    ['600.00', 'rail_fare', 'emu', true], ['600.00', 'hotel', 'high_speed', true],
    ['', 'hotel', 'unknown', false],
  ] as const)('requires proof for %s %s %s: %s', (amount, category, railType, expected) => {
    expect(requiresPaymentProof({ amount, category, railType })).toBe(expected)
  })

  it('exempts manual aggregates and keeps source receipts subject to the single-ticket rule', () => {
    const item = { amount: '12000.00', category: 'local_transport', receiptCount: 20 }
    expect(requiresPaymentProof(item)).toBe(false)
    expect(requiresPaymentProof({ ...item, receiptCount: 1 })).toBe(true)
    expect(requiresPaymentProof({ ...item, sourceFileId: 'invoice' })).toBe(true)
    expect(requiresPaymentProof({ ...item, source: 'ocr' })).toBe(true)
  })

  it.each([
    { subjectValue: undefined, transportType: 'rail', railType: 'high_speed', expected: true },
    { subjectValue: 'synthetic-transport', transportType: 'rail', railType: 'high_speed', expected: false },
    { subjectValue: 'synthetic-transport', transportType: 'rail', railType: 'emu', expected: true },
    { subjectValue: 'synthetic-transport', transportType: 'taxi', railType: 'high_speed', expected: true },
  ] as const)('keeps the high-speed exemption scoped to the chosen expense subject and transport: %j', ({ expected, ...item }) => {
    expect(requiresPaymentProof({ ...item, amount: '600.00', category: 'local_transport' })).toBe(expected)
  })

  it('does not request itineraries for taxis with a stale legacy flag', () => {
    expect(missingExpenseMaterials({ amount: '20.00', transportType: 'taxi', requiresItinerary: true } as ExpenseItem, [])).toEqual([])
  })
})

describe('hotel stay details', () => {
  const bill = { id: 'hotel-1', status: 'ACTIVE', role: 'ATTACHMENT_ONLY', attachmentKind: 'hotel_bill' } as ReimbursementDraftFile
  const expense = (amount: string) => ({ category: 'lodging', amount, hotelBillFileIds: ['hotel-1'] }) as ExpenseItem
  it('requires details at any amount and independent payment proof above 500', () => {
    expect(missingExpenseMaterials(expense('1.00'), [])).toEqual(['住宿明细'])
    expect(missingExpenseMaterials(expense('500.00'), [bill])).toEqual([])
    expect(missingExpenseMaterials(expense('500.01'), [bill])).toEqual(['付款凭证'])
  })
  it('rejects deleted, wrong-purpose and unconfirmed files', () => {
    for (const file of [
      { ...bill, status: 'PURGED' }, { ...bill, attachmentKind: 'payment_proof' },
      { ...bill, materialClassification: { status: 'needs_confirmation' } },
    ]) expect(missingExpenseMaterials(expense('20.00'), [file as ReimbursementDraftFile])).toContain('住宿明细')
  })
  it('allows manually confirmed details despite failed OCR and sharing', () => {
    const manual = { ...bill, ocrStatus: 'FAILED', materialClassification: { status: 'confirmed' } } as ReimbursementDraftFile
    expect(missingExpenseMaterials(expense('20.00'), [manual])).toEqual([])
    expect(missingExpenseMaterials(expense('40.00'), [manual])).toEqual([])
  })
})

describe('material submission readiness', () => {
  const file = (overrides: Partial<ReimbursementDraftFile> = {}) => ({
    id: 'file-1',
    name: '材料.pdf',
    role: 'ATTACHMENT_ONLY',
    attachmentKind: 'other',
    sortOrder: 0,
    status: 'ACTIVE',
    mediaType: 'application/pdf',
    sizeBytes: 128,
    ocrStatus: 'COMPLETE',
    ocrResult: null,
    ...overrides,
  }) as ReimbursementDraftFile

  it.each([
    [{ status: 'WRITING' }, '文件仍在上传'],
    [{ ocrStatus: 'RUNNING' }, '材料仍在识别'],
    [{ materialClassification: {
      status: 'needs_confirmation', kind: 'unknown', reason: null, pageCount: 1,
    } }, '材料用途待确认'],
    [{ role: 'EXPENSE_SOURCE', ocrStatus: 'NOT_REQUESTED' }, '票据尚未识别'],
    [{ role: 'EXPENSE_SOURCE', ocrStatus: 'FAILED' }, '票据尚未加入费用明细'],
  ] as const)('identifies a blocking file: %o', (overrides, reason) => {
    expect(materialSubmissionBlockReason(file(overrides), [], [])).toBe(reason)
  })

  it('does not block confirmed optional materials or disposed expense sources', () => {
    expect(materialSubmissionBlockReason(file(), [], [])).toBe('')
    const source = file({ role: 'EXPENSE_SOURCE', ocrStatus: 'COMPLETE' })
    expect(materialSubmissionBlockReason(source, [{ sourceFileId: source.id }], [])).toBe('')
    expect(materialSubmissionBlockReason(source, [], [source.id])).toBe('')
  })

  it.each([
    ['itinerary', 'itineraryFileIds', '行程单'],
    ['payment_proof', 'paymentProofFileIds', '付款凭证'],
    ['hotel_bill', 'hotelBillFileIds', '住宿明细'],
  ] as const)('requires %s to have an explicit expense association', (kind, field, label) => {
    const proof = file({ attachmentKind: kind })
    expect(materialSubmissionBlockReason(proof, [], [])).toBe(`${label}尚未关联费用`)
    expect(materialSubmissionBlockReason(proof, [], [proof.id])).toBe(`${label}尚未关联费用`)
    expect(materialSubmissionBlockReason(proof, [{ [field]: [proof.id] }], [])).toBe('')
    expect(materialSubmissionBlockReason({ ...proof, attachmentKind: 'other' }, [], [])).toBe('')
    expect(materialSubmissionBlockReason({ ...proof, status: 'PURGED' }, [], [])).toBe('')
    expect(materialSubmissionBlockReason({ ...proof, ocrStatus: 'RUNNING' }, [], [])).toBe('材料仍在识别')
  })
})

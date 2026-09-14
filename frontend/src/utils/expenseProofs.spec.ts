import { describe, expect, it } from 'vitest'

import {
  materialSubmissionBlockReason,
  missingExpenseMaterials,
  requiresPaymentProof,
} from './expenseProofs'
import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraftFile } from '@/types/reimbursements'

describe('payment proof requirement', () => {
  it.each([
    ['500.00', 'hotel', 'unknown', false], ['500.01', 'hotel', 'unknown', true],
    ['600.00', 'rail_fare', 'high_speed', false], ['600.00', 'rail_fare', 'regular', true],
    ['600.00', 'rail_fare', 'emu', true], ['600.00', 'hotel', 'high_speed', true],
    ['', 'hotel', 'unknown', false],
  ] as const)('requires proof for %s %s %s: %s', (amount, category, railType, expected) => {
    expect(requiresPaymentProof({ amount, category, railType })).toBe(expected)
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
})

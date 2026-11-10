import { describe, expect, it } from 'vitest'

import type { ExpenseItem } from '@/types/expenses'
import type { ItineraryOcrResult, OcrReceiptCandidate } from '@/types/receipts'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { expenseReviewHints, expenseReviewWarnings } from './expenseReview'

function invoice(id: string, changes: Partial<OcrReceiptCandidate> = {}): ReimbursementDraftFile {
  return {
    id, name: `${id}.pdf`, role: 'EXPENSE_SOURCE', attachmentKind: 'other', status: 'ACTIVE',
    sortOrder: 0, mediaType: 'application/pdf', sizeBytes: 10, ocrStatus: 'COMPLETE',
    ocrResult: {
      fileId: id, type: 'ride_hailing', categoryId: 'local_transport', categoryName: '市内交通',
      date: '2026-09-01', description: '合肥机场至滨湖酒店', amount: '60.00', transportType: 'ride_hailing',
      receiptCount: 1, source: 'ocr', confidence: '0.95', warnings: [], status: 'recognized', error: null,
      invoiceNumbers: [], orderNumbers: [], ...changes,
    },
  }
}
function item(sourceFileId: string, changes: Partial<ExpenseItem> = {}): ExpenseItem {
  return {
    id: `ocr-${sourceFileId}`, sourceFileId, source: 'ocr', category: 'local_transport',
    date: '2026-09-01', displayDate: '2026-09-01', description: '合肥机场至滨湖酒店', amount: '60.00',
    receiptCount: 1, transportType: 'ride_hailing', itineraryFileIds: [], ...changes,
  }
}
function itinerary(id: string, changes: Partial<ItineraryOcrResult> = {}): ReimbursementDraftFile {
  return {
    id, name: `${id}.pdf`, role: 'ATTACHMENT_ONLY', attachmentKind: 'itinerary', status: 'ACTIVE',
    sortOrder: 1, mediaType: 'application/pdf', sizeBytes: 10, ocrStatus: 'COMPLETE',
    ocrResult: {
      fileId: id, version: 1, kind: 'itinerary', status: 'recognized', source: 'pdf_text', pageCount: 1,
      processedPageCount: 1, complete: true, warnings: [], error: null,
      summary: { currency: 'CNY', amount: '60.00', startDate: '2026-09-01', endDate: '2026-09-01', invoiceNumbers: ['INV-001'], orderNumbers: [] },
      trips: [{ page: 1, row: 1, date: '2026-09-01', amount: '60.00', origin: '合肥机场', destination: '滨湖酒店', invoiceNumbers: [], orderNumbers: [] }],
      ...changes,
    },
  }
}

describe('current expense review evidence', () => {
  function linked(sourceWarnings = ['INVOICE_DATE_USED_AS_OCCURRENCE']) {
    const source = invoice('invoice', { warnings: sourceWarnings })
    const expense = item('invoice', { itineraryFileIds: ['proof'], itineraryAutoMatchDisabled: true,
      description: '合肥机场 → 滨湖酒店', warnings: [...sourceWarnings, 'MANUAL_REVIEW_REQUIRED'] })
    return { source, expense, proof: itinerary('proof') }
  }

  it('keeps an unconfirmed date warning and restores it when its unchanged-date proof is removed', () => {
    const { source, expense, proof } = linked()
    expense.itineraryAutoMatchDisabled = false
    expect(expenseReviewWarnings(expense, [source, proof])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
    expense.itineraryAutoMatchDisabled = true
    expense.warnings = expenseReviewWarnings(expense, [source, proof])
    expect(expense.warnings).toEqual([])
    expense.itineraryFileIds = []
    expect(expenseReviewWarnings(expense, [source, proof])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it.each([
    'LOW_OCR_CONFIDENCE', 'QR_AMOUNT_MISMATCH', 'QR_AMOUNT_REQUIRES_REVIEW',
    'MANUAL_REVIEW_REQUIRED', 'UNRECOGNIZED_REVIEW_REASON',
  ])('preserves independent %s evidence after a date is resolved and runtime warnings are cleared', code => {
    const { source, expense, proof } = linked(['INVOICE_DATE_USED_AS_OCCURRENCE', code])
    expense.warnings = []
    const original = JSON.stringify(source)
    expect(expenseReviewWarnings(expense, [source, proof])).toEqual([code])
    expect(expenseReviewHints(expense, [source, proof])).toHaveLength(1)
    expect(JSON.stringify(source)).toBe(original)
  })

  it.each([
    { complete: false }, { warnings: ['ITINERARY_TOTAL_CONFLICT'] },
    { status: 'failed' as const }, { processedPageCount: 0 }, { pageCount: 0, processedPageCount: 0 },
  ])('does not use incomplete itinerary evidence to clear date warnings: %j', changes => {
    const { source, expense } = linked()
    expect(expenseReviewWarnings(expense, [source, itinerary('proof', changes)])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it('does not trust pending, unavailable, wrong-file or unconfirmed-purpose evidence', () => {
    const { source, expense, proof } = linked()
    for (const changes of [
      { ocrStatus: 'RUNNING' as const }, { status: 'PURGED' as const }, { ocrStale: true },
      { materialClassification: { status: 'needs_confirmation' as const, kind: 'unknown' as const, reason: null, pageCount: 1 } },
      { ocrResult: { ...proof.ocrResult!, fileId: 'another-file' } },
    ]) expect(expenseReviewWarnings(expense, [source, { ...proof, ...changes }])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it('retains a source warning when invoice identifiers contradict the selected proof', () => {
    const { source, expense, proof } = linked()
    ;(source.ocrResult as OcrReceiptCandidate).invoiceNumbers = ['OTHER-INVOICE']
    const warnings = expenseReviewWarnings(expense, [source, proof])
    expect(warnings).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
    expect(warnings).toContain('ITINERARY_IDENTIFIER_MISMATCH')
    expect(expenseReviewHints(expense, [source, proof]).find(hint => hint.code === 'ITINERARY_IDENTIFIER_MISMATCH')?.field).toBe('itinerary')
  })

  it.each(['invoiceNumbers', 'orderNumbers'] as const)('does not let a matching file hide another file with conflicting %s', field => {
    const { source, expense } = linked()
    ;(source.ocrResult as OcrReceiptCandidate)[field] = ['NUMBER-A']
    const proofs = ['NUMBER-A', 'NUMBER-B'].map((number, index) => {
      const file = itinerary(`proof-${index}`)
      const result = file.ocrResult as ItineraryOcrResult
      result.summary[field] = [number]
      result.summary.amount = result.trips[0]!.amount = '30.00'
      return file
    })
    expense.itineraryFileIds = proofs.map(file => file.id)
    expect(expenseReviewWarnings(expense, [source, ...proofs])).toEqual(expect.arrayContaining([
      'INVOICE_DATE_USED_AS_OCCURRENCE', 'ITINERARY_IDENTIFIER_MISMATCH',
    ]))
    // One shared PDF can legitimately contain several invoice/order numbers.
    const shared = proofs[0]!.ocrResult as ItineraryOcrResult
    shared.summary[field] = ['NUMBER-A', 'NUMBER-B']
    shared.summary.amount = shared.trips[0]!.amount = '60.00'
    expense.itineraryFileIds = [proofs[0]!.id]
    expect(expenseReviewWarnings(expense, [source, ...proofs])).toEqual([])
  })

  it('does not infer a date or route edit from a newer OCR result', () => {
    const source = invoice('invoice', { date: '2026-09-02', description: '新识别说明',
      warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'MISSING_ROUTE'] })
    const expense = item('invoice')
    expect(expenseReviewWarnings(expense, [source])).toEqual(['INVOICE_DATE_USED_AS_OCCURRENCE', 'MISSING_ROUTE'])
  })

  it('shows real amount and date differences at their fields, and clears only the corrected difference', () => {
    const { source, expense, proof } = linked()
    expense.amount = '59.00'
    expense.date = expense.displayDate = '2026-09-02'
    expense.dateEdited = true
    expect(expenseReviewHints(expense, [source, proof])).toEqual(expect.arrayContaining([
      expect.objectContaining({ code: 'ITINERARY_AMOUNT_MISMATCH', field: 'amount' }),
      expect.objectContaining({ code: 'ITINERARY_DATE_MISMATCH', field: 'date' }),
    ]))
    expense.amount = '60.00'
    expect(expenseReviewWarnings(expense, [source, proof])).toEqual(['ITINERARY_DATE_MISMATCH'])
    expense.date = expense.displayDate = '2026-09-01'
    expect(expenseReviewHints(expense, [source, proof])).toEqual([])
  })

  it('does not compare a shared multi-trip document total with a single expense', () => {
    const { source, expense, proof } = linked()
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.amount = '120.00'
    result.trips.push({ ...result.trips[0]!, row: 2 })
    expect(expenseReviewWarnings(expense, [source, proof])).not.toContain('ITINERARY_AMOUNT_MISMATCH')
  })

  it('supports several single-trip files without taking the first file as the whole amount', () => {
    const { source, expense } = linked()
    const files = ['40.00', '20.00'].map((amount, index) => {
      const proof = itinerary(`proof-${index}`)
      const result = proof.ocrResult as ItineraryOcrResult
      result.summary.amount = result.trips[0]!.amount = amount
      return proof
    })
    expense.itineraryFileIds = files.map(file => file.id)
    expect(expenseReviewWarnings(expense, [source, ...files])).toEqual([])
    expense.amount = '59.00'
    expect(expenseReviewWarnings(expense, [source, ...files])).toEqual(['ITINERARY_AMOUNT_MISMATCH'])
  })

  it('keeps unresolved dates for a document covering different days', () => {
    const { source, expense, proof } = linked()
    ;(proof.ocrResult as ItineraryOcrResult).trips.push({ ...(proof.ocrResult as ItineraryOcrResult).trips[0]!, row: 2, date: '2026-09-02' })
    expect(expenseReviewWarnings(expense, [source, proof])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it('does not double-count duplicate file references or hide their original date warning', () => {
    const { source, expense, proof } = linked()
    expense.itineraryFileIds = [proof.id, proof.id]
    expect(expenseReviewWarnings(expense, [source, proof])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it('keeps the CNY confirmation for foreign evidence even when optional currency details are corrected or cleared', () => {
    const source = invoice('invoice', { type: 'foreign_receipt', originalCurrency: 'USD', warnings: ['FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'] })
    const expense = item('invoice', { originalDetailsEdited: true, originalCurrency: 'CNY', requiresCnyConfirmation: false,
      warnings: ['FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'] })
    expect(expenseReviewWarnings(expense, [source])).toContain('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
    delete expense.originalCurrency
    expect(expenseReviewWarnings(expense, [source])).toContain('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
    expense.originalCurrency = 'USD'
    expense.requiresCnyConfirmation = true
    expect(expenseReviewWarnings(expense, [source])).toContain('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
    expense.cnyAmountConfirmed = true
    expect(expenseReviewWarnings(expense, [source])).not.toContain('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
  })

  it('clears only explicitly edited date and description reminders', () => {
    const source = invoice('invoice', { warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'MISSING_ROUTE', 'LOW_OCR_CONFIDENCE'] })
    const expense = item('invoice', { dateEdited: true })
    expect(expenseReviewWarnings(expense, [source])).toEqual(['MISSING_ROUTE', 'LOW_OCR_CONFIDENCE'])
    expense.descriptionEdited = true
    expect(expenseReviewWarnings(expense, [source])).toEqual(['LOW_OCR_CONFIDENCE'])
    expense.date = expense.displayDate = ''
    expect(expenseReviewWarnings(expense, [source])).toContain('MISSING_DATE')
  })

  it('does not revive a transient foreign warning when durable evidence and saved values are domestic', () => {
    const source = invoice('invoice', { originalCurrency: 'CNY' })
    const expense = item('invoice', { warnings: ['FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'] })
    expect(expenseReviewWarnings(expense, [source])).toEqual([])
  })

  it('reuses frozen metadata after submitted file bytes are purged', () => {
    const { source, expense, proof } = linked()
    source.status = proof.status = 'PURGED'
    expect(expenseReviewWarnings(expense, [source, proof])).toEqual([])
  })

  it('does not mark valid display-date ranges as missing, or accept an invalid calendar date', () => {
    const expense = item('invoice', { displayDate: '2026-09-01 至 2026-09-02' })
    expect(expenseReviewWarnings(expense, [invoice('invoice')])).not.toContain('MISSING_DATE')
    expense.date = expense.displayDate = '2026-02-31'
    expect(expenseReviewWarnings(expense, [invoice('invoice')])).toContain('MISSING_DATE')
    expense.date = '2026-09-01'
    expense.displayDate = ''
    expect(expenseReviewWarnings(expense, [invoice('invoice')])).toContain('MISSING_DATE')
  })

  it('accepts old itinerary metadata without optional number lists and rejects missing recognition warnings', () => {
    const { source, expense, proof } = linked()
    const result = proof.ocrResult as ItineraryOcrResult
    delete (result.summary as Partial<typeof result.summary>).invoiceNumbers
    delete (result.trips[0] as Partial<typeof result.trips[number]>).orderNumbers
    expect(expenseReviewWarnings(expense, [source, proof])).toEqual([])
    delete (result as Partial<ItineraryOcrResult>).warnings
    expect(expenseReviewWarnings(expense, [source, proof])).toContain('INVOICE_DATE_USED_AS_OCCURRENCE')
  })

  it('shows the most specific date and QR reminder without losing the underlying evidence', () => {
    const source = invoice('invoice', { warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'QR_ISSUE_DATE_USED', 'QR_AMOUNT_REQUIRES_REVIEW', 'QR_AMOUNT_MISMATCH'] })
    const expense = item('invoice')
    expect(expenseReviewHints(expense, [source]).map(hint => hint.code)).toEqual(['QR_ISSUE_DATE_USED', 'QR_AMOUNT_MISMATCH'])
    expect(expenseReviewWarnings(expense, [source])).toHaveLength(4)
    expense.displayDate = ''
    expect(expenseReviewHints(expense, [source]).filter(hint => hint.field === 'date').map(hint => hint.code)).toEqual(['MISSING_DATE'])
  })
})

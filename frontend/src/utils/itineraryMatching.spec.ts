import { describe, expect, it } from 'vitest'

import type { ExpenseItem } from '@/types/expenses'
import type { ItineraryOcrResult, OcrReceiptCandidate } from '@/types/receipts'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { matchItineraries, suggestItineraries } from './itineraryMatching'

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

describe('conservative itinerary matching', () => {
  it('never matches or recommends an itinerary for a confirmed taxi', () => {
    const source = invoice('invoice', { transportType: 'taxi', invoiceNumbers: ['INV-001'] })
    const taxi = item('invoice', { transportType: 'taxi' })
    expect(matchItineraries([taxi], [source, itinerary('proof')])).toEqual([])
    expect(suggestItineraries([taxi], [source, itinerary('proof')])).toEqual([])
    Object.assign(source.ocrResult!, { type: 'invoice', description: null, warnings: ['MISSING_ROUTE'] })
    expect(suggestItineraries([taxi], [source, itinerary('proof')])).toEqual([])
  })

  it('uses an explicit unique invoice number before route/amount evidence', () => {
    const files = [invoice('invoice', { invoiceNumbers: [' inv-001 '], amount: '99.00' }), itinerary('proof')]
    expect(matchItineraries([item('invoice', { amount: '99.00', description: '打车' })], files))
      .toEqual([{ sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'identifier' }])
  })

  it('matches by amount, occurrence date and both route endpoints only when unique', () => {
    const files = [invoice('invoice'), itinerary('proof')]
    expect(matchItineraries([item('invoice')], files)).toEqual([{ sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'amount_date_route' }])
    expect(matchItineraries([item('invoice', { description: '市内打车' })], files)).toEqual([])
    expect(matchItineraries([item('invoice', { date: '2026-09-02' })], files)).toEqual([])
  })

  it.each(['|', '｜'])('ignores address separator %s without relaxing amount, date or direction checks', (separator) => {
    const proof = itinerary('proof')
    ;(proof.ocrResult as ItineraryOcrResult).trips[0]!.destination = `滨湖区${separator}滨湖酒店`
    const files = [invoice('invoice'), proof]
    const expense = item('invoice', { description: '合肥机场至滨湖区滨湖酒店' })
    expect(matchItineraries([expense], files)).toEqual([
      { sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'amount_date_route' },
    ])
    for (const changes of [
      { amount: '60.01' }, { date: '2026-09-02' }, { description: '滨湖区滨湖酒店至合肥机场' },
    ]) expect(matchItineraries([{ ...expense, ...changes }], files)).toEqual([])
  })

  it('rejects duplicate proof candidates and duplicate invoice candidates', () => {
    const source = invoice('invoice', { invoiceNumbers: ['INV-001'] })
    expect(matchItineraries([item('invoice')], [source, itinerary('proof-a'), itinerary('proof-b')])).toEqual([])
    expect(matchItineraries([item('invoice'), item('duplicate')], [source, invoice('duplicate', { invoiceNumbers: ['INV-001'] }), itinerary('proof')])).toEqual([])
    expect(matchItineraries([item('invoice'), item('duplicate')], [invoice('invoice'), invoice('duplicate'), itinerary('proof')])).toEqual([])
  })

  it('rejects reversed, overlapping or repeated endpoints even for one candidate', () => {
    const files = [invoice('invoice'), itinerary('proof')]
    expect(matchItineraries([item('invoice', { description: '滨湖酒店至合肥机场' })], files)).toEqual([])
    expect(matchItineraries([item('invoice', { description: '合肥机场至滨湖酒店再返回合肥机场' })], files)).toEqual([])
    const proof = itinerary('proof')
    const result = proof.ocrResult as ItineraryOcrResult
    result.trips[0]!.destination = '机场'
    expect(matchItineraries([item('invoice')], [files[0]!, proof])).toEqual([])
  })

  it('rejects conflicting comparable identifiers instead of falling back or accepting another shared identifier', () => {
    const proof = itinerary('proof')
    expect(matchItineraries([item('invoice')], [invoice('invoice', { invoiceNumbers: ['INV-002'] }), proof])).toEqual([])
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.orderNumbers = ['ORDER-001']
    expect(matchItineraries([item('invoice')], [invoice('invoice', { invoiceNumbers: ['INV-002'], orderNumbers: ['ORDER-001'] }), proof])).toEqual([])
    expect(matchItineraries([item('invoice')], [invoice('invoice', { invoiceNumbers: ['INV-001'], orderNumbers: ['ORDER-002'] }), proof])).toEqual([])
    expect(matchItineraries([item('invoice')], [invoice('invoice', { orderNumbers: ['ORDER-002'] }), proof])).toEqual([])
    expect(matchItineraries([item('invoice')], [invoice('invoice'), proof])).toHaveLength(1)
    expect(matchItineraries([item('invoice')], [invoice('invoice', { invoiceNumbers: ['INV-001'] }), proof]))
      .toEqual([{ sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'identifier' }])
  })

  it('never overwrites an existing link or matches non-taxi, partial, failed, foreign or warned evidence', () => {
    const source = invoice('invoice', { invoiceNumbers: ['INV-001'] })
    expect(matchItineraries([item('invoice', { itineraryFileIds: ['manual-proof'] })], [source, itinerary('proof')])).toEqual([])
    expect(matchItineraries([item('invoice', { itineraryAutoMatchDisabled: true })], [source, itinerary('proof')])).toEqual([])
    expect(matchItineraries([item('invoice', { itineraryAutoMatchDisabled: false })], [source, itinerary('proof')])).toHaveLength(1)
    expect(matchItineraries([item('invoice', { transportType: 'rail' })], [source, itinerary('proof')])).toEqual([])
    for (const changes of [{ complete: false }, { status: 'failed' as const }, { warnings: ['ITINERARY_TOTAL_CONFLICT'] }]) {
      expect(matchItineraries([item('invoice')], [source, itinerary('proof', changes)])).toEqual([])
    }
    const proof = itinerary('proof')
    proof.attachmentKind = 'payment_proof'
    expect(matchItineraries([item('invoice')], [source, proof])).toEqual([])
    const foreign = itinerary('proof')
    ;(foreign.ocrResult as ItineraryOcrResult).summary.currency = 'USD'
    expect(matchItineraries([item('invoice')], [invoice('invoice'), foreign])).toEqual([])
  })

  it('supports multiple expenses referring to distinct trips in one itinerary regardless of file order', () => {
    const first = invoice('invoice-a', { orderNumbers: ['ORDER-A'] })
    const second = invoice('invoice-b', { orderNumbers: ['ORDER-B'] })
    const proof = itinerary('proof')
    const result = proof.ocrResult as ItineraryOcrResult
    result.trips = [
      { ...result.trips[0]!, orderNumbers: ['ORDER-A'] },
      { ...result.trips[0]!, row: 2, orderNumbers: ['ORDER-B'] },
    ]
    const expected = [
      { sourceFileId: 'invoice-a', itineraryFileId: 'proof', basis: 'identifier' },
      { sourceFileId: 'invoice-b', itineraryFileId: 'proof', basis: 'identifier' },
    ]
    expect(matchItineraries([item('invoice-a'), item('invoice-b')], [first, second, proof])).toEqual(expected)
    expect(matchItineraries([item('invoice-a'), item('invoice-b')], [proof, first, second])).toEqual(expected)
  })
})

describe('amount-only suggestions for incomplete passenger invoices', () => {
  function incompleteInvoice(id = 'invoice', changes: Partial<OcrReceiptCandidate> = {}) {
    return invoice(id, { type: 'invoice', description: null,
      warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'MANUAL_REVIEW_REQUIRED'], ...changes })
  }
  function incompleteItem(id = 'invoice', changes: Partial<ExpenseItem> = {}) {
    return item(id, { description: '市内交通', ...changes })
  }
  function earlierTrip(id = 'proof') {
    const proof = itinerary(id)
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.startDate = result.summary.endDate = '2026-08-13'
    result.trips[0]!.date = '2026-08-13'
    return proof
  }

  it('offers a unique equal amount only as a proposal, with missing fields to fill after confirmation', () => {
    const items = [incompleteItem()]
    const files = [incompleteInvoice(), earlierTrip()]
    const before = JSON.stringify({ items, files })
    expect(matchItineraries(items, files)).toEqual([])
    expect(suggestItineraries(items, files)).toEqual([expect.objectContaining({
      sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'amount_only',
      fill: { date: '2026-08-13', displayDate: '2026-08-13', description: '合肥机场 → 滨湖酒店' },
    })])
    expect(JSON.stringify({ items, files })).toBe(before)
  })

  it('leaves manual edits intact and fills only a missing date or the OCR placeholder', () => {
    const files = [incompleteInvoice(), earlierTrip()]
    const manual = incompleteItem('invoice', { date: '2026-08-12', displayDate: '2026-08-12', description: '人工填写的路线' })
    expect(suggestItineraries([manual], files)[0]?.fill).toEqual({})
    expect(suggestItineraries([incompleteItem('invoice', { date: undefined, displayDate: '', description: '' })], files)[0]?.fill)
      .toEqual({ date: '2026-08-13', displayDate: '2026-08-13', description: '合肥机场 → 滨湖酒店' })
    expect(suggestItineraries([incompleteItem('invoice', { description: '手动说明' })], files)[0]?.fill)
      .not.toHaveProperty('description')
  })

  it('does not guess among duplicate invoices, duplicate proofs, or occupied proofs', () => {
    const source = incompleteInvoice()
    expect(suggestItineraries([incompleteItem()], [source, earlierTrip('a'), earlierTrip('b')])).toEqual([])
    for (const changes of [{}, { itineraryAutoMatchDisabled: true }, { itineraryFileIds: ['other'] }]) {
      expect(suggestItineraries([incompleteItem(), incompleteItem('other', changes)],
        [source, incompleteInvoice('other'), earlierTrip()])).toEqual([])
    }
    const linked = item('manual', { source: 'manual', sourceFileId: undefined, amount: '10.00', itineraryFileIds: ['proof'] })
    expect(suggestItineraries([incompleteItem(), linked], [source, earlierTrip()])).toEqual([])
  })

  it('rejects conflicts, uncertain amounts, non-invoices and inactive sources', () => {
    for (const changes of [
      { invoiceNumbers: ['INV-OTHER'] }, { warnings: ['LOW_OCR_CONFIDENCE'] },
      { warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'QR_AMOUNT_MISMATCH'] },
      { originalCurrency: 'USD' }, { amount: null }, { type: 'taxi_receipt' },
      { transportType: 'rail' as const }, { status: 'failed' as const }, { fileId: 'wrong' },
    ]) expect(suggestItineraries([incompleteItem()], [incompleteInvoice('invoice', changes), earlierTrip()])).toEqual([])
    for (const changes of [
      { amount: '0' }, { amount: '60.01' }, { itineraryAutoMatchDisabled: true },
      { itineraryFileIds: ['manual'] }, { category: 'lodging' }, { originalCurrency: 'USD' },
    ]) expect(suggestItineraries([incompleteItem('invoice', changes)], [incompleteInvoice(), earlierTrip()])).toEqual([])
    const source = incompleteInvoice()
    source.ocrStatus = 'RUNNING'
    expect(suggestItineraries([incompleteItem()], [source, earlierTrip()])).toEqual([])
  })

  it('requires a complete, unambiguous single-trip document with the same total', () => {
    for (const changes of [{ complete: false }, { warnings: ['ITINERARY_TOTAL_CONFLICT'] }, { processedPageCount: 0 }]) {
      expect(suggestItineraries([incompleteItem()], [incompleteInvoice(), itinerary('proof', changes)])).toEqual([])
    }
    for (const field of ['currency', 'amount'] as const) {
      const proof = earlierTrip()
      ;(proof.ocrResult as ItineraryOcrResult).summary[field] = null
      expect(suggestItineraries([incompleteItem()], [incompleteInvoice(), proof])).toEqual([])
    }
    const proof = earlierTrip()
    const result = proof.ocrResult as ItineraryOcrResult
    result.trips.push({ ...result.trips[0]!, row: 2 })
    expect(suggestItineraries([incompleteItem()], [incompleteInvoice(), proof])).toEqual([])
  })

  it('suggests a whole multi-trip document only when all rows sum to the invoice, without filling a single trip', () => {
    const proof = earlierTrip()
    const result = proof.ocrResult as ItineraryOcrResult
    result.trips = [
      { ...result.trips[0]!, amount: '25.00' },
      { ...result.trips[0]!, row: 2, date: '2026-08-14', amount: '35.00' },
    ]
    result.summary.endDate = '2026-08-14'
    const suggestions = suggestItineraries([incompleteItem()], [incompleteInvoice(), proof])
    expect(suggestions).toHaveLength(1)
    expect(suggestions[0]).toMatchObject({ basis: 'amount_only', fill: {},
      documentSummary: { amount: '60.00', tripCount: 2, startDate: '2026-08-13', endDate: '2026-08-14' } })
    expect(suggestions[0]?.trip).toBeUndefined()
    expect(suggestItineraries([incompleteItem('invoice', { description: '人工备注', date: '2026-08-20' })],
      [incompleteInvoice(), proof])[0]?.fill).toEqual({})
    const duplicate = structuredClone(proof)
    duplicate.id = 'duplicate'
    ;(duplicate.ocrResult as ItineraryOcrResult).fileId = 'duplicate'
    expect(suggestItineraries([incompleteItem()], [incompleteInvoice(), proof, duplicate])).toEqual([])
    expect(suggestItineraries([incompleteItem(), item('used', { amount: '10.00', itineraryFileIds: ['proof'] })],
      [incompleteInvoice(), proof])).toEqual([])
    result.trips[1]!.invoiceNumbers = ['CONFLICTING']
    expect(suggestItineraries([incompleteItem()], [incompleteInvoice('invoice', { invoiceNumbers: ['ORIGINAL'] }), proof])).toEqual([])
    result.trips[1]!.invoiceNumbers = []
    result.trips[1]!.amount = '34.99'
    expect(suggestItineraries([incompleteItem()], [incompleteInvoice(), proof])).toEqual([])
  })
})

describe('uncertain passenger invoice suggestions', () => {
  function uncertainInvoice(id = 'invoice', changes: Partial<OcrReceiptCandidate> = {}) {
    return invoice(id, { type: 'invoice', transportType: 'other', ...changes })
  }
  function uncertainItem(id = 'invoice', changes: Partial<ExpenseItem> = {}) {
    return item(id, { transportType: 'other', ...changes })
  }

  it('proposes an exact unique trip for an uncertain invoice without changing its type or links', () => {
    const items = [uncertainItem()]
    const files = [uncertainInvoice(), itinerary('proof')]
    const before = JSON.stringify({ items, files })
    expect(matchItineraries(items, files)).toEqual([])
    expect(suggestItineraries(items, files)).toEqual([{
      sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'amount_date_route',
      transportType: 'ride_hailing', trip: (files[1]!.ocrResult as ItineraryOcrResult).trips[0],
    }])
    expect(JSON.stringify({ items, files })).toBe(before)
    expect(suggestItineraries([uncertainItem('invoice', { transportType: undefined })],
      [uncertainInvoice('invoice', { transportType: undefined }), itinerary('proof')])).toHaveLength(1)
  })

  it('does not propose on amount alone, reversed routes, date mismatch, or invalid amounts', () => {
    const files = [uncertainInvoice(), itinerary('proof')]
    for (const changes of [
      { description: '旅客运输服务' }, { description: '滨湖酒店至合肥机场' },
      { description: '合肥机场至滨湖酒店再回合肥机场' }, { date: '2026-09-02' },
      { amount: '' }, { amount: '0.00' }, { amount: '60.01' },
    ]) expect(suggestItineraries([uncertainItem('invoice', changes)], files)).toEqual([])
  })

  it('requires a unique trip in both directions, including already linked or confirmed taxi competitors', () => {
    expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), itinerary('a'), itinerary('b')])).toEqual([])
    for (const competitor of [uncertainItem('other'), item('other'),
      item('other', { itineraryFileIds: ['proof'] }), uncertainItem('other', { itineraryAutoMatchDisabled: true })]) {
      expect(suggestItineraries([uncertainItem(), competitor],
        [uncertainInvoice(), uncertainInvoice('other'), itinerary('proof')])).toEqual([])
    }
    const proof = itinerary('proof')
    const result = proof.ocrResult as ItineraryOcrResult
    result.trips.push({ ...result.trips[0]!, row: 2 })
    expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), proof])).toEqual([])
  })

  it('never uses a shared identifier to bypass route checks and rejects conflicting invoice or order identifiers', () => {
    const proof = itinerary('proof')
    ;(proof.ocrResult as ItineraryOcrResult).summary.orderNumbers = ['ORDER-1']
    expect(suggestItineraries([uncertainItem('invoice', { description: '客运' })],
      [uncertainInvoice('invoice', { invoiceNumbers: ['INV-001'] }), proof])).toEqual([])
    for (const changes of [
      { invoiceNumbers: ['INV-002'] }, { orderNumbers: ['ORDER-2'] },
      { invoiceNumbers: ['INV-002'], orderNumbers: ['ORDER-1'] },
    ]) expect(suggestItineraries([uncertainItem()], [uncertainInvoice('invoice', changes), proof])).toEqual([])
    const rowConflict = itinerary('proof')
    const result = rowConflict.ocrResult as ItineraryOcrResult
    result.summary.invoiceNumbers = ['INV-001', 'INV-002']
    result.trips[0]!.invoiceNumbers = ['INV-002']
    expect(suggestItineraries([uncertainItem()], [uncertainInvoice('invoice', { invoiceNumbers: ['INV-001'] }), rowConflict])).toEqual([])
  })

  it('skips already linked, manually disabled, rail, air, lodging, non-invoice or non-local expenses', () => {
    const files = [uncertainInvoice(), itinerary('proof')]
    for (const changes of [
      { itineraryFileIds: ['manual'] }, { itineraryAutoMatchDisabled: true },
      { transportType: 'rail' as const }, { transportType: 'hotel' as const },
      { category: 'rail_fare' }, { category: 'air_fare' }, { category: 'other' },
      { originalCurrency: 'USD', cnyAmountConfirmed: true },
    ]) expect(suggestItineraries([uncertainItem('invoice', changes)], files)).toEqual([])
    for (const changes of [
      { type: 'train' }, { type: 'other' }, { categoryId: 'air_fare' },
      { transportType: 'rail' as const }, { originalCurrency: 'USD' },
      { status: 'failed' as const }, { fileId: 'wrong-file' },
      { warnings: ['LOW_OCR_CONFIDENCE'] }, { error: { code: 'FAILED', message: 'failed' } },
    ]) expect(suggestItineraries([uncertainItem()], [uncertainInvoice('invoice', changes), itinerary('proof')])).toEqual([])
  })

  it('rejects incomplete, warning, failed, mismatched or unknown-amount itinerary evidence', () => {
    for (const changes of [
      { complete: false }, { processedPageCount: 0 }, { pageCount: 0, processedPageCount: 0 },
      { status: 'failed' as const }, { warnings: ['ITINERARY_TOTAL_CONFLICT'] },
      { fileId: 'wrong-proof' }, { error: { code: 'FAILED', message: 'failed' } },
    ]) expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), itinerary('proof', changes)])).toEqual([])
    for (const summaryChange of [{ currency: null }, { currency: 'USD' }, { amount: null }, { amount: '0' }]) {
      const proof = itinerary('proof')
      Object.assign((proof.ocrResult as ItineraryOcrResult).summary, summaryChange)
      expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), proof])).toEqual([])
    }
    const source = uncertainInvoice()
    source.ocrStatus = 'RUNNING'
    expect(suggestItineraries([uncertainItem()], [source, itinerary('proof')])).toEqual([])
    const proof = itinerary('proof')
    proof.attachmentKind = 'payment_proof'
    expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), proof])).toEqual([])
    for (const changes of [{ amount: null }, { origin: null }, { destination: null }, { date: null }]) {
      const incompleteTrip = itinerary('proof')
      Object.assign((incompleteTrip.ocrResult as ItineraryOcrResult).trips[0]!, changes)
      expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), incompleteTrip])).toEqual([])
    }
  })

  it('returns the matched trip rather than using a multi-trip file total as the matching amount', () => {
    const proof = itinerary('proof')
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.amount = '80.00'
    result.trips.push({ ...result.trips[0]!, row: 2, amount: '20.00', origin: '滨湖酒店', destination: '合肥机场' })
    expect(suggestItineraries([uncertainItem()], [uncertainInvoice(), proof])[0]?.trip?.amount).toBe('60.00')
    expect(suggestItineraries([uncertainItem('invoice', { amount: '80.00' })], [uncertainInvoice(), proof])).toEqual([])
  })

  it('does not reuse a manually occupied single trip or an ambiguous file-level manual link', () => {
    const linked = item('manual', { source: 'manual', sourceFileId: undefined, itineraryFileIds: ['proof'] })
    expect(suggestItineraries([uncertainItem(), linked], [uncertainInvoice(), itinerary('proof')])).toEqual([])
    expect(suggestItineraries([uncertainItem(), { ...linked, description: '手动补录打车费用', amount: '20.00' }],
      [uncertainInvoice(), itinerary('proof')])).toEqual([])
  })

  it('can recommend an unused trip in a multi-trip file when the existing link unambiguously refers to another trip', () => {
    const proof = itinerary('proof')
    const result = proof.ocrResult as ItineraryOcrResult
    result.summary.amount = '80.00'
    result.trips.push({ ...result.trips[0]!, row: 2, amount: '20.00', origin: '滨湖酒店', destination: '合肥机场' })
    const linked = item('manual', {
      source: 'manual', sourceFileId: undefined, itineraryFileIds: ['proof'], amount: '20.00', description: '滨湖酒店至合肥机场',
    })
    expect(suggestItineraries([uncertainItem(), linked], [uncertainInvoice(), proof])).toHaveLength(1)
    expect(suggestItineraries([uncertainItem(), { ...linked, description: '市内打车' }], [uncertainInvoice(), proof])).toEqual([])
  })

  it('returns an independent proposal whose consumers cannot mutate the OCR evidence', () => {
    const file = itinerary('proof')
    const result = file.ocrResult as ItineraryOcrResult
    result.trips[0]!.orderNumbers = ['ORDER-1']
    const suggestion = suggestItineraries([uncertainItem()], [uncertainInvoice(), file])[0]!
    suggestion.trip!.orderNumbers.push('ORDER-2')
    suggestion.trip!.amount = '1.00'
    expect(result.trips[0]!.orderNumbers).toEqual(['ORDER-1'])
    expect(result.trips[0]!.amount).toBe('60.00')
  })
})

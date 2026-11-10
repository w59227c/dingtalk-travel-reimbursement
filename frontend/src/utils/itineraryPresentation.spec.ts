import { describe, expect, it } from 'vitest'
import type { ExpenseItem } from '@/types/expenses'
import type { ItineraryOcrResult } from '@/types/receipts'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { itineraryOptionPresentation } from './itineraryPresentation'

function proof(): ReimbursementDraftFile {
  return {
    id: 'proof', name: '打车行程单3.pdf', role: 'ATTACHMENT_ONLY', attachmentKind: 'itinerary',
    sortOrder: 0, status: 'ACTIVE', mediaType: 'application/pdf', sizeBytes: 1, ocrStatus: 'COMPLETE',
    ocrResult: {
      fileId: 'proof', version: 1, kind: 'itinerary', status: 'recognized', source: 'pdf_text', pageCount: 1,
      processedPageCount: 1, complete: true, warnings: [], error: null,
      summary: { currency: 'CNY', amount: '9.20', startDate: '2026-07-06', endDate: '2026-07-06', invoiceNumbers: [], orderNumbers: [] },
      trips: [{ page: 1, row: 1, date: '2026-07-06', amount: '9.20', origin: '示例存储', destination: '示例酒店', invoiceNumbers: [], orderNumbers: [] }],
    },
  }
}

describe('itinerary option presentation', () => {
  it('does not present unreadable trip rows as an actual zero-trip itinerary', () => {
    const file = proof()
    Object.assign(file.ocrResult!, { trips: [], complete: false, warnings: ['ITINERARY_ROWS_INCOMPLETE'] })
    const result = itineraryOptionPresentation(file)
    expect(result.label).toContain('行程明细未识别')
    expect(result.summary).toContain('行程明细未识别')
    expect(result.summary).not.toContain('0 次行程')
  })

  it('includes useful amounts, date, trip count, route, and file name without guessing unrecognized fields', () => {
    const result = itineraryOptionPresentation(proof())
    expect(result.label).toBe('打车行程单3.pdf · ¥9.20 · 1 次行程')
    expect(result.summary).toBe('合计 ¥9.20 · 2026-07-06 · 1 次行程')
    expect(result.route).toBe('示例存储 → 示例酒店')
    expect(result.association).toBe('尚未关联')
    for (const term of ['9.20', '2026-07-06', '示例存储', '示例酒店', '打车行程单3.pdf']) expect(result.searchText).toContain(term)
    const file = proof()
    file.ocrResult = null
    expect(itineraryOptionPresentation(file).summary).toBe('行程信息待识别')
    file.ocrResult = { ...(proof().ocrResult as ItineraryOcrResult), fileId: 'wrong-file' }
    expect(itineraryOptionPresentation(file).summary).toBe('行程信息待识别')
  })

  it('clearly distinguishes the whole-file total and recommended trip in a multi-trip document', () => {
    const file = proof()
    const result = file.ocrResult as ItineraryOcrResult
    result.summary.amount = '29.20'
    result.summary.endDate = '2026-07-07'
    result.trips.push({ ...result.trips[0]!, row: 2, date: '2026-07-07', amount: '20.00', origin: '示例酒店', destination: '示例存储' })
    const presentation = itineraryOptionPresentation(file, [], {
      sourceFileId: 'invoice', itineraryFileId: 'proof', basis: 'amount_date_route',
      transportType: 'ride_hailing', trip: result.trips[0]!,
    })
    expect(presentation.summary).toBe('合计 ¥29.20 · 2026-07-06 至 2026-07-07 · 2 次行程')
    expect(presentation.matchedTripSummary).toBe('推荐行程 ¥9.20 · 2026-07-06')
    expect(presentation.route).toBe('示例存储 → 示例酒店')
    expect(presentation.searchText).toContain('20.00')
    expect(itineraryOptionPresentation(file).route).toContain('多行程：')
  })

  it('shows existing associations and uncertainty instead of approval or zero amounts', () => {
    const file = proof()
    const result = file.ocrResult as ItineraryOcrResult
    result.summary.amount = null
    result.summary.currency = null
    result.summary.startDate = null
    result.complete = false
    const linked: ExpenseItem = {
      id: 'item', source: 'ocr', category: 'local_transport', description: '打车',
      displayDate: '2026-07-06', amount: '9.20', receiptCount: 1, itineraryFileIds: ['proof'],
    }
    const presentation = itineraryOptionPresentation(file, [linked, { ...linked, id: 'other' }])
    expect(presentation.association).toBe('已关联 2 笔费用')
    expect(presentation.summary).toContain('金额未识别')
    expect(presentation.summary).toContain('日期未识别')
    expect(presentation.summary).toContain('信息不完整，需核对')
    expect(presentation.summary).not.toContain('¥0.00')
  })
})

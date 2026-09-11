import type { RailType } from '@/types/expenses'

export interface OcrReceiptError {
  code: string
  message: string
}

export interface OcrReceiptCandidate {
  fileId: string
  type: string
  categoryId: string
  categoryName: string
  date: string | null
  description: string | null
  amount: string | null
  transportType?: 'ride_hailing' | 'taxi' | 'rail' | 'hotel' | 'other'
  requiresItinerary?: boolean
  railType?: RailType | null
  invoiceNumbers?: string[]
  orderNumbers?: string[]
  originalCurrency?: string | null
  originalAmount?: string | null
  receiptCount: 1
  source: 'ocr'
  confidence: string
  warnings: string[]
  status: 'recognized' | 'failed'
  error: OcrReceiptError | null
}

export interface ItineraryOcrResult {
  fileId: string
  version: 1
  kind: 'itinerary'
  status: 'recognized' | 'failed'
  source: 'pdf_text' | 'paddle' | 'mixed' | 'unknown'
  pageCount: number
  processedPageCount: number
  complete: boolean
  summary: {
    currency: string | null
    amount: string | null
    startDate: string | null
    endDate: string | null
    invoiceNumbers: string[]
    orderNumbers: string[]
  }
  trips: Array<{
    page: number
    row: number
    date: string | null
    amount: string | null
    origin: string | null
    destination: string | null
    invoiceNumbers: string[]
    orderNumbers: string[]
  }>
  warnings: string[]
  error: OcrReceiptError | null
}

export function isItineraryOcrResult(result: OcrReceiptCandidate | ItineraryOcrResult | null | undefined): result is ItineraryOcrResult {
  return Boolean(result && 'kind' in result && result.kind === 'itinerary')
}

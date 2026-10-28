import { http } from '@/api/http'
import type { ApiEnvelope } from '@/types/auth'

export interface ReceiptDiagnostic {
  requestId: string
  keywordRuleCount: number
  recognition: {
    categoryName?: string
    categoryId?: string
    transportType?: string | null
    requiresItinerary?: boolean
    originalCurrency?: string | null
    originalAmount?: string | null
    date?: string | null
    amount?: string | null
    description?: string | null
    confidence?: string
    warnings?: string[]
    _materialClassification: { kind: string, reason: string | null, pageCount: number }
    diagnostic: {
      recognizedText: string
      recognizedTextTruncated: boolean
      sources: string[]
      complete: boolean
      extractions?: {
        page: number
        source: 'pdf_text' | 'paddle'
        text: string
        truncated: boolean
        usedForParsing: boolean
      }[]
      derivedFields?: { page: number, text: string, truncated: boolean }[]
      derivedFieldsTruncated?: boolean
      parser?: string
      selectionThreshold?: number
      parserScores?: { parser: string, score: number }[]
      baseCategory?: string
      finalCategory?: string
      policy?: string
      configuredKeywordCount?: number
      matchedRules?: { keyword: string, category: string, categoryName: string, transportType: string | null }[]
      classificationText?: string
      classificationTextTruncated?: boolean
    }
  }
}

export async function diagnoseReceipt(file: File, tripYear?: number, signal?: AbortSignal): Promise<ReceiptDiagnostic> {
  const form = new FormData()
  form.append('files[]', file)
  const response = await http.post<ApiEnvelope<ReceiptDiagnostic>>('/admin/receipts/diagnose', form, {
    params: tripYear ? { tripYear } : {}, signal, timeout: 330_000,
  })
  return response.data.data
}

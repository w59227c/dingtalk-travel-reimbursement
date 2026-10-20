import { http } from './http'
import type { ApiEnvelope } from '@/types/auth'
import type { ExpenseCategoryId } from '@/types/expenses'

export type ReceiptKeywordTransportType = 'other' | 'ride_hailing' | 'taxi'

export interface ReceiptKeywordMapping {
  id: number
  keyword: string
  categoryId: ExpenseCategoryId
  categoryName: string
  transportType?: ReceiptKeywordTransportType
}

export interface ReceiptKeywordInput {
  keyword: string
  categoryId: ExpenseCategoryId
  transportType?: ReceiptKeywordTransportType
}

export async function listReceiptKeywords(): Promise<ReceiptKeywordMapping[]> {
  const response = await http.get<ApiEnvelope<ReceiptKeywordMapping[]>>(
    '/admin/receipt-keywords',
  )
  return response.data.data
}

export async function createReceiptKeyword(
  input: ReceiptKeywordInput,
): Promise<ReceiptKeywordMapping> {
  const response = await http.post<ApiEnvelope<ReceiptKeywordMapping>>(
    '/admin/receipt-keywords',
    input,
  )
  return response.data.data
}

export async function updateReceiptKeyword(
  id: number,
  input: ReceiptKeywordInput,
): Promise<ReceiptKeywordMapping> {
  const response = await http.put<ApiEnvelope<ReceiptKeywordMapping>>(
    `/admin/receipt-keywords/${id}`,
    input,
  )
  return response.data.data
}

export async function deleteReceiptKeyword(id: number): Promise<void> {
  await http.delete(`/admin/receipt-keywords/${id}`)
}

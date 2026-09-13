import { http } from './http'
import { buildCalculateTotalsPayload } from './calculateTotalsPayload'
import type { ApiEnvelope } from '@/types/auth'
import type {
  ExpenseCategoryMetadata,
  ExpenseItem,
  ExpenseTotals,
  TripInput,
} from '@/types/expenses'

export async function getExpenseCategories(): Promise<ExpenseCategoryMetadata[]> {
  const response = await http.get<ApiEnvelope<ExpenseCategoryMetadata[]>>('/expense-categories')
  return response.data.data
}

export async function calculateTotals(
  tripOrTrips: TripInput | readonly TripInput[] | null,
  items: readonly ExpenseItem[],
): Promise<ExpenseTotals> {
  const response = await http.post<ApiEnvelope<ExpenseTotals>>(
    '/calculate/totals',
    buildCalculateTotalsPayload(tripOrTrips, items),
  )
  return response.data.data
}

import { http } from './http'
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
  const response = await http.post<ApiEnvelope<ExpenseTotals>>('/calculate/totals', {
    ...(Array.isArray(tripOrTrips) ? { trips: tripOrTrips } : { trip: tripOrTrips }),
    items: items.map((item) => ({
      id: item.id,
      source: item.source,
      category: item.category,
      date: item.date,
      displayDate: item.displayDate,
      description: item.description,
      amount: item.amount,
      receiptCount: item.receiptCount,
    })),
  })
  return response.data.data
}

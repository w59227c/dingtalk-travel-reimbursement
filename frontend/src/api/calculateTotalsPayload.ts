import type { ExpenseItem, TripInput } from '../types/expenses'

export interface CalculateTotalsPayload {
  trip?: TripInput | null
  trips?: readonly TripInput[]
  items: Array<Pick<
    ExpenseItem,
    | 'id'
    | 'source'
    | 'category'
    | 'date'
    | 'displayDate'
    | 'description'
    | 'amount'
    | 'receiptCount'
  >>
}

function isTripList(value: TripInput | readonly TripInput[] | null): value is readonly TripInput[] {
  return Array.isArray(value)
}

/**
 * Keep the backend request contract independent from the HTTP client so the
 * exact frontend serializer can also run in lightweight cross-stack tests.
 */
export function buildCalculateTotalsPayload(
  tripOrTrips: TripInput | readonly TripInput[] | null,
  items: readonly ExpenseItem[],
): CalculateTotalsPayload {
  return {
    ...(isTripList(tripOrTrips) ? { trips: tripOrTrips } : { trip: tripOrTrips }),
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
  }
}

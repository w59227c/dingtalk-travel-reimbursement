export type TripDayPeriod = 'morning' | 'afternoon'

// Keep the existing HH:mm API contract; these values encode a half-day, not an exact trip time.
export const TRIP_PERIOD_TIME: Record<TripDayPeriod, string> = {
  morning: '09:00',
  afternoon: '18:00',
}

export function tripPeriodFromTime(time: string | undefined): TripDayPeriod | undefined {
  if (!time || !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(time)) return undefined
  return time < '12:00' ? 'morning' : 'afternoon'
}

import type { ExcludedPeriodInput, TripInput } from '@/types/expenses'
import { tripPeriodFromTime } from '@/utils/tripPeriod'

export function overlappingExcludedPeriods(
  periods: readonly ExcludedPeriodInput[],
): [number, number] | null {
  const ordered = periods.flatMap((period, index) => {
    const startPeriod = tripPeriodFromTime(period.startTime)
    const endPeriod = tripPeriodFromTime(period.endTime)
    if (!period.startDate || !period.endDate || !startPeriod || !endPeriod) return []
    return [{
      start: `${period.startDate} ${startPeriod === 'morning' ? 0 : 1}`,
      end: `${period.endDate} ${endPeriod === 'morning' ? 0 : 1}`,
      index,
    }]
  }).sort((left, right) => left.start.localeCompare(right.start) || left.end.localeCompare(right.end))
  for (let index = 1; index < ordered.length; index += 1) {
    const previous = ordered[index - 1]!
    const current = ordered[index]!
    if (current.start <= previous.end) {
      return [Math.min(previous.index, current.index), Math.max(previous.index, current.index)]
    }
  }
  return null
}

export interface SubsidyTripGroup {
  key: string
  trips: TripInput[]
  startDate: string
  endDate: string
  startTime: string
  endTime: string
  primaryRelatedApprovalId?: string
  excludedPeriods: ExcludedPeriodInput[]
}

/** Group inclusive date ranges only when they overlap; adjacent ranges stay separate. */
export function groupOverlappingSubsidyTrips(
  trips: readonly TripInput[],
): SubsidyTripGroup[] {
  const ordered = [...trips].sort((left, right) =>
    left.startDate.localeCompare(right.startDate)
    || left.endDate.localeCompare(right.endDate)
    || (left.relatedApprovalId ?? '').localeCompare(right.relatedApprovalId ?? ''))
  const grouped: TripInput[][] = []
  let coveredEnd = ''
  for (const trip of ordered) {
    if (!grouped.length || trip.startDate > coveredEnd) {
      grouped.push([trip])
      coveredEnd = trip.endDate
      continue
    }
    grouped.at(-1)!.push(trip)
    if (trip.endDate > coveredEnd) coveredEnd = trip.endDate
  }
  return grouped.map((items) => {
    const startDate = items[0]!.startDate
    const endDate = items.reduce(
      (latest, item) => item.endDate > latest ? item.endDate : latest,
      items[0]!.endDate,
    )
    const startTime = items
      .filter((item) => item.startDate === startDate)
      .reduce((earliest, item) => item.startTime < earliest ? item.startTime : earliest, '23:59')
    const endTime = items
      .filter((item) => item.endDate === endDate)
      .reduce((latest, item) => item.endTime > latest ? item.endTime : latest, '00:00')
    const approvalIds = items.flatMap((item) => item.relatedApprovalId ? [item.relatedApprovalId] : [])
    return {
      key: approvalIds.join('|') || `${startDate}:${endDate}`,
      trips: items,
      startDate,
      endDate,
      startTime,
      endTime,
      primaryRelatedApprovalId: approvalIds[0],
      excludedPeriods: items.flatMap((item) => item.excludedPeriods ?? []),
    }
  })
}

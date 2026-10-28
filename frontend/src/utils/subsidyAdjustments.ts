import type { ExcludedPeriodInput, SubsidyAdjustmentType, TripInput } from '@/types/expenses'
import { tripPeriodFromTime } from '@/utils/tripPeriod'

export function adjustmentType(value: ExcludedPeriodInput): SubsidyAdjustmentType {
  return value.type ?? 'temporary_absence'
}

/** Canonicalise defaults and omit the endpoint that a one-way adjustment does not use. */
export function adjustmentPayload(value: ExcludedPeriodInput): ExcludedPeriodInput {
  const type = adjustmentType(value)
  return {
    ...(type === 'temporary_absence' ? {} : { type }),
    ...(type !== 'late_departure' ? { startDate: value.startDate ?? '', startTime: value.startTime ?? '' } : {}),
    ...(type !== 'early_end' ? { endDate: value.endDate ?? '', endTime: value.endTime ?? '' } : {}),
  }
}

export function subsidyHalfDayIndex(day: string | undefined, clock: string | undefined): number | null {
  const period = tripPeriodFromTime(clock)
  if (!day || day.startsWith('0000') || !/^\d{4}-\d{2}-\d{2}$/.test(day) || !period) return null
  const parsed = new Date(`${day}T00:00:00Z`)
  if (!Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== day) return null
  return parsed.getTime() / 86_400_000 * 2 + (period === 'afternoon' ? 1 : 0)
}

export function adjustmentValidationError(
  group: Pick<TripInput, 'startDate' | 'startTime' | 'endDate' | 'endTime' | 'excludedPeriods'>,
): string {
  const values = group.excludedPeriods ?? []
  if (!values.length) return ''
  const lower = subsidyHalfDayIndex(group.startDate, group.startTime)
  const upper = subsidyHalfDayIndex(group.endDate, group.endTime)
  if (lower === null || upper === null) return ''
  let actualStart = lower
  let actualEnd = upper
  const intervals: { start: number; end: number; index: number }[] = []
  for (const type of ['late_departure', 'early_end'] as const) {
    if (values.filter((value) => adjustmentType(value) === type).length > 1) {
      return `每项补助最多添加一条${type === 'late_departure' ? '延后出发' : '提前结束'}`
    }
  }
  for (const [index, value] of values.entries()) {
    const type = adjustmentType(value)
    if (!['temporary_absence', 'late_departure', 'early_end'].includes(type)) return '请选择有效的调整类型'
    const start = type === 'late_departure' ? lower : subsidyHalfDayIndex(value.startDate, value.startTime)
    const end = type === 'early_end' ? upper : subsidyHalfDayIndex(value.endDate, value.endTime)
    if (start === null || end === null) return '请补齐每个非补助时段的日期和出发时段'
    if (start < lower || start > upper || end < lower || end > upper) return '非补助时段必须在本项出差时段内'
    if (end < start) return '非补助时段的结束时间不能早于开始时间'
    if (type === 'late_departure') actualStart = end
    if (type === 'early_end') actualEnd = start
    const occupiedEnd = end + (type === 'late_departure' ? 0 : 1)
    if (start < occupiedEnd) intervals.push({ start, end: occupiedEnd, index })
  }
  if (actualStart > actualEnd) return '实际出发时间不能晚于提前结束时间'
  intervals.sort((left, right) => left.start - right.start || left.end - right.end)
  for (let index = 1; index < intervals.length; index += 1) {
    const previous = intervals[index - 1]!
    const current = intervals[index]!
    if (current.start < previous.end) {
      const [first, second] = [previous.index, current.index].sort((a, b) => a - b)
      return `非补助时段 ${first! + 1} 与时段 ${second! + 1} 重叠，请调整日期或出发时段`
    }
  }
  return ''
}

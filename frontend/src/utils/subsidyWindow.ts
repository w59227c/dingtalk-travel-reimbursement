import type { ExcludedPeriodInput } from '@/types/expenses'
import { adjustmentType, adjustmentValidationError } from '@/utils/subsidyAdjustments'
import type { SubsidyTripGroup } from '@/utils/subsidyTripGroups'

type WindowField = 'startDate' | 'startTime' | 'endDate' | 'endTime'

/** Keep editable absences while removing even malformed legacy boundary adjustments. */
export function resetSubsidyWindow(group: SubsidyTripGroup): void {
  for (const trip of group.trips) {
    trip.excludedPeriods = (trip.excludedPeriods ?? [])
      .filter((item) => adjustmentType(item) === 'temporary_absence')
  }
}

export function subsidyWindow(group: SubsidyTripGroup): Record<WindowField, string> {
  const late = group.excludedPeriods.find((item) => adjustmentType(item) === 'late_departure')
  const early = group.excludedPeriods.find((item) => adjustmentType(item) === 'early_end')
  return {
    startDate: late ? late.endDate ?? '' : group.startDate,
    startTime: late ? late.endTime ?? '' : group.startTime,
    endDate: early ? early.startDate ?? '' : group.endDate,
    endTime: early ? early.startTime ?? '' : group.endTime,
  }
}

/** Edit the effective window using the existing server-validated edge adjustments. */
export function updateSubsidyWindow(group: SubsidyTripGroup, field: WindowField, value: string): string {
  const next = { ...subsidyWindow(group), [field]: value }
  for (const day of [next.startDate, next.endDate]) {
    if (!day) continue
    const parsed = new Date(`${day}T00:00:00Z`)
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isFinite(parsed.getTime())
      || parsed.toISOString().slice(0, 10) !== day) return '请填写有效的补助日期'
    if (day < group.startDate || day > group.endDate) return '补助日期必须在审批日期范围内'
  }
  if (next.startDate && next.endDate && (next.startDate > next.endDate
    || (next.startDate === next.endDate && next.startTime && next.endTime && next.startTime > next.endTime))) {
    return '补助开始时间不能晚于结束时间'
  }
  const temporary = group.excludedPeriods.filter((item) => adjustmentType(item) === 'temporary_absence')
  const edges: ExcludedPeriodInput[] = []
  if (next.startDate !== group.startDate) edges.push({ type: 'late_departure', endDate: next.startDate, endTime: next.startTime })
  if (next.endDate !== group.endDate) edges.push({ type: 'early_end', startDate: next.endDate, startTime: next.endTime })
  const counts = group.trips.map((trip) => (trip.excludedPeriods ?? [])
    .filter((item) => adjustmentType(item) === 'temporary_absence').length)
  if (counts.some((count) => count > 100)
    || temporary.length + edges.length > group.trips.length * 100) {
    return '审批的调整数量已达上限，请先删除不需要的非补助时段'
  }
  const startTime = next.startDate === group.startDate ? next.startTime : group.startTime
  const endTime = next.endDate === group.endDate ? next.endTime : group.endTime
  const error = adjustmentValidationError({ ...group, startTime, endTime, excludedPeriods: [...temporary, ...edges] })
  if (error && !error.startsWith('请补齐')) return `补助日期与已有非补助时段冲突，请先调整或删除相应时段（${error}）`
  for (const trip of group.trips) {
    trip.excludedPeriods = (trip.excludedPeriods ?? []).filter((item) => adjustmentType(item) === 'temporary_absence')
    if (trip.startDate === group.startDate) trip.startTime = startTime
    if (trip.endDate === group.endDate) trip.endTime = endTime
  }
  for (const edge of edges) {
    const owner = group.trips.find((trip) => trip.excludedPeriods!.length < 100)!
    owner.excludedPeriods!.push(edge)
  }
  return ''
}

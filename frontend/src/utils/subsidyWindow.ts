import type { ExcludedPeriodInput } from '@/types/expenses'
import { adjustmentType, adjustmentValidationError, subsidyHalfDayIndex } from '@/utils/subsidyAdjustments'
import type { SubsidyTripGroup } from '@/utils/subsidyTripGroups'
import { tripPeriodFromTime } from '@/utils/tripPeriod'

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

/** Explain existing absences without changing or dropping the user's input. */
export function subsidyAbsenceIssue(group: SubsidyTripGroup, absence: ExcludedPeriodInput): string {
  const window = subsidyWindow(group)
  const lower = subsidyHalfDayIndex(window.startDate, window.startTime)
  const upper = subsidyHalfDayIndex(window.endDate, window.endTime)
  if (lower === null || upper === null || lower > upper) return ''
  const start = subsidyHalfDayIndex(absence.startDate, absence.startTime)
  const end = subsidyHalfDayIndex(absence.endDate, absence.endTime)
  for (const [day, time] of [[absence.startDate, absence.startTime], [absence.endDate, absence.endTime]]) {
    if (day && subsidyHalfDayIndex(day, '09:00') === null) return '日期或时段无效'
    if (time && !tripPeriodFromTime(time)) return '日期或时段无效'
    if (day && (day < window.startDate || day > window.endDate)) return '超出补助时段'
  }
  if ((start !== null && (start < lower || start > upper))
    || (end !== null && (end < lower || end > upper))) return '超出补助时段'
  if (start === null || end === null) return ''
  if (end < start) return '返回早于离开'
  const edges = group.excludedPeriods.filter((item) => adjustmentType(item) !== 'temporary_absence')
  const edgeError = adjustmentValidationError({ ...group, excludedPeriods: [...edges, absence] })
  if (edgeError && !edgeError.startsWith('请补齐')) return '与补助起止时段冲突'
  for (const other of group.excludedPeriods) {
    if (other === absence || adjustmentType(other) !== 'temporary_absence') continue
    const otherStart = subsidyHalfDayIndex(other.startDate, other.startTime)
    const otherEnd = subsidyHalfDayIndex(other.endDate, other.endTime)
    if (otherStart !== null && otherEnd !== null && otherStart <= otherEnd && start <= otherEnd && otherStart <= end) {
      return '与其他时段重叠'
    }
  }
  return ''
}

/** Edit the effective window using the existing server-validated edge adjustments. */
export function updateSubsidyWindow(group: SubsidyTripGroup, field: WindowField, value: string): string {
  const next = { ...subsidyWindow(group), [field]: value }
  if (group.trips[0]?.tripType === 'same_city_project') {
    next.startTime = '09:00'
    next.endTime = '18:00'
  }
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
  const error = adjustmentValidationError({ ...group, startTime, endTime, excludedPeriods: edges })
  if (error && !error.startsWith('请补齐')) return error
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

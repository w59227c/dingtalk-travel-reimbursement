import type { SubsidyTripGroup } from '@/utils/subsidyTripGroups'
import type { AttendanceCalendar } from '@/types/attendance'
import { adjustmentType } from '@/utils/subsidyAdjustments'
import { subsidyWindow } from '@/utils/subsidyWindow'

export const MAX_ATTENDANCE_DATES = 3660

export function attendanceDate(day: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return null
  const date = new Date(`${day}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === day ? date : null
}

export function defaultAttendanceDates(start: string, end: string, calendar?: AttendanceCalendar): string[] {
  const first = attendanceDate(start), last = attendanceDate(end)
  if (!first || !last || first > last || (last.getTime() - first.getTime()) / 86_400_000 >= MAX_ATTENDANCE_DATES || missingAttendanceYears(start, end, calendar).length) return []
  const dates: string[] = []
  for (let day = first.getTime(); day <= last.getTime(); day += 86_400_000) {
    const date = new Date(day)
    const key = date.toISOString().slice(0, 10)
    const override = calendar?.days[key]
    if (override?.working ?? (date.getUTCDay() !== 0 && date.getUTCDay() !== 6)) dates.push(key)
  }
  return dates
}

export function missingAttendanceYears(start: string, end: string, calendar?: AttendanceCalendar): number[] {
  const first = attendanceDate(start), last = attendanceDate(end)
  if (!first || !last || first > last) return []
  return Array.from({ length: last.getUTCFullYear() - first.getUTCFullYear() + 1 }, (_, i) => first.getUTCFullYear() + i)
    .filter((year) => !calendar?.years.includes(year))
}

export function groupAttendanceDates(group: SubsidyTripGroup): string[] | undefined {
  if (group.trips.some((trip) => trip.attendanceDates === undefined)) return undefined
  return [...new Set(group.trips.flatMap((trip) => trip.attendanceDates ?? []))].sort()
}

export function setGroupAttendanceDates(group: SubsidyTripGroup, dates: readonly string[]): void {
  for (const trip of group.trips) {
    trip.attendanceDates = []
    trip.confirmedEffectiveDays = undefined
    trip.noSubsidyException = false
  }
  for (const day of [...new Set(dates)].sort()) {
    const owner = group.trips.find((trip) => day >= trip.startDate && day <= trip.endDate) ?? group.trips[0]!
    owner.attendanceDates!.push(day)
  }
}

export function attendanceInputError(group: SubsidyTripGroup, calendar?: AttendanceCalendar): string {
  const window = subsidyWindow(group)
  const start = attendanceDate(window.startDate), end = attendanceDate(window.endDate)
  if (!start || !end || start > end) return '请填写有效的补助日期范围'
  const missing = missingAttendanceYears(window.startDate, window.endDate, calendar)
  if (missing.length) return `${missing.join('、')} 年节假日与调休安排尚未完整维护，请联系管理员维护后重新检查`
  if ((end.getTime() - start.getTime()) / 86_400_000 >= MAX_ATTENDANCE_DATES) return '出勤范围过长，请缩小补助日期范围'
  if (group.excludedPeriods.some((item) => adjustmentType(item) === 'temporary_absence')) return '请核对并逐项删除市内出差的旧非补助时段，改用出勤日期'
  const dates = groupAttendanceDates(group)
  if (dates === undefined) return '请核对并选择市内出差的出勤日期'
  if (dates.length > MAX_ATTENDANCE_DATES) return '出勤日期数量超出限制'
  for (const trip of group.trips) {
    if (new Set(trip.attendanceDates).size !== trip.attendanceDates!.length) return '出勤日期重复，请重新勾选对应日期'
    if (trip.attendanceDates!.some((day) => !attendanceDate(day) || day < trip.startDate || day > trip.endDate)) return '出勤日期必须位于所属审批日期内，请取消标记日期'
  }
  if (dates.some((day) => day < window.startDate || day > window.endDate)) return '已选出勤日期超出当前补助范围，请逐项取消或调整范围'
  return ''
}

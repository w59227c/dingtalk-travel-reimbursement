import type { AttendanceCalendarDay } from '@/types/attendance'
import { attendanceDate } from '@/utils/attendanceDates'

export interface CalendarRange { start: string; end: string; working: boolean; name: string }
export function calendarRanges(days: readonly AttendanceCalendarDay[]): CalendarRange[] {
  const ranges: CalendarRange[] = []
  for (const day of [...days].sort((a, b) => a.date.localeCompare(b.date))) {
    const previous = ranges.at(-1)
    if (previous && previous.working === day.working && previous.name === day.name
      && Date.parse(`${day.date}T00:00:00Z`) - Date.parse(`${previous.end}T00:00:00Z`) === 86_400_000) previous.end = day.date
    else ranges.push({ start: day.date, end: day.date, working: day.working, name: day.name })
  }
  return ranges
}
export function calendarMonthDays(year: number, month: number): { date: string; number: number; otherMonth: boolean; weekend: boolean }[] {
  const first = attendanceDate(`${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-01`)
  if (!first) return []
  const offset = (first.getUTCDay() + 6) % 7
  return Array.from({ length: 42 }, (_, i) => {
    const date = new Date(first.getTime() + (i - offset) * 86_400_000)
    return { date: date.toISOString().slice(0, 10), number: date.getUTCDate(), otherMonth: date.getUTCMonth() + 1 !== month || date.getUTCFullYear() !== year, weekend: [0, 6].includes(date.getUTCDay()) }
  })
}

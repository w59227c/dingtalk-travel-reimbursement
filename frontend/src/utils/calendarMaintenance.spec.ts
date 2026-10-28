import { describe, expect, it } from 'vitest'
import { calendarMonthDays, calendarRanges } from './calendarMaintenance'
describe('read-only calendar presentation', () => {
  it('groups consecutive days with matching labels without mutating dates', () => {
    const days = [{ date: '2042-01-03', working: false, name: '假期' }, { date: '2042-01-02', working: false, name: '假期' }, { date: '2042-01-04', working: true, name: '补班' }, { date: '2042-01-05', working: false, name: '不同' }]
    expect(calendarRanges(days)).toEqual([{ start: '2042-01-02', end: '2042-01-03', working: false, name: '假期' }, { start: '2042-01-04', end: '2042-01-04', working: true, name: '补班' }, { start: '2042-01-05', end: '2042-01-05', working: false, name: '不同' }])
    expect(days[0]?.date).toBe('2042-01-03')
  })
  it('renders six Monday-first calendar weeks including leap days', () => {
    const cells = calendarMonthDays(2028, 2)
    expect(cells).toHaveLength(42)
    expect(cells[0]?.date).toBe('2028-01-31')
    expect(cells.find((day) => day.date === '2028-02-29')?.otherMonth).toBe(false)
    expect(calendarMonthDays(2026, 13)).toEqual([])
  })
})

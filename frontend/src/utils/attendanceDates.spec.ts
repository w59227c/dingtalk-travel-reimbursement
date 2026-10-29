import { describe, expect, it } from 'vitest'

import { attendanceInputError, defaultAttendanceDates, groupAttendanceDates, setGroupAttendanceDates, missingAttendanceYears } from '@/utils/attendanceDates'
import { groupOverlappingSubsidyTrips } from '@/utils/subsidyTripGroups'
import type { TripInput } from '@/types/expenses'

const complete = { years: [2026, 2027, 2028], days: {} }

describe('attendance dates', () => {
  it('defaults to weekdays including holidays as ordinary dates, with bounded invalid ranges', () => {
    expect(defaultAttendanceDates('2026-09-01', '2026-09-07', complete)).toEqual(['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07'])
    expect(defaultAttendanceDates('2026-12-31', '2027-01-04', complete)).toEqual(['2026-12-31', '2027-01-01', '2027-01-04'])
    expect(defaultAttendanceDates('2028-02-28', '2028-03-01', complete)).toContain('2028-02-29')
    expect(defaultAttendanceDates('2026-02-30', '2026-03-02')).toEqual([])
    expect(defaultAttendanceDates('2020-01-01', '2040-01-01')).toEqual([])
  })

  it('uses uploaded rest and make-up workdays, with explicit fallback for missing years', () => {
    const calendar = { years: [2026], days: { '2026-09-20': { working: true, name: '补班' }, '2026-09-25': { working: false, name: '休息' } } }
    expect(defaultAttendanceDates('2026-09-20', '2026-09-27', calendar)).toEqual(['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'])
    expect(missingAttendanceYears('2026-12-31', '2027-01-03', calendar)).toEqual([2027])
    expect(defaultAttendanceDates('2026-12-31', '2027-01-04', calendar)).toEqual([])
  })

  it('applies known cross-year dates without treating the adjacent year as fully maintained', () => {
    const calendar = { years: [2019], days: { '2018-12-29': { working: true, name: '元旦' }, '2018-12-31': { working: false, name: '元旦' } } }
    expect(defaultAttendanceDates('2018-12-28', '2018-12-31', calendar)).toEqual([])
    expect(missingAttendanceYears('2018-12-28', '2019-01-01', calendar)).toEqual([2018])
  })

  it('distributes overlapping approval dates once, preserves invalid selections, and keeps legacy input explicit', () => {
    const trips: TripInput[] = [
      { relatedApprovalId: 'a', tripType: 'same_city_project', startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-05', endTime: '18:00' },
      { relatedApprovalId: 'b', tripType: 'same_city_project', startDate: '2026-09-05', startTime: '09:00', endDate: '2026-09-10', endTime: '18:00' },
    ]
    const group = () => groupOverlappingSubsidyTrips(trips)[0]!
    expect(groupAttendanceDates(group())).toBeUndefined()
    expect(attendanceInputError(group(), complete)).toContain('核对并选择')
    setGroupAttendanceDates(group(), ['2026-09-01', '2026-09-05', '2026-09-06'])
    expect(trips[0]!.attendanceDates).toEqual(['2026-09-01', '2026-09-05'])
    expect(trips[1]!.attendanceDates).toEqual(['2026-09-06'])
    expect(attendanceInputError(group(), complete)).toBe('')
    trips[0]!.excludedPeriods = [{ type: 'late_departure', endDate: '2026-09-03', endTime: '09:00' }]
    expect(groupAttendanceDates(group())).toContain('2026-09-01')
    expect(attendanceInputError(group(), complete)).toContain('超出当前补助范围')
    setGroupAttendanceDates(group(), ['2026-09-05', '2026-09-06'])
    expect(attendanceInputError(group(), complete)).toBe('')
  })
})

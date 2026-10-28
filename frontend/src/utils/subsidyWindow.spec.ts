import { describe, expect, it } from 'vitest'

import type { TripInput } from '@/types/expenses'
import { groupOverlappingSubsidyTrips } from '@/utils/subsidyTripGroups'
import { resetSubsidyWindow, subsidyAbsenceIssue, subsidyWindow, updateSubsidyWindow } from '@/utils/subsidyWindow'

function trips(): TripInput[] {
  return [{ relatedApprovalId: 'a', tripType: 'business', startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-06', endTime: '18:00' }]
}

describe('effective subsidy window', () => {
  it('narrows and restores dates without altering immutable approval dates or losing absences', () => {
    const rows = trips()
    const absence = { startDate: '2026-09-03', startTime: '09:00', endDate: '2026-09-04', endTime: '18:00' }
    rows[0]!.excludedPeriods = [absence]
    const group = () => groupOverlappingSubsidyTrips(rows)[0]!
    expect(updateSubsidyWindow(group(), 'startDate', '2026-09-02')).toBe('')
    expect(updateSubsidyWindow(group(), 'endDate', '2026-09-05')).toBe('')
    expect(updateSubsidyWindow(group(), 'startTime', '18:00')).toBe('')
    expect(subsidyWindow(group())).toEqual({ startDate: '2026-09-02', startTime: '18:00', endDate: '2026-09-05', endTime: '18:00' })
    expect(rows[0]!.startDate).toBe('2026-09-01')
    expect(rows[0]!.endDate).toBe('2026-09-06')
    expect(rows[0]!.excludedPeriods![0]).toBe(absence)
    expect(updateSubsidyWindow(group(), 'startDate', '2026-09-01')).toBe('')
    expect(updateSubsidyWindow(group(), 'endDate', '2026-09-06')).toBe('')
    expect(rows[0]!.excludedPeriods).toEqual([absence])
    expect(rows[0]!.startTime).toBe('18:00')
  })

  it.each(['2026-08-31', '2026-09-07', '2026-02-30', 'bad-date'])('rejects invalid/outside date %s without mutation', (value) => {
    const rows = trips()
    const before = JSON.stringify(rows)
    expect(updateSubsidyWindow(groupOverlappingSubsidyTrips(rows)[0]!, 'startDate', value)).not.toBe('')
    expect(JSON.stringify(rows)).toBe(before)
  })

  it('rejects reversed dates and same-day reversed periods', () => {
    const rows = trips()
    const group = () => groupOverlappingSubsidyTrips(rows)[0]!
    expect(updateSubsidyWindow(group(), 'endDate', '2026-09-01')).toBe('')
    expect(updateSubsidyWindow(group(), 'startDate', '2026-09-02')).toContain('不能晚于')
    expect(updateSubsidyWindow(group(), 'endTime', '09:00')).toBe('')
    expect(updateSubsidyWindow(group(), 'startTime', '18:00')).toContain('不能晚于')
  })

  it('accepts a new window while preserving and marking existing absences for correction', () => {
    const rows = trips()
    const invalidated = { startDate: '2026-09-02', startTime: '09:00', endDate: '2026-09-03', endTime: '18:00' }
    const valid = { startDate: '2026-09-04', startTime: '09:00', endDate: '2026-09-05', endTime: '09:00' }
    rows[0]!.excludedPeriods = [invalidated, valid]
    const group = () => groupOverlappingSubsidyTrips(rows)[0]!
    expect(updateSubsidyWindow(group(), 'startDate', '2026-09-04')).toBe('')
    expect(subsidyWindow(group()).startDate).toBe('2026-09-04')
    expect(group().excludedPeriods).toContain(invalidated)
    expect(group().excludedPeriods).toContain(valid)
    expect(invalidated.startDate).toBe('2026-09-02')
    expect(subsidyAbsenceIssue(group(), invalidated)).toBe('超出补助时段')
    expect(subsidyAbsenceIssue(group(), valid)).toBe('')
    resetSubsidyWindow(group())
    expect(rows[0]!.excludedPeriods).toEqual([invalidated, valid])
    expect(subsidyAbsenceIssue(group(), invalidated)).toBe('')
  })

  it('marks half-day boundaries, reversed times and overlapping absences without mutation', () => {
    const rows = trips()
    const first = { startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-02', endTime: '09:00' }
    const second = { startDate: '2026-09-03', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00' }
    rows[0]!.excludedPeriods = [first, second]
    const group = () => groupOverlappingSubsidyTrips(rows)[0]!
    expect(updateSubsidyWindow(group(), 'startTime', '18:00')).toBe('')
    const before = JSON.stringify(rows)
    expect(subsidyAbsenceIssue(group(), first)).toBe('超出补助时段')
    expect(subsidyAbsenceIssue(group(), second)).toBe('返回早于离开')
    expect(JSON.stringify(rows)).toBe(before)
    first.startTime = '18:00'
    second.startDate = '2026-09-02'
    second.endTime = '18:00'
    expect(subsidyAbsenceIssue(group(), second)).toBe('')
    second.startTime = '09:00'
    expect(subsidyAbsenceIssue(group(), first)).toBe('与其他时段重叠')
    expect(subsidyAbsenceIssue(group(), second)).toBe('与其他时段重叠')
    second.startDate = 'bad-date'
    expect(subsidyAbsenceIssue(group(), second)).toBe('日期或时段无效')
  })

  it('reads legacy adjustments and edits the union of overlapping approvals', () => {
    const rows = trips()
    rows.push({ ...rows[0]!, relatedApprovalId: 'b', startDate: '2026-09-05', endDate: '2026-09-10', excludedPeriods: [{ type: 'early_end', startDate: '2026-09-09', startTime: '09:00' }] })
    const group = () => groupOverlappingSubsidyTrips(rows)[0]!
    expect(subsidyWindow(group()).endDate).toBe('2026-09-09')
    expect(updateSubsidyWindow(group(), 'startDate', '2026-09-07')).toBe('')
    expect(subsidyWindow(group()).startDate).toBe('2026-09-07')
    expect(group().excludedPeriods).toHaveLength(2)
    expect(rows[1]!.excludedPeriods).toEqual([])
  })

  it('keeps each approval within its own adjustment capacity when assigning edge adjustments', () => {
    const rows = trips()
    rows[0]!.startDate = '2026-01-01'
    rows[0]!.endDate = '2026-09-30'
    rows[0]!.excludedPeriods = Array.from({ length: 100 }, (_, index) => {
      const day = new Date(Date.UTC(2026, 4, index + 1)).toISOString().slice(0, 10)
      return { startDate: day, startTime: '09:00', endDate: day, endTime: '09:00' }
    })
    rows.push({ ...rows[0]!, relatedApprovalId: 'b', startDate: '2026-09-01', endDate: '2026-12-30', excludedPeriods: [] })
    expect(updateSubsidyWindow(groupOverlappingSubsidyTrips(rows)[0]!, 'startDate', '2026-02-01')).toBe('')
    expect(rows[0]!.excludedPeriods).toHaveLength(100)
    expect(rows[1]!.excludedPeriods).toEqual([{ type: 'late_departure', endDate: '2026-02-01', endTime: '09:00' }])
  })

  it('repairs malformed legacy boundaries while preserving the approval and editable absences', () => {
    const rows = trips()
    const absence = { startDate: '2026-09-03', startTime: '09:00', endDate: '2026-09-04', endTime: '18:00' }
    rows[0]!.excludedPeriods = [
      { type: 'late_departure', endDate: '2026-08-31', endTime: '09:00' },
      { type: 'early_end', startDate: '2026-09-07', startTime: '18:00' },
      absence,
    ]
    resetSubsidyWindow(groupOverlappingSubsidyTrips(rows)[0]!)
    const group = groupOverlappingSubsidyTrips(rows)[0]!
    expect(subsidyWindow(group)).toEqual({ startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-06', endTime: '18:00' })
    expect(rows[0]!.excludedPeriods).toEqual([absence])
    expect(rows[0]!.excludedPeriods![0]).toBe(absence)
    expect(updateSubsidyWindow(group, 'startDate', '2026-09-02')).toBe('')
  })

})

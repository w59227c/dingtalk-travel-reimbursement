import { describe, expect, it } from 'vitest'
import { adjustmentPayload, adjustmentValidationError } from './subsidyAdjustments'
import type { ExcludedPeriodInput, TripInput } from '@/types/expenses'

const late: ExcludedPeriodInput = { type: 'late_departure', endDate: '2026-07-02', endTime: '18:00' }
const early: ExcludedPeriodInput = { type: 'early_end', startDate: '2026-07-09', startTime: '09:00' }
const middle: ExcludedPeriodInput = { startDate: '2026-07-04', startTime: '09:00', endDate: '2026-07-06', endTime: '18:00' }
const base: TripInput = { tripType: 'long_term_project', startDate: '2026-07-01', startTime: '09:00', endDate: '2026-07-10', endTime: '18:00' }

describe('subsidy adjustments', () => {
  it('keeps legacy JSON stable and serializes only the active endpoints', () => {
    expect(adjustmentPayload(middle)).toEqual(middle)
    expect(adjustmentPayload({ ...middle, type: 'temporary_absence' })).toEqual(middle)
    expect(adjustmentPayload({ ...late, startDate: '', startTime: '' })).toEqual(late)
    expect(adjustmentPayload({ ...early, endDate: '', endTime: '' })).toEqual(early)
  })
  it('accepts all three adjustment types together and a same-half-day actual trip', () => {
    expect(adjustmentValidationError({ ...base, excludedPeriods: [late, early, middle] })).toBe('')
    expect(adjustmentValidationError({ ...base, excludedPeriods: [late, { ...early, startDate: '2026-07-02', startTime: '18:00' }] })).toBe('')
  })
  it.each([
    { values: [late, late], error: '最多添加一条延后出发' },
    { values: [early, early], error: '最多添加一条提前结束' },
    { values: [{ ...late, endDate: '' }], error: '请补齐' },
    { values: [{ ...early, startTime: '' }], error: '请补齐' },
    { values: [{ ...late, endDate: '2026-06-30' }], error: '在本项出差时段内' },
    { values: [{ ...early, startDate: '2026-07-11' }], error: '在本项出差时段内' },
    { values: [{ ...late, endDate: '2026-07-09' }, early], error: '实际出发时间不能晚于提前结束' },
    { values: [{ ...late, endDate: '2026-07-05' }, middle], error: '重叠' },
    { values: [{ ...early, startDate: '2026-07-05' }, middle], error: '重叠' },
    { values: [{ ...late, endDate: '2026-02-30' }], error: '请补齐' },
  ])('blocks invalid input $error before calculation', ({ values, error }) => {
    expect(adjustmentValidationError({ ...base, excludedPeriods: values })).toContain(error)
  })
})

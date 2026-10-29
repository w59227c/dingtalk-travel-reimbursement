import { describe, expect, it } from 'vitest'
import { approvalQueryDateError, approvalQueryStartDisabled, approvalQueryEndDisabled, shanghaiToday } from './approvalQueryDates'

describe('approval query date policy', () => {
  const today = '2026-10-29'
  it('uses Shanghai calendar dates even when UTC has not reached midnight', () => {
    expect(shanghaiToday(new Date('2026-10-28T16:00:00Z'))).toBe(today)
    expect(shanghaiToday(new Date('2026-10-28T15:59:59Z'))).toBe('2026-10-28')
  })
  it.each([
    ['', '', ''],
    ['2026-10-01', '', '同时填写'],
    ['2026-10-02', '2026-10-01', '晚于结束'],
    ['2025-01-01', '2025-05-31', '365'],
    ['2025-10-29', '2025-10-30', '2025-10-30'],
    ['2026-10-29', '2026-10-30', '晚于今天'],
    ['2025-10-30', '2026-04-28', '180'],
    ['2025-10-30', '2026-04-27', ''],
    ['2026-02-30', '2026-03-01', '有效'],
  ])('validates %s through %s', (from, to, message) => {
    const error = approvalQueryDateError(from, to, today)
    if (message) expect(error).toContain(message)
    else expect(error).toBe('')
  })
  it('constrains both pickers by history, order and inclusive 180-day span', () => {
    expect(approvalQueryStartDisabled(new Date(2025, 9, 29), '', today)).toBe(true)
    expect(approvalQueryStartDisabled(new Date(2025, 9, 30), '', today)).toBe(false)
    expect(approvalQueryEndDisabled(new Date(2026, 9, 30), '', today)).toBe(true)
    expect(approvalQueryEndDisabled(new Date(2026, 9, 1), '2026-10-02', today)).toBe(true)
    expect(approvalQueryEndDisabled(new Date(2026, 9, 2), '2026-10-02', today)).toBe(false)
    expect(approvalQueryEndDisabled(new Date(2026, 3, 27), '2025-10-30', today)).toBe(false)
    expect(approvalQueryEndDisabled(new Date(2026, 3, 28), '2025-10-30', today)).toBe(true)
    expect(approvalQueryStartDisabled(new Date(2025, 9, 30), '2026-04-28', today)).toBe(true)
    expect(approvalQueryStartDisabled(new Date(2025, 9, 31), '2026-04-28', today)).toBe(false)
  })
})

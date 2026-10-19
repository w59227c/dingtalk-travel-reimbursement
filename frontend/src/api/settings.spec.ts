import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '@/api/http'
import { normalizeSubsidyRate, updateExpenseSettings } from '@/api/settings'

vi.mock('@/api/http', () => ({
  http: { put: vi.fn() },
}))

describe('settings API', () => {
  beforeEach(() => {
    vi.mocked(http.put).mockReset()
  })

  it('normalizes the decimal string before sending it', async () => {
    const rates = {
      business: '88.5',
      short_term_project: '100',
      long_term_project: '150',
      same_city_project: '50',
      short_term_internal: '100',
        long_term_internal: '100',
        short_term_overseas: '0.00',
        long_term_overseas: '0.00',
    } as const
    vi.mocked(http.put).mockResolvedValue({
      data: {
        data: {
          appTitle: '智能差旅费报销申请',
          adminUserIds: ['admin-2'],
          environmentAdminUserIds: ['admin-1'],
          subsidyRates: rates,
          calculationMode: 'half_day_12',
        },
      },
    })

    await updateExpenseSettings({
      appTitle: '智能差旅费报销申请',
      adminUserIds: ['admin-2'],
      environmentAdminUserIds: ['admin-1'],
      subsidyRates: rates,
      calculationMode: 'half_day_12',
    })

    expect(http.put).toHaveBeenCalledWith('/admin/settings', {
      appTitle: '智能差旅费报销申请',
      adminUserIds: ['admin-2'],
      subsidyRates: {
        business: '88.50',
        short_term_project: '100.00',
        long_term_project: '150.00',
        same_city_project: '50.00',
        short_term_internal: '100.00',
        long_term_internal: '100.00',
        short_term_overseas: '0.00',
        long_term_overseas: '0.00',
      },
      calculationMode: 'half_day_12',
    })
  })

  it('rejects excessive rates, negative values and excess precision locally', async () => {
    for (const value of ['10000.01', '-1', '88.501']) {
      expect(normalizeSubsidyRate(value)).toBeNull()
      await expect(
        updateExpenseSettings({
          appTitle: '智能差旅费报销申请',
          adminUserIds: [],
          environmentAdminUserIds: ['admin-1'],
          subsidyRates: {
            business: value,
            short_term_project: '100.00',
            long_term_project: '150.00',
            same_city_project: '50.00',
            short_term_internal: '100.00',
        long_term_internal: '100.00',
        short_term_overseas: '0.00',
        long_term_overseas: '0.00',
          },
          calculationMode: 'half_day_12',
        }),
      ).rejects.toThrow('每日补助不能小于 0')
    }
    expect(normalizeSubsidyRate('0')).toBe('0.00')
    expect(normalizeSubsidyRate('0', true)).toBe('0.00')
    expect(normalizeSubsidyRate('-0.01', true)).toBeNull()
    expect(http.put).toHaveBeenCalledTimes(0)
  })
})

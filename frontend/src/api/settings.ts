import { http } from './http'
import type { ApiEnvelope } from '@/types/auth'
import type { SubsidyRateType } from '@/types/expenses'
import { centsToMoney, moneyToCents } from '@/utils/money'

export const MAX_DAILY_SUBSIDY_CENTS = 1_000_000

export type SubsidyRates = Record<SubsidyRateType, string>

export interface ExpenseSettings {
  appTitle: string
  subsidyRates: SubsidyRates
  calculationMode: 'half_day_12'
}

export interface AdminExpenseSettings extends ExpenseSettings {
  adminUserIds: string[]
  environmentAdminUserIds: string[]
}

export async function getExpenseSettings(): Promise<ExpenseSettings> {
  const response = await http.get<ApiEnvelope<ExpenseSettings>>('/settings')
  return response.data.data
}

export async function getAdminExpenseSettings(): Promise<AdminExpenseSettings> {
  const response = await http.get<ApiEnvelope<AdminExpenseSettings>>('/admin/settings')
  return response.data.data
}

export function normalizeSubsidyRate(value: string, allowZero = true): string | null {
  const cents = moneyToCents(value)
  if (
    cents === null
    || cents < 0
    || (!allowZero && cents === 0)
    || cents > MAX_DAILY_SUBSIDY_CENTS
  ) return null
  return centsToMoney(cents)
}

export async function updateExpenseSettings(
  input: AdminExpenseSettings,
): Promise<AdminExpenseSettings> {
  const normalizedRates = Object.fromEntries(
    Object.entries(input.subsidyRates).map(([tripType, value]) => {
      const normalized = normalizeSubsidyRate(value)
      if (normalized === null) {
        throw new Error('每日补助不能小于 0 或超过 10000 元，最多两位小数')
      }
      return [tripType, normalized]
    }),
  ) as SubsidyRates
  const response = await http.put<ApiEnvelope<AdminExpenseSettings>>('/admin/settings', {
    appTitle: input.appTitle,
    adminUserIds: input.adminUserIds,
    subsidyRates: normalizedRates,
    calculationMode: input.calculationMode,
  })
  return response.data.data
}

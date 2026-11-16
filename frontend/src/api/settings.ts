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
  revision: string
  adminUserIds: string[]
  adminDepartmentIds?: string[]
  environmentAdminUserIds: string[]
}

export type DirectoryKind = 'department' | 'employee'

export interface DirectoryEntry {
  id: string
  name: string
}

export async function listAdminDepartments(parentId: string, signal?: AbortSignal): Promise<DirectoryEntry[]> {
  const response = await http.get<ApiEnvelope<{ items: DirectoryEntry[] }>>('/admin/directory/departments', {
    params: { parentId }, signal,
  })
  return response.data.data.items
}

export async function searchAdminEmployees(q: string, signal?: AbortSignal): Promise<DirectoryEntry[]> {
  const response = await http.get<ApiEnvelope<{ items: DirectoryEntry[] }>>('/admin/directory/employees', {
    params: { q }, signal,
  })
  return response.data.data.items
}

export async function lookupAdminDirectoryLabels(kind: DirectoryKind, ids: string[], signal?: AbortSignal): Promise<DirectoryEntry[]> {
  const response = await http.get<ApiEnvelope<{ items: DirectoryEntry[] }>>('/admin/directory/labels', {
    params: { kind, ids }, paramsSerializer: { indexes: null }, signal,
  })
  return response.data.data.items
}

export interface ReimbursementDelegationSettings {
  employeeUserIds: string[]
  departmentIds: string[]
  revision: string
}

export async function getReimbursementDelegationSettings(signal?: AbortSignal): Promise<ReimbursementDelegationSettings> {
  const response = await http.get<ApiEnvelope<ReimbursementDelegationSettings>>('/admin/reimbursement-delegation', { signal })
  return response.data.data
}

export async function updateReimbursementDelegationSettings(
  value: ReimbursementDelegationSettings,
  signal?: AbortSignal,
): Promise<ReimbursementDelegationSettings> {
  const response = await http.put<ApiEnvelope<ReimbursementDelegationSettings>>('/admin/reimbursement-delegation', {
    employeeUserIds: value.employeeUserIds,
    departmentIds: value.departmentIds,
    expectedRevision: value.revision,
  }, { signal })
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
    expectedRevision: input.revision,
    appTitle: input.appTitle,
    adminUserIds: input.adminUserIds,
    adminDepartmentIds: input.adminDepartmentIds,
    subsidyRates: normalizedRates,
    calculationMode: input.calculationMode,
  })
  return response.data.data
}

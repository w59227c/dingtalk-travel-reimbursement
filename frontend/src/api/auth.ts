import { http, reimbursementContextHeaders } from './http'
import type {
  ApiEnvelope,
  AuthSession,
  PublicConfig,
  ReimbursementDepartmentResolution,
  ReimbursementEmployee,
} from '@/types/auth'
import type { ReimbursementRelatedApprovalSelection } from '@/types/reimbursements'

export async function getPublicConfig(): Promise<PublicConfig> {
  const response = await http.get<ApiEnvelope<PublicConfig>>('/config/public')
  return response.data.data
}

export async function loginWithDingTalk(authCode: string): Promise<AuthSession> {
  const response = await http.post<ApiEnvelope<AuthSession>>('/auth/dingtalk', { authCode })
  return response.data.data
}

export async function loginWithMock(): Promise<AuthSession> {
  const response = await http.post<ApiEnvelope<AuthSession>>('/auth/mock')
  return response.data.data
}

export async function getMe(reimbursementContext?: string | null, signal?: AbortSignal): Promise<AuthSession> {
  const response = await http.get<ApiEnvelope<AuthSession>>('/me', {
    headers: reimbursementContextHeaders(reimbursementContext),
    signal,
  })
  return response.data.data
}

export async function prepareReimbursementContext(userId: string | null, signal?: AbortSignal): Promise<AuthSession> {
  const response = await http.post<ApiEnvelope<AuthSession>>(
    '/me/reimbursement-context', { userId }, { signal, timeout: 30_000 },
  )
  return response.data.data
}

export async function listReimbursementEmployees(query: string, signal?: AbortSignal): Promise<ReimbursementEmployee[]> {
  const response = await http.get<ApiEnvelope<{ items: ReimbursementEmployee[] }>>(
    '/me/reimbursement-employees', { params: { q: query }, signal, timeout: 30_000 },
  )
  return response.data.data.items
}

export async function selectDepartmentFromTravelApproval(
  selection: ReimbursementRelatedApprovalSelection,
  selectedDepartmentId?: string,
): Promise<ReimbursementDepartmentResolution> {
  const response = await http.post<ApiEnvelope<ReimbursementDepartmentResolution>>(
    '/me/department/from-travel-approval',
    {
      ...selection,
      ...(selectedDepartmentId ? { selectedDepartmentId } : {}),
    },
    { timeout: 60_000 },
  )
  return response.data.data
}

export async function logout(): Promise<void> {
  await http.post('/auth/logout')
}

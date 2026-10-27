import { http } from './http'
import type { ApiEnvelope } from '@/types/auth'
import type { ApprovalDiagnosticInput, ApprovalDiagnosticReport } from '@/types/approvalDiagnostics'

export async function diagnoseTravelApproval(
  input: ApprovalDiagnosticInput,
  signal?: AbortSignal,
): Promise<ApprovalDiagnosticReport> {
  const response = await http.post<ApiEnvelope<ApprovalDiagnosticReport>>(
    '/admin/oa/travel-approvals/diagnose', input, { signal, timeout: 75_000 },
  )
  return response.data.data
}

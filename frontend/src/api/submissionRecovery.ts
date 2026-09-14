import { http } from './http'

import type { ApiEnvelope } from '@/types/auth'
import type { ReimbursementSubmission } from '@/types/reimbursements'

export type AdminSubmissionRecoveryInput =
  | { action: 'ATTACH_INSTANCE'; processInstanceId: string; verificationNote: string }
  | {
    action: 'CONFIRM_NOT_CREATED'
    verificationNote: string
    confirmUncertainUploadsAbsent?: boolean
  }

export async function recoverReimbursementSubmission(
  submissionId: string,
  input: AdminSubmissionRecoveryInput,
): Promise<ReimbursementSubmission> {
  const response = await http.post<ApiEnvelope<ReimbursementSubmission>>(
    `/admin/oa/reimbursements/submissions/${encodeURIComponent(submissionId)}/recover`,
    input,
  )
  return response.data.data
}

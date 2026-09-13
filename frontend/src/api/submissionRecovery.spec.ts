import { beforeEach, describe, expect, it, vi } from 'vitest'

import { http } from '@/api/http'
import { recoverReimbursementSubmission } from '@/api/submissionRecovery'

vi.mock('@/api/http', () => ({
  http: { post: vi.fn() },
}))

describe('submission recovery API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('encodes the id and preserves the explicit administrator decision', async () => {
    const result = { submissionId: 'submission/一', status: 'ORPHAN_CLEANUP' }
    vi.mocked(http.post).mockResolvedValue({ data: { data: result } })

    await expect(recoverReimbursementSubmission('submission/一', {
      action: 'CONFIRM_NOT_CREATED',
      confirmUncertainUploadsAbsent: true,
    })).resolves.toBe(result)

    expect(http.post).toHaveBeenCalledWith(
      '/admin/oa/reimbursements/submissions/submission%2F%E4%B8%80/recover',
      {
        action: 'CONFIRM_NOT_CREATED',
        confirmUncertainUploadsAbsent: true,
      },
    )
  })
})

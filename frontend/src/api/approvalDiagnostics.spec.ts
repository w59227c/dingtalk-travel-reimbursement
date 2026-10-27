import { beforeEach, describe, expect, it, vi } from 'vitest'

import { diagnoseTravelApproval } from '@/api/approvalDiagnostics'
import { http } from '@/api/http'

vi.mock('@/api/http', () => ({ http: { post: vi.fn() } }))

describe('approval diagnostics API', () => {
  beforeEach(() => vi.clearAllMocks())

  it('keeps identifiers out of URLs and attaches cancellation to the admin POST', async () => {
    const response = { requestId: 'synthetic-request', outcome: 'visible' }
    vi.mocked(http.post).mockResolvedValue({ data: { data: response } })
    const controller = new AbortController()
    const input = {
      userId: 'synthetic-employee', approvalIdentifier: 'synthetic-instance',
      identifierKind: 'instance_id' as const, from: '2026-08-01', to: '2026-08-31',
    }
    expect(await diagnoseTravelApproval(input, controller.signal)).toEqual(response)
    expect(http.post).toHaveBeenCalledWith('/admin/oa/travel-approvals/diagnose', input, {
      signal: controller.signal, timeout: 75_000,
    })
  })
})

import { expect, it, vi } from 'vitest'
import { http } from '@/api/http'
import { listRuntimeEvents } from '@/api/runtimeEvents'

vi.mock('@/api/http', () => ({ http: { get: vi.fn() } }))
it('queries the fixed admin endpoint with bounded filters and cancellation', async () => {
  vi.mocked(http.get).mockResolvedValue({ data: { data: { items: [] } } })
  const controller = new AbortController()
  expect(await listRuntimeEvents({ requestId: 'synthetic-request', before: 10 }, controller.signal)).toEqual({ items: [] })
  expect(http.get).toHaveBeenCalledWith('/admin/runtime-events', { params: { requestId: 'synthetic-request', before: 10 }, signal: controller.signal })
})

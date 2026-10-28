import { expect, it, vi } from 'vitest'

import { diagnoseReceipt } from '@/api/receiptDiagnostics'
import { http } from '@/api/http'

vi.mock('@/api/http', () => ({ http: { post: vi.fn() } }))

it('uploads one file to the admin endpoint with cancellation and bounded timeout', async () => {
  const report = { requestId: 'synthetic' }
  vi.mocked(http.post).mockResolvedValue({ data: { data: report } })
  const file = new File(['synthetic'], 'synthetic.png', { type: 'image/png' })
  const controller = new AbortController()
  expect(await diagnoseReceipt(file, controller.signal)).toEqual(report)
  const [url, body, options] = vi.mocked(http.post).mock.calls[0]!
  expect(url).toBe('/admin/receipts/diagnose')
  expect((body as FormData).getAll('files[]')).toEqual([file])
  expect(options).toEqual({ signal: controller.signal, timeout: 330_000 })
})

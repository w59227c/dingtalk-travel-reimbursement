import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { http, reimbursementContextHeaders, setCsrfToken, setReimbursementContext, setUnauthorizedHandler } from '@/api/http'

describe('HTTP session expiry handling', () => {
  afterEach(() => {
    setCsrfToken(null)
    setReimbursementContext(null)
    setUnauthorizedHandler(null)
  })

  it('uses the active context for reimbursement requests and honors a staged override', async () => {
    setReimbursementContext('active-delegation')
    const adapter = vi.fn(async (config: InternalAxiosRequestConfig) => ({
      data: {}, status: 200, statusText: 'OK', headers: {}, config,
    }))
    await http.get('/oa/travel-approvals', { adapter })
    await http.get('/reimbursements/workspace', { adapter, headers: reimbursementContextHeaders('next-delegation') })
    await http.get('/reimbursements/workspace', { adapter, headers: reimbursementContextHeaders(null) })
    await http.put('/admin/reimbursement-delegation', {}, { adapter })
    expect(adapter.mock.calls[0]?.[0].headers.get('X-Reimbursement-Context')).toBe('active-delegation')
    expect(adapter.mock.calls[1]?.[0].headers.get('X-Reimbursement-Context')).toBe('next-delegation')
    expect(adapter.mock.calls[2]?.[0].headers.toJSON()).not.toHaveProperty('X-Reimbursement-Context')
    expect(adapter.mock.calls[3]?.[0].headers.toJSON()).not.toHaveProperty('X-Reimbursement-Context')
  })

  it.each([null, 'previous-session'])(
    'ignores a late calculation 401 from before the current login (previous token: %s)', async (previousToken) => {
    const unauthorized = vi.fn()
    setUnauthorizedHandler(unauthorized)
    setCsrfToken(previousToken)
    let finish!: () => void
    let started!: () => void
    const requestStarted = new Promise<void>((resolve) => { started = resolve })
    const request = http.post('/calculate/totals', {}, {
      adapter: (config: InternalAxiosRequestConfig) => new Promise((_resolve, reject) => {
        finish = () => reject(new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
          data: {}, status: 401, statusText: 'Unauthorized', headers: {}, config,
        }))
        started()
      }),
    }).catch((error: unknown) => error)
    await requestStarted
    setCsrfToken('new-authenticated-session')
    finish()
    expect(await request).toBeInstanceOf(AxiosError)
    expect(unauthorized).not.toHaveBeenCalled()

    await expect(http.get('/me', {
      adapter: async (config) => {
        throw new AxiosError('Unauthorized', 'ERR_BAD_REQUEST', config, undefined, {
          data: {}, status: 401, statusText: 'Unauthorized', headers: {}, config,
        })
      },
    })).rejects.toBeInstanceOf(AxiosError)
    expect(unauthorized).toHaveBeenCalledOnce()
    },
  )
})

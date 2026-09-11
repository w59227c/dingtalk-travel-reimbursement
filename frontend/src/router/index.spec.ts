import { describe, expect, it } from 'vitest'

import router from './index'

describe('application routes', () => {
  it('serves the shared reimbursement flow at /m with the mobile presentation enabled', () => {
    const resolved = router.resolve('/m')
    const record = resolved.matched.at(-1)

    expect(resolved.name).toBe('mobile-reimburse')
    expect(record?.props.default).toEqual({ mobile: true })
  })
})

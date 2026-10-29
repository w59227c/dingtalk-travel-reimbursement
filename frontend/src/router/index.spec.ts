import { afterEach, describe, expect, it, vi } from 'vitest'
import { pinia } from '@/pinia'
import { useAuthStore } from '@/stores/auth'

import router from './index'

afterEach(() => vi.restoreAllMocks())

describe.each([
  ['/admin/settings', '/'], ['/m/settings', '/m'],
])('admin route guard %s', (settings, home) => {
  it.each([
    [false, false], [true, false], [false, true], [true, true],
  ])('refreshes cached role %s to current role %s before deciding access', async (cached, current) => {
    const auth = useAuthStore(pinia)
    auth.status = 'authenticated'
    auth.session = { user: { userId: 'synthetic-user', name: '合成员工' }, departments: [], selectedDepartment: null, csrfToken: 'synthetic-csrf', isAdmin: cached }
    vi.spyOn(auth, 'bootstrap').mockResolvedValue(undefined)
    const refresh = vi.spyOn(auth, 'refreshMe').mockImplementation(async () => { auth.session!.isAdmin = current })
    await router.push(home!); await router.push(settings!)
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.path).toBe(current ? settings : home)
  })
  it('refuses to render admin tools if a cached role cannot be verified', async () => {
    const auth = useAuthStore(pinia)
    auth.session = { user: { userId: 'synthetic-admin', name: '合成管理员' }, departments: [], selectedDepartment: null, csrfToken: 'synthetic-csrf', isAdmin: true }
    vi.spyOn(auth, 'bootstrap').mockResolvedValue(undefined)
    vi.spyOn(auth, 'refreshMe').mockRejectedValue(new Error('network unavailable'))
    await router.push(home!); await router.push(settings!)
    expect(router.currentRoute.value.path).toBe(home)
  })
})

describe('application routes', () => {
  it('serves the shared reimbursement flow at /m with the mobile presentation enabled', () => {
    const resolved = router.resolve('/m')
    const record = resolved.matched.at(-1)

    expect(resolved.name).toBe('mobile-reimburse')
    expect(record?.props.default).toEqual({ mobile: true })
  })

  it('keeps the settings round trip in the mobile presentation', () => {
    const resolved = router.resolve('/m/settings')
    const record = resolved.matched.at(-1)

    expect(resolved.name).toBe('mobile-admin-settings')
    expect(record?.props.default).toEqual({ mobile: true })
  })
})

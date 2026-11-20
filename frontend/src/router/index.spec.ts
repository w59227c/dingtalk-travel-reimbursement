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

  it('returns to reimbursement when refreshing a delegated session fails', async () => {
    const auth = useAuthStore(pinia)
    auth.session = { user: { userId: 'synthetic-beneficiary', name: '合成报销人' }, operator: { userId: 'synthetic-admin', name: '合成管理员' }, departments: [], selectedDepartment: null, csrfToken: 'synthetic-csrf', isAdmin: true }
    vi.spyOn(auth, 'bootstrap').mockResolvedValue(undefined)
    const refresh = vi.spyOn(auth, 'refreshMe').mockResolvedValue(undefined)
    await router.push(settings!)
    expect(router.currentRoute.value.path).toBe(settings)

    refresh.mockRejectedValue(new Error('network unavailable'))
    await router.push(home!)

    expect(refresh).toHaveBeenCalledTimes(2)
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
    const props = resolved.matched.at(-1)?.props.default
    if (typeof props !== 'function') throw new Error('Settings routes must resolve the active template tab')
    expect(resolved.name).toBe('mobile-admin-settings')
    expect(props({ ...resolved, name: resolved.name ?? undefined })).toEqual({ mobile: true, initialTemplateTab: 'travel' })
  })

  it.each(['/admin/settings', '/m/settings'])('opens the ordinary template directly from %s', (path) => {
    const resolved = router.resolve(`${path}#general-oa-template`)
    const props = resolved.matched.at(-1)?.props.default
    if (typeof props !== 'function') throw new Error('Settings routes must resolve the active template tab')
    expect(props({ ...resolved, name: resolved.name ?? undefined })).toMatchObject({ initialTemplateTab: 'general' })
  })
})

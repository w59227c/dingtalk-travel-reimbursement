import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getMe, selectDepartmentFromTravelApproval } from '@/api/auth'
import { useAuthStore } from '@/stores/auth'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { AuthSession, ReimbursementDepartmentResolution } from '@/types/auth'

vi.mock('@/api/auth', () => ({
  getMe: vi.fn(),
  getPublicConfig: vi.fn(),
  loginWithDingTalk: vi.fn(),
  loginWithMock: vi.fn(),
  logout: vi.fn(),
  selectDepartmentFromTravelApproval: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

const firstDepartment = { id: '100', name: '合成部门一' }
const secondDepartment = { id: '200', name: '合成部门二' }
const selection = {
  processInstanceId: 'synthetic-approval', profileKey: 'business',
  queryWindow: { from: '2026-10-01', to: '2026-11-01' },
}
function session(userId = 'synthetic-user'): AuthSession {
  return {
    user: { userId, name: '合成员工' },
    departments: [firstDepartment, secondDepartment], selectedDepartment: firstDepartment,
    isAdmin: false, csrfToken: 'synthetic-csrf',
  }
}

describe('authentication request ownership', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    setActivePinia(createPinia())
  })

  it.each(['new-user', 'department-roundtrip'])('rejects a late department resolution after %s', async (change) => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    const pending = deferred<ReimbursementDepartmentResolution>()
    vi.mocked(selectDepartmentFromTravelApproval).mockReturnValueOnce(pending.promise)
    const request = auth.selectDepartmentFromTravelApproval(selection)
    if (change === 'new-user') auth.session = session('replacement-user')
    else {
      auth.session.selectedDepartment = secondDepartment
      auth.session.selectedDepartment = firstDepartment
    }
    const drafts = useReimbursementDraftStore()
    drafts.loadError = '新会话自己的状态'
    pending.resolve({ selectedDepartment: secondDepartment, selectionRequired: false,
      departments: [secondDepartment] })
    await expect(request).rejects.toThrow('登录或部门状态已变化')
    expect(auth.session.selectedDepartment).toEqual(firstDepartment)
    expect(auth.session.departments).toEqual([firstDepartment, secondDepartment])
    expect(drafts.loadError).toBe('新会话自己的状态')
  })

  it('applies a department resolution from the same live session', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    const drafts = useReimbursementDraftStore()
    drafts.loadError = '原部门状态'
    vi.mocked(selectDepartmentFromTravelApproval).mockResolvedValueOnce({
      selectedDepartment: secondDepartment, selectionRequired: false,
      departments: [firstDepartment, secondDepartment],
    })
    await auth.selectDepartmentFromTravelApproval(selection)
    expect(auth.session.selectedDepartment).toEqual(secondDepartment)
    expect(drafts.loadError).toBe('')
  })

  it('does not replace a new session with an older identity read', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    const pending = deferred<AuthSession>()
    vi.mocked(getMe).mockReturnValueOnce(pending.promise)
    const request = auth.refreshMe()
    auth.session = session('replacement-user')
    const drafts = useReimbursementDraftStore()
    drafts.loadError = '新会话自己的状态'
    pending.resolve(session())
    await request
    expect(auth.session.user.userId).toBe('replacement-user')
    expect(drafts.loadError).toBe('新会话自己的状态')
  })

  it('uses the latest identity read when same-scope requests finish out of order', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    const older = deferred<AuthSession>()
    const newer = deferred<AuthSession>()
    vi.mocked(getMe).mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise)
    const first = auth.refreshMe(), second = auth.refreshMe()
    newer.resolve({ ...session(), isAdmin: false })
    await second
    older.resolve({ ...session(), isAdmin: true })
    await first
    expect(auth.session.isAdmin).toBe(false)
  })

  it('reads the actual session after a department change loses its response', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    vi.mocked(selectDepartmentFromTravelApproval).mockRejectedValueOnce(new Error('部门响应丢失'))
    vi.mocked(getMe).mockResolvedValueOnce({ ...session(), selectedDepartment: secondDepartment })
    await expect(auth.selectDepartmentFromTravelApproval(selection)).rejects.toThrow('部门响应丢失')
    expect(auth.session.selectedDepartment).toEqual(secondDepartment)
    expect(getMe).toHaveBeenCalledOnce()
  })

  it('offers login retry when both department change and session readback fail', async () => {
    const auth = useAuthStore()
    auth.session = session()
    auth.status = 'authenticated'
    vi.mocked(selectDepartmentFromTravelApproval).mockRejectedValueOnce(new Error('部门响应丢失'))
    vi.mocked(getMe).mockRejectedValueOnce(new Error('登录状态无法读取'))
    await expect(auth.selectDepartmentFromTravelApproval(selection)).rejects.toThrow()
    expect(auth.status).toBe('error')
    expect(auth.errorMessage).toContain('重新登录')
  })
})

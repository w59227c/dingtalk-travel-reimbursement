import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope } from 'vue'

import {
  createReimbursementDraft, getOaReimbursementOptions, getReimbursementDraft,
  listReimbursementDraftFiles, listWorkspaceReimbursements, openReimbursementWorkspace,
} from '@/api/reimbursements'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { authSessionFixture, draftFileListFixture, emptyTravelInputFixture, reimbursementDraftFixture, reimbursementFileFixture } from '@/tests/fixtures/reimbursements'
import type { AuthSession } from '@/types/auth'
import type { ReimbursementDraft, ReimbursementKind } from '@/types/reimbursements'
import { emptyReimbursementInput, useReimbursementWorkspaceReader } from './useReimbursementWorkspaceReader'

vi.mock('@/api/reimbursements')

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}
function draft(overrides: Partial<ReimbursementDraft> = {}): ReimbursementDraft {
  return reimbursementDraftFixture({
    id: 'synthetic-current',
    department: { id: '100', name: '合成部门' }, templateConfigVersion: 1,
    expiresAt: '2026-12-01T00:00:00Z', createdAt: '2026-11-01T00:00:00Z',
    updatedAt: '2026-11-01T00:00:00Z',
    template: { processCode: 'synthetic-template', configVersion: 1, schemaFingerprint: 'a'.repeat(64) },
    input: emptyTravelInputFixture(),
    totals: { expenseTotal: '0.00', subsidyTotal: '0.00', totalAmount: '0.00', receiptCount: 0,
      uppercaseAmount: '零元整', subsidy: null },
    ...overrides,
  })
}
const file = reimbursementFileFixture({
  id: 'synthetic-file', name: '合成材料.png', mediaType: 'image/png', sizeBytes: 128,
})
function employee(): AuthSession {
  return authSessionFixture({ user: { userId: 'synthetic-employee', name: '合成员工' },
    operator: { userId: 'synthetic-operator', name: '合成经办人' },
    reimbursementContext: 'synthetic-context',
    departments: [{ id: '200', name: '目标部门' }], selectedDepartment: { id: '200', name: '目标部门' } })
}

describe('reimbursement workspace reader', () => {
  let scope: EffectScope
  let currentScope: string
  let request: { kind?: ReimbursementKind; expectedDepartment: { id: string; name: string }; reimbursementContext: string | null }
  let drafts: ReturnType<typeof useReimbursementDraftStore>
  let reader: ReturnType<typeof useReimbursementWorkspaceReader>
  let server: ReimbursementDraft

  beforeEach(() => {
    vi.resetAllMocks()
    setActivePinia(createPinia())
    scope = effectScope()
    currentScope = 'synthetic-scope-1'
    request = { expectedDepartment: { id: '100', name: '合成部门' }, reimbursementContext: null }
    server = draft()
    drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft({ id: 'synthetic-original' })
    drafts.files = [file]
    reader = scope.run(() => useReimbursementWorkspaceReader({
      scope: () => currentScope, request: () => request, drafts,
    }))!
    vi.mocked(listWorkspaceReimbursements).mockImplementation(async () => ({ items: [server], total: 1 }))
    vi.mocked(getReimbursementDraft).mockImplementation(async () => structuredClone(server))
    vi.mocked(listReimbursementDraftFiles).mockImplementation(async () => draftFileListFixture(server, [file], []))
    vi.mocked(getOaReimbursementOptions).mockRejectedValue(new Error('合成选项不可用'))
  })
  afterEach(() => { scope.stop(); drafts.reset() })

  it.each(['empty', 'current', 'legacy'] as const)('classifies %s without selecting or creating a draft', async state => {
    const items = state === 'empty' ? [] : state === 'current' ? [server] : [server, draft({ id: 'synthetic-other' })]
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items, total: items.length })
    const result = await reader.list(reader.begin())
    expect(result.kind).toBe(state)
    if (result.kind === 'current') expect(result.draft.id).toBe(server.id)
    if (result.kind === 'legacy') expect(result.items).toEqual(items)
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(getReimbursementDraft).not.toHaveBeenCalled()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
  })

  it('preserves an authoritative read failure instead of treating it as an empty workspace', async () => {
    const error = new Error('合成工作区读取失败')
    vi.mocked(listWorkspaceReimbursements).mockRejectedValue(error)
    await expect(reader.list(reader.begin())).rejects.toBe(error)
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(createReimbursementDraft).not.toHaveBeenCalled()
  })

  it.each([false, true])('discards a superseded workspace response, failure=%s', async failure => {
    const pending = deferred<Awaited<ReturnType<typeof listWorkspaceReimbursements>>>()
    vi.mocked(listWorkspaceReimbursements).mockReturnValueOnce(pending.promise)
    const old = reader.list(reader.begin())
    const current = reader.begin()
    if (failure) pending.reject(new Error('旧请求失败'))
    else pending.resolve({ items: [server], total: 1 })
    await expect(old).resolves.toEqual({ kind: 'stale' })
    await expect(reader.list(current)).resolves.toEqual({ kind: 'current', draft: server })
  })

  it('captures request attributes without using the department as a workspace identity', async () => {
    request.kind = 'general'
    const context = reader.begin()
    request.expectedDepartment.name = '更正后的部门'
    request.kind = 'travel'
    await reader.list(context)
    expect(listWorkspaceReimbursements).toHaveBeenCalledWith({
      kind: 'general', expectedDepartment: { id: '100', name: '合成部门' }, reimbursementContext: null,
    })
    expect(reader.accepts(context)).toBe(true)
  })

  it.each(['scope', 'round-trip', 'dispose', 'empty'] as const)('rejects reads after %s', async reason => {
    const context = reader.begin()
    if (reason === 'scope') currentScope = 'synthetic-scope-2'
    if (reason === 'round-trip') { currentScope = 'synthetic-scope-2'; reader.invalidate(); currentScope = 'synthetic-scope-1' }
    if (reason === 'dispose') scope.stop()
    if (reason === 'empty') currentScope = ''
    expect(reader.accepts(context)).toBe(false)
    await expect(reader.list(context)).resolves.toEqual({ kind: 'stale' })
    await expect(reader.load(context, server.id)).resolves.toEqual({ kind: 'stale' })
    expect(listWorkspaceReimbursements).not.toHaveBeenCalled()
    expect(getReimbursementDraft).not.toHaveBeenCalled()
  })

  it('adopts a consistent draft and materials through the real store', async () => {
    // The first pair describes different revisions; the Store must read again.
    vi.mocked(listReimbursementDraftFiles).mockResolvedValueOnce({ draftId: server.id, revision: 0, items: [] })
    await expect(reader.load(reader.begin(), server.id)).resolves.toEqual({ kind: 'loaded', draft: server })
    expect(getReimbursementDraft).toHaveBeenCalledTimes(2)
    expect(drafts.files).toEqual([file])
  })

  it('does not adopt a late detail response over the next read', async () => {
    const pending = deferred<ReimbursementDraft>()
    vi.mocked(getReimbursementDraft).mockReturnValueOnce(pending.promise)
    const old = reader.load(reader.begin(), server.id)
    reader.begin()
    pending.resolve(server)
    await expect(old).resolves.toEqual({ kind: 'stale' })
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(drafts.files).toEqual([file])
  })

  it.each(['workspace', 'saved', 'history'] as const)('uses the %s adoption policy', async source => {
    server.workspaceRetired = true
    const result = await reader.load(reader.begin(), server.id, { source })
    expect(result.kind).toBe(source === 'saved' ? 'loaded' : 'rejected')
    expect(drafts.currentDraft?.id).toBe(source === 'saved' ? server.id : 'synthetic-original')
    if (source === 'history') expect(getReimbursementDraft).toHaveBeenCalledWith(server.id, expect.objectContaining({ history: true }))
  })

  it('reads locked submitted history even after it has exited the current workspace', async () => {
    server.status = 'LOCKED'
    server.workspaceRetired = true
    await expect(reader.load(reader.begin(), server.id, { source: 'history' })).resolves.toEqual({ kind: 'loaded', draft: server })
  })

  it('keeps the prior form and materials when the view rejects changed business input', async () => {
    const accept = vi.fn(() => false)
    await expect(reader.load(reader.begin(), server.id, { accept })).resolves.toEqual({ kind: 'rejected', message: '' })
    expect(accept).toHaveBeenCalledOnce()
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(drafts.files).toEqual([file])
  })

  it('does not mistake a cached newer revision for a successful read', async () => {
    drafts.currentDraft = draft({ revision: 9 })
    await expect(reader.load(reader.begin(), server.id)).resolves.toEqual({ kind: 'rejected', message: '' })
    expect(drafts.currentDraft.revision).toBe(9)
  })

  it('reports inconsistent file revisions without adopting either half', async () => {
    vi.mocked(listReimbursementDraftFiles).mockResolvedValue({ draftId: server.id, revision: 0, items: [] })
    const result = await reader.load(reader.begin(), server.id)
    expect(result).toEqual({ kind: 'rejected', message: '报销内容读取期间持续发生变化，请稍后重试' })
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(drafts.files).toEqual([file])
  })

  it('prepares an existing employee workspace despite an options failure without adopting it', async () => {
    const target = employee()
    server.department = target.selectedDepartment!
    const result = await reader.prepareEmployee(target, new AbortController().signal)
    expect(result?.read.draft).toEqual(server)
    expect(result?.read.files).toEqual([file])
    expect(result?.options.status).toBe('rejected')
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
    expect(getReimbursementDraft).toHaveBeenCalledWith(server.id, expect.objectContaining({ reimbursementContext: 'synthetic-context' }))
    expect(createReimbursementDraft).not.toHaveBeenCalled()
  })

  it('uses the renewed employee context after opening their saved department', async () => {
    const target = employee()
    vi.mocked(openReimbursementWorkspace).mockResolvedValue({ selectedDepartment: server.department, reimbursementContext: 'synthetic-renewed-context' })
    await reader.prepareEmployee(target, new AbortController().signal)
    expect(target.selectedDepartment).toEqual(server.department)
    expect(target.reimbursementContext).toBe('synthetic-renewed-context')
    expect(getReimbursementDraft).toHaveBeenCalledWith(server.id, expect.objectContaining({ reimbursementContext: 'synthetic-renewed-context' }))
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
  })

  it.each(['legacy', 'department', 'options'] as const)('does not create an employee draft when %s remains unresolved', async reason => {
    const target = employee()
    const items = reason === 'legacy' ? [server, draft({ id: 'synthetic-legacy' })] : []
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items, total: items.length })
    if (reason === 'department') target.selectedDepartment = null
    const result = reader.prepareEmployee(target, new AbortController().signal)
    if (reason === 'options') await expect(result).rejects.toThrow('合成选项不可用')
    else await expect(result).resolves.toBeNull()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
  })

  it('prepares an empty employee workspace with reuseExisting and consistent readback', async () => {
    const target = employee()
    vi.mocked(listWorkspaceReimbursements).mockResolvedValue({ items: [], total: 0 })
    vi.mocked(getOaReimbursementOptions).mockResolvedValue({ reimbursementProcessCode: 'synthetic-template', templateConfigVersion: 1, companyOptions: [], budgetCodeOptions: [], travelProfiles: [] })
    vi.mocked(createReimbursementDraft).mockResolvedValue(server)
    const controller = new AbortController()
    const result = await reader.prepareEmployee(target, controller.signal)
    expect(result?.read.draft.id).toBe(server.id)
    expect(createReimbursementDraft).toHaveBeenCalledWith(emptyReimbursementInput('travel'), {
      signal: controller.signal, reimbursementContext: 'synthetic-context', reuseExisting: true,
      expectedDepartment: target.selectedDepartment,
    })
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
  })

  it.each(['cancel', 'scope', 'supersede'] as const)('stops preparing an employee after %s before creating anything', async reason => {
    const pending = deferred<Awaited<ReturnType<typeof listWorkspaceReimbursements>>>()
    vi.mocked(listWorkspaceReimbursements).mockReturnValue(pending.promise)
    const controller = new AbortController()
    const result = reader.prepareEmployee(employee(), controller.signal)
    if (reason === 'cancel') controller.abort()
    if (reason === 'scope') currentScope = 'synthetic-scope-2'
    if (reason === 'supersede') reader.begin()
    pending.resolve({ items: [], total: 0 })
    await expect(result).resolves.toBeUndefined()
    expect(createReimbursementDraft).not.toHaveBeenCalled()
    expect(openReimbursementWorkspace).not.toHaveBeenCalled()
  })

  it('ignores a cancelled department opening without mutating the target or reading its files', async () => {
    const target = employee()
    const pending = deferred<Awaited<ReturnType<typeof openReimbursementWorkspace>>>()
    vi.mocked(openReimbursementWorkspace).mockReturnValue(pending.promise)
    const controller = new AbortController()
    const result = reader.prepareEmployee(target, controller.signal)
    await vi.waitFor(() => expect(openReimbursementWorkspace).toHaveBeenCalledOnce())
    controller.abort()
    pending.resolve({ selectedDepartment: server.department, reimbursementContext: 'synthetic-late-context' })
    await expect(result).resolves.toBeUndefined()
    expect(target.reimbursementContext).toBe('synthetic-context')
    expect(getReimbursementDraft).not.toHaveBeenCalled()
  })

  it('rejects an employee workspace that was retired during its consistent read', async () => {
    server.workspaceRetired = true
    const target = employee()
    target.selectedDepartment = server.department
    await expect(reader.prepareEmployee(target, new AbortController().signal)).rejects.toThrow('目标报销已发生变化')
    expect(drafts.currentDraft?.id).toBe('synthetic-original')
  })
})

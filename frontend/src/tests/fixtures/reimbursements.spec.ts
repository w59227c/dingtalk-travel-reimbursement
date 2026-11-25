import { describe, expect, it } from 'vitest'

import {
  authSessionFixture, draftFileListFixture, manualExpenseInputFixture, reimbursementDraftFixture,
  reimbursementFileFixture, savedExpenseReadbackFixture,
} from './reimbursements'

describe('synthetic reimbursement fixture isolation', () => {
  it('keeps caller overrides and later draft snapshots independent', () => {
    const input = manualExpenseInputFixture()
    const first = reimbursementDraftFixture({ input })
    const second = reimbursementDraftFixture({ input })
    first.input.items[0]!.description = 'only the first snapshot changed'
    first.totals.totalAmount = '88.88'
    expect(second.input.items[0]?.description).toBe('打车费')
    expect(input.items[0]?.description).toBe('打车费')
    expect(second.totals.totalAmount).toBe('44.89')
  })

  it('replaces nested input without adding defaults to deliberately incomplete content', () => {
    const input = manualExpenseInputFixture()
    input.items = []
    const saved = reimbursementDraftFixture({ input })
    expect(saved.input).toEqual(input)
    expect(saved.input).not.toHaveProperty('editingState')
    expect(saved.input.items).toEqual([])
    expect(saved.input).not.toBe(input)
  })

  it('isolates a delegated session from its original directory and subsequent sessions', () => {
    const department = { id: '200', name: '合成目标部门' }
    const original = authSessionFixture({ departments: [department], selectedDepartment: department })
    const delegated = authSessionFixture({ ...original, reimbursementContext: 'synthetic-context' })
    delegated.user.name = 'only delegated display changed'
    delegated.selectedDepartment!.name = 'only delegated department changed'
    expect(original.user.name).toBe('测试用户')
    expect(original.departments[0]?.name).toBe('合成目标部门')
    expect(department.name).toBe('合成目标部门')
    expect(authSessionFixture()).not.toHaveProperty('reimbursementContext')
  })

  it('freezes a planned file-list response before the synthetic server changes', () => {
    const draft = reimbursementDraftFixture()
    const files = [reimbursementFileFixture()]
    const response = draftFileListFixture(draft, files)
    draft.revision += 1
    files[0]!.status = 'DELETING'
    files.push(reimbursementFileFixture({ id: 'synthetic-second' }))
    expect(response.revision).toBe(1)
    expect(response.items).toHaveLength(1)
    expect(response.items[0]?.status).toBe('ACTIVE')
    expect(response).not.toHaveProperty('pendingUploads')
  })

  it('keeps a request independent from its contract-shaped server readback', () => {
    const { request, readback } = savedExpenseReadbackFixture()
    request.items[0]!.description = 'typed after the request was sent'
    request.items[0]!.receiptAttachmentFileIds!.push('synthetic-later-file')
    expect(readback.items[0]?.description).toBe('合成员工已保存的修改')
    expect(readback.items[0]).not.toHaveProperty('receiptAttachmentFileIds')
    expect(savedExpenseReadbackFixture().request.items[0]?.receiptAttachmentFileIds).toEqual([])
  })
})

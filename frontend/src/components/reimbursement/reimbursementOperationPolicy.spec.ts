import { describe, expect, it } from 'vitest'

import {
  reimbursementEditingAvailability, reimbursementOutputAvailability, reimbursementWorkspaceAvailability,
  type ReimbursementOperationState, type ReimbursementOutputInput, type WorkspaceOperationInput,
} from './reimbursementOperationPolicy'

function state(): ReimbursementOperationState {
  return {
    draft: { status: 'DRAFT', retired: false, recoveryPending: false, expired: false, unavailable: false, trackedSubmission: false },
    mode: { general: false, administrator: false },
    options: { unavailable: false, changed: false, needsAdministrator: false },
    verification: { initializationFailed: false, delegationRevoked: false, workspaceUncertain: false, reloadRequired: false },
    activity: { switchingEmployee: false, initializing: false, bindingDepartment: false,
      pendingDepartmentApproval: false, verifyingApprovals: false, submitting: false, submissionRequest: false,
      recovering: false, replacingTemplate: false, confirmingReload: false, processingFiles: false,
      materialEditor: false, busy: false, saving: false, refreshingMaterials: false },
  }
}
function output(overrides: Partial<ReimbursementOutputInput> = {}): ReimbursementOutputInput {
  return { mobile: false, failedUploadReason: '', companyError: '', budgetError: '', accountingChanged: false,
    approvalReconfirmationRequired: false, approvalVerificationPending: false, companySelected: true,
    budgetSelected: true, expenseCount: 1, itemReadinessError: '', subsidyInputError: '', subsidyCalculationReason: '',
    serviceReason: '', hasActiveMaterials: true, pendingUploadCount: 0, materialIssueCount: 0,
    missingMaterialExpenseCount: 0, saveError: '', formDirty: false, ...overrides }
}
function workspace(overrides: Partial<WorkspaceOperationInput> = {}): WorkspaceOperationInput {
  return { departmentUnconfirmed: false, failedUploadReason: '', awaitingMaterials: false,
    hasUnsavedChanges: false, ...overrides }
}
function availability(observation: ReimbursementOperationState, input = output()) {
  return reimbursementOutputAvailability(observation, reimbursementEditingAvailability(observation), input)
}

describe('reimbursement operation policy', () => {
  it.each(['DRAFT', 'REVIEW_READY'] as const)('permits editing, preview and submission of a ready %s', status => {
    const observation = state()
    observation.draft!.status = status
    expect(reimbursementEditingAvailability(observation)).toEqual({ allowed: true, reason: '' })
    expect(availability(observation)).toEqual({ previewReason: '', submitReason: '' })
    expect(reimbursementWorkspaceAvailability(observation, reimbursementEditingAvailability(observation), workspace()))
      .toEqual({ operationReason: '', switchBlockReason: '', switchRecheckReason: '' })
  })

  it.each(['locked', 'expired', 'retired', 'recovery', 'unavailable', 'tracked'] as const)(
    'blocks editing a %s record while preserving its actual state explanation', condition => {
      const observation = state()
      const expected = {
        locked: '本次报销内容已锁定，请查看提交进度', expired: '本次填写内容已过期，请重新填写',
        retired: '已新开另一笔报销，请重新进入当前报销', recovery: '材料尚在恢复，请等待恢复完成',
        unavailable: '当前报销记录已不可用，请新开一笔重新填写', tracked: '本次报销内容已锁定，请查看提交进度',
      }
      if (condition === 'locked') observation.draft!.status = 'LOCKED'
      if (condition === 'expired') { observation.draft!.status = 'EXPIRED'; observation.draft!.expired = true }
      if (condition === 'retired') observation.draft!.retired = true
      if (condition === 'recovery') observation.draft!.recoveryPending = true
      if (condition === 'unavailable') observation.draft!.unavailable = true
      if (condition === 'tracked') observation.draft!.trackedSubmission = true
      expect(reimbursementEditingAvailability(observation)).toEqual({ allowed: false, reason: expected[condition] })
      expect(availability(observation).submitReason).toBe(expected[condition])
    },
  )

  it.each([false, true])('keeps authority and configuration errors visible, administrator=%s', administrator => {
    const observation = state()
    observation.mode.administrator = administrator
    observation.options.unavailable = true
    observation.options.needsAdministrator = true
    expect(reimbursementEditingAvailability(observation)).toEqual({ allowed: false, reason: administrator
      ? 'OA 表单需要确认，请前往系统设置处理' : 'OA 表单需要管理员确认，请联系管理员处理' })
    observation.verification.delegationRevoked = true
    observation.draft = null
    expect(reimbursementEditingAvailability(observation)).toEqual({ allowed: false, reason: '代报权限已失效，请返回本人报销' })
  })

  it('explains preparation and a failed initialization before template configuration', () => {
    const observation = state()
    observation.draft = null
    observation.verification.initializationFailed = true
    observation.options.unavailable = true
    expect(reimbursementEditingAvailability(observation).reason).toBe('正在准备报销表单')
    observation.draft = state().draft
    expect(reimbursementEditingAvailability(observation).reason).toBe('请先重新加载当前报销')
    expect(availability(observation).previewReason).toBe('请先重新加载当前报销')
  })

  it('prioritizes retired and expired records over recovery, lock and template warnings', () => {
    const observation = state()
    Object.assign(observation.draft!, { status: 'LOCKED', retired: true, expired: true, recoveryPending: true })
    observation.options.changed = true
    expect(reimbursementEditingAvailability(observation).reason).toBe('已新开另一笔报销，请重新进入当前报销')
    observation.draft!.retired = false
    expect(reimbursementEditingAvailability(observation).reason).toBe('本次填写内容已过期，请重新填写')
    expect(availability(observation).previewReason).toBe('保留的材料已过期，请新开报销并重新上传材料')
  })

  it('preserves unsaved conflict checks before configuration errors', () => {
    const observation = state()
    observation.verification.reloadRequired = true
    observation.options.unavailable = true
    observation.options.changed = true
    const editing = reimbursementEditingAvailability(observation)
    expect(editing.reason).toBe('报销内容已更新，请核对最新报销内容')
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ hasUnsavedChanges: true })).switchRecheckReason)
      .toBe('请先核对最新报销内容')
  })

  it.each([false, true])('keeps the travel/general approval-verification distinction, general=%s', general => {
    const observation = state()
    observation.mode.general = general
    observation.activity.verifyingApprovals = true
    const editing = reimbursementEditingAvailability(observation)
    expect(editing.allowed).toBe(!general)
    expect(editing.reason).toBe('')
    expect(availability(observation, output({ approvalVerificationPending: true })).submitReason).toBe('正在核验关联审批，请稍候')
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请等待当前操作完成')
  })

  it.each(['switchingEmployee', 'bindingDepartment', 'submitting', 'recovering'] as const)(
    'blocks editing during %s', activity => {
      const observation = state()
      observation.activity[activity] = true
      expect(reimbursementEditingAvailability(observation).allowed).toBe(false)
      expect(reimbursementEditingAvailability(observation).reason).not.toBe('')
      expect(reimbursementWorkspaceAvailability(observation, reimbursementEditingAvailability(observation), workspace()).operationReason)
        .toBe('请等待当前操作完成')
    },
  )

  it.each(['initializing', 'pendingDepartmentApproval', 'submissionRequest', 'replacingTemplate', 'confirmingReload'] as const)(
    'blocks workspace changes during %s without replacing editing rules', activity => {
      const observation = state()
      observation.activity[activity] = true
      const editing = reimbursementEditingAvailability(observation)
      expect(editing.allowed).toBe(true)
      expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请等待当前操作完成')
    },
  )

  it('requires department confirmation before reporting concurrent operations', () => {
    const observation = state()
    observation.activity.busy = true
    observation.activity.processingFiles = true
    observation.activity.bindingDepartment = true
    const editing = reimbursementEditingAvailability(observation)
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ departmentUnconfirmed: true })).operationReason)
      .toBe('请先确认办理部门')
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请等待当前操作完成')
  })

  it('keeps file processing, material editor and generic busy explanations in order', () => {
    const observation = state()
    Object.assign(observation.activity, { processingFiles: true, materialEditor: true, busy: true })
    const editing = reimbursementEditingAvailability(observation)
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请等待本批文件上传和识别完成')
    observation.activity.processingFiles = false
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请先完成材料编辑')
    observation.activity.materialEditor = false
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).operationReason).toBe('请等待当前报销操作完成')
    observation.activity.busy = false
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ failedUploadReason: '合成上传失败' })).switchBlockReason)
      .toBe('合成上传失败')
  })

  it('keeps revoked-delegate return eligibility separate from ordinary switching rechecks', () => {
    const observation = state()
    observation.verification.delegationRevoked = true
    const editing = reimbursementEditingAvailability(observation)
    const policy = reimbursementWorkspaceAvailability(observation, editing, workspace({ hasUnsavedChanges: true }))
    expect(policy.switchBlockReason).toBe('')
    expect(policy.switchRecheckReason).toBe('代报权限已失效，请返回本人报销')
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ awaitingMaterials: true })).switchRecheckReason)
      .toBe('请等待本批文件上传和识别完成')
    observation.verification.workspaceUncertain = true
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ awaitingMaterials: true })).switchRecheckReason)
      .toBe('请先核对当前报销，确认新开结果后再切换')
  })

  it('requires a discard check only for actual unsaved changes on a read-only record', () => {
    const observation = state()
    observation.options.changed = true
    const editing = reimbursementEditingAvailability(observation)
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace()).switchRecheckReason).toBe('')
    expect(reimbursementWorkspaceAvailability(observation, editing, workspace({ hasUnsavedChanges: true })).switchRecheckReason)
      .toBe('OA 表单已更新，请按新表单重新填写')
  })

  it.each([false, true].flatMap(mobile => [false, true].map(general => ({ mobile, general }))))(
    'uses the intended save-before-preview workflow, mobile=$mobile general=$general', ({ mobile, general }) => {
      const observation = state()
      observation.mode.general = general
      const expected = mobile ? '正在保存当前内容，请稍候' : ''
      expect(availability(observation, output({ mobile, formDirty: true })).previewReason).toBe(expected)
      expect(availability(observation, output({ mobile, saveError: '合成保存失败' })).previewReason)
        .toBe(mobile ? '保存失败，请重试保存后再打开 Excel' : '')
      expect(availability(observation, output({ mobile, formDirty: true })).submitReason).toBe('')
    },
  )

  it.each([false, true])('can preview frozen submitted content without accepting another submission, general=%s', general => {
    const observation = state()
    observation.mode.general = general
    Object.assign(observation.draft!, { status: 'LOCKED', retired: true, trackedSubmission: true })
    observation.options.changed = true
    const result = availability(observation, output({ mobile: true, formDirty: true, saveError: '旧输入错误',
      pendingUploadCount: 1, materialIssueCount: 1, subsidyCalculationReason: '旧补助输入不完整' }))
    expect(result.previewReason).toBe('')
    expect(result.submitReason).toBe('已新开另一笔报销，请重新进入当前报销')
  })

  it('keeps an ordinary reimbursement expense/material requirement separate from travel subsidy-only output', () => {
    const observation = state()
    const noExpenses = output({ expenseCount: 0, hasActiveMaterials: false })
    expect(availability(observation, noExpenses).previewReason).toBe('')
    observation.mode.general = true
    expect(availability(observation, noExpenses).previewReason).toBe('请先添加费用明细')
    expect(availability(observation, output({ hasActiveMaterials: false })).previewReason).toBe('请先上传报销材料')
  })

  it.each([
    ['itemReadinessError', '逐项选择费用科目'], ['companyError', '原报销主体已停用'],
    ['budgetError', '原预算代码已停用'], ['subsidyInputError', '补助时段不完整'],
  ] as const)('preserves the actual %s reason for preview', (field, message) => {
    expect(availability(state(), output({ [field]: message })).previewReason).toBe(message)
  })

  it('shows failed upload before incomplete fields and prevents both outputs', () => {
    const result = availability(state(), output({ failedUploadReason: '合成上传失败', companyError: '合成主体错误',
      pendingUploadCount: 2, materialIssueCount: 3, missingMaterialExpenseCount: 4 }))
    expect(result).toEqual({ previewReason: '合成上传失败', submitReason: '合成上传失败' })
  })

  it('retains unresolved material counts and their different next actions', () => {
    const observation = state()
    let result = availability(observation, output({ pendingUploadCount: 2, materialIssueCount: 3, missingMaterialExpenseCount: 4 }))
    expect(result).toEqual({ previewReason: '请等待材料上传完成后再预览 Excel', submitReason: '有材料尚未完成上传，请查看上传进度' })
    result = availability(observation, output({ materialIssueCount: 3, missingMaterialExpenseCount: 4 }))
    expect(result).toEqual({ previewReason: '请先处理全部待确认或未完成的材料（含待关联材料），再预览 Excel', submitReason: '还有 3 份材料需处理后才能提交 OA' })
    expect(availability(observation, output({ missingMaterialExpenseCount: 4 })).submitReason).toBe('还有 4 笔费用需补材料')
  })

  it('keeps service readiness exclusive to submission and preserves higher-priority accounting errors', () => {
    const observation = state()
    expect(availability(observation, output({ serviceReason: 'OA提交服务未开启，可继续填写' })))
      .toEqual({ previewReason: '', submitReason: 'OA提交服务未开启，可继续填写' })
    expect(availability(observation, output({ accountingChanged: true, serviceReason: 'OA 服务不可用' })))
      .toEqual({ previewReason: '请重新关联出差审批', submitReason: '请重新关联出差审批' })
  })
})

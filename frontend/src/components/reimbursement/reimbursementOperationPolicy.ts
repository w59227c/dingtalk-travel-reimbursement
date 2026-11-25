import type { ReimbursementDraftStatus } from '@/types/reimbursements'

/** Server observations and active UI operations, independent of unsaved form values. */
export interface ReimbursementOperationState {
  draft: {
    status: ReimbursementDraftStatus
    retired: boolean
    recoveryPending: boolean
    expired: boolean
    unavailable: boolean
    trackedSubmission: boolean
  } | null
  mode: { general: boolean; administrator: boolean }
  options: { unavailable: boolean; changed: boolean; needsAdministrator: boolean }
  verification: {
    initializationFailed: boolean
    delegationRevoked: boolean
    workspaceUncertain: boolean
    reloadRequired: boolean
  }
  activity: {
    switchingEmployee: boolean
    initializing: boolean
    bindingDepartment: boolean
    pendingDepartmentApproval: boolean
    verifyingApprovals: boolean
    submitting: boolean
    submissionRequest: boolean
    recovering: boolean
    replacingTemplate: boolean
    confirmingReload: boolean
    processingFiles: boolean
    materialEditor: boolean
    busy: boolean
    saving: boolean
    refreshingMaterials: boolean
  }
}
export interface EditingAvailability { allowed: boolean; reason: string }

/** One ordered decision supplies the form lock and its visible explanation. */
export function reimbursementEditingAvailability(state: ReimbursementOperationState): EditingAvailability {
  const { draft, activity, options, verification } = state
  let reason = ''
  if (verification.delegationRevoked) reason = '代报权限已失效，请返回本人报销'
  else if (!draft) reason = '正在准备报销表单'
  else if (verification.initializationFailed) reason = '请先重新加载当前报销'
  else if (activity.switchingEmployee) reason = '正在切换报销人，请稍候'
  else if (activity.bindingDepartment) reason = '正在确认报销部门，请稍候'
  else if (verification.workspaceUncertain) reason = '请先核对当前报销，确认新开结果后继续'
  else if (draft.retired) reason = '已新开另一笔报销，请重新进入当前报销'
  else if (draft.unavailable) reason = '当前报销记录已不可用，请新开一笔重新填写'
  else if (draft.expired) reason = '本次填写内容已过期，请重新填写'
  else if (draft.recoveryPending || activity.recovering) reason = '材料尚在恢复，请等待恢复完成'
  else if (activity.submitting) reason = '正在提交，请稍候'
  else if (draft.status === 'LOCKED' || draft.trackedSubmission) reason = '本次报销内容已锁定，请查看提交进度'
  else if (verification.reloadRequired) reason = '报销内容已更新，请核对最新报销内容'
  else if (options.unavailable) reason = options.needsAdministrator
    ? state.mode.administrator ? 'OA 表单需要确认，请前往系统设置处理' : 'OA 表单需要管理员确认，请联系管理员处理'
    : '当前 OA 模板或选项不可用，请重新加载后继续填写'
  else if (options.changed) reason = 'OA 表单已更新，请按新表单重新填写'
  // General approval verification uses its inline progress UI instead of a read-only banner.
  return { allowed: !reason && !(state.mode.general && activity.verifyingApprovals), reason }
}

export interface WorkspaceOperationInput {
  departmentUnconfirmed: boolean
  failedUploadReason: string
  awaitingMaterials: boolean
  hasUnsavedChanges: boolean
}

/** A revoked delegate may return to their own scope after the caller rechecks permission. */
export function reimbursementWorkspaceAvailability(
  state: ReimbursementOperationState, editing: EditingAvailability, input: WorkspaceOperationInput,
) {
  const { activity, verification } = state
  let operationReason = ''
  if (input.departmentUnconfirmed) operationReason = '请先确认办理部门'
  else if (activity.switchingEmployee || activity.initializing || activity.bindingDepartment
    || activity.pendingDepartmentApproval || activity.verifyingApprovals || activity.submitting
    || activity.submissionRequest || activity.recovering || activity.replacingTemplate
    || activity.confirmingReload) operationReason = '请等待当前操作完成'
  else if (activity.processingFiles) operationReason = '请等待本批文件上传和识别完成'
  else if (activity.materialEditor) operationReason = '请先完成材料编辑'
  else if (activity.busy || activity.saving || activity.refreshingMaterials) operationReason = '请等待当前报销操作完成'

  let switchRecheckReason = ''
  if (verification.workspaceUncertain) switchRecheckReason = '请先核对当前报销，确认新开结果后再切换'
  else if (input.awaitingMaterials) switchRecheckReason = '请等待本批文件上传和识别完成'
  else if (verification.reloadRequired) switchRecheckReason = '请先核对最新报销内容'
  else if (state.draft && !editing.allowed && input.hasUnsavedChanges) switchRecheckReason = editing.reason

  return { operationReason, switchBlockReason: operationReason || input.failedUploadReason, switchRecheckReason }
}

export interface ReimbursementOutputInput {
  mobile: boolean
  failedUploadReason: string
  companyError: string
  budgetError: string
  accountingChanged: boolean
  approvalReconfirmationRequired: boolean
  approvalVerificationPending: boolean
  companySelected: boolean
  budgetSelected: boolean
  expenseCount: number
  itemReadinessError: string
  subsidyInputError: string
  subsidyCalculationReason: string
  serviceReason: string
  hasActiveMaterials: boolean
  pendingUploadCount: number
  materialIssueCount: number
  missingMaterialExpenseCount: number
  saveError: string
  formDirty: boolean
}

/** Preview may read frozen content; submitting always uses the editable saved workflow. */
export function reimbursementOutputAvailability(
  state: ReimbursementOperationState, editing: EditingAvailability, input: ReimbursementOutputInput,
) {
  const { draft, activity, verification } = state
  const submitReason = editing.reason || input.failedUploadReason || input.companyError || input.budgetError
    || (input.accountingChanged ? '请重新关联出差审批' : '')
    || (input.approvalReconfirmationRequired ? '请先重新确认出差审批' : '')
    || input.subsidyInputError || input.subsidyCalculationReason || input.serviceReason
    || (input.approvalVerificationPending ? '正在核验关联审批，请稍候' : '')
    || (activity.busy ? '请等待材料处理完成' : '')
    || (input.pendingUploadCount ? '有材料尚未完成上传，请查看上传进度' : '')
    || (input.materialIssueCount ? `还有 ${input.materialIssueCount} 份材料需处理后才能提交 OA` : '')
    || (input.missingMaterialExpenseCount ? `还有 ${input.missingMaterialExpenseCount} 笔费用需补材料` : '')

  const previewReason = (verification.initializationFailed ? '请先重新加载当前报销' : '')
    || input.failedUploadReason
    || (activity.processingFiles ? '请等待本批文件上传和识别完成后再预览 Excel' : '')
    || (draft?.unavailable ? '当前报销记录已不可用，请新开一笔重新填写' : '')
    || (draft?.recoveryPending && draft.expired ? '保留的材料已过期，请新开报销并重新上传材料' : '')
    || (draft?.recoveryPending || activity.recovering ? '请等待材料恢复完成后再预览 Excel' : '')
    || (verification.workspaceUncertain ? '请先核对当前报销，确认新开结果后再预览 Excel' : '')
    || (draft?.retired && draft.status !== 'LOCKED' ? '请先打开当前报销' : '')
    || (state.mode.general && state.options.changed && draft?.status !== 'LOCKED' ? 'OA 表单已更新，请按新表单重新填写' : '')
    || input.companyError || input.budgetError || input.itemReadinessError
    || (state.mode.general && editing.allowed
      ? (!input.expenseCount ? '请先添加费用明细' : !input.hasActiveMaterials ? '请先上传报销材料' : '') : '')
    || (verification.reloadRequired ? '请先核对最新报销内容' : '')
    || (input.accountingChanged ? '请重新关联出差审批' : '')
    || input.subsidyInputError || (editing.allowed ? input.subsidyCalculationReason : '')
    || (editing.allowed && input.pendingUploadCount ? '请等待材料上传完成后再预览 Excel' : '')
    || (editing.allowed && input.materialIssueCount ? '请先处理全部待确认或未完成的材料（含待关联材料），再预览 Excel' : '')
    || (state.mode.general && !input.companySelected ? '请先选择报销主体' : '')
    || (!input.budgetSelected ? '请先选择预算代码' : '')
    || (input.approvalVerificationPending ? '正在核验关联审批，请稍候' : '')
    || (input.mobile && editing.allowed && input.saveError ? '保存失败，请重试保存后再打开 Excel' : '')
    || (input.mobile && editing.allowed && (activity.saving || input.formDirty) ? '正在保存当前内容，请稍候' : '')
    || (activity.submitting ? '正在提交，请稍候' : '')
  return { previewReason, submitReason }
}

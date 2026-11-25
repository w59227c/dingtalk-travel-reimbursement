<script setup lang="ts">
import { inputSignature } from '@/utils/reimbursementInput'
import axios from 'axios'
import { ArrowLeft, CircleCloseFilled, InfoFilled, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRouter } from 'vue-router'

import ExpenseItemsCard from '@/components/reimbursement/ExpenseItemsCard.vue'
import { useReimbursementSaveCoordinator } from '@/components/reimbursement/useReimbursementSaveCoordinator'
import { reimbursementEditingAvailability, reimbursementWorkspaceAvailability, reimbursementOutputAvailability, type ReimbursementOperationState } from '@/components/reimbursement/reimbursementOperationPolicy'
import { emptyReimbursementInput, useReimbursementWorkspaceReader, type WorkspaceReadContext } from '@/components/reimbursement/useReimbursementWorkspaceReader'
import ApprovalAccountingLine from '@/components/reimbursement/ApprovalAccountingLine.vue'
import ReimbursementEmployeePicker from '@/components/reimbursement/ReimbursementEmployeePicker.vue'
import ReimbursementReviewOverview from '@/components/reimbursement/ReimbursementReviewOverview.vue'
import ExpenseSummaryCard from '@/components/reimbursement/ExpenseSummaryCard.vue'
import ReimbursementAttentionNotice from '@/components/reimbursement/ReimbursementAttentionNotice.vue'
import TravelApprovalSelector from '@/components/reimbursement/TravelApprovalSelector.vue'
import TripSubsidyCard from '@/components/reimbursement/TripSubsidyCard.vue'
import GeneralApprovalPicker from '@/components/reimbursement/GeneralApprovalPicker.vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { getMe, prepareReimbursementContext, selectDepartmentForGeneralReimbursement } from '@/api/auth'
import {
  correctReimbursementDepartment,
  openReimbursementWorkspace,
  getOaReimbursementSubmissionForDraft,
  getReimbursementDraft,
  listReimbursementSubmissionHistory,
} from '@/api/reimbursements'
import { isDelegationContextError, useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useHealthStore } from '@/stores/health'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'
import { isForeignExpense, type TripType } from '@/types/expenses'
import type { AuthSession, ReimbursementEmployee } from '@/types/auth'
import type {
  ReimbursementDraft,
  ReimbursementKind,
  ReimbursementDraftInput,
  ReimbursementDraftSummary,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
  ReimbursementSubmission,
  ReimbursementSubmissionHistoryEntry,
  ReimbursementSubmissionStatus,
  OaTravelApproval,
} from '@/types/reimbursements'
import {
  isActiveProof,
  materialSubmissionBlockReason,
  missingExpenseMaterials,
  requiresPaymentProof,
} from '@/utils/expenseProofs'
import { groupOverlappingSubsidyTrips } from '@/utils/subsidyTripGroups'
import {
  mappedTravelTypeOption,
  subsidyTripTypeForProfile,
  subsidyTripTypeForTravelLabel,
} from '@/utils/travelTypes'

const props = withDefaults(defineProps<{
  mobile?: boolean
}>(), {
  mobile: false,
})

const auth = useAuthStore()
const router = useRouter()
const expense = useExpenseStore()
const health = useHealthStore()
const drafts = useReimbursementDraftStore()
const submission = useReimbursementSubmissionStore()
const expenseItemsCard = ref<InstanceType<typeof ExpenseItemsCard> | null>(null)
const approvalSelector = ref<InstanceType<typeof TravelApprovalSelector> | null>(null)
const departmentApprovalSelector = ref<InstanceType<typeof TravelApprovalSelector> | null>(null)
const mobileFooterDock = ref<HTMLElement | null>(null)
const mobileFooterHeight = ref(0)
const mobileStep = ref(0)
const isGeneral = computed(() => (historyOrigin.value ? drafts.currentDraft?.kind : drafts.reimbursementKind) === 'general')
const mobileSteps = computed(() => isGeneral.value
  ? ['基本信息', '费用材料', '核对提交']
  : ['关联审批', '范围补助', '费用材料', '核对提交'])
const materialStep = computed(() => mobileSteps.value.length - 2)
const reviewStep = computed(() => mobileSteps.value.length - 1)
const companyValue = ref('')
const budgetCodeValue = ref('')
const generalDepartment = ref('')
const generalDepartmentConfirmed = ref(false)
const changingDepartment = ref(false)
const legacyDrafts = ref<ReimbursementDraftSummary[]>([])
const choosingLegacyDraft = ref(false)
const generalDepartmentUnconfirmed = ref(false)
const newEntryOpen = ref(false)
const generalApprovalIds = computed(() => drafts.currentDraft?.input.generalApprovalIds ?? [])
const generalApprovalError = ref('')
const generalApprovalOpen = ref(false)
const switchingKind = ref(false)
const usedSubjects = computed(() => {
  const selected = new Set(expense.items.map(item => item.subjectValue))
  return expense.generalSubjects.filter(option => selected.has(option.value))
})
const generalScopeOptions = computed(() => drafts.reimbursementKind === 'general' ? { kind: 'general' as const } : {})
const selectedRelatedApprovals = ref<ReimbursementRelatedApprovalSelection[]>([])
const initializingWorkspace = ref(false)
const initializationError = ref('')
const departmentBindingError = ref('')
const employeePickerOpen = ref(false)
const reimbursementSwitchError = ref('')
const switchingReimbursement = ref(false)
const deniedDelegationScope = ref('')
let reimbursementSwitchRequest: AbortController | undefined
const bindingApprovalDepartment = ref(false)
const pendingDepartmentApproval = ref<ReimbursementRelatedApprovalSelection | null>(null)
const retryDepartmentApproval = ref<ReimbursementRelatedApprovalSelection | null>(null)
const recheckApprovalDepartment = ref(false)
const selectedDepartmentChoiceId = ref('')
const saving = ref(false)
const refreshingMaterialStatus = ref(false)
const deferredMaterialRevision = ref<string | null>(null)
const saveError = ref('')
const saveRequiresReload = ref(false)
const editableInputBaseline = ref('')
const expiredDraftId = ref('')
const unavailableDraftId = ref('')
const sessionGeneration = ref(0)
let departmentBindingGeneration = 0
const workspaceErrorMessage = ref('')
const recoveryErrorOwner = ref<{ sourceId: string; scope: string; generation: number } | null>(null)
const workspaceActionError = computed({
  get: () => workspaceErrorMessage.value,
  set: (message: string) => {
    recoveryErrorOwner.value = null
    workspaceErrorMessage.value = message
  },
})
const workspaceConfirmationRequired = ref(false)
type ReplacementReadResult = { kind: 'unchanged' } | { kind: 'conflict' } | { kind: 'stale' } | { kind: 'empty' }
  | { kind: 'loaded'; draft: ReimbursementDraft }
const workspaceReader = useReimbursementWorkspaceReader({
  scope: sessionScope,
  request: () => ({ expectedDepartment: auth.session?.selectedDepartment ?? undefined,
    reimbursementContext: auth.session?.reimbursementContext ?? null, ...generalScopeOptions.value }),
  drafts,
})
const confirmingDraftReload = ref(false)
const pendingNewApproval = ref<ReimbursementRelatedApprovalSelection | null>(null)
const historyVisible = ref(false)
const recoveringReimbursement = ref(false)
let recoveryGeneration = 0
const historyRecords = ref<ReimbursementSubmissionHistoryEntry[]>([])
const historyError = ref('')
const historyLoading = ref(false)
const historyDetailError = ref('')
const historyOpeningId = ref('')
const historicalSubmission = ref<ReimbursementSubmission | null>(null)
const hydratedLockedDraftId = ref('')
const receiptLoadError = ref('')
type AttentionKind = 'save' | 'workspace' | 'options' | 'submission'
const attentionSuspended = computed(() => Boolean(historyVisible.value || pendingDepartmentApproval.value
  || confirmingDraftReload.value || recoveringReimbursement.value || submitFlowPending.value || expenseItemsCard.value?.hasOpenMaterialEditor))
const pageAttention = computed(() => {
  const draft = drafts.currentDraft
  if (auth.status !== 'authenticated' || !draft || initializingWorkspace.value) return null
  const notice = (kind: AttentionKind, title: string, tone: 'error' | 'warning' = 'error') => ({
    key: JSON.stringify([sessionScope(), draft.id, kind, title]), title, tone,
    target: `[data-attention-target="${kind}"]`,
  })
  if (draft.workspaceRetired && !historyOrigin.value) return notice('workspace', '已新开另一笔报销，请打开当前报销', 'warning')
  if (saveRequiresReload.value || saveError.value) return notice('save', saveRequiresReload.value
    ? '报销内容已更新，请核对后继续' : '保存失败，内容仍保留在本页')
  if (draftUnavailable.value) return notice('workspace', '当前报销记录已不可用，请新开一笔')
  if (workspaceActionError.value) return notice('workspace', workspaceActionError.value)
  if (draft.status !== 'LOCKED' && reimbursementOptionsUnavailable.value) return notice('options', templateNeedsAdmin.value
    ? 'OA 表单需要确认，请查看处理方式' : '暂时无法读取报销表单', 'warning')
  if (submission.activeDraftId !== draft.id) return null
  if (submission.requestError) return notice('submission', submission.requestError)
  if (submission.status === 'FAILED_FINAL') return notice('submission', '审批未发起，请查看原因')
  if (submission.status === 'MANUAL_REVIEW') return notice('submission', '提交结果待人工核对', 'warning')
  if (submission.status === 'FAILED_RETRYABLE') return notice('submission', '提交遇到临时问题，等待自动重试', 'warning')
  return null
})
let attentionRevealGeneration = 0
async function revealAttention(kind?: AttentionKind, explicit = false): Promise<void> {
  const generation = ++attentionRevealGeneration
  const scope = sessionScope()
  const draftId = drafts.currentDraft?.id
  const selector = kind ? `[data-attention-target="${kind}"]` : pageAttention.value?.target
  if (!scope || !draftId || !selector) return
  await nextTick()
  if (disposed || generation !== attentionRevealGeneration || scope !== sessionScope()
    || draftId !== drafts.currentDraft?.id || attentionSuspended.value) return
  const active = document.activeElement
  if (!explicit && active?.matches('input, textarea, select, [contenteditable="true"]')) return
  const target = document.querySelector<HTMLElement>(`.reimbursement-page ${selector}`)
  target?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  if (target) { target.setAttribute('tabindex', '-1'); target.focus({ preventScroll: true }) }
}
let historyRequest: AbortController | undefined
let historyDetailRequest: AbortController | undefined
const historyOrigin = ref<{
  draftId?: string
  mobileStep: number
  pendingApproval: ReimbursementRelatedApprovalSelection | null
} | null>(null)
const verifyingRelatedApprovals = ref(false)
const relatedApprovalVerificationError = ref('')
const relatedApprovalRetry = ref<ReimbursementRelatedApprovalSelection[] | null>(null)
const submitFlowPending = ref(false)
const replacingTemplate = ref(false)
const refreshingServiceStatus = ref(false)
const savedInputSignature = ref('')
const savedRelatedSignature = ref('')
let calculationTimer: ReturnType<typeof setTimeout> | undefined
let initializationPromise: Promise<void> | null = null
let initializationScope = ''
let initializedScope = ''
let disposed = false
let mobileFooterResizeObserver: ResizeObserver | undefined
const saveCoordinator = useReimbursementSaveCoordinator({
  target: () => ({ scope: sessionScope(), draftId: drafts.currentDraft?.id }),
  canAutosave: () => Boolean(sessionScope()) && !changingDepartment.value
    && !initializingWorkspace.value && !formReadOnly.value && !drafts.busy
    && !confirmingDraftReload.value && !verifyingRelatedApprovals.value
    && !bindingApprovalDepartment.value && !pendingDepartmentApproval.value && formDirty.value,
  autosave: () => flushAutosave(),
})
const scheduleAutosave = saveCoordinator.schedule

const companyOptions = computed(() => drafts.reimbursementOptions?.companyOptions ?? [])
const budgetOptions = computed(() => drafts.reimbursementOptions?.budgetCodeOptions ?? [])
const selectableDepartments = computed(() => {
  const names = new Set<string>()
  return (auth.session?.departments ?? []).flatMap((department) => {
    const name = department.name.trim()
    if (!name || name.startsWith('其他') || names.has(name)) return []
    names.add(name)
    return [{ id: department.id, name }]
  })
})
const travelDepartmentNeedsChoice = computed(() => !isGeneral.value && selectableDepartments.value.length > 1
  && Boolean(drafts.currentDraft?.input.accountingSourceVerified)
  && Boolean(drafts.currentDraft?.relatedApprovals.length)
  && (new Set(drafts.currentDraft!.relatedApprovals.map(a => a.originatorDepartmentId)).size !== 1
    || !drafts.currentDraft!.relatedApprovals.some(approval => selectableDepartments.value.some(
      department => department.id === approval.originatorDepartmentId,
    ))))
const showDepartmentChange = computed(() => isGeneral.value
  ? selectableDepartments.value.length > 1 || draftDepartmentInvalid.value
  : travelDepartmentNeedsChoice.value)
const pendingDepartmentApprovalTitle = computed(() => {
  const pending = pendingDepartmentApproval.value
  if (!pending) return ''
  return drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === pending.processInstanceId,
  )?.title ?? pending.processInstanceId
})
const companyLabel = computed(() => companyOptions.value.find((option) => option.value === companyValue.value)?.label ?? companyValue.value)
const budgetLabel = computed(() => budgetOptions.value.find((option) => option.value === budgetCodeValue.value)?.label ?? budgetCodeValue.value)
function approvalForDisplay(
  selection: ReimbursementRelatedApprovalSelection,
): OaTravelApproval | ReimbursementRelatedApproval | undefined {
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === selection.processInstanceId,
  )
  const linked = drafts.currentDraft?.relatedApprovals.find(
    (approval) => approval.processInstanceId === selection.processInstanceId,
  )
  return relatedDirty.value ? candidate ?? linked : linked ?? candidate
}
const selectedTravelTypeLabel = computed(() => {
  const first = selectedRelatedApprovals.value[0]
  if (!first) return ''
  const approval = approvalForDisplay(first)
  if (approval && 'travelTypeOption' in approval) return approval.travelTypeOption.label
  const profile = drafts.reimbursementOptions?.travelProfiles.find(
    (item) => item.profileKey === first.profileKey,
  )
  return mappedTravelTypeOption(profile, approval?.sourceTravelTypeValue)?.label ?? ''
})
const selectedSubsidyApprovals = computed(() => selectedRelatedApprovals.value.flatMap((selection) => {
  const approval = approvalForDisplay(selection)
  return approval ? [{
    processInstanceId: approval.processInstanceId,
    title: approval.title,
    startDate: approval.startDate,
    endDate: approval.endDate,
  }] : []
}))
const selectedSubsidyTripType = computed<TripType | null>(() => {
  const first = selectedRelatedApprovals.value[0]
  if (!first) return null
  const approval = approvalForDisplay(first)
  if (approval && 'subsidyTripType' in approval && approval.subsidyTripType) {
    return approval.subsidyTripType
  }
  const profile = drafts.reimbursementOptions?.travelProfiles.find(
    (item) => item.profileKey === first.profileKey,
  )
  return subsidyTripTypeForProfile(profile, approval?.sourceTravelTypeValue)
    ?? subsidyTripTypeForTravelLabel(selectedTravelTypeLabel.value)
})
const submittedSubsidyTripType = computed(() => {
  const draft = drafts.currentDraft
  return draft?.totals.subsidies?.[0]?.tripType ?? draft?.totals.subsidy?.tripType
    ?? draft?.input.trips?.[0]?.tripType ?? draft?.input.trip?.tripType ?? null
})
const selectedTravelPeriods = computed(() => groupOverlappingSubsidyTrips(
  selectedSubsidyApprovals.value.length ? selectedSubsidyApprovals.value.map(({ processInstanceId, startDate, endDate }) => ({
    ...expense.trip, relatedApprovalId: processInstanceId, startDate, endDate,
  })) : expense.subsidyTrips,
)
  .map((item) => ({ startDate: item.startDate, endDate: item.endDate })))
const trackedSubmission = computed(() => Boolean(drafts.currentDraft
  && submission.activeDraftId === drafts.currentDraft.id
  && (submission.submission || submission.submitting || submission.idempotencyKey)))
const templateMismatch = computed(() => {
  const draft = drafts.currentDraft
  const options = drafts.reimbursementOptions
  return Boolean(draft && options && (
    draft.template.processCode !== options.reimbursementProcessCode
    || draft.template.configVersion !== options.templateConfigVersion
  ))
})
const reimbursementOptionsUnavailable = computed(() => Boolean(drafts.reimbursementOptionsError))
const delegationRevoked = computed(() => Boolean(deniedDelegationScope.value
  && deniedDelegationScope.value === sessionScope()))
const templateNeedsAdmin = computed(() => [
  'OA_TEMPLATE_CONFIRMATION_REQUIRED', 'OA_TEMPLATE_SCHEMA_CHANGED', 'OA_TEMPLATE_NOT_CONFIGURED',
  'INVALID_SYSTEM_CONFIGURATION',
  'OA_GENERAL_TEMPLATE_NOT_CONFIGURED', 'OA_GENERAL_TEMPLATE_CHANGED', 'OA_GENERAL_TEMPLATE_INVALID',
].includes(drafts.reimbursementOptionsErrorCode))
const draftExpired = computed(() => Boolean(drafts.currentDraft
  && (drafts.currentDraft.status === 'EXPIRED' || drafts.currentDraft.id === expiredDraftId.value)))
const draftUnavailable = computed(() => Boolean(drafts.currentDraft && drafts.currentDraft.id === unavailableDraftId.value))
const activeMaterialRecovery = computed(() => Boolean(drafts.currentDraft?.recoveryPending
  && !drafts.currentDraft.workspaceRetired && !draftExpired.value && !draftUnavailable.value))
const canReplaceOutdatedForm = computed(() => templateMismatch.value && Boolean(drafts.currentDraft
  && ['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status))
  && !trackedSubmission.value && !submitFlowPending.value && !initializingWorkspace.value && !workspaceConfirmationRequired.value
  && !switchingReimbursement.value)
const draftDepartmentInvalid = computed(() => Boolean(drafts.currentDraft
  && !historyOrigin.value && !draftExpired.value && !drafts.currentDraft.workspaceRetired
  && !drafts.currentDraft.recoveryPending && ['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status)
  && !selectableDepartments.value.some(d => d.id === drafts.currentDraft?.department.id
    && d.name === drafts.currentDraft.department.name)))
const operationState = computed<ReimbursementOperationState>(() => ({
  draft: drafts.currentDraft ? {
    status: drafts.currentDraft.status, retired: Boolean(drafts.currentDraft.workspaceRetired),
    recoveryPending: Boolean(drafts.currentDraft.recoveryPending), expired: draftExpired.value,
    unavailable: draftUnavailable.value, trackedSubmission: trackedSubmission.value,
  } : null,
  mode: { general: isGeneral.value, administrator: auth.isAdmin },
  options: { unavailable: reimbursementOptionsUnavailable.value, changed: templateMismatch.value,
    needsAdministrator: templateNeedsAdmin.value },
  verification: { initializationFailed: Boolean(initializationError.value), delegationRevoked: delegationRevoked.value,
    workspaceUncertain: workspaceConfirmationRequired.value, reloadRequired: saveRequiresReload.value },
  activity: {
    switchingEmployee: switchingReimbursement.value, initializing: initializingWorkspace.value,
    bindingDepartment: bindingApprovalDepartment.value, pendingDepartmentApproval: Boolean(pendingDepartmentApproval.value),
    verifyingApprovals: verifyingRelatedApprovals.value, submitting: submitFlowPending.value,
    submissionRequest: submission.submitting, recovering: recoveringReimbursement.value,
    replacingTemplate: replacingTemplate.value, confirmingReload: confirmingDraftReload.value,
    processingFiles: drafts.processingFiles, materialEditor: Boolean(expenseItemsCard.value?.hasOpenMaterialEditor),
    busy: drafts.busy, saving: saving.value, refreshingMaterials: refreshingMaterialStatus.value,
  },
}))
const editingAvailability = computed(() => reimbursementEditingAvailability(operationState.value))
const formOperationBlocked = computed(() => !editingAvailability.value.allowed)
const formReadOnly = formOperationBlocked
const canRecoverTravelDepartmentFromApproval = computed(() => !isGeneral.value
  && draftDepartmentInvalid.value && !formOperationBlocked.value
  && !drafts.currentDraft?.relatedApprovals.length)
const changingDepartmentUnconfirmed = computed(() => generalDepartmentUnconfirmed.value)
const departmentGateError = computed(() => departmentBindingError.value || initializationError.value
  || (changingDepartment.value ? saveError.value : ''))
const formReadOnlyReason = computed(() => editingAvailability.value.reason)
const currentFormInput = computed<ReimbursementDraftInput>(() => {
  if (isGeneral.value) return {
    ocrDispositionVersion: 1,
    companyValue: companyValue.value,
    budgetCodeValue: budgetCodeValue.value,
    generalApprovalIds: generalApprovalIds.value,
    trip: null,
    items: expense.buildDraftExpenseItems(),
    dismissedOcrFileIds: [...expense.dismissedOcrFileIds],
  }
  const trips = expense.tripPayloads()
  return {
    ocrDispositionVersion: 1,
    companyValue: companyValue.value,
    budgetCodeValue: budgetCodeValue.value,
    trip: expense.subsidyTrips.length ? null : trips[0] ?? null,
    trips: expense.subsidyTrips.length ? trips : [],
    editingState: {
      includeSubsidy: expense.includeSubsidy,
      trip: {
        ...expense.trip,
        // Persist the same half-day encoding checked by the backend at submission.
        startTime: trips[0]?.startTime ?? expense.trip.startTime,
        endTime: trips.at(-1)?.endTime ?? expense.trip.endTime,
      },
      trips: expense.subsidyTrips.map((item) => {
        const payload = trips.find((trip) => trip.relatedApprovalId === item.relatedApprovalId)
        return {
          ...item,
          startTime: payload?.startTime ?? item.startTime,
          endTime: payload?.endTime ?? item.endTime,
        }
      }),
    },
    items: expense.buildDraftExpenseItems(),
    dismissedOcrFileIds: [...expense.dismissedOcrFileIds],
  }
})
const inputDirty = computed(() => inputSignature(currentFormInput.value) !== savedInputSignature.value)
const relatedDirty = computed(() => relatedSignature(selectedRelatedApprovals.value) !== savedRelatedSignature.value)
const formDirty = computed(() => inputDirty.value || relatedDirty.value)
// Saved legacy input may gain defaults or half-day time encoding on hydration.
// It still needs canonical saving when editable, but is not an employee edit.
const hasUnsavedUserChanges = computed(() => relatedDirty.value || (inputDirty.value
  && inputSignature(currentFormInput.value) !== editableInputBaseline.value))
const pendingApprovalAccounting = computed(() => {
  if (!relatedDirty.value || !selectedRelatedApprovals.value.length) return null
  const candidates = selectedRelatedApprovals.value.map((selection) => drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === selection.processInstanceId,
  ))
  if (candidates.some((candidate) => !candidate)) return null
  const company = candidates[0]?.companyOption
  const budget = candidates[0]?.budgetCodeOption
  if (!company || !budget || candidates.some((candidate) => (
    candidate?.companyOption?.value !== company.value
    || candidate?.budgetCodeOption?.value !== budget.value
  ))) return null
  return {
    companyLabel: company.label || company.value,
    budgetLabel: budget.label || budget.value,
  }
})
const displayedCompanyLabel = computed(() => relatedDirty.value
  ? pendingApprovalAccounting.value?.companyLabel ?? ''
  : companyLabel.value)
const displayedBudgetLabel = computed(() => relatedDirty.value
  ? pendingApprovalAccounting.value?.budgetLabel ?? ''
  : budgetLabel.value)
const accountingOptionsChanged = computed(() => !formReadOnly.value && !relatedDirty.value
  && Boolean(drafts.reimbursementOptions) && selectedRelatedApprovals.value.length > 0
  && ((companyValue.value && !companyOptions.value.some((option) => option.value === companyValue.value))
    || (budgetCodeValue.value && !budgetOptions.value.some((option) => option.value === budgetCodeValue.value))))
const checkGeneralAccounting = computed(() => isGeneral.value && !formReadOnly.value && Boolean(drafts.reimbursementOptions))
const generalCompanyError = computed(() => checkGeneralAccounting.value && companyValue.value
  && !companyOptions.value.some((option) => option.value === companyValue.value) ? '原报销主体已停用，请重新选择' : '')
const generalBudgetError = computed(() => checkGeneralAccounting.value && budgetCodeValue.value
  && !budgetOptions.value.some((option) => option.value === budgetCodeValue.value) ? '原预算代码已停用，请重新选择' : '')
const needsApprovalReconfirmation = computed(() => Boolean(!formReadOnly.value && !accountingOptionsChanged.value
  && drafts.currentDraft?.relatedApprovals.length && !drafts.currentDraft.input.accountingSourceVerified))
const relatedApprovalVerificationPending = computed(() => Boolean(
  verifyingRelatedApprovals.value || (selectedRelatedApprovals.value.length && relatedDirty.value),
))
const saveState = computed(() => saving.value || verifyingRelatedApprovals.value ? 'saving'
  : formDirty.value ? 'pending' : 'saved')
const saveLabels = { saving: '正在保存…', pending: '等待保存…', saved: '已保存' }
const mobileShellStyle = computed(() => props.mobile && mobileFooterHeight.value > 0
  ? { '--mobile-footer-height': `${mobileFooterHeight.value}px` }
  : undefined)
const submissionServiceReason = computed(() => submission.oaSubmissionEnabled === false
  ? (trackedSubmission.value || drafts.currentDraft?.status === 'LOCKED'
      ? 'OA提交服务未开启，请保留本次提交并稍后核对' : 'OA提交服务未开启，可继续填写')
  : submission.oaSubmissionEnabled === null ? '正在确认 OA 提交服务状态' : '')
const materialSubmissionIssues = computed(() => drafts.files
  .map((file) => ({
    file,
    reason: materialSubmissionBlockReason(file, expense.items, expense.dismissedOcrFileIds),
    itemId: expense.items.find((item) => item.sourceFileId === file.id
      || item.itineraryFileIds?.includes(file.id)
      || item.paymentProofFileIds?.includes(file.id)
      || item.hotelBillFileIds?.includes(file.id))?.id,
  }))
  .filter((issue) => Boolean(issue.reason)))
const subsidyInputError = computed(() => formReadOnly.value ? '' : expense.policyInputError)
const hasValidSubsidy = computed(() => expense.includeSubsidy && !expense.policyInputError
  && expense.calculationsCurrent && Number(expense.totals?.subsidyTotal ?? 0) > 0)
const hasActiveMaterials = computed(() => drafts.files.some((file) => file.status === 'ACTIVE'))
const excelOnlySubmission = computed(() => !expense.items.length
  && hasValidSubsidy.value && !hasActiveMaterials.value)
const awaitingMaterialUpload = computed(() => drafts.pendingUploads.length > 0
  || drafts.files.some((file) => file.status === 'RESERVED' || file.status === 'WRITING'))
const reimbursementSubject = computed(() => JSON.stringify([
  auth.session?.operator?.userId, auth.session?.user.userId,
]))
const localUploadFailureReason = computed(() => expenseItemsCard.value?.hasUnpersistedFailedUploads
  ? '请先重试或移除尚未上传成功的文件' : '')
const workspaceAvailability = computed(() => reimbursementWorkspaceAvailability(operationState.value, editingAvailability.value, {
  departmentUnconfirmed: generalDepartmentUnconfirmed.value || (changingDepartment.value && changingDepartmentUnconfirmed.value),
  failedUploadReason: localUploadFailureReason.value,
  awaitingMaterials: awaitingMaterialUpload.value || drafts.files.some(file => file.status === 'ACTIVE'
    && file.ocrStatus === 'RUNNING' && file.ocrStale !== true),
  hasUnsavedChanges: hasUnsavedUserChanges.value,
}))
const reimbursementOperationBlock = computed(() => workspaceAvailability.value.operationReason)
const reimbursementSwitchBlock = computed(() => workspaceAvailability.value.switchBlockReason)
const reimbursementSwitchRecheck = computed(() => workspaceAvailability.value.switchRecheckReason)

async function switchKind(kind: ReimbursementKind): Promise<void> {
  if (kind === drafts.reimbursementKind || switchingKind.value || reimbursementSwitchBlock.value
    || reimbursementSwitchRecheck.value || (kind === 'general' && auth.session?.operator)) return
  const originalScope = sessionScope()
  switchingKind.value = true
  try {
    if (drafts.currentDraft && !formReadOnly.value) {
      await flushAutosave()
      if (formDirty.value) throw new Error('请先保存当前报销')
    }
    if (disposed || sessionScope() !== originalScope) return
    newEntryOpen.value = false
    generalDepartment.value = ''
    generalDepartmentConfirmed.value = false
    changingDepartment.value = false
    departmentBindingError.value = ''
    drafts.reset()
    expense.reset()
    submission.reset()
    companyValue.value = ''
    budgetCodeValue.value = ''
    generalApprovalError.value = ''
    initializationError.value = ''
    mobileStep.value = 0
    drafts.reimbursementKind = kind
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '当前报销保存失败，请重试后切换'))
  } finally {
    switchingKind.value = false
  }
}

async function openDepartmentPicker(): Promise<void> {
  const draft = drafts.currentDraft
  if (!auth.session || !draft || !showDepartmentChange.value
    || reimbursementOperationBlock.value || reimbursementSwitchRecheck.value) return
  generalDepartment.value = draft.department.id
  selectedDepartmentChoiceId.value = ''
  departmentBindingError.value = ''
  changingDepartment.value = true
  await nextTick()
  document.querySelector<HTMLElement>('[data-testid="reimbursement-department-gate"]')?.focus()
}

function closeDepartmentPicker(): void {
  if (bindingApprovalDepartment.value || changingDepartmentUnconfirmed.value) return
  changingDepartment.value = false
  selectedDepartmentChoiceId.value = ''
  departmentBindingError.value = ''
  scheduleAutosave()
}

const generalDepartmentSelectionDisabled = computed(() => !generalDepartment.value
  || (changingDepartment.value && !generalDepartmentUnconfirmed.value
    && generalDepartment.value === (drafts.currentDraft?.department.id ?? auth.session?.selectedDepartment?.id)
    && !draftDepartmentInvalid.value))

async function confirmGeneralDepartment(): Promise<void> {
  const origin = auth.session
  if (!origin || generalDepartmentSelectionDisabled.value || bindingApprovalDepartment.value) return
  const departmentId = generalDepartment.value
  if (changingDepartment.value && drafts.currentDraft) {
    await correctCurrentDepartment(departmentId)
    return
  }
  const leavingEditableDraft = changingDepartment.value && !formReadOnly.value
  const originScope = sessionScope()
  const isCurrent = () => !disposed && isGeneral.value && auth.session === origin && sessionScope() === originScope
  let confirmed = false
  bindingApprovalDepartment.value = true
  departmentBindingError.value = ''
  try {
    if (leavingEditableDraft && !generalDepartmentUnconfirmed.value) {
      await flushAutosave()
      if (!isCurrent()) return
      if (formDirty.value) throw new Error('请先保存当前报销')
    }
    let boundSession: AuthSession
    try {
      const selectedDepartment = await selectDepartmentForGeneralReimbursement(departmentId)
      boundSession = { ...origin, selectedDepartment }
    } catch (error) {
      const latest = await getMe().catch(() => null)
      if (!isCurrent()) return
      if (latest?.user.userId !== origin.user.userId || latest?.csrfToken !== origin.csrfToken
        || (latest.selectedDepartment?.id !== departmentId
          && latest.selectedDepartment?.id !== origin.selectedDepartment?.id)) {
        generalDepartmentUnconfirmed.value = true
        throw new Error('暂时无法确认已切换到哪个部门，请在这里重新选择并确认', { cause: error })
      }
      generalDepartmentUnconfirmed.value = false
      if (latest.selectedDepartment?.id !== departmentId) throw error
      boundSession = latest
    }
    if (!isCurrent()) return
    if (boundSession.selectedDepartment?.id !== departmentId) throw new Error('办理部门已变化，请重新选择')
    auth.applyReimbursementContext(boundSession)
    generalDepartmentUnconfirmed.value = false
    changingDepartment.value = false
    generalDepartmentConfirmed.value = true
    mobileStep.value = 0
    confirmed = true
  } catch (error) {
    if (isCurrent()) departmentBindingError.value = apiErrorMessage(error, '部门确认失败，请重试')
  } finally {
    bindingApprovalDepartment.value = false
  }
  if (confirmed) {
    await nextTick()
    if (disposed || !isGeneral.value || auth.session?.user.userId !== origin.user.userId
      || auth.session?.selectedDepartment?.id !== departmentId) return
    if (newEntryOpen.value) await finishNewEntry()
    else await initializeWorkspace()
  }
}

async function correctCurrentDepartment(
  departmentId?: string, selection?: ReimbursementRelatedApprovalSelection,
): Promise<void> {
  const origin = auth.session
  const draft = drafts.currentDraft
  let scope = sessionScope()
  if (!origin || !draft || bindingApprovalDepartment.value) return
  const accepts = () => !disposed && scope === sessionScope() && drafts.currentDraft?.id === draft.id
  bindingApprovalDepartment.value = true
  departmentBindingError.value = ''
  try {
    await flushAutosave()
    if (!accepts()) return
    if (formDirty.value) throw new Error('请先保存当前报销')
    const expectedRevision = drafts.currentDraft!.revision
    const expectedInput = drafts.currentDraft!.input
    const approvalKey = (approval: { processInstanceId: string; profileKey: string }) =>
      [approval.processInstanceId, approval.profileKey]
    const expectedApprovals = JSON.stringify((selection ? [selection] : drafts.currentDraft!.relatedApprovals).map(approvalKey))
    let result
    try {
      result = await correctReimbursementDepartment(draft.id, expectedRevision, departmentId, selection)
    } catch (error) {
      if (apiErrorCode(error) === 'REIMBURSEMENT_DEPARTMENT_CHOICE_REQUIRED' && selection) {
        pendingDepartmentApproval.value = selection
        selectedDepartmentChoiceId.value = ''
        return
      }
      if (apiErrorCode(error) && axios.isAxiosError(error) && error.response && error.response.status < 500) {
        if (['REIMBURSEMENT_DRAFT_REVISION_CONFLICT', 'REIMBURSEMENT_DRAFT_REPLACED',
          'REIMBURSEMENT_DRAFT_LOCKED', 'REIMBURSEMENT_RECOVERY_PENDING',
          'REIMBURSEMENT_DRAFT_EXPIRED', 'REIMBURSEMENT_WORKSPACE_SELECTION_REQUIRED'].includes(apiErrorCode(error)!)) {
          saveRequiresReload.value = true
        }
        throw error
      }
      // A successful write may lose its response. Read the same record and the
      // actual Session before deciding; never create a replacement on retry.
      const [latest, session] = await Promise.all([
        getReimbursementDraft(draft.id), getMe(),
      ]).catch(() => [null, null] as const)
      if (!accepts()) return
      const businessInput = (input: ReimbursementDraftInput) => inputSignature(!isGeneral.value
        ? { ...input, companyValue: '', budgetCodeValue: '', project: null, accountingSourceVerified: false }
        : input)
      if (!latest || !session || session.user.userId !== origin.user.userId
        || session.csrfToken !== origin.csrfToken || latest.id !== draft.id
        || session.operator?.userId !== origin.operator?.userId
        || latest.revision !== expectedRevision + 1
        || latest.workspaceRetired || latest.recoveryPending || !['DRAFT', 'REVIEW_READY'].includes(latest.status)
        || businessInput(latest.input) !== businessInput(expectedInput)
        || latest.department.id !== (departmentId ?? latest.department.id)
        || JSON.stringify(latest.relatedApprovals.map(approvalKey)) !== expectedApprovals) {
        saveRequiresReload.value = true
        throw error
      }
      result = { draft: latest, selectedDepartment: latest.department,
        reimbursementContext: session.reimbursementContext }
    }
    if (!accepts()) return
    // Suppress workspace reinitialization while adopting this same record.
    bindingApprovalDepartment.value = true
    auth.applyReimbursementContext({ ...origin, selectedDepartment: result.selectedDepartment,
      ...(result.reimbursementContext ? { reimbursementContext: result.reimbursementContext } : {}) }, draft.id)
    scope = sessionScope()
    await nextTick()
    if (disposed || scope !== sessionScope()) return
    drafts.currentDraft = result.draft
    hydrate(result.draft)
    changingDepartment.value = false
    generalDepartmentConfirmed.value = true
    selectedDepartmentChoiceId.value = ''
    pendingDepartmentApproval.value = null
    initializedScope = scope
  } catch (error) {
    if (accepts()) departmentBindingError.value = apiErrorMessage(error, '部门更正失败，请核对当前报销后重试')
  } finally {
    if (!disposed && scope === sessionScope()) {
      bindingApprovalDepartment.value = false
      verifyingRelatedApprovals.value = false
      scheduleAutosave()
    }
  }
}

async function openSavedWorkspace(
  chosen: ReimbursementDraftSummary, candidates: ReimbursementDraftSummary[], confirm = false,
): Promise<void> {
  const origin = auth.session
  const scope = sessionScope()
  if (!origin || choosingLegacyDraft.value) return
  choosingLegacyDraft.value = true
  try {
    if (confirm) await ElMessageBox.confirm(
      `保留「${chosen.department?.name ?? '原部门'}」这笔报销。其余 ${candidates.length - 1} 笔旧报销将退出工作区，普通未提交材料会清理；已提交记录及在途任务继续保留。`,
      '选择保留的当前报销', { confirmButtonText: '保留这笔', cancelButtonText: '返回核对', type: 'warning' },
    )
    if (disposed || scope !== sessionScope()) return
    const result = await openReimbursementWorkspace(chosen.id,
      candidates.map(d => ({ id: d.id, revision: d.revision })), generalScopeOptions.value)
    if (disposed || scope !== sessionScope()) return
    bindingApprovalDepartment.value = true
    auth.applyReimbursementContext({ ...origin, ...result })
    generalDepartmentConfirmed.value = true
    legacyDrafts.value = []
    await nextTick()
    bindingApprovalDepartment.value = false
    initializedScope = ''
    // The previous read belongs to the old department scope; start a fresh one.
    initializationScope = ''
    initializationPromise = null
    await initializeWorkspace(true)
  } catch (error) {
    if (!disposed && auth.session?.user.userId === origin.user.userId && error !== 'cancel' && error !== 'close') {
      initializationError.value = apiErrorMessage(error, '无法打开保留的报销，请重新加载后核对')
    }
  } finally { choosingLegacyDraft.value = false }
}

async function saveGeneralApprovalSelection(ids: string[]): Promise<void> {
  if (!isGeneral.value || formReadOnly.value || drafts.busy || verifyingRelatedApprovals.value) return
  const scope = sessionScope()
  const draftId = drafts.currentDraft?.id
  verifyingRelatedApprovals.value = true
  generalApprovalError.value = ''
  try {
    await flushAutosave()
    if (!draftId || drafts.currentDraft?.id !== draftId || scope !== sessionScope() || formDirty.value) return
    const saved = await drafts.saveGeneralApprovals(ids)
    if (scope === sessionScope() && drafts.currentDraft?.id === draftId) hydrate(saved)
  } catch (error) {
    if (scope === sessionScope() && drafts.currentDraft?.id === draftId) {
      const latest = drafts.currentDraft
      if (latest && ['DRAFT', 'REVIEW_READY'].includes(latest.status)
        && !latest.workspaceRetired && !latest.recoveryPending
        && JSON.stringify(latest.input.generalApprovalIds ?? []) === JSON.stringify(ids)
        && inputSignature(currentFormInput.value) === inputSignature(latest.input)) {
        drafts.mutationError = ''
        hydrate(latest)
      } else {
        generalApprovalError.value = apiErrorMessage(error, '关联审批保存失败，请重试')
        if (drafts.revisionConflict) saveRequiresReload.value = true
        void refreshOptionsAfterError(error)
      }
    }
  } finally {
    if (scope === sessionScope()) verifyingRelatedApprovals.value = false
  }
}
const attachmentNames = computed(() => {
  const draft = drafts.currentDraft
  if (draft?.status === 'LOCKED') {
    return draft.attachmentMode === 'excel_only' ? ['报销单.xlsx'] : ['票据汇总.pdf', '报销单.xlsx']
  }
  if (awaitingMaterialUpload.value || (!expense.items.length && !hasValidSubsidy.value)) return []
  if (hasActiveMaterials.value) return ['票据汇总.pdf', '报销单.xlsx']
  return excelOnlySubmission.value ? ['报销单.xlsx'] : []
})
const attachmentPendingHint = computed(() => awaitingMaterialUpload.value
  ? '材料上传完成后会显示附件清单。'
  : expense.items.length ? '请先上传报销材料。' : isGeneral.value
    ? '填写费用明细后会显示附件清单。' : '填写费用明细或核对出差补助后会显示附件清单。')
const outputAvailability = computed(() => reimbursementOutputAvailability(operationState.value, editingAvailability.value, {
  mobile: props.mobile, failedUploadReason: localUploadFailureReason.value,
  companyError: generalCompanyError.value, budgetError: generalBudgetError.value,
  accountingChanged: Boolean(accountingOptionsChanged.value), approvalReconfirmationRequired: needsApprovalReconfirmation.value,
  approvalVerificationPending: relatedApprovalVerificationPending.value,
  companySelected: Boolean(companyValue.value), budgetSelected: Boolean(budgetCodeValue.value),
  expenseCount: expense.items.length, itemReadinessError: expense.itemReadinessError,
  subsidyInputError: subsidyInputError.value, subsidyCalculationReason: expense.subsidyCalculationReason,
  serviceReason: submissionServiceReason.value, hasActiveMaterials: hasActiveMaterials.value,
  pendingUploadCount: drafts.pendingUploads.length, materialIssueCount: materialSubmissionIssues.value.length,
  missingMaterialExpenseCount: missingMaterialItems.value.length, saveError: saveError.value, formDirty: formDirty.value,
}))
const submissionButtonReason = computed(() => outputAvailability.value.submitReason)
const mobileNextLabel = computed(() => mobileStep.value === mobileSteps.value.length - 1
  ? '提交 OA'
  : `下一步：${mobileSteps.value[mobileStep.value + 1]}`)
const missingMaterialItems = computed(() => expense.sortedItems.map((item) => ({ item, missing: missingExpenseMaterials(item, drafts.files) }))
  .filter((entry) => entry.missing.length))
const previewDisabledReason = computed(() => outputAvailability.value.previewReason)
const submissionProgress: ReimbursementSubmissionStatus[] = [
  'QUEUED', 'VALIDATING', 'GENERATING_EXCEL', 'UPLOADING', 'OA_CREATING', 'VERIFYING', 'SUBMITTED',
]
const submissionProgressPercentage = computed(() => {
  if (submission.terminal) return 100
  const index = submissionProgress.indexOf(submission.status!)
  return index < 0 ? 0 : Math.round(index / (submissionProgress.length - 1) * 100)
})
const submissionAlertType = computed(() => submission.status === 'FAILED_FINAL' ? 'error'
    : submission.status === 'FAILED_RETRYABLE' ? 'warning' : 'info')
const hasRestartableReimbursement = computed(() => Boolean(drafts.currentDraft
  && (trackedSubmission.value || drafts.currentDraft.status === 'LOCKED' || draftExpired.value
    || draftUnavailable.value || drafts.currentDraft.recoveryPending || expense.items.length
    || selectedRelatedApprovals.value.length || drafts.files.length || drafts.pendingUploads.length
    || (isGeneral.value && (companyValue.value || budgetCodeValue.value
      || generalApprovalIds.value.length))
    || expense.includeSubsidy)))
const canStartNewReimbursement = computed(() => Boolean(drafts.currentDraft && !historyOrigin.value
  && hasRestartableReimbursement.value && !initializingWorkspace.value && !drafts.currentDraft.workspaceRetired

  && !switchingReimbursement.value && !bindingApprovalDepartment.value && !verifyingRelatedApprovals.value && !pendingDepartmentApproval.value
  && !workspaceConfirmationRequired.value && !recoveringReimbursement.value && !submitFlowPending.value && !submission.submitting))
const submittedResult = computed(() => historyOrigin.value ? historicalSubmission.value
  : trackedSubmission.value && submission.succeeded ? submission.submission : null)
const submittedReimbursement = computed(() => drafts.currentDraft?.status === 'LOCKED'
  && hydratedLockedDraftId.value === drafts.currentDraft.id ? submittedResult.value : null)
const receiptDetails = computed(() => drafts.currentDraft?.submittedDetails)
const hasReimbursementLogin = computed(() => auth.status === 'authenticated' || auth.status === 'department_required')
const generalDepartmentRequired = computed(() => isGeneral.value && selectableDepartments.value.length > 1
  && !generalDepartmentConfirmed.value)
const needsReimbursementDepartment = computed(() => auth.status === 'department_required'
  && !drafts.currentDraft || generalDepartmentRequired.value || changingDepartment.value || newEntryOpen.value)
const canOpenHistory = computed(() => Boolean(hasReimbursementLogin.value && !historyOrigin.value
  && !switchingReimbursement.value && !changingDepartment.value
  && !bindingApprovalDepartment.value && !verifyingRelatedApprovals.value && !pendingDepartmentApproval.value
  && !workspaceConfirmationRequired.value && !submitFlowPending.value && !submission.submitting && !recoveringReimbursement.value
  && !localUploadFailureReason.value))
const showHeaderNewReimbursement = computed(() => hasReimbursementLogin.value && !historyOrigin.value)
async function showWorkspaceReimbursement(restored: ReimbursementDraft): Promise<void> {
  submission.abort()
  hydrate(restored)
  if (restored.status === 'LOCKED') {
    try { await submission.restoreForDraft(restored) }
    catch { /* Keep the persisted submission and its existing refresh action. */ }
  }
}
async function recoverFailedReimbursement(): Promise<void> {
  const current = drafts.currentDraft
  const scope = sessionScope()
  if (!current || current.workspaceRetired || draftExpired.value || draftUnavailable.value || workspaceConfirmationRequired.value || recoveringReimbursement.value || drafts.busy) return
  const resumeSourceId = current.recoveryPending ? current.recoverySourceDraftId : undefined
  if (!resumeSourceId && (submission.activeDraftId !== current.id || submission.status !== 'FAILED_FINAL')) return
  const sourceId = resumeSourceId ?? current.id
  const generation = ++recoveryGeneration
  const acceptsRecovery = () => !disposed && sessionScope() === scope && generation === recoveryGeneration
  recoveringReimbursement.value = true
  workspaceActionError.value = ''
  try {
    if (hasUnsavedUserChanges.value) {
      await ElMessageBox.confirm(
        '将恢复服务器已保存的报销内容和材料。本页未保存的修改不会带入，继续将舍弃这些修改。',
        '按已保存内容恢复',
        { confirmButtonText: '按已保存内容恢复', cancelButtonText: '保留本页', type: 'warning' },
      )
      if (!acceptsRecovery() || drafts.currentDraft?.id !== current.id || drafts.currentDraft.workspaceRetired) return
    }
    const source = resumeSourceId ? await getReimbursementDraft(resumeSourceId) : current
    if (!acceptsRecovery() || drafts.currentDraft?.id !== current.id || drafts.currentDraft.workspaceRetired || draftExpired.value || draftUnavailable.value) return
    const restored = await drafts.recoverDraft(source.id, source.revision)
    if (!acceptsRecovery()) return
    await showWorkspaceReimbursement(restored)
    if (!acceptsRecovery()) return
    await returnToApprovalStep()
    if (!acceptsRecovery()) return
    ElMessage.success(restored.status === 'LOCKED' ? '已打开这笔报销的最新提交状态'
      : restored.recoveryPending ? '材料正在恢复，请稍后继续恢复' : '已保留报销内容，请核对关联审批和材料后再提交')
  } catch (error) {
    if (!acceptsRecovery()) return
    if (error === 'cancel' || error === 'close') return
    if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_NOT_FOUND'
      && resumeSourceId && drafts.currentDraft?.id === current.id) {
      unavailableDraftId.value = current.id
      void revealAttention('workspace')
      return
    }
    if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_EXPIRED'
      && resumeSourceId && drafts.currentDraft?.id === current.id) {
      expiredDraftId.value = current.id
      return
    }
    workspaceActionError.value = apiErrorMessage(error, '恢复失败，原报销和材料仍保留，请重试')
    recoveryErrorOwner.value = { sourceId, scope, generation }
    // The server may have durably begun a copy before a response was lost.
    try {
      const context = workspaceReader.capture()
      const workspace = await workspaceReader.list(context)
      const latest = workspace.kind === 'current' ? workspace.draft : undefined
      if (acceptsRecovery() && drafts.currentDraft?.id === current.id
        && latest?.recoverySourceDraftId === sourceId) {
        const loaded = await workspaceReader.load(context, latest.id, {
          accept: () => acceptsRecovery() && drafts.currentDraft?.id === current.id,
        })
        if (acceptsRecovery() && workspaceReader.accepts(context) && loaded.kind === 'loaded') await showWorkspaceReimbursement(loaded.draft)
      }
    } catch { /* Preserve the original error and original form; a reload resumes the durable copy. */ }
  }
  finally { if (acceptsRecovery()) { recoveringReimbursement.value = false; scheduleAutosave() } }
}
let recoveryRefreshTimer: ReturnType<typeof setTimeout> | undefined
let recoveryRefreshGeneration = 0
watch([() => sessionScope(), () => drafts.currentDraft?.id], () => {
  recoveryRefreshGeneration++
}, { flush: 'sync' })
watch(() => [sessionScope(), drafts.currentDraft?.id, activeMaterialRecovery.value] as const,
  ([scope, draftId, pending]) => {
    if (recoveryRefreshTimer) clearTimeout(recoveryRefreshTimer)
    if (!scope || !draftId || !pending) return
    const generation = ++recoveryRefreshGeneration
    const acceptsRefresh = () => !disposed && generation === recoveryRefreshGeneration
      && scope === sessionScope() && drafts.currentDraft?.id === draftId && activeMaterialRecovery.value
    const refresh = async (): Promise<void> => {
      try {
        if (!acceptsRefresh()) return
        if (!recoveringReimbursement.value && !drafts.busy) {
          const latest = await getReimbursementDraft(draftId)
          if (!acceptsRefresh() || recoveringReimbursement.value) return
          if (latest.workspaceRetired) {
            drafts.currentDraft = { ...drafts.currentDraft!, workspaceRetired: true }
            return
          }
          if (latest.status === 'EXPIRED') {
            expiredDraftId.value = draftId
            return
          }
          if (!latest.recoveryPending) {
            await drafts.loadDraft(draftId, true)
            if (generation === recoveryRefreshGeneration && !disposed && scope === sessionScope()
              && drafts.currentDraft?.id === draftId) await showWorkspaceReimbursement(drafts.currentDraft)
          }
        }
      } catch (error) {
        if (acceptsRefresh() && apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_NOT_FOUND') {
          unavailableDraftId.value = draftId
        }
        // Transient failures retain the pending state and its explicit retry action.
      }
      finally {
        if (acceptsRefresh()) {
          recoveryRefreshTimer = setTimeout(() => void refresh(), 3000)
        }
      }
    }
    recoveryRefreshTimer = setTimeout(() => void refresh(), 3000)
  })
const reimbursementTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
})

function formatRecordTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : reimbursementTime.format(date)
}

function sessionIdentity(): string {
  const user = auth.session?.user.userId
  return ['authenticated', 'department_required'].includes(auth.status) && user
    ? JSON.stringify([user, auth.session?.operator?.userId, auth.session?.csrfToken, drafts.reimbursementKind]) : ''
}
function sessionScope(): string {
  const identity = sessionIdentity()
  return identity ? `${sessionGeneration.value}:${identity}` : ''
}
function relatedSignature(selections: ReimbursementRelatedApprovalSelection[]): string { return JSON.stringify(selections) }
function acceptDerivedAccounting(): void {
  if (isGeneral.value) return
  const input = drafts.currentDraft?.input
  if (!input) return
  companyValue.value = input.companyValue
  budgetCodeValue.value = input.budgetCodeValue
  // Merge only server-owned values; expense edits made during the request stay live.
  if (savedInputSignature.value) {
    const saved = JSON.parse(savedInputSignature.value) as ReimbursementDraftInput
    savedInputSignature.value = inputSignature({ ...saved, companyValue: input.companyValue, budgetCodeValue: input.budgetCodeValue })
  }
  if (editableInputBaseline.value) {
    const baseline = JSON.parse(editableInputBaseline.value) as ReimbursementDraftInput
    editableInputBaseline.value = inputSignature({ ...baseline, companyValue: input.companyValue, budgetCodeValue: input.budgetCodeValue })
  }
}
function shanghaiDate(milliseconds: number): string {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date(milliseconds))
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}`
}
function approvalSelection(approval: ReimbursementRelatedApproval): ReimbursementRelatedApprovalSelection {
  return {
    processInstanceId: approval.processInstanceId, profileKey: approval.profileKey,
    queryWindow: { from: shanghaiDate(approval.queryWindow.startTimeMs), to: shanghaiDate(approval.queryWindow.endTimeMs) },
  }
}
function cloneRelatedApprovalSelections(
  selections: ReimbursementRelatedApprovalSelection[],
): ReimbursementRelatedApprovalSelection[] {
  return JSON.parse(JSON.stringify(selections)) as ReimbursementRelatedApprovalSelection[]
}
function acceptAuthoritativeRelatedApprovals(): void {
  const authoritativeSelections = drafts.currentDraft?.relatedApprovals.map(approvalSelection) ?? []
  selectedRelatedApprovals.value = authoritativeSelections
  savedRelatedSignature.value = relatedSignature(authoritativeSelections)
  acceptDerivedAccounting()
}
function acceptApprovalReadback(draft: ReimbursementDraft): boolean {
  acceptAuthoritativeRelatedApprovals()
  if (savedInputSignature.value && inputSignature(draft.input) !== savedInputSignature.value) {
    saveRequiresReload.value = true
  }
  return !saveRequiresReload.value
}
function reconcileRelatedApprovalsFromServer(
  targetSignature: string,
  draftId: string,
  scope: string,
): 'accepted' | 'restored' | 'conflict' | 'stale' {
  const draft = drafts.currentDraft
  if (
    !draft
    || draft.id !== draftId
    || sessionScope() !== scope
    || relatedSignature(selectedRelatedApprovals.value) !== targetSignature
  ) return 'stale'
  if (!acceptApprovalReadback(draft)) return 'conflict'
  return savedRelatedSignature.value === targetSignature
    && (targetSignature === relatedSignature([]) || draft.input.accountingSourceVerified === true)
    ? 'accepted'
    : 'restored'
}
async function verifyRelatedApprovals(
  selections: ReimbursementRelatedApprovalSelection[],
): Promise<'accepted' | 'restored' | 'conflict' | 'stale'> {
  const requestedSelections = cloneRelatedApprovalSelections(selections)
  const targetSignature = relatedSignature(requestedSelections)
  const draftId = drafts.currentDraft?.id ?? ''
  const scope = sessionScope()
  relatedApprovalVerificationError.value = ''
  relatedApprovalRetry.value = null
  verifyingRelatedApprovals.value = true
  selectedRelatedApprovals.value = requestedSelections
  try {
    await saveCoordinator.run(() => drafts.saveRelatedApprovals(requestedSelections))
    const reconciliation = reconcileRelatedApprovalsFromServer(targetSignature, draftId, scope)
    if (reconciliation === 'accepted') {
      drafts.mutationError = ''
      saveError.value = ''
    } else if (reconciliation === 'restored') {
      relatedApprovalVerificationError.value = '所选出差审批未保存，请重新选择或重试'
      drafts.mutationError = ''
      saveError.value = relatedApprovalVerificationError.value
      relatedApprovalRetry.value = requestedSelections
    }
    return reconciliation
  } catch (error) {
    const reconciliation = reconcileRelatedApprovalsFromServer(targetSignature, draftId, scope)
    if (reconciliation === 'accepted') {
      drafts.mutationError = ''
      saveError.value = ''
    } else if (reconciliation === 'restored') {
      relatedApprovalVerificationError.value = drafts.mutationError
        || (error instanceof Error ? error.message : '出差审批核验失败，请重新选择或重试')
      drafts.mutationError = ''
      saveError.value = relatedApprovalVerificationError.value
      relatedApprovalRetry.value = requestedSelections
    }
    if (reconciliation === 'restored') void refreshOptionsAfterError(error)
    return reconciliation
  } finally {
    if (scope === sessionScope()) {
      verifyingRelatedApprovals.value = false
      scheduleAutosave()
      if (drafts.currentDraft?.id === draftId && relatedApprovalVerificationError.value) void revealAttention('save')
    }
  }
}
async function reconfirmRelatedApprovals(): Promise<void> {
  await verifyRelatedApprovals(selectedRelatedApprovals.value)
}
async function retrySave(): Promise<void> {
  if (verifyingRelatedApprovals.value || formReadOnly.value) return
  if (relatedApprovalRetry.value) await verifyRelatedApprovals(relatedApprovalRetry.value)
  else await flushAutosave().catch(() => undefined)
}
async function prepareExcelPreview(): Promise<void> {
  const scope = sessionScope(), draftId = drafts.currentDraft?.id
  try {
    if (localUploadFailureReason.value) throw new Error(localUploadFailureReason.value)
    await flushAutosave()
    if (localUploadFailureReason.value) throw new Error(localUploadFailureReason.value)
    if (disposed || scope !== sessionScope() || draftId !== drafts.currentDraft?.id) {
      throw new DOMException('当前报销已变化，已取消原预览', 'AbortError')
    }
  }
  catch (error) {
    if (scope === sessionScope() && draftId === drafts.currentDraft?.id) await revealAttention('save')
    throw error
  }
}
function hydrate(draft: ReimbursementDraft): void {
  initializationError.value = ''
  const recoveryError = recoveryErrorOwner.value
  if (recoveryError && !draft.recoveryPending && draft.recoverySourceDraftId === recoveryError.sourceId
    && recoveryError.scope === sessionScope() && recoveryError.generation === recoveryGeneration) {
    workspaceActionError.value = ''
  }
  deferredMaterialRevision.value = null
  hydratedLockedDraftId.value = draft.status === 'LOCKED' ? draft.id : ''
  companyValue.value = draft.input.companyValue
  budgetCodeValue.value = draft.input.budgetCodeValue
  expense.hydrateFromDraft(draft, drafts.files)
  drafts.acknowledgeDraftInput(draft.id, draft.revision)
  selectedRelatedApprovals.value = draft.relatedApprovals.map(approvalSelection)
  savedInputSignature.value = inputSignature(draft.input)
  savedRelatedSignature.value = relatedSignature(selectedRelatedApprovals.value)
  saveError.value = ''
  saveRequiresReload.value = false
  drafts.revisionConflict = false
  expiredDraftId.value = ''
  unavailableDraftId.value = ''
  relatedApprovalVerificationError.value = ''
  relatedApprovalRetry.value = null
  const scope = sessionScope()
  void nextTick(() => {
    if (!disposed && scope === sessionScope() && drafts.currentDraft?.id === draft.id) {
      editableInputBaseline.value = inputSignature(currentFormInput.value)
    }
  })
}
async function loadSubmittedReceipt(): Promise<void> {
  const result = submittedResult.value
  const scope = sessionScope()
  if (!result || !scope || drafts.loadingCurrentDraft || submittedReimbursement.value) return
  if (hasUnsavedUserChanges.value) {
    // A different tab may have submitted while this page still held real edits.
    // Status discovery is a read, not consent to discard those edits.
    saveRequiresReload.value = true
    saveError.value = '这笔报销已提交，本页未保存的修改仍保留，请核对最新报销内容'
    return
  }
  receiptLoadError.value = ''
  await drafts.loadDraft(result.draftId, true)
  if (disposed || sessionScope() !== scope || submittedResult.value?.draftId !== result.draftId) return
  const locked = drafts.currentDraft
  if (drafts.loadError || locked?.id !== result.draftId || locked.status !== 'LOCKED') {
    receiptLoadError.value = drafts.loadError || '暂未读到本次提交的报销内容，请重试。'
    return
  }
  hydrate(locked)
}
async function readReplacementResult(previousId: string, context: WorkspaceReadContext): Promise<ReplacementReadResult> {
  const workspace = await workspaceReader.list(context)
  if (workspace.kind === 'stale' || !workspaceReader.accepts(context) || drafts.currentDraft?.id !== previousId) return { kind: 'stale' }
  if (workspace.kind === 'legacy') throw new Error('发现多笔旧报销，请重新加载并选择保留哪一笔')
  if (workspace.kind === 'empty') return { kind: 'empty' }
  const latest = workspace.draft, previous = drafts.currentDraft
  if (latest.id === previousId && latest.revision === previous.revision && latest.status === previous.status
    && Boolean(latest.recoveryPending) === Boolean(previous.recoveryPending)
    && Boolean(latest.workspaceRetired) === Boolean(previous.workspaceRetired)) {
    workspaceConfirmationRequired.value = false
    return { kind: 'unchanged' }
  }
  let conflict = false, preserveEdits = false
  const loaded = await workspaceReader.load(context, latest.id, { accept: (incoming) => {
    if (incoming.id !== previousId || !formDirty.value) return true
    const editable = ['DRAFT', 'REVIEW_READY'].includes(incoming.status) && !incoming.recoveryPending
    if (!editable || inputSignature(incoming.input) !== savedInputSignature.value
      || relatedSignature(incoming.relatedApprovals.map(approvalSelection)) !== savedRelatedSignature.value) {
      conflict = true
      return false
    }
    preserveEdits = true
    return true
  } })
  if (loaded.kind === 'stale' || !workspaceReader.accepts(context)) return { kind: 'stale' }
  if (conflict) {
    workspaceConfirmationRequired.value = false
    saveRequiresReload.value = true
    saveError.value = '服务器中的报销状态或内容已变化，本页修改已保留，请核对最新报销内容'
    return { kind: 'conflict' }
  }
  if (loaded.kind === 'rejected') throw new Error(loaded.message || '当前报销读取期间已变化，请重新核对')
  workspaceConfirmationRequired.value = false
  drafts.mutationError = ''
  return preserveEdits ? { kind: 'unchanged' } : loaded
}
async function confirmCurrentWorkspace(): Promise<void> {
  const previousId = drafts.currentDraft?.id
  if (!previousId || !sessionScope() || !workspaceConfirmationRequired.value || initializingWorkspace.value) return
  const context = workspaceReader.begin()
  initializingWorkspace.value = true
  try {
    const result = await readReplacementResult(previousId, context)
    if (!workspaceReader.accepts(context) || result.kind === 'stale') return
    if (result.kind === 'empty') {
      if (!await openEditableReimbursement(false, context)) return
      if (!workspaceReader.accepts(context)) return
      pendingNewApproval.value = null
      await returnToApprovalStep()
      if (!workspaceReader.accepts(context)) return
    } else if (result.kind === 'loaded') {
      await showWorkspaceReimbursement(result.draft)
      if (!workspaceReader.accepts(context) || drafts.currentDraft?.id !== result.draft.id) return
      pendingNewApproval.value = null
      await returnToApprovalStep()
      if (!workspaceReader.accepts(context)) return
    }
    workspaceActionError.value = ''
    drafts.mutationError = ''
  } catch {
    if (workspaceReader.accepts(context)) workspaceActionError.value = '暂时无法核对当前报销，请稍后重试'
  } finally {
    if (workspaceReader.accepts(context)) { initializingWorkspace.value = false; scheduleAutosave() }
  }
}
async function openEditableReimbursement(
  replaceCurrent = false,
  context = workspaceReader.begin(),
): Promise<boolean> {
  if (legacyDrafts.value.length > 1) throw new Error('请先选择保留哪一笔当前报销')
  const previous = replaceCurrent && drafts.currentDraft
    ? { id: drafts.currentDraft.id, revision: drafts.currentDraft.revision } : undefined
  let created: ReimbursementDraft
  try {
    created = await drafts.createDraft(emptyReimbursementInput(drafts.reimbursementKind), true, auth.session?.selectedDepartment ?? undefined, previous)
  } catch (error) {
    if (!workspaceReader.accepts(context)) return false
    if (!previous || drafts.currentDraft?.id !== previous.id) throw error
    workspaceConfirmationRequired.value = true
    let result: ReplacementReadResult
    try { result = await readReplacementResult(previous.id, context) }
    catch { throw new Error('无法确认新开结果，请先核对当前报销') }
    if (!workspaceReader.accepts(context) || result.kind === 'stale') return false
    if (result.kind === 'empty') return openEditableReimbursement(false, context)
    if (result.kind === 'conflict') throw new Error(saveError.value, { cause: error })
    if (result.kind === 'unchanged') throw error
    created = result.draft
  }
  if (!workspaceReader.accepts(context) || drafts.currentDraft?.id !== created.id) return false
  workspaceConfirmationRequired.value = false
  expense.reset()
  await showWorkspaceReimbursement(created)
  if (!workspaceReader.accepts(context) || drafts.currentDraft?.id !== created.id) return false
  if (templateMismatch.value) await drafts.loadReimbursementOptions()
  return workspaceReader.accepts(context) && drafts.currentDraft?.id === created.id
}

async function switchReimbursementEmployee(userId: string | null): Promise<void> {
  const origin = auth.session
  if (!origin || (!origin.canDelegateReimbursements && !origin.operator) || reimbursementSwitchBlock.value) return
  const returningToPersonal = userId === null && Boolean(origin.operator)
  const recheckReason = reimbursementSwitchRecheck.value
  if (!returningToPersonal && (delegationRevoked.value || recheckReason)) return
  if (userId === origin.user.userId || (userId === null && !origin.operator)) {
    employeePickerOpen.value = false
    return
  }
  const formWasReadOnly = formReadOnly.value
  const oldScope = sessionScope()
  const draftId = drafts.currentDraft?.id
  const request = new AbortController()
  const accepts = () => !disposed && !request.signal.aborted && auth.session === origin
    && oldScope === sessionScope() && draftId === drafts.currentDraft?.id
  reimbursementSwitchRequest = request
  reimbursementSwitchError.value = ''
  switchingReimbursement.value = true
  saveCoordinator.cancelScheduled()
  let discardDeniedEdits = false
  try {
    if (returningToPersonal && recheckReason && !delegationRevoked.value) {
      try {
        // A conflict or cached file status can hide a revoked scope from autosave.
        await getMe(origin.reimbursementContext, request.signal)
      } catch (error) {
        if (!isDelegationContextError(error)) throw error
        if (accepts()) deniedDelegationScope.value = oldScope
      }
      if (!accepts()) return
      if (!delegationRevoked.value) throw new Error(recheckReason)
    }
    if (!delegationRevoked.value && !formWasReadOnly && (formDirty.value || saveCoordinator.pending.value)) {
      try {
        await flushAutosave()
      } catch (error) {
        if (!returningToPersonal || !isDelegationContextError(error)) throw error
      }
    }
    if (!accepts()) return
    if (draftId && hasUnsavedUserChanges.value) {
      if (!returningToPersonal || !delegationRevoked.value) {
        throw new Error('当前报销还有未保存的内容，请确认保存后再切换')
      }
      try {
        await ElMessageBox.confirm(
          '代报权限或办理对象已变化。返回本人将舍弃本页未保存的修改。已保存的草稿和票据会保留。',
          '返回本人报销',
          { type: 'warning', confirmButtonText: '舍弃修改并返回', cancelButtonText: '保留本页' },
        )
      } catch { return }
      if (!accepts()) return
      discardDeniedEdits = true
    }
    const target = await prepareReimbursementContext(userId, request.signal)
    if (!accepts()) return
    if (userId === null ? Boolean(target.operator || target.reimbursementContext)
      : target.user.userId !== userId || !target.operator || !target.reimbursementContext) {
      throw new Error('返回的报销人不匹配，请重新选择')
    }
    const workspace = await workspaceReader.prepareEmployee(target, request.signal)
    if (!accepts() || workspace === undefined) return
    if (draftId && hasUnsavedUserChanges.value && !discardDeniedEdits) {
      throw new Error('当前报销还有未保存的内容，请确认保存后再切换')
    }
    auth.applyReimbursementContext(target)
    employeePickerOpen.value = false
    mobileStep.value = 0
    if (workspace) {
      drafts.restoreWorkspace(workspace.read, workspace.options)
      initializedScope = sessionScope()
      void expense.loadCategories()
      void showWorkspaceReimbursement(workspace.read.draft)
    } else {
      initializationError.value = ''
      saveError.value = ''
      saveRequiresReload.value = false
      companyValue.value = ''
      budgetCodeValue.value = ''
      selectedRelatedApprovals.value = []
      savedInputSignature.value = ''
      savedRelatedSignature.value = ''
      editableInputBaseline.value = ''
    }
  } catch (error) {
    if (accepts()) reimbursementSwitchError.value = apiErrorMessage(error, '无法切换报销人，请重试')
  } finally {
    if (reimbursementSwitchRequest === request) {
      reimbursementSwitchRequest = undefined
      switchingReimbursement.value = false
      if (!saveError.value) scheduleAutosave()
    }
  }
}

function openReimbursementEmployeePicker(): void {
  reimbursementSwitchError.value = ''
  employeePickerOpen.value = true
}

function chooseReimbursementEmployee(employee: ReimbursementEmployee): void {
  void switchReimbursementEmployee(employee.userId === auth.session?.operator?.userId ? null : employee.userId)
}
async function runWorkspaceInitialization(scope: string, reservedDraftId?: string): Promise<void> {
  const context = workspaceReader.begin()
  if (context.scope !== scope) return
  initializingWorkspace.value = true
  initializationError.value = ''
  try {
    const [, , , workspace] = await Promise.all([
      expense.loadCategories(), drafts.loadReimbursementOptions(),
      auth.session?.selectedDepartment ? drafts.loadDrafts(0, 50, auth.session.selectedDepartment) : Promise.resolve(),
      workspaceReader.list(context),
    ])
    if (workspace.kind === 'stale' || !workspaceReader.accepts(context)) return
    if (drafts.listError) throw new Error(drafts.listError)
    // Only the current reimbursement is restored; replaced work is never
    // rediscovered merely because a background submission changed status.
    legacyDrafts.value = workspace.kind === 'legacy' ? workspace.items : []
    if (workspace.kind === 'legacy') return
    const recent = workspace.kind === 'current' ? workspace.draft : undefined
    const currentDepartment = selectableDepartments.value.find(d => d.id === recent?.department.id)
    if (recent && (!auth.session?.selectedDepartment || (currentDepartment
      && (currentDepartment.id !== auth.session.selectedDepartment.id
        || currentDepartment.name !== auth.session.selectedDepartment.name)))) {
      await openSavedWorkspace(recent, [recent])
      return
    }
    if (recent) generalDepartmentConfirmed.value = true
    if (!recent && (auth.status === 'department_required' || generalDepartmentRequired.value)) {
      // The gate can mount before workspace options finish loading. A newer
      // options observation correctly invalidates that first query; perform the
      // initial lookup after initialization rather than showing a false empty list.
      if (!isGeneral.value && !drafts.reimbursementOptionsError
        && !drafts.travelApprovals.length && !drafts.travelApprovalsError) {
        await nextTick()
        if (!workspaceReader.accepts(context)) return
        await departmentApprovalSelector.value?.search()
      }
      return
    }
    if (reservedDraftId && recent?.id !== reservedDraftId) {
      throw new Error('目标部门的报销已在其他页面更新，请确认后重新加载')
    }
    if (recent) {
      const loaded = await workspaceReader.load(context, recent.id)
      if (loaded.kind === 'stale' || !workspaceReader.accepts(context)) return
      if (loaded.kind === 'rejected') throw new Error(loaded.message || '报销内容加载失败')
      const opened = loaded.draft
      workspaceConfirmationRequired.value = false
      hydrate(opened)
      try {
        await submission.restoreForDraft(opened)
      } catch { /* The submission card renders the recoverable error. */ }
    } else {
      if (drafts.reimbursementOptionsError) throw new Error(drafts.reimbursementOptionsError)
      if (!await openEditableReimbursement(false, context)) return
    }
    if (workspaceReader.accepts(context)) initializedScope = scope
  } catch (error) {
    if (workspaceReader.accepts(context)) initializationError.value = error instanceof Error ? error.message : '报销表单加载失败，请重试'
  } finally {
    if (workspaceReader.accepts(context)) { initializingWorkspace.value = false; scheduleAutosave() }
  }
}
function initializeWorkspace(force = false, reservedDraftId?: string): Promise<void> {
  const scope = sessionScope()
  if (!scope || disposed || changingDepartment.value
    || (!force && initializedScope === scope)) return Promise.resolve()
  if (initializationPromise && initializationScope === scope) return initializationPromise
  initializationScope = scope
  const running = runWorkspaceInitialization(scope, reservedDraftId).finally(() => {
    if (initializationPromise === running) initializationPromise = null
  })
  initializationPromise = running
  return running
}
async function openCurrentWorkspace(): Promise<void> {
  const draftId = drafts.currentDraft?.id
  const readContext = workspaceReader.capture()
  if (initializingWorkspace.value || drafts.busy || saving.value || confirmingDraftReload.value) return
  if (hasUnsavedUserChanges.value) {
    confirmingDraftReload.value = true
    try {
      await ElMessageBox.confirm(
        '这笔报销已被新开内容替换，本页尚未保存的修改无法再保存。打开当前报销将舍弃这些修改，请先留存需要的内容。',
        '打开当前报销',
        { confirmButtonText: '舍弃本页修改并打开', cancelButtonText: '保留本页', type: 'warning' },
      )
    } catch { return }
    finally { if (workspaceReader.accepts(readContext)) confirmingDraftReload.value = false }
  }
  if (!workspaceReader.accepts(readContext)
    || draftId !== drafts.currentDraft?.id || drafts.busy || saving.value) return
  await initializeWorkspace(true)
}
async function flushAutosave(options: { includeRelated?: boolean } = {}): Promise<void> {
  const includeRelated = options.includeRelated !== false
  await saveCoordinator.run(async () => {
    // Never enqueue a snapshot behind a file mutation: its references can already
    // be deleted by the time the queued save runs. The idle watcher retries using
    // the latest form; explicit preview/submit must not treat a skipped save as success.
    if (drafts.busy) throw new Error('请等待材料处理完成后重试')
    if (saveRequiresReload.value) throw new Error('请先核对最新报销内容')
    if (!drafts.currentDraft || (!inputDirty.value && !(includeRelated && relatedDirty.value))) return
    const draftId = drafts.currentDraft.id
    const scope = sessionScope()
    const fileOperationPause = new Error('请等待材料处理完成后重试')
    saving.value = true
    saveError.value = ''
    try {
      // Keep typing enabled, save snapshots, and never hydrate an older response over live edits.
      while ((inputDirty.value || (includeRelated && relatedDirty.value))
        && drafts.currentDraft?.id === draftId && sessionScope() === scope) {
        if (drafts.busy) throw fileOperationPause
        // Verify the selected approvals first. Their company/budget values are
        // server-owned and should not wait behind unrelated form autosaves.
        if (includeRelated && relatedDirty.value) {
          const selections = cloneRelatedApprovalSelections(selectedRelatedApprovals.value)
          await drafts.saveRelatedApprovals(selections)
          if (drafts.currentDraft?.id !== draftId || sessionScope() !== scope) return
          if (!acceptApprovalReadback(drafts.currentDraft)) throw new Error('请先核对最新报销内容')
        }
        if (inputDirty.value) {
          const snapshot = JSON.parse(JSON.stringify(currentFormInput.value)) as ReimbursementDraftInput
          await drafts.saveDraft(snapshot)
          if (drafts.currentDraft?.id !== draftId || sessionScope() !== scope) return
          savedInputSignature.value = inputSignature(snapshot)
          editableInputBaseline.value = savedInputSignature.value
          acceptDerivedAccounting()
        }
      }
    } catch (error) {
      if (error !== fileOperationPause && drafts.currentDraft?.id === draftId && sessionScope() === scope) {
        if (includeRelated && relatedDirty.value) acceptApprovalReadback(drafts.currentDraft)
        if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_EXPIRED') expiredDraftId.value = draftId
        if (auth.session?.operator && isDelegationContextError(error)) deniedDelegationScope.value = scope
        saveError.value = drafts.mutationError || (error instanceof Error ? error.message : '自动保存失败')
        void refreshOptionsAfterError(error)
      }
      throw error
    } finally { if (scope === sessionScope() && drafts.currentDraft?.id === draftId) saving.value = false }
  })
}
onBeforeRouteLeave(async (to) => {
  if (localUploadFailureReason.value) {
    ElMessage.warning(localUploadFailureReason.value)
    return false
  }
  if (switchingReimbursement.value || recoveringReimbursement.value || submitFlowPending.value || bindingApprovalDepartment.value || replacingTemplate.value
    || initializingWorkspace.value || verifyingRelatedApprovals.value || pendingDepartmentApproval.value) {
    ElMessage.warning('请等待当前操作完成，或先完成部门选择')
    return false
  }
  // Before a workspace can open, default values have no saved draft baseline.
  // They are not employee edits; file failures and pending operations stay guarded above.
  if (!sessionScope() || !drafts.currentDraft
    || ((!formDirty.value || (formReadOnly.value && !hasUnsavedUserChanges.value)) && !saveCoordinator.pending.value)) return true
  if (auth.isAdmin && templateNeedsAdmin.value
    && to.path === (props.mobile ? '/m/settings' : '/admin/settings')) {
    if (drafts.busy || saving.value) {
      ElMessage.warning('请等待当前保存或材料处理完成后再进入设置')
      return false
    }
    // Hydrating older saved input can normalize defaults without an employee edit.
    if (!hasUnsavedUserChanges.value) return true
    const scope = sessionScope()
    const draftId = drafts.currentDraft?.id
    try {
      await ElMessageBox.confirm(
        '当前 OA 表单尚未确认，暂时无法保存本页修改。进入系统设置后，本页尚未保存的修改会被舍弃，请先留存需要的内容。',
        '前往系统设置处理表单',
        { confirmButtonText: '舍弃本页修改并前往设置', cancelButtonText: '保留本页', type: 'warning' },
      )
      return !disposed && scope === sessionScope() && draftId === drafts.currentDraft?.id
        && !drafts.busy && !saving.value
    } catch { return false }
  }
  if (formReadOnly.value && !saveRequiresReload.value) {
    ElMessage.warning('本页还有未保存的修改，请先处理后再离开。')
    return false
  }
  const scope = sessionScope()
  const draftId = drafts.currentDraft?.id
  try {
    await flushAutosave()
    if (sessionScope() !== scope || drafts.currentDraft?.id !== draftId || formDirty.value) {
      throw new Error('报销内容已变化，请确认保存状态后重试')
    }
    return true
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : '保存失败，请重试后再离开'
    ElMessage.error(saveError.value)
    return false
  }
})
interface SubmissionValidationIssue { message: string; step: number; itemId?: string; fileId?: string }
function validateSubmission(): SubmissionValidationIssue | null {
  const issue = (message: string, step: number, itemId?: string, fileId?: string) => ({ message, step, itemId, fileId })
  if (localUploadFailureReason.value) return issue(localUploadFailureReason.value, materialStep.value)
  if (generalCompanyError.value || generalBudgetError.value) return issue(generalCompanyError.value || generalBudgetError.value, 0)
  if (!companyOptions.value.some((option) => option.value === companyValue.value)) {
    return issue(isGeneral.value ? '请选择报销主体' : '请选择所属公司', 0)
  }
  if (!budgetOptions.value.some((option) => option.value === budgetCodeValue.value)) return issue('请选择预算代码', 0)
  if (expense.includeSubsidy && expense.tripPayloads().length !== selectedRelatedApprovals.value.length) {
    return issue(expense.policyInputError || '请完整确认每个补助项的出发和返回时段', 1)
  }
  if (expense.subsidyCalculationReason) return issue(expense.subsidyCalculationReason, 1)
  if (!isGeneral.value && expense.items.length && (expense.categoryLoadError || !expense.categories.length)) return issue('请先加载费用类别', materialStep.value)
  if (!expense.items.length && !hasValidSubsidy.value) {
    return issue(isGeneral.value ? '请添加费用明细' : '请添加费用明细或申请有效的出差补助', materialStep.value)
  }
  if (expense.itemReadinessError) return issue(expense.itemReadinessError, materialStep.value)
  if (materialSubmissionIssues.value.length) {
    const material = materialSubmissionIssues.value[0]!
    return issue(`请先处理“${material.file.name}”：${material.reason}`, materialStep.value, material.itemId, material.itemId ? undefined : material.file.id)
  }
  if (!isGeneral.value && !selectedRelatedApprovals.value.length) return issue('请至少关联一张已通过的出差审批', 0)
  if (!hasActiveMaterials.value && !excelOnlySubmission.value) return issue('请上传报销材料', materialStep.value)
  for (const item of expense.items) {
    if (item.category === 'lodging' && !item.hotelBillFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'hotel_bill')),
    )) return issue(`“${item.description || '住宿费用'}”缺少住宿明细，请补充后提交`, materialStep.value, item.id)
    if (item.transportType !== 'taxi' && (item.requiresItinerary || item.transportType === 'ride_hailing') && !item.itineraryFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'itinerary')),
    )) return issue(`“${item.description || '网约车费用'}”缺少对应行程单，请点击编辑补齐`, materialStep.value, item.id)
    if (isForeignExpense(item) && !item.cnyAmountConfirmed) {
      return issue(`请确认“${item.description || '国外票据'}”的人民币报销金额`, materialStep.value, item.id)
    }
    if (requiresPaymentProof(item) && !item.paymentProofFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'payment_proof')),
    )) return issue(`“${item.description || '本行费用'}”超过 500 元，请补充付款凭证`, materialStep.value, item.id)
  }
  return null
}
async function revealValidationIssue(issue: SubmissionValidationIssue): Promise<void> {
  const scope = sessionScope(), draftId = drafts.currentDraft?.id
  if (props.mobile) selectMobileStep(issue.step)
  await nextTick()
  if (disposed || scope !== sessionScope() || draftId !== drafts.currentDraft?.id) return
  const panel = document.getElementById(`reimbursement-step-${issue.step}`)
  const target = panel?.querySelector<HTMLElement>('.mobile-step-heading') ?? panel
  target?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  if (issue.itemId || issue.fileId || localUploadFailureReason.value) expenseItemsCard.value?.focusMaterial?.(issue.itemId, issue.fileId)
}
async function focusReviewApprovals(): Promise<void> {
  if (isGeneral.value) generalApprovalOpen.value = true
  if (props.mobile) selectMobileStep(0)
  await nextTick()
  document.getElementById(isGeneral.value ? 'general-approval-heading' : 'travel-approval-heading')
    ?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}
async function confirmAndSubmit(): Promise<void> {
  if (submissionButtonReason.value) return
  const error = validateSubmission()
  if (error) { ElMessage.warning(error.message); await revealValidationIssue(error); return }
  const scope = sessionScope(), draftId = drafts.currentDraft?.id
  const acceptsSubmit = () => !disposed && scope === sessionScope() && draftId === drafts.currentDraft?.id
  const confirmedInput = inputSignature(currentFormInput.value)
  const confirmedDepartment = { ...drafts.currentDraft!.department }
  const requireConfirmedDepartment = (draft: ReimbursementDraft): void => {
    if (draft.department.id !== confirmedDepartment.id || draft.department.name !== confirmedDepartment.name) {
      throw new Error('报销部门已更新，请核对后重新提交')
    }
  }
  const confirmedTotalAmount = expense.displayTotal
  const confirmedTotals = JSON.stringify([expense.displayExpenseTotal, expense.displaySubsidyTotal,
    expense.displayTotal, expense.displayReceiptCount])
  let failureTarget: AttentionKind | undefined
  submitFlowPending.value = true
  try {
    const subject = auth.session?.operator
      ? `报销人：${auth.session.user.name}（${confirmedDepartment.name}）。` : ''
    await ElMessageBox.confirm(
      subject + (excelOnlySubmission.value
        ? '核对无误后将生成报销单 Excel，并正式提交钉钉 OA。提交后内容将锁定。'
        : '核对无误后将生成票据汇总 PDF 和报销单 Excel，并正式提交钉钉 OA。提交后内容将锁定。'),
      '提交报销申请', { confirmButtonText: '确认提交', cancelButtonText: '返回检查', type: 'warning' },
    )
    if (!acceptsSubmit()) return
    workspaceActionError.value = ''
    await flushAutosave()
    if (!acceptsSubmit()) return
    requireConfirmedDepartment(drafts.currentDraft!)
    if (localUploadFailureReason.value) throw new Error(localUploadFailureReason.value)
    const ready = await drafts.markReviewReady()
    if (!acceptsSubmit() || ready.id !== draftId) return
    requireConfirmedDepartment(ready)
    if (confirmedInput !== inputSignature(currentFormInput.value)) {
      throw new Error('报销内容已更新，请核对后重新提交')
    }
    if (confirmedTotals !== JSON.stringify([ready.totals.expenseTotal, ready.totals.subsidyTotal,
      ready.totals.totalAmount, ready.totals.receiptCount])) {
      hydrate(ready)
      throw new Error('报销金额已更新，请核对最新金额后重新提交')
    }
    await submission.submit(ready.id, ready.revision, confirmedTotalAmount)
    if (acceptsSubmit()) ElMessage.success('正在生成材料并提交 OA')
  } catch (error) {
    if (disposed || scope !== sessionScope() || draftId !== drafts.currentDraft?.id) return
    if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_EXPIRED' && drafts.currentDraft) {
      expiredDraftId.value = drafts.currentDraft.id
    }
    void refreshOptionsAfterError(error)
    if (apiErrorCode(error) === 'REIMBURSEMENT_TOTAL_CHANGED') void expense.refreshCalculations()
    if (error !== 'cancel' && error !== 'close') {
      const message = submission.errorMessage || saveError.value || drafts.mutationError
        || (error instanceof Error ? error.message : '提交失败，请重试')
      ElMessage.error(message)
      if (apiErrorCode(error) === 'REIMBURSEMENT_TOTAL_CHANGED') {
        workspaceActionError.value = message
        failureTarget = 'workspace'
      } else if (saveError.value || saveRequiresReload.value) failureTarget = 'save'
      else if (submission.activeDraftId === draftId && submission.errorMessage) failureTarget = 'submission'
      else { workspaceActionError.value = message; failureTarget = 'workspace' }
    }
  } finally {
    if (acceptsSubmit()) {
      submitFlowPending.value = false
      scheduleAutosave()
      if (failureTarget) void revealAttention(failureTarget)
    }
  }
}
async function newReimbursement(): Promise<void> {
  if (needsReimbursementDepartment.value) {
    if (!newEntryOpen.value) generalDepartment.value = ''
    newEntryOpen.value = true
    await nextTick()
    const target = document.querySelector<HTMLElement>('[data-testid="reimbursement-department-gate"]')
    target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
    target?.focus({ preventScroll: true })
    return
  }
  await startNewInDepartment()
}

async function finishNewEntry(selection?: ReimbursementRelatedApprovalSelection): Promise<void> {
  const scope = sessionScope()
  if (!scope || !newEntryOpen.value) return
  await initializeWorkspace()
  if (disposed || scope !== sessionScope() || !newEntryOpen.value) return
  if (canStartNewReimbursement.value) {
    await startNewInDepartment(selection)
    if (!disposed && scope === sessionScope()) newEntryOpen.value = false
    return
  }
  newEntryOpen.value = false
  if (selection && !formReadOnly.value) await verifyRelatedApprovals([selection])
}

async function startNewInDepartment(newSelection?: ReimbursementRelatedApprovalSelection): Promise<void> {
  if (!canStartNewReimbursement.value || initializingWorkspace.value || drafts.busy
    || reimbursementOptionsUnavailable.value || confirmingDraftReload.value) return
  const readContext = workspaceReader.capture()
  const draftId = drafts.currentDraft?.id
  if (!submission.succeeded || hasUnsavedUserChanges.value) {
    confirmingDraftReload.value = true
    try {
      await ElMessageBox.confirm(
        submission.succeeded
          ? '新开后将舍弃本页未保存的修改，已提交的报销记录会保留。确定新开一笔吗？'
          : trackedSubmission.value && submission.status !== 'FAILED_FINAL'
          ? '新开后，本页将填写另一笔报销。上一笔仍会继续提交或核对结果，请勿重复报销同一笔费用。'
          : '新开后将放弃当前填写内容和材料，不能返回继续填写。确定新开一笔吗？',
        '新开一笔报销',
        { confirmButtonText: '确认新开', cancelButtonText: '继续当前报销', type: 'warning' },
      )
    } catch { return }
    finally { if (workspaceReader.accepts(readContext)) confirmingDraftReload.value = false }
    if (!workspaceReader.accepts(readContext) || draftId !== drafts.currentDraft?.id
      || !canStartNewReimbursement.value || drafts.busy || reimbursementOptionsUnavailable.value) return
  }
  const selection = newSelection ?? pendingNewApproval.value
  const context = workspaceReader.begin()
  workspaceActionError.value = ''
  initializingWorkspace.value = true
  try {
    if (!await openEditableReimbursement(true, context)) return
    if (!workspaceReader.accepts(context)) return
    await returnToApprovalStep()
    if (!workspaceReader.accepts(context)) return
    const opened = drafts.currentDraft
    if (opened?.status !== 'DRAFT' && opened?.status !== 'REVIEW_READY') return
    pendingNewApproval.value = null
    if (selection && !opened.relatedApprovals.length) {
      await verifyRelatedApprovals([selection])
    } else if (selection && !opened.relatedApprovals.some((approval) => approval.processInstanceId === selection.processInstanceId)) {
      ElMessage.warning('已打开当前报销并保留原有关联，请在出差审批中核对后再调整。')
    }
  }
  catch (error) {
    if (!workspaceReader.accepts(context)) return
    workspaceActionError.value = error instanceof Error ? error.message : '新报销准备失败'
    ElMessage.error(workspaceActionError.value)
    void refreshOptionsAfterError(error)
  }
  finally {
    if (workspaceReader.accepts(context)) {
      initializingWorkspace.value = false
      if (!saveError.value) scheduleAutosave()
    }
  }
}
async function returnToApprovalStep(): Promise<void> {
  const readContext = workspaceReader.capture()
  mobileStep.value = 0
  await nextTick()
  if (workspaceReader.accepts(readContext)) scrollToWorkspaceStart()
}
function scrollToWorkspaceStart(): void {
  document.querySelector<HTMLElement>('[data-testid="reimbursement-workspace-start"]')
    ?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
}

async function refreshFormOptions(): Promise<void> {
  const scope = sessionScope()
  if (!scope || drafts.loadingReimbursementOptions) return
  await drafts.loadReimbursementOptions()
  await nextTick()
  if (scope === sessionScope() && !disposed && templateNeedsAdmin.value) {
    ElMessage.warning(auth.isAdmin ? 'OA 表单仍需确认，请前往系统设置处理' : 'OA 表单仍需管理员确认，请联系管理员处理')
  }
  if (scope === sessionScope() && !disposed && !formReadOnly.value) {
    await approvalSelector.value?.search?.()
  }
}

function openTemplateSettings(): void {
  if (!auth.isAdmin) return
  void router.push({ path: props.mobile ? '/m/settings' : '/admin/settings',
    hash: isGeneral.value ? '#general-oa-template' : '#oa-template-catalog' })
}

async function recoverAccountingOptions(): Promise<void> {
  await returnToApprovalStep()
  await refreshFormOptions()
}

async function refreshOptionsAfterError(error: unknown): Promise<void> {
  if ([
    'OA_TEMPLATE_CONFIGURATION_CHANGED', 'OA_TEMPLATE_SCHEMA_CHANGED', 'OA_TEMPLATE_CONFIRMATION_REQUIRED',
    'OA_TEMPLATE_NOT_CONFIGURED',
    'OA_GENERAL_TEMPLATE_NOT_CONFIGURED', 'OA_GENERAL_TEMPLATE_CHANGED', 'OA_GENERAL_TEMPLATE_INVALID',
    'REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED', 'REIMBURSEMENT_COMPANY_OPTION_INVALID',
    'REIMBURSEMENT_BUDGET_OPTION_INVALID', 'REIMBURSEMENT_SUBJECT_OPTION_INVALID',
    'REIMBURSEMENT_SUBJECT_CATEGORY_CHANGED',
  ].includes(apiErrorCode(error) ?? '')) await refreshFormOptions()
}

async function openSavedReimbursement(draftId: string, history?: AbortSignal): Promise<void> {
  const scope = history ? historyScope() : sessionScope()
  let context = workspaceReader.capture()
  const acceptsRead = () => workspaceReader.accepts(context) && !history?.aborted && scope === (history ? historyScope() : sessionScope())
  const previousId = drafts.currentDraft?.id
  if (!scope || initializingWorkspace.value || drafts.busy || saving.value
    || submitFlowPending.value || confirmingDraftReload.value || submission.submitting) return
  if (history && (!canOpenHistory.value || bindingApprovalDepartment.value
    || verifyingRelatedApprovals.value || pendingDepartmentApproval.value)) return
  if (!history && draftId !== previousId) return
  if (history && !trackedSubmission.value && formDirty.value && !formReadOnly.value) {
    try {
      await flushAutosave()
      if (formDirty.value) throw new Error('报销内容尚未保存，请核对后再查看记录')
    } catch (error) {
      if (acceptsRead() && previousId === drafts.currentDraft?.id) {
        workspaceActionError.value = error instanceof Error ? error.message : '保存失败，请重试'
      }
      return
    }
  }
  if (!acceptsRead() || previousId !== drafts.currentDraft?.id) return
  if (previousId && hasUnsavedUserChanges.value) {
    confirmingDraftReload.value = true
    saveCoordinator.cancelScheduled()
    try {
      await ElMessageBox.confirm(
        '本页有尚未保存的修改。继续将读取服务器保存的内容，本页未保存的修改会被舍弃。',
        '读取已保存的报销',
        { confirmButtonText: '读取已保存内容', cancelButtonText: '保留本页', type: 'warning' },
      )
    } catch { return }
    finally {
      // Cancelling the history read must still release its own confirmation.
      if (workspaceReader.accepts(context)) { confirmingDraftReload.value = false; scheduleAutosave() }
    }
  }
  if (!acceptsRead() || previousId !== drafts.currentDraft?.id
    || drafts.busy || saving.value || (history && !canOpenHistory.value)) return
  context = workspaceReader.begin()
  saveCoordinator.cancelScheduled()
  initializingWorkspace.value = true
  workspaceActionError.value = ''
  try {
    const receipt = history ? await getOaReimbursementSubmissionForDraft(draftId, { history: true, signal: history }) : null
    if (!acceptsRead()) return
    if (history && (receipt?.status !== 'SUBMITTED' || receipt.draftId !== draftId)) {
      throw new Error('这笔报销的成功回执暂不可用，请重新加载')
    }
    const loaded = await workspaceReader.load(context, draftId, {
      source: history ? 'history' : 'saved', accept: acceptsRead,
    })
    if (!acceptsRead() || loaded.kind === 'stale') return
    if (loaded.kind === 'rejected') {
      const message = loaded.message || '这笔报销已不可用，请重新加载报销列表'
      if (history) historyDetailError.value = message
      else workspaceActionError.value = message
      return
    }
    const opened = loaded.draft
    if (history) {
      historyOrigin.value = { draftId: previousId, mobileStep: mobileStep.value, pendingApproval: pendingNewApproval.value }
      pendingNewApproval.value = null
    }
    drafts.mutationError = ''
    hydrate(opened)
    if (history) historicalSubmission.value = receipt
    else {
      try {
        await submission.restoreForDraft(opened)
      } catch { /* The submission panel shows the recovery action. */ }
    }
    if (!acceptsRead() || drafts.currentDraft?.id !== draftId) return
    if (previousId !== draftId) {
      await returnToApprovalStep()
    }
  } catch (error) {
    if (acceptsRead() && history) historyDetailError.value = apiErrorMessage(error, '报销明细加载失败，请重试')
  } finally {
    if (workspaceReader.accepts(context)) { initializingWorkspace.value = false; scheduleAutosave() }
  }
}
async function reconcileMaterialRevision(): Promise<void> {
  const draft = drafts.currentDraft
  if (!draft || !sessionScope() || !['DRAFT', 'REVIEW_READY'].includes(draft.status)) return
  if (expenseItemsCard.value?.hasOpenMaterialEditor) {
    deferredMaterialRevision.value = draft.id
    return
  }
  deferredMaterialRevision.value = null
  if (formDirty.value || saveRequiresReload.value) {
    saveRequiresReload.value = true
    drafts.mutationError = '材料状态已更新，请先核对最新报销内容。'
  } else await openSavedReimbursement(draft.id)
}

async function refreshMaterialStatus(): Promise<void> {
  const draft = drafts.currentDraft
  const scope = sessionScope()
  if (!draft || !scope || drafts.busy || refreshingMaterialStatus.value) return
  refreshingMaterialStatus.value = true
  try {
    const result = await drafts.refreshCurrentFiles()
    if (disposed || sessionScope() !== scope || drafts.currentDraft?.id !== draft.id) return
    if (result === 'revision-changed') await reconcileMaterialRevision()
  } catch (error) {
    if (!disposed && sessionScope() === scope && drafts.currentDraft?.id === draft.id) {
      ElMessage.error(apiErrorMessage(error, '材料状态刷新失败，请重试'))
    }
  } finally {
    if (scope === sessionScope()) refreshingMaterialStatus.value = false
  }
}
function openHistory(): void {
  historyVisible.value = true
  void loadHistory()
}
function historyScope(): string {
  const session = auth.session
  return hasReimbursementLogin.value && session ? JSON.stringify([
    sessionGeneration.value, session.operator?.userId ?? session.user.userId, session.csrfToken,
  ]) : ''
}
async function loadHistory(): Promise<void> {
  if (!historyVisible.value || historyLoading.value) return
  const scope = historyScope()
  if (!scope) return
  const request = new AbortController()
  historyRequest = request
  historyRecords.value = []
  historyLoading.value = true
  historyError.value = ''
  historyDetailError.value = ''
  try {
    const records = await listReimbursementSubmissionHistory({
      signal: request.signal,
    })
    if (request.signal.aborted || scope !== historyScope()) return
    historyRecords.value = records
  } catch (error) {
    if (!request.signal.aborted && scope === historyScope()) {
      historyError.value = apiErrorMessage(error, '报销记录加载失败，请重试')
    }
  } finally {
    if (historyRequest === request) {
      historyRequest = undefined
      historyLoading.value = false
    }
  }
}
async function chooseHistoryRecord(draftId: string): Promise<void> {
  if (historyOpeningId.value) return
  const request = new AbortController()
  historyDetailRequest = request
  historyOpeningId.value = draftId
  historyDetailError.value = ''
  try { await openSavedReimbursement(draftId, request.signal) }
  finally {
    if (historyDetailRequest === request) {
      historyDetailRequest = undefined
      historyOpeningId.value = ''
      if (historyOrigin.value) historyVisible.value = false
    }
  }
}
async function returnFromHistory(): Promise<void> {
  const origin = historyOrigin.value
  const historicalId = drafts.currentDraft?.id
  if (!origin || !historicalId || initializingWorkspace.value || drafts.busy || saving.value
    || submitFlowPending.value || submission.submitting || confirmingDraftReload.value) return
  if (!sessionScope()) {
    historyOrigin.value = null
    historicalSubmission.value = null
    drafts.reset()
    expense.reset()
    return
  }
  const context = workspaceReader.begin()
  initializingWorkspace.value = true
  workspaceActionError.value = ''
  try {
    const workspace = await workspaceReader.list(context)
    if (workspace.kind === 'stale' || !workspaceReader.accepts(context) || historyOrigin.value !== origin || drafts.currentDraft?.id !== historicalId) return
    if (workspace.kind === 'legacy') {
      legacyDrafts.value = workspace.items
      historyOrigin.value = null
      historicalSubmission.value = null
      drafts.reset()
      expense.reset()
      return
    }
    const latest = workspace.kind === 'current' ? workspace.draft : undefined
    if (!latest) {
      // History's type only controls its display. First-entry department
      // selection belongs to the original workspace and must follow this read.
      const needsFirstGeneralDepartment = drafts.reimbursementKind === 'general'
        && selectableDepartments.value.length > 1 && !generalDepartmentConfirmed.value
      if (!auth.session?.selectedDepartment || needsFirstGeneralDepartment) {
        historyOrigin.value = null
        historicalSubmission.value = null
        drafts.reset()
        expense.reset()
        pendingNewApproval.value = null
        mobileStep.value = 0
        return
      }
      if (!await openEditableReimbursement(false, context) || !workspaceReader.accepts(context)) return
      historyOrigin.value = null
      historicalSubmission.value = null
      pendingNewApproval.value = null
      await returnToApprovalStep()
      return
    }
    const loaded = await workspaceReader.load(context, latest.id)
    if (loaded.kind === 'stale' || !workspaceReader.accepts(context)) return
    if (loaded.kind === 'rejected') throw new Error(loaded.message || '当前报销已变化，请重新返回')
    if (drafts.reimbursementKind === 'general') generalDepartmentConfirmed.value = true
    historyOrigin.value = null
    historicalSubmission.value = null
    await showWorkspaceReimbursement(loaded.draft)
    if (!workspaceReader.accepts(context) || drafts.currentDraft?.id !== latest.id) return
    pendingNewApproval.value = latest.id === origin.draftId ? origin.pendingApproval : null
    mobileStep.value = latest.id === origin.draftId ? origin.mobileStep : 0
    await nextTick()
    if (workspaceReader.accepts(context)) scrollToWorkspaceStart()
  } catch (error) {
    if (workspaceReader.accepts(context)) workspaceActionError.value = error instanceof Error ? error.message : '当前报销读取失败，请重试返回'
  } finally {
    if (workspaceReader.accepts(context)) { initializingWorkspace.value = false; scheduleAutosave() }
  }
}

async function replaceOutdatedForm(): Promise<void> {
  if (!canReplaceOutdatedForm.value || replacingTemplate.value || drafts.busy) return
  const draftId = drafts.currentDraft?.id
  if (!draftId) return
  const readContext = workspaceReader.capture()
  let context: WorkspaceReadContext | undefined
  const acceptsReplacement = () => context ? workspaceReader.accepts(context)
    : workspaceReader.accepts(readContext)
  replacingTemplate.value = true
  try {
    await ElMessageBox.confirm(
      '将打开新版 OA 表单，当前填写内容和材料不会自动转入。确认后将放弃当前内容，需要重新填写并上传材料。',
      '按新表单重新填写',
      { confirmButtonText: '重新填写', cancelButtonText: '返回查看', type: 'warning' },
    )
    if (!acceptsReplacement() || drafts.currentDraft?.id !== draftId
      || !canReplaceOutdatedForm.value || drafts.busy) return
    await drafts.loadDraft(draftId)
    if (!acceptsReplacement()) return
    const latest = drafts.currentDraft
    if (!latest || latest.id !== draftId || drafts.loadError) {
      throw new Error(drafts.loadError || '无法核对原报销状态，请重试')
    }
    if (latest.status === 'LOCKED') {
      hydrate(latest)
      try {
        await submission.restoreForDraft(latest)
      } catch { /* The submission panel shows the recovery action. */ }
      return
    }
    if (!canReplaceOutdatedForm.value || drafts.busy) return
    context = workspaceReader.begin()
    initializingWorkspace.value = true
    if (!await openEditableReimbursement(true, context)) return
    if (acceptsReplacement()) await returnToApprovalStep()
  } catch (error) {
    if (!acceptsReplacement()) return
    if (error !== 'cancel' && error !== 'close') {
      if (workspaceConfirmationRequired.value) workspaceActionError.value = error instanceof Error ? error.message : '请核对当前报销'
      ElMessage.error(error instanceof Error ? error.message : '新报销准备失败')
    }
  } finally {
    if (acceptsReplacement()) { replacingTemplate.value = false; initializingWorkspace.value = false }
  }
}
async function chooseTravelApprovalForDepartment(
  selections: ReimbursementRelatedApprovalSelection[],
): Promise<void> {
  if (bindingApprovalDepartment.value) return
  const selection = selections.at(-1)
  if (!selection) {
    selectedRelatedApprovals.value = []
    pendingDepartmentApproval.value = null
    selectedDepartmentChoiceId.value = ''
    verifyingRelatedApprovals.value = false
    relatedApprovalVerificationError.value = ''
    return
  }
  await resolveDepartmentForApproval(selection)
}
async function persistRelatedApprovalsImmediately(
  selections: ReimbursementRelatedApprovalSelection[],
): Promise<void> {
  await verifyRelatedApprovals(selections)
}
async function resolveDepartmentForApproval(
  selection: ReimbursementRelatedApprovalSelection,
  selectedDepartmentId?: string,
): Promise<void> {
  if (bindingApprovalDepartment.value) return
  if (drafts.currentDraft && !newEntryOpen.value && (!formReadOnly.value || draftDepartmentInvalid.value)) {
    await correctCurrentDepartment(selectedDepartmentId, selection)
    return
  }
  const generation = ++departmentBindingGeneration
  let scope = sessionScope()
  const acceptsBinding = () => !disposed && generation === departmentBindingGeneration && scope === sessionScope()
  const requestedSelections = [selection]
  const explicitSwitch = changingDepartment.value && !isGeneral.value
  const saveSource = explicitSwitch ? !formReadOnly.value && formDirty.value : inputDirty.value
  saveCoordinator.cancelScheduled()
  relatedApprovalVerificationError.value = ''
  verifyingRelatedApprovals.value = true
  if (!explicitSwitch) selectedRelatedApprovals.value = requestedSelections
  bindingApprovalDepartment.value = true
  departmentBindingError.value = ''
  retryDepartmentApproval.value = null
  try {
    const preserveInput = auth.status === 'authenticated' && drafts.currentDraft && saveSource
    if (preserveInput) {
      await flushAutosave({ includeRelated: explicitSwitch })
      if (!acceptsBinding()) return
    }
    const previousScope = sessionScope()
    const resolution = await auth.selectDepartmentFromTravelApproval(
      selection,
      selectedDepartmentId,
      Boolean(drafts.currentDraft && !newEntryOpen.value),
    )
    if (disposed || generation !== departmentBindingGeneration || !resolution.isCurrent()) return
    // Selecting the approval can legitimately change the department. Adopt only
    // the session change performed by this still-current authenticated request.
    scope = sessionScope()
    bindingApprovalDepartment.value = true
    verifyingRelatedApprovals.value = true
    if (resolution.selectionRequired || !resolution.selectedDepartment) {
      pendingDepartmentApproval.value = selection
      selectedDepartmentChoiceId.value = ''
      return
    }
    recheckApprovalDepartment.value = false
    pendingDepartmentApproval.value = null
    selectedDepartmentChoiceId.value = ''
    if (explicitSwitch) {
      changingDepartment.value = false
        mobileStep.value = 0
      if (scope === previousScope) return
    }
    if (!drafts.currentDraft || sessionScope() !== previousScope) {
      initializedScope = ''
      await initializeWorkspace(true, resolution.reservedDraftId)
      if (!acceptsBinding()) return
    }
    const opened = drafts.currentDraft
    if (!opened) throw new Error(initializationError.value || '报销表单加载失败，请重试')
    if (newEntryOpen.value) {
      acceptAuthoritativeRelatedApprovals()
      bindingApprovalDepartment.value = false
      verifyingRelatedApprovals.value = false
      await finishNewEntry(selection)
      return
    }
    const alreadyLinked = opened.relatedApprovals.some(
      (item) => item.processInstanceId === selection.processInstanceId,
    )
    if (alreadyLinked) {
      acceptAuthoritativeRelatedApprovals()
      drafts.mutationError = ''
      saveError.value = ''
      return
    }
    if (!['DRAFT', 'REVIEW_READY'].includes(opened.status) || opened.recoveryPending
      || opened.workspaceRetired || draftExpired.value) {
      pendingNewApproval.value = selection
      return
    }
    if (opened.relatedApprovals.length) {
      acceptAuthoritativeRelatedApprovals()
      ElMessage.warning('已打开当前报销并保留原有关联，请在出差审批中核对后再调整。')
      return
    }
    await verifyRelatedApprovals(requestedSelections)
  } catch (error) {
    if (!acceptsBinding()) return
    if (!explicitSwitch) acceptAuthoritativeRelatedApprovals()
    verifyingRelatedApprovals.value = false
    const message = error instanceof Error
      ? error.message
      : '无法读取出差审批的所在部门，请重试'
    if (auth.status === 'authenticated' && !drafts.currentDraft) {
      initializationError.value = message
      if (newEntryOpen.value) retryDepartmentApproval.value = selection
    }
    else if (!saveError.value && !saveRequiresReload.value) {
      if (drafts.currentDraft) recheckApprovalDepartment.value = true
      departmentBindingError.value = message
    }
  } finally {
    if (acceptsBinding()) {
      bindingApprovalDepartment.value = false
      if (!pendingDepartmentApproval.value) verifyingRelatedApprovals.value = false
      scheduleAutosave()
    }
  }
}
function updateRelatedApprovals(
  selections: ReimbursementRelatedApprovalSelection[],
): void {
  if (canRecoverTravelDepartmentFromApproval.value) {
    const selection = selections.at(-1)
    if (selection) void resolveDepartmentForApproval(selection)
    return
  }
  const firstSelection = selections.length === 1 && selectedRelatedApprovals.value.length === 0
    ? selections[0]
    : undefined
  if (!firstSelection || (selectableDepartments.value.length <= 1 && !draftDepartmentInvalid.value)) {
    void persistRelatedApprovalsImmediately(selections)
    return
  }
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === firstSelection.processInstanceId,
  )
  const currentDepartmentId = drafts.currentDraft?.department.id ?? auth.session?.selectedDepartment?.id
  if (!recheckApprovalDepartment.value && candidate?.originatorDepartmentId === currentDepartmentId) {
    void persistRelatedApprovalsImmediately(selections)
    return
  }
  void resolveDepartmentForApproval(firstSelection)
}
function confirmDepartmentChoice(): void {
  const pending = pendingDepartmentApproval.value
  if (!pending || !selectedDepartmentChoiceId.value) return
  void resolveDepartmentForApproval(pending, selectedDepartmentChoiceId.value)
}
function cancelDepartmentChoice(): void {
  pendingDepartmentApproval.value = null
  retryDepartmentApproval.value = null
  selectedDepartmentChoiceId.value = ''
  acceptAuthoritativeRelatedApprovals()
  verifyingRelatedApprovals.value = false
  relatedApprovalVerificationError.value = ''
  scheduleAutosave()
}
function selectMobileStep(step: number): void {
  if (!props.mobile || step < 0 || step >= mobileSteps.value.length) return
  mobileStep.value = step
}
async function advanceMobileStep(): Promise<void> {
  if (!isGeneral.value && mobileStep.value === 0 && !selectedRelatedApprovals.value.length) {
    ElMessage.warning('请至少关联一张已通过的出差审批')
    return
  }
  if (mobileStep.value < mobileSteps.value.length - 1) {
    selectMobileStep(mobileStep.value + 1)
    return
  }
  await confirmAndSubmit()
}
async function handleMobilePrimaryAction(): Promise<void> {
  if (mobileStep.value === mobileSteps.value.length - 1 && canReplaceOutdatedForm.value) {
    await replaceOutdatedForm()
    return
  }
  if (formReadOnly.value) {
    scrollToWorkspaceStart()
    return
  }
  await advanceMobileStep()
}
async function focusMobileMaterial(itemId?: string, fileId?: string): Promise<void> {
  if (props.mobile) {
    mobileStep.value = materialStep.value
    await nextTick()
  }
  expenseItemsCard.value?.focusMaterial(itemId, fileId)
}
async function retrySameSubmission(): Promise<void> {
  const draft = drafts.currentDraft
  if (!draft) return
  try { await submission.submit(draft.id, draft.revision) } catch { /* Render the status error. */ }
}
async function retrySubmissionRecovery(): Promise<void> {
  const draft = drafts.currentDraft
  if (!draft) return
  try {
    await submission.restore(draft.id, {
      discoverByDraft: true,
      expectedRevision: draft.revision,
    })
  } catch { /* Render the recovery error in the submission card. */ }
}
async function refreshSubmissionService(refreshProgress = false): Promise<void> {
  if (refreshingServiceStatus.value) return
  const scope = sessionScope()
  refreshingServiceStatus.value = true
  try {
    const results = await Promise.allSettled([
      auth.refreshPublicConfig(),
      ...(refreshProgress ? [submission.pollNow()] : [health.check()]),
    ])
    const failed = results.find((result) => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
  } catch (error) {
    if (scope === sessionScope()) ElMessage.error(error instanceof Error ? error.message : '服务状态刷新失败，请重试')
  } finally { if (scope === sessionScope()) refreshingServiceStatus.value = false }
}
onMounted(async () => { void health.check(); await auth.bootstrap(); await initializeWorkspace() })
// Increment synchronously even for A -> B -> A within one tick. The asynchronous
// view callbacks use this opaque scope, independently of store request guards.
watch(sessionIdentity, () => { sessionGeneration.value++ }, { flush: 'sync' })
watch(reimbursementSubject, () => {
  newEntryOpen.value = false
  generalDepartmentConfirmed.value = false
  changingDepartment.value = false
  legacyDrafts.value = []
  generalDepartmentUnconfirmed.value = false
})
watch(() => drafts.currentDraft?.id, () => { generalApprovalOpen.value = false })
watch([sessionScope, reimbursementSubject], ([currentScope, subject], [, previousSubject]) => {
  const resolvingTravelDepartment = subject === previousSubject
    && bindingApprovalDepartment.value
  saveCoordinator.reset()
  saving.value = false
  verifyingRelatedApprovals.value = false
  bindingApprovalDepartment.value = false
  pendingDepartmentApproval.value = null
  retryDepartmentApproval.value = null
  recheckApprovalDepartment.value = false
  selectedDepartmentChoiceId.value = ''
  departmentBindingError.value = ''
  submitFlowPending.value = false
  refreshingMaterialStatus.value = false
  refreshingServiceStatus.value = false
  recoveryGeneration++
  workspaceReader.invalidate()
  confirmingDraftReload.value = false
  replacingTemplate.value = false
  recoveringReimbursement.value = false
  workspaceConfirmationRequired.value = false
  pendingNewApproval.value = null
  historyVisible.value = false
  historyOrigin.value = null
  historicalSubmission.value = null
  workspaceActionError.value = ''
  deferredMaterialRevision.value = null
  if (currentScope && !resolvingTravelDepartment) void initializeWorkspace()
  else { initializedScope = ''; initializationScope = '' }
})
watch(historyScope, () => {
  historyVisible.value = false
  historyOrigin.value = null
  historicalSubmission.value = null
})
watch(() => expenseItemsCard.value?.hasOpenMaterialEditor, (open) => {
  if (open === false && deferredMaterialRevision.value === drafts.currentDraft?.id) {
    void reconcileMaterialRevision()
  }
})
watch([currentFormInput, selectedRelatedApprovals], scheduleAutosave, { deep: true })
watch(formReadOnly, (readOnly) => {
  if (readOnly) saveCoordinator.cancelScheduled()
  else if (!saveError.value) scheduleAutosave()
})
watch(workspaceActionError, (message) => { if (message) void revealAttention('workspace') }, { flush: 'post' })
watch(() => submittedResult.value?.draftId, (draftId) => {
  if (draftId) void loadSubmittedReceipt()
})
watch(canOpenHistory, (canOpen) => {
  if (!canOpen) historyVisible.value = false
})
watch(historyVisible, (visible) => {
  if (visible) return
  historyRequest?.abort()
  historyRequest = undefined
  historyDetailRequest?.abort()
  historyDetailRequest = undefined
  historyOpeningId.value = ''
  historyLoading.value = false
  historyRecords.value = []
}, { flush: 'sync' })
watch(() => drafts.revisionConflict, (conflict) => {
  if (conflict && drafts.currentDraft && !trackedSubmission.value) saveRequiresReload.value = true
}, { flush: 'sync' })
watch(() => drafts.busy, () => {
  // A file operation becoming idle should resume saving. Our own failed save
  // must keep the existing explicit-retry behavior, not retry every 600 ms.
  if (!saving.value && !saveError.value) scheduleAutosave()
})
watch(budgetLabel, (label) => { expense.manualProjectText = label })
watch(() => drafts.reimbursementOptions?.attendanceCalendar, (calendar) => {
  expense.attendanceCalendar = calendar
}, { immediate: true, flush: 'sync' })
const deferSubsidySync = computed(() => verifyingRelatedApprovals.value || bindingApprovalDepartment.value
  || relatedDirty.value || saveRequiresReload.value
  || (drafts.currentDraft && !['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status)))
watch([selectedSubsidyApprovals, deferSubsidySync], ([approvals, deferred]) => {
  if (deferred) return
  expense.syncSubsidyApprovals(approvals)
}, {
  deep: true,
  immediate: true,
})
watch([selectedSubsidyTripType, deferSubsidySync], ([value, deferred]) => {
  if (deferred) return
  if (value && expense.trip.tripType !== value) expense.setTripType(value)
}, { immediate: true })
watch(() => [
  sessionScope(),
  expense.includeSubsidy,
  expense.attendanceCalendar,
  expense.trip,
  expense.subsidyTrips,
  expense.items,
  drafts.processingFiles,
  formReadOnly.value,
], () => {
  if (calculationTimer) clearTimeout(calculationTimer)
  const scope = sessionScope()
  if (!scope || disposed || drafts.processingFiles || formReadOnly.value) return
  calculationTimer = setTimeout(() => {
    if (!disposed && sessionScope() === scope && !drafts.processingFiles && !formReadOnly.value) void expense.refreshCalculations()
  }, 250)
}, { deep: true })
function warnBeforeUnload(event: BeforeUnloadEvent): void {
  if (drafts.currentDraft && (hasUnsavedUserChanges.value || localUploadFailureReason.value)) {
    event.preventDefault()
    event.returnValue = ''
  }
}
let lastBackgroundOptionsRefreshAt = 0
function refreshOptionsOnForeground(): void {
  if (document.visibilityState !== 'visible' || !sessionScope() || disposed
    || initializingWorkspace.value || switchingReimbursement.value || drafts.busy
    || saving.value || submitFlowPending.value || trackedSubmission.value
    || drafts.currentDraft?.status === 'LOCKED'
    || Date.now() - lastBackgroundOptionsRefreshAt < 30_000) return
  lastBackgroundOptionsRefreshAt = Date.now()
  void drafts.loadReimbursementOptions({ background: true })
}
function updateMobileFooterHeight(): void {
  const height = Math.ceil(mobileFooterDock.value?.getBoundingClientRect().height ?? 0)
  if (height > 0) mobileFooterHeight.value = height
}
watch(mobileFooterDock, (footer) => {
  mobileFooterResizeObserver?.disconnect()
  mobileFooterResizeObserver = undefined
  mobileFooterHeight.value = 0
  if (!props.mobile || !footer) return
  updateMobileFooterHeight()
  if (typeof ResizeObserver === 'undefined') return
  mobileFooterResizeObserver = new ResizeObserver(updateMobileFooterHeight)
  mobileFooterResizeObserver.observe(footer)
}, { flush: 'post' })
onMounted(() => {
  window.addEventListener('beforeunload', warnBeforeUnload)
  window.addEventListener('focus', refreshOptionsOnForeground)
  document.addEventListener('visibilitychange', refreshOptionsOnForeground)
})
onBeforeUnmount(() => {
  if (recoveryRefreshTimer) clearTimeout(recoveryRefreshTimer)
  disposed = true
  workspaceReader.invalidate()
  reimbursementSwitchRequest?.abort()
  historyRequest?.abort()
  historyDetailRequest?.abort()
  if (calculationTimer) clearTimeout(calculationTimer)
  mobileFooterResizeObserver?.disconnect()
  window.removeEventListener('beforeunload', warnBeforeUnload)
  window.removeEventListener('focus', refreshOptionsOnForeground)
  document.removeEventListener('visibilitychange', refreshOptionsOnForeground)
  submission.abort()
})
</script>

<template>
  <main
    class="page-shell reimbursement-page"
    :class="{ 'page-shell--mobile': props.mobile, 'page-shell--submitted': submittedResult }"
    :style="mobileShellStyle"
  >
    <section
      class="hero"
      :class="{ 'hero--mobile': props.mobile }"
      aria-labelledby="page-title"
      data-testid="reimbursement-workspace-start"
    >
      <div class="hero-intro">
        <div class="hero-meta">
          <p class="eyebrow">
            <span>钉钉工作台应用</span>
            <span
              v-if="auth.session?.user.userId && !props.mobile"
              class="current-user-id"
              :title="`当前 userId：${auth.session.user.userId}`"
            >
              userId：{{ auth.session.user.userId }}
            </span>
          </p>
          <div class="hero-links">
            <RouterLink
              v-if="auth.isAdmin"
              :to="props.mobile ? '/m/settings' : '/admin/settings'"
              class="hero-link"
            >
              系统设置
            </RouterLink>
            <el-button
              v-if="historyOrigin"
              link
              type="primary"
              class="hero-link hero-return-link"
              :icon="ArrowLeft"
              :loading="initializingWorkspace"
              :disabled="submission.submitting || refreshingServiceStatus || switchingReimbursement"
              @click="returnFromHistory"
            >
              返回当前报销
            </el-button>
            <button
              v-else-if="hasReimbursementLogin"
              type="button"
              class="hero-link"
              aria-haspopup="dialog"
              :disabled="initializingWorkspace || !canOpenHistory || drafts.busy || saving || confirmingDraftReload"
              @click="openHistory"
            >
              报销记录
            </button>
            <button
              v-if="showHeaderNewReimbursement"
              type="button"
              class="hero-link"
              :disabled="changingDepartment || generalDepartmentUnconfirmed
                || (needsReimbursementDepartment ? bindingApprovalDepartment || switchingKind
                  : !canStartNewReimbursement || drafts.busy || saving || reimbursementOptionsUnavailable)"
              :title="needsReimbursementDepartment || hasRestartableReimbursement ? undefined : '当前已是空白报销，可直接填写'"
              @click="newReimbursement"
            >
              新开一笔
            </button>
          </div>
        </div>
        <nav
          v-if="hasReimbursementLogin && !historyOrigin"
          class="reimbursement-kind-selector"
          aria-label="报销类型"
        >
          <el-button-group class="reimbursement-kind-buttons">
            <el-button
              :type="!isGeneral ? 'primary' : 'default'"
              :aria-pressed="!isGeneral"
              :disabled="switchingKind || Boolean(reimbursementSwitchBlock || reimbursementSwitchRecheck)"
              data-testid="switch-travel"
              @click="switchKind('travel')"
            >
              差旅报销
            </el-button>
            <el-button
              :type="isGeneral ? 'primary' : 'default'"
              :aria-pressed="isGeneral"
              :disabled="switchingKind || Boolean(auth.session?.operator || reimbursementSwitchBlock || reimbursementSwitchRecheck)"
              data-testid="switch-general"
              @click="switchKind('general')"
            >
              普通费用报销
            </el-button>
          </el-button-group>
        </nav>
        <div class="hero-title">
          <h1 id="page-title">
            {{ isGeneral ? '普通费用报销申请' : auth.appTitle }}
          </h1>
          <span
            v-if="auth.status === 'authenticated' && !needsReimbursementDepartment && !initializingWorkspace && !formReadOnly && !saveError"
            class="save-indicator"
            :data-state="saveState"
            data-testid="autosave-status"
            role="status"
          >
            {{ saveLabels[saveState] }}
          </span>
        </div>
        <p
          v-if="!submittedResult"
          class="summary"
        >
          {{ isGeneral ? '选择报销主体与预算代码，整理费用和材料，再提交 OA。' : props.mobile
            ? '关联审批、核对补助、整理材料，最后统一提交 OA。'
            : '关联审批、核对补助和费用，自动生成报销单并提交 OA。' }}
        </p>
        <div
          v-if="!isGeneral && (auth.session?.canDelegateReimbursements || auth.session?.operator)
            && hasReimbursementLogin && !historyOrigin"
          class="reimbursement-subject"
          aria-label="为谁报销"
        >
          <strong data-testid="reimbursement-subject-name">{{ auth.session?.operator
            ? `正在为${auth.session.user.name}报销` : '为本人报销' }}</strong>
          <el-button
            v-if="auth.session?.canDelegateReimbursements"
            type="primary"
            link
            :disabled="Boolean(reimbursementSwitchBlock || reimbursementSwitchRecheck) || delegationRevoked"
            data-testid="select-reimbursement-employee"
            @click="openReimbursementEmployeePicker"
          >
            {{ auth.session.operator ? '更换报销人' : '帮同事报销' }}
          </el-button>
          <el-button
            v-if="auth.session?.operator"
            link
            :disabled="Boolean(reimbursementSwitchBlock)"
            data-testid="return-to-personal-reimbursement"
            @click="switchReimbursementEmployee(null)"
          >
            返回本人报销
          </el-button>
          <small
            v-if="reimbursementSwitchBlock || (!delegationRevoked && reimbursementSwitchRecheck)"
            class="reimbursement-switch-note"
            role="status"
          >{{ switchingReimbursement ? '正在切换报销人…' : reimbursementSwitchBlock || reimbursementSwitchRecheck }}</small>
        </div>
        <p
          v-if="auth.reimbursementContextNotice || (!employeePickerOpen && reimbursementSwitchError)"
          class="reimbursement-switch-note"
          role="alert"
        >
          {{ reimbursementSwitchError || auth.reimbursementContextNotice }}
        </p>
      </div>
      <div
        v-if="health.available !== true"
        class="hero-actions"
      >
        <el-tag
          :type="health.available === false ? 'danger' : 'info'"
          round
        >
          {{ health.label }}
        </el-tag>
        <el-button
          v-if="health.available === false"
          link
          type="primary"
          :loading="health.loading"
          @click="health.check()"
        >
          重新检查服务
        </el-button>
      </div>
    </section>
    <el-card
      v-if="auth.status === 'loading' || auth.status === 'idle'"
      shadow="never"
      class="content-card"
    >
      <el-skeleton
        :rows="4"
        animated
      />
    </el-card>
    <el-card
      v-else-if="auth.status === 'mock_required'"
      shadow="never"
      class="content-card"
    >
      <el-result
        icon="info"
        title="开发免登已启用"
        sub-title="此入口只在开发或测试环境出现。"
      >
        <template #extra>
          <el-button
            type="primary"
            @click="auth.useDevelopmentMock"
          >
            使用固定测试身份
          </el-button>
        </template>
      </el-result>
    </el-card>
    <el-card
      v-else-if="legacyDrafts.length"
      data-testid="legacy-drafts"
      class="content-card"
    >
      <p>发现同一种报销有多笔跨部门旧稿。请先核对并选择保留哪一笔，选择前不会删除内容。</p>
      <p
        v-if="initializationError"
        role="alert"
      >
        {{ initializationError }}
      </p>
      <div
        v-for="draft in legacyDrafts"
        :key="draft.id"
        class="legacy-draft-choice"
      >
        <p>{{ draft.department?.name }} · {{ draft.status === 'LOCKED' ? '已进入提交处理' : draft.status === 'EXPIRED' ? '已过期' : '未提交' }} · {{ formatRecordTime(draft.updatedAt) }}</p>
        <p>{{ draft.itemCount ?? 0 }} 笔费用 · {{ draft.fileCount ?? 0 }} 份材料 · {{ draft.relatedApprovalCount }} 张关联审批</p>
        <p v-if="draft.itemDescriptions?.length">
          {{ draft.itemDescriptions.join('、') }}
        </p>
        <el-button
          :disabled="choosingLegacyDraft"
          @click="openSavedWorkspace(draft, legacyDrafts, true)"
        >
          保留这笔
        </el-button>
      </div>
      <el-button
        :disabled="choosingLegacyDraft"
        @click="initializeWorkspace(true)"
      >
        重新加载核对
      </el-button>
    </el-card>

    <template v-else-if="hasReimbursementLogin || historyOrigin">
      <el-card
        v-if="needsReimbursementDepartment && !historyOrigin"
        data-testid="reimbursement-department-gate"
        tabindex="-1"
        shadow="never"
        class="content-card"
      >
        <template
          v-if="isGeneral || changingDepartment"
          #header
        >
          <div class="department-gate-heading">
            <strong>{{ changingDepartment ? '更换报销部门' : '选择本次报销部门' }}</strong>
            <el-button
              v-if="changingDepartment"
              link
              type="primary"
              :disabled="bindingApprovalDepartment || changingDepartmentUnconfirmed"
              @click="closeDepartmentPicker"
            >
              取消
            </el-button>
            <el-button
              v-else-if="newEntryOpen && auth.status === 'authenticated'"
              link
              type="primary"
              :disabled="bindingApprovalDepartment || Boolean(pendingDepartmentApproval)"
              @click="newEntryOpen = false"
            >
              返回当前报销
            </el-button>
          </div>
        </template>
        <p
          v-if="changingDepartment"
          class="field-help department-switch-help"
        >
          {{ isGeneral ? '确认后更正本笔报销的办理部门，已填明细和已上传材料会保留。'
            : '所选出差审批无法对应当前有效部门，请选择本笔报销的办理部门。已选审批、费用明细和材料会保留。' }}
        </p>
        <el-form
          v-if="isGeneral || changingDepartment"
          label-position="top"
          @submit.prevent="confirmGeneralDepartment"
        >
          <el-form-item label="所属部门">
            <el-select
              v-model="generalDepartment"
              aria-label="所属部门"
              placeholder="请选择部门"
              :disabled="bindingApprovalDepartment"
              class="full-width"
            >
              <el-option
                v-for="department in selectableDepartments"
                :key="department.id"
                :value="department.id"
                :label="department.name"
              />
            </el-select>
          </el-form-item>
          <el-button
            type="primary"
            :loading="bindingApprovalDepartment"
            :disabled="generalDepartmentSelectionDisabled"
            @click="confirmGeneralDepartment"
          >
            {{ changingDepartment ? '确认更换' : '确认部门' }}
          </el-button>
        </el-form>
        <template v-else>
          <TravelApprovalSelector
            ref="departmentApprovalSelector"
            :key="reimbursementSubject"
            :model-value="newEntryOpen && !bindingApprovalDepartment ? [] : selectedRelatedApprovals"
            :mobile="props.mobile"
            :busy="bindingApprovalDepartment || switchingReimbursement"
            :delegated="Boolean(auth.session?.operator)"
            single
            class="department-gate-approvals"
            @update:model-value="chooseTravelApprovalForDepartment"
          />
        </template>
        <el-alert
          v-if="departmentGateError"
          :title="departmentGateError"
          type="error"
          :closable="false"
          show-icon
          class="workspace-alert"
        >
          <el-button
            v-if="retryDepartmentApproval"
            link
            type="primary"
            :disabled="bindingApprovalDepartment"
            @click="resolveDepartmentForApproval(retryDepartmentApproval, auth.session?.selectedDepartment?.id)"
          >
            重新加载报销
          </el-button>
        </el-alert>
      </el-card>
      <div
        v-show="!needsReimbursementDepartment || Boolean(historyOrigin)"
        class="workspace-content"
      >
        <el-card
          v-if="initializingWorkspace"
          shadow="never"
          class="content-card"
          aria-busy="true"
        >
          <el-skeleton
            :rows="6"
            animated
          />
        </el-card>
        <template v-else>
          <el-alert
            v-if="initializationError || departmentBindingError || draftDepartmentInvalid"
            :title="initializationError || departmentBindingError || (isGeneral || travelDepartmentNeedsChoice ? '原办理部门已变更，可通过更换部门更新本笔报销，当前内容和材料会保留' : '原办理部门已变更，可按出差审批更新部门，当前内容和材料会保留')"
            :type="initializationError || departmentBindingError ? 'error' : 'info'"
            :closable="false"
            class="workspace-alert"
          >
            <template #default>
              <el-button
                v-if="canRecoverTravelDepartmentFromApproval"
                link
                type="primary"
                @click="returnToApprovalStep()"
              >
                选择出差审批
              </el-button>
              <el-button
                v-if="draftDepartmentInvalid && !isGeneral && !travelDepartmentNeedsChoice && drafts.currentDraft?.relatedApprovals.length"
                link
                type="primary"
                :loading="bindingApprovalDepartment"
                @click="correctCurrentDepartment()"
              >
                按出差审批重新确认部门
              </el-button>
              <el-button
                v-if="initializationError"
                link
                type="primary"
                @click="initializeWorkspace(true)"
              >
                重新加载
              </el-button>
            </template>
          </el-alert>
          <el-alert
            v-if="workspaceActionError"
            data-attention-target="workspace"
            :title="workspaceActionError"
            type="error"
            :closable="false"
            class="workspace-alert"
          >
            <el-button
              v-if="workspaceConfirmationRequired"
              type="primary"
              @click="confirmCurrentWorkspace"
            >
              核对当前报销
            </el-button>
          </el-alert>
          <el-alert
            v-if="!drafts.currentDraft && !initializationError"
            title="当前报销记录已不可用"
            type="warning"
            :closable="false"
            class="workspace-alert"
          >
            <el-button
              type="primary"
              @click="initializeWorkspace(true)"
            >
              重新加载报销
            </el-button>
          </el-alert>
          <el-alert
            v-if="drafts.currentDraft && reimbursementOptionsUnavailable && !historyOrigin"
            data-attention-target="options"
            :title="templateNeedsAdmin ? (auth.isAdmin ? '请前往系统设置确认 OA 表单' : '请联系管理员确认 OA 表单') : '暂时无法读取报销表单'"
            :description="drafts.reimbursementOptionsError"
            type="warning"
            :closable="false"
            class="workspace-alert"
          >
            <div class="form-options-actions">
              <el-button
                v-if="templateNeedsAdmin && auth.isAdmin"
                type="primary"
                @click="openTemplateSettings"
              >
                前往系统设置确认表单
              </el-button>
              <el-button
                :type="templateNeedsAdmin && auth.isAdmin ? 'default' : 'primary'"
                :loading="drafts.loadingReimbursementOptions"
                @click="refreshFormOptions"
              >
                {{ templateNeedsAdmin ? '重新检查表单' : '刷新表单与审批' }}
              </el-button>
            </div>
          </el-alert>
          <section
            v-if="accountingOptionsChanged || needsApprovalReconfirmation"
            class="accounting-verification-alert"
            data-testid="accounting-options-recovery"
          >
            <p>{{ accountingOptionsChanged ? '所属公司或预算选项已变化，请刷新并重新关联出差审批。' : '此报销需重新核验关联审批的所属公司和预算代码。' }}</p>
            <el-button
              v-if="accountingOptionsChanged"
              :disabled="drafts.busy || saving"
              @click="recoverAccountingOptions"
            >
              刷新表单与审批
            </el-button>
            <el-button
              v-else
              :loading="verifyingRelatedApprovals"
              :disabled="drafts.busy || saving"
              @click="reconfirmRelatedApprovals"
            >
              重新确认出差审批
            </el-button>
          </section>
          <template v-if="drafts.currentDraft">
            <el-alert
              v-if="draftUnavailable"
              title="当前报销记录已不可用"
              description="本页内容仅供核对，请从右上角新开一笔，重新填写并上传材料。"
              type="warning"
              :closable="false"
              class="workspace-alert"
              data-attention-target="workspace"
            />
            <el-alert
              v-if="drafts.currentDraft.workspaceRetired && !historyOrigin"
              title="已新开另一笔报销"
              type="info"
              :closable="false"
              class="workspace-alert"
              data-attention-target="workspace"
            >
              <el-button
                :disabled="drafts.busy || saving || confirmingDraftReload"
                @click="openCurrentWorkspace"
              >
                打开当前报销
              </el-button>
            </el-alert>
            <el-alert
              v-if="activeMaterialRecovery"
              title="报销内容已保留，材料尚在恢复"
              type="info"
              :closable="false"
              class="workspace-alert"
            >
              <p>请等待原材料恢复完成后再编辑；中断后可以继续恢复。</p>
              <div class="submission-feedback-actions">
                <el-button
                  type="primary"
                  :loading="recoveringReimbursement"
                  :disabled="workspaceConfirmationRequired || drafts.busy"
                  @click="recoverFailedReimbursement"
                >
                  继续恢复材料
                </el-button>
              </div>
            </el-alert>
            <el-alert
              v-if="draftExpired && !trackedSubmission"
              title="本次填写内容已过期"
              description="可以开始新的报销，请重新填写并上传材料。"
              type="warning"
              :closable="false"
              class="workspace-alert"
            >
              <el-button
                type="primary"
                :disabled="!canStartNewReimbursement || drafts.busy || reimbursementOptionsUnavailable"
                @click="newReimbursement"
              >
                重新填写
              </el-button>
            </el-alert>
            <section
              v-if="canReplaceOutdatedForm"
              class="template-recovery-panel"
              data-testid="template-recovery-panel"
              role="alert"
            >
              <div>
                <strong>OA 表单已更新</strong>
                <p>当前记录不能继续保存或提交。按新表单重新填写后，需要重新关联审批并上传材料。</p>
              </div>
              <el-button
                type="primary"
                :loading="replacingTemplate"
                :disabled="drafts.busy"
                @click="replaceOutdatedForm"
              >
                按新表单重新填写
              </el-button>
            </section>
            <el-alert
              v-if="!submittedResult && submissionServiceReason"
              :title="submissionServiceReason"
              type="warning"
              :closable="false"
              class="submission-status"
              show-icon
            >
              <el-button
                link
                type="primary"
                :loading="refreshingServiceStatus"
                @click="refreshSubmissionService()"
              >
                刷新服务状态
              </el-button>
            </el-alert>
            <el-alert
              v-if="delegationRevoked || ((saveError || saveRequiresReload) && !drafts.currentDraft.workspaceRetired)"
              :title="delegationRevoked ? '代报权限已失效，请返回本人报销'
                : saveRequiresReload ? '报销内容已更新，请核对最新报销内容' : '保存失败，内容仍保留在本页'"
              type="error"
              :closable="false"
              show-icon
              class="workspace-alert"
              data-testid="autosave-error"
              data-attention-target="save"
            >
              <div
                v-if="saveError"
                class="save-recovery-detail"
              >
                {{ saveError }}
              </div>
              <el-button
                v-if="!delegationRevoked && (saveRequiresReload || (saveError && formReadOnly && !trackedSubmission))"
                link
                type="primary"
                :disabled="drafts.busy || saving"
                @click="openSavedReimbursement(drafts.currentDraft.id)"
              >
                核对最新报销内容
              </el-button>
              <el-button
                v-else-if="!formReadOnly"
                link
                type="primary"
                :disabled="drafts.busy || saving"
                @click="retrySave"
              >
                重试保存
              </el-button>
            </el-alert>
            <section
              v-if="submittedReimbursement"
              class="submitted-receipt"
              aria-labelledby="submitted-receipt-title"
              data-testid="submission-status"
              data-attention-target="submission"
            >
              <header class="receipt-heading">
                <div class="receipt-heading-copy">
                  <svg
                    class="receipt-check"
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    fill="none"
                  >
                    <path
                      d="m6 12 4 4 8-8"
                      stroke="currentColor"
                      stroke-width="2"
                      stroke-linecap="round"
                      stroke-linejoin="round"
                    />
                  </svg>
                  <div>
                    <h2 id="submitted-receipt-title">
                      审批已成功发起
                    </h2>
                    <p>票据材料和后续审批进度可在钉钉查看。</p>
                  </div>
                </div>
                <div class="receipt-amount">
                  <span>报销合计</span>
                  <strong>¥{{ drafts.currentDraft.totals.totalAmount }}</strong>
                </div>
              </header>
              <dl
                class="receipt-metrics"
                :class="{ 'receipt-metrics--general': isGeneral }"
              >
                <div><dt>票据金额</dt><dd>¥{{ drafts.currentDraft.totals.expenseTotal }}</dd></div>
                <div v-if="!isGeneral">
                  <dt>出差补助</dt><dd>¥{{ drafts.currentDraft.totals.subsidyTotal }}</dd>
                </div>
                <div><dt>票据张数</dt><dd>{{ drafts.currentDraft.totals.receiptCount }} 张</dd></div>
              </dl>
              <div class="submitted-receipt-toolbar">
                <p
                  v-if="!props.mobile"
                  class="receipt-attachment-summary"
                >
                  OA 附件：{{ attachmentNames.join('、') }}
                </p>
                <div class="submitted-receipt-actions">
                  <el-button
                    v-if="!props.mobile && submittedReimbursement.approvalUrl?.trim()"
                    tag="a"
                    :href="submittedReimbursement.approvalUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="approval-link"
                  >
                    打开钉钉 OA
                  </el-button>
                  <ExpenseSummaryCard
                    :mobile="props.mobile"
                    :preview-disabled-reason="historyOrigin ? '' : previewDisabledReason"
                    :history="Boolean(historyOrigin)"
                    readonly
                    preview-only
                  />
                </div>
              </div>
              <p
                v-if="pendingNewApproval"
                class="receipt-next-note"
              >
                已选出差审批。打开报销表单后可继续处理该审批。
              </p>
              <dl class="receipt-info">
                <div v-if="submittedReimbursement.businessId">
                  <dt>审批编号</dt><dd>{{ submittedReimbursement.businessId }}</dd>
                </div>
                <div v-if="submittedReimbursement.submittedAt">
                  <dt>提交时间</dt><dd>{{ formatRecordTime(submittedReimbursement.submittedAt) }}</dd>
                </div>
                <div><dt>报销部门</dt><dd>{{ drafts.currentDraft.department.name }}</dd></div>
                <div v-if="receiptDetails?.employeeName">
                  <dt>报销人</dt><dd>{{ receiptDetails.employeeName }}</dd>
                </div>
                <div v-if="receiptDetails?.companyLabel">
                  <dt>{{ isGeneral ? '报销主体' : '所属公司' }}</dt><dd>{{ receiptDetails.companyLabel }}</dd>
                </div>
                <div v-if="receiptDetails?.budgetLabel">
                  <dt>{{ isGeneral ? '预算代码' : '预算代码 / 项目' }}</dt><dd>{{ receiptDetails.budgetLabel }}</dd>
                </div>
                <div v-if="receiptDetails?.travelTypeLabel">
                  <dt>出差类别</dt><dd>{{ receiptDetails.travelTypeLabel }}</dd>
                </div>
                <div v-if="isGeneral">
                  <dt>关联审批单</dt><dd>{{ generalApprovalIds.length ? `已关联 ${generalApprovalIds.length} 张` : '未关联' }}</dd>
                </div>
              </dl>
              <div
                v-if="drafts.currentDraft.relatedApprovals.length"
                class="receipt-approvals"
              >
                <h3>关联出差审批（{{ drafts.currentDraft.relatedApprovals.length }} 张）</h3>
                <ul>
                  <li
                    v-for="approval in drafts.currentDraft.relatedApprovals"
                    :key="approval.processInstanceId"
                  >
                    <strong>{{ approval.title }}</strong>
                    <p class="receipt-approval-meta">
                      <span
                        class="receipt-approval-dates"
                        aria-label="出差日期"
                      >{{ approval.startDate }} 至 {{ approval.endDate }}</span><span
                        v-if="receiptDetails?.travelTypeLabel"
                        aria-label="出差类别"
                      > · {{ receiptDetails.travelTypeLabel }}</span>
                    </p>
                    <p
                      v-if="approval.businessId"
                      class="receipt-approval-number"
                    >
                      审批编号：{{ approval.businessId }}
                    </p>
                    <ApprovalAccountingLine
                      :company="receiptDetails?.companyLabel"
                      :budget="receiptDetails?.budgetLabel"
                    />
                  </li>
                </ul>
              </div>
              <p
                v-if="props.mobile"
                class="receipt-attachment-summary"
              >
                OA 附件：{{ attachmentNames.join('、') }}
              </p>
            </section>
            <el-alert
              v-else-if="submittedResult"
              title="审批已成功发起"
              :type="receiptLoadError ? 'warning' : 'success'"
              :closable="false"
              show-icon
              data-testid="submitted-receipt-loading"
            >
              <p class="save-recovery-detail">
                {{ receiptLoadError || '正在读取本次提交的报销回执。' }}
              </p>
              <template v-if="receiptLoadError">
                <el-button
                  :loading="drafts.loadingCurrentDraft"
                  @click="loadSubmittedReceipt"
                >
                  重新读取回执
                </el-button>
              </template>
            </el-alert>
            <section
              v-else-if="submission.activeDraftId === drafts.currentDraft.id && submission.submission?.status === 'MANUAL_REVIEW'"
              class="submission-status manual-review-panel"
              aria-labelledby="manual-review-title"
              aria-live="polite"
              data-testid="submission-status"
              data-attention-target="submission"
            >
              <header class="manual-review-heading">
                <el-icon
                  class="manual-review-icon"
                  aria-hidden="true"
                >
                  <WarningFilled />
                </el-icon>
                <div>
                  <h2 id="manual-review-title">
                    提交结果待人工核对
                  </h2>
                  <p>请将提交编号发给管理员核对。核对后可在这里刷新进度。</p>
                </div>
              </header>
              <dl class="manual-review-details">
                <dt>提交编号</dt><dd>{{ submission.submission.submissionId }}</dd>
                <template v-if="submission.submission.error?.message">
                  <dt>状态说明</dt><dd>{{ submission.submission.error.message }}</dd>
                </template>
              </dl>
              <p
                v-if="submission.requestError"
                class="manual-review-error"
              >
                {{ submission.requestError }}
              </p>
              <div class="manual-review-actions submission-feedback-actions">
                <el-button
                  v-if="submission.submission.processInstanceId"
                  class="submission-recheck-action"
                  :loading="submission.submitting"
                  :disabled="refreshingServiceStatus"
                  @click="submission.recheck()"
                >
                  重新核对（不会重复提交）
                </el-button>
                <el-button
                  type="primary"
                  :loading="refreshingServiceStatus"
                  :disabled="submission.submitting"
                  @click="refreshSubmissionService(true)"
                >
                  刷新提交进度
                </el-button>
              </div>
            </section>
            <section
              v-else-if="submission.activeDraftId === drafts.currentDraft.id && (submission.progressLabel || submission.errorMessage)"
              class="submission-status submission-feedback-panel"
              :class="`submission-feedback-panel--${submissionAlertType}`"
              aria-labelledby="submission-feedback-title"
              aria-live="polite"
              data-testid="submission-status"
              data-attention-target="submission"
            >
              <header class="submission-feedback-heading">
                <el-icon
                  class="submission-feedback-icon"
                  aria-hidden="true"
                >
                  <CircleCloseFilled v-if="submissionAlertType === 'error'" />
                  <WarningFilled v-else-if="submissionAlertType === 'warning'" />
                  <InfoFilled v-else />
                </el-icon>
                <div>
                  <h2 id="submission-feedback-title">
                    {{ submission.progressLabel || '提交需要处理' }}
                  </h2>
                  <p v-if="submission.errorMessage">
                    {{ submission.errorMessage }}
                  </p>
                </div>
              </header>
              <el-progress
                v-if="submission.status && submissionProgress.includes(submission.status)"
                :percentage="submissionProgressPercentage"
              />
              <p v-if="submission.submission?.businessId">
                审批编号：{{ submission.submission.businessId }}
              </p>
              <p v-if="pendingNewApproval">
                已选出差审批。打开报销表单后可继续处理该审批。
              </p>
              <div class="submission-status-actions submission-feedback-actions">
                <el-button
                  v-if="submission.status === 'FAILED_FINAL' && !drafts.currentDraft.workspaceRetired"
                  type="primary"
                  :disabled="workspaceConfirmationRequired || drafts.busy || reimbursementOptionsUnavailable"
                  :loading="recoveringReimbursement"
                  @click="recoverFailedReimbursement"
                >
                  保留内容重新处理
                </el-button>
                <el-button
                  v-if="submission.submission && !submission.terminal"
                  type="primary"
                  :loading="refreshingServiceStatus"
                  :disabled="submission.submitting"
                  @click="refreshSubmissionService(true)"
                >
                  刷新提交进度
                </el-button>
                <el-button
                  v-if="submission.requestError && !submission.submission && submission.requestAction"
                  type="primary"
                  :loading="submission.submitting || submission.polling"
                  :disabled="submission.requestAction === 'retry' && submission.oaSubmissionEnabled !== true"
                  @click="submission.requestAction === 'retry' ? retrySameSubmission() : retrySubmissionRecovery()"
                >
                  {{ submission.requestAction === 'retry' ? '重试本次提交' : '重新查找提交记录' }}
                </el-button>
              </div>
            </section>
            <el-alert
              v-else-if="drafts.currentDraft.status === 'LOCKED'"
              title="这笔报销已锁定，暂未读取到提交记录"
              type="warning"
              :closable="false"
              class="workspace-alert"
            >
              <div class="submission-feedback-actions">
                <el-button
                  type="primary"
                  :loading="submission.polling"
                  @click="retrySubmissionRecovery"
                >
                  重新查找提交记录
                </el-button>
              </div>
            </el-alert>
            <template v-if="submittedReimbursement">
              <ExpenseItemsCard
                v-if="expense.items.length || drafts.files.length"
                :mobile="props.mobile"
                readonly
                submitted
                @refresh-draft="openSavedReimbursement(drafts.currentDraft.id)"
              />
              <TripSubsidyCard
                v-if="expense.includeSubsidy"
                :mobile="props.mobile"
                :approvals="selectedSubsidyApprovals"
                :approval-trip-type="submittedSubsidyTripType"
                readonly
              />
            </template>
            <nav
              v-if="props.mobile && !submittedResult"
              class="mobile-step-nav"
              aria-label="报销填写步骤"
            >
              <button
                v-for="(step, index) in mobileSteps"
                :key="step"
                type="button"
                :class="{ 'is-active': mobileStep === index, 'is-done': mobileStep > index }"
                :aria-current="mobileStep === index ? 'step' : undefined"
                :aria-controls="`reimbursement-step-${index}`"
                @click="selectMobileStep(index)"
              >
                <span>{{ index + 1 }}</span>
                <small>{{ step }}</small>
              </button>
            </nav>
            <section
              v-if="!submittedResult"
              v-show="!props.mobile || mobileStep === 0"
              id="reimbursement-step-0"
              class="mobile-step-panel"
              data-testid="mobile-approval-step"
            >
              <header
                v-if="props.mobile"
                class="mobile-step-heading"
              >
                <h2>{{ isGeneral ? '基本信息' : '关联出差审批' }}</h2>
                <p v-if="!isGeneral">
                  先选择本次报销对应的审批，报销范围和补助将自动生成。
                </p>
              </header>
              <el-card
                shadow="never"
                class="content-card reimbursement-card"
                data-testid="reimbursement-basics-card"
              >
                <template
                  v-if="!props.mobile || !isGeneral"
                  #header
                >
                  <div class="card-header">
                    <strong>基本信息</strong>
                  </div>
                </template>
                <el-descriptions
                  :column="props.mobile ? 1 : 2"
                  border
                  class="identity-grid"
                >
                  <el-descriptions-item label="姓名">
                    {{ auth.session?.user.name }}
                  </el-descriptions-item>
                  <el-descriptions-item label="部门">
                    <div
                      v-if="showDepartmentChange"
                      class="department-identity"
                    >
                      <span>{{ drafts.currentDraft?.department.name ?? auth.session?.selectedDepartment?.name }}</span>
                      <el-button
                        link
                        type="primary"
                        :disabled="Boolean(reimbursementOperationBlock || reimbursementSwitchRecheck)"
                        :title="reimbursementOperationBlock || reimbursementSwitchRecheck"
                        @click="openDepartmentPicker"
                      >
                        更换部门
                      </el-button>
                    </div>
                    <template v-else>
                      {{ drafts.currentDraft?.department.name ?? auth.session?.selectedDepartment?.name }}
                    </template>
                  </el-descriptions-item>
                </el-descriptions>

                <el-form
                  v-if="isGeneral"
                  :disabled="formReadOnly || drafts.processingFiles"
                  label-position="top"
                  class="general-accounting-form"
                  @submit.prevent
                >
                  <el-form-item
                    label="报销主体"
                    :error="generalCompanyError"
                    required
                  >
                    <el-select
                      v-model="companyValue"
                      aria-label="报销主体"
                      filterable
                      placeholder="请选择报销主体"
                      class="full-width"
                    >
                      <el-option
                        v-for="option in companyOptions"
                        :key="option.value"
                        :label="option.label"
                        :value="option.value"
                      />
                    </el-select>
                  </el-form-item>
                  <el-form-item
                    label="预算代码"
                    :error="generalBudgetError"
                    required
                  >
                    <el-select
                      v-model="budgetCodeValue"
                      aria-label="预算代码"
                      filterable
                      placeholder="请选择预算代码"
                      class="full-width"
                    >
                      <el-option
                        v-for="option in budgetOptions"
                        :key="option.value"
                        :label="option.label"
                        :value="option.value"
                      />
                    </el-select>
                  </el-form-item>
                </el-form>
                <GeneralApprovalPicker
                  v-if="isGeneral"
                  :key="drafts.currentDraft.id"
                  v-model:expanded="generalApprovalOpen"
                  :draft-id="drafts.currentDraft.id"
                  :model-value="generalApprovalIds"
                  :saved-approvals="drafts.currentDraft.input.generalApprovalSummaries"
                  :mobile="props.mobile"
                  :active="!props.mobile || mobileStep === 0"
                  :readonly="formReadOnly && !verifyingRelatedApprovals"
                  :busy="verifyingRelatedApprovals || drafts.processingFiles || drafts.busy"
                  :save-error="generalApprovalError"
                  @update:model-value="saveGeneralApprovalSelection"
                />
                <TravelApprovalSelector
                  v-else
                  :key="reimbursementSubject"
                  ref="approvalSelector"
                  :delegated="Boolean(auth.session?.operator)"
                  :model-value="selectedRelatedApprovals"
                  :mobile="props.mobile"
                  :linked-approvals="drafts.currentDraft.relatedApprovals"
                  :readonly="formReadOnly && !canRecoverTravelDepartmentFromApproval"
                  :busy="drafts.processingFiles || verifyingRelatedApprovals || bindingApprovalDepartment"
                  @update:model-value="updateRelatedApprovals"
                />
                <section
                  v-if="!isGeneral"
                  class="derived-accounting-section"
                  data-testid="derived-accounting-summary"
                  aria-labelledby="derived-accounting-heading"
                >
                  <div class="derived-accounting-heading">
                    <div>
                      <h2 id="derived-accounting-heading">
                        已自动带入
                      </h2>
                      <p>以所选出差审批为准，无需重复填写。</p>
                    </div>
                  </div>
                  <dl
                    v-if="props.mobile"
                    class="derived-accounting-list"
                    data-testid="derived-accounting-mobile-list"
                  >
                    <div class="derived-accounting-list__row">
                      <dt>所属公司</dt>
                      <dd>{{ displayedCompanyLabel || '选择出差审批后自动填入' }}</dd>
                    </div>
                    <div class="derived-accounting-list__row">
                      <dt>预算代码 / 项目</dt>
                      <dd>{{ displayedBudgetLabel || '选择出差审批后自动填入' }}</dd>
                    </div>
                    <div class="derived-accounting-list__row">
                      <dt>出差类别</dt>
                      <dd>{{ selectedTravelTypeLabel || '选择出差审批后自动填入' }}</dd>
                    </div>
                    <div class="derived-accounting-list__row">
                      <dt>出差日期</dt>
                      <dd>
                        <div
                          v-if="selectedTravelPeriods.length"
                          class="travel-periods travel-periods--mobile"
                          data-testid="travel-periods"
                        >
                          <span
                            v-if="selectedTravelPeriods.length > 1"
                            class="travel-periods__count"
                          >共 {{ selectedTravelPeriods.length }} 个时间段</span>
                          <span
                            v-for="period in selectedTravelPeriods"
                            :key="`${period.startDate}-${period.endDate}`"
                            class="travel-periods__item"
                            :aria-label="period.startDate === period.endDate
                              ? period.startDate
                              : `${period.startDate} 至 ${period.endDate}`"
                          >
                            <time :datetime="period.startDate">{{ period.startDate }}</time>
                            <template v-if="period.startDate !== period.endDate">
                              <span aria-hidden="true">至</span>
                              <time :datetime="period.endDate">{{ period.endDate }}</time>
                            </template>
                          </span>
                        </div>
                        <template v-else>
                          选择出差审批后自动填入
                        </template>
                      </dd>
                    </div>
                  </dl>
                  <el-descriptions
                    v-else
                    :column="2"
                    border
                    class="derived-accounting-grid"
                  >
                    <el-descriptions-item label="所属公司">
                      {{ displayedCompanyLabel || '选择出差审批后自动填入' }}
                    </el-descriptions-item>
                    <el-descriptions-item label="预算代码 / 项目">
                      {{ displayedBudgetLabel || '选择出差审批后自动填入' }}
                    </el-descriptions-item>
                    <el-descriptions-item label="出差类别">
                      {{ selectedTravelTypeLabel || '选择出差审批后自动填入' }}
                    </el-descriptions-item>
                    <el-descriptions-item label="出差日期">
                      <div
                        v-if="selectedTravelPeriods.length"
                        class="travel-periods"
                        data-testid="travel-periods"
                      >
                        <span
                          v-if="selectedTravelPeriods.length > 1"
                          class="travel-periods__count"
                        >共 {{ selectedTravelPeriods.length }} 个时间段</span>
                        <span
                          v-for="period in selectedTravelPeriods"
                          :key="`${period.startDate}-${period.endDate}`"
                          class="travel-periods__item"
                          :aria-label="period.startDate === period.endDate
                            ? period.startDate
                            : `${period.startDate} 至 ${period.endDate}`"
                        >
                          <time :datetime="period.startDate">{{ period.startDate }}</time>
                          <template v-if="period.startDate !== period.endDate">
                            <span aria-hidden="true">—</span>
                            <time :datetime="period.endDate">{{ period.endDate }}</time>
                          </template>
                        </span>
                      </div>
                      <template v-else>
                        选择出差审批后自动填入
                      </template>
                    </el-descriptions-item>
                  </el-descriptions>
                  <el-alert
                    v-if="relatedApprovalVerificationError"
                    :title="relatedApprovalVerificationError"
                    type="error"
                    show-icon
                    :closable="false"
                    class="related-approval-verification-error"
                  >
                    <el-button
                      :disabled="drafts.busy || formReadOnly"
                      @click="refreshFormOptions"
                    >
                      刷新表单与审批
                    </el-button>
                  </el-alert>
                  <p class="field-help">
                    所属公司和预算代码由关联审批自动填入，不可修改；预算代码完整名称会填入报销单 Excel 的项目栏。
                  </p>
                </section>
                <el-alert
                  v-if="formReadOnlyReason"
                  :title="formReadOnlyReason"
                  type="info"
                  :closable="false"
                />
              </el-card>
            </section>
            <fieldset
              v-if="!submittedResult"
              v-show="!props.mobile || mobileStep === materialStep || (!isGeneral && mobileStep === 1)"
              class="editor-fieldset"
              :disabled="formReadOnly || drafts.processingFiles"
            >
              <section
                v-if="!isGeneral"
                v-show="!props.mobile || mobileStep === 1"
                id="reimbursement-step-1"
                class="mobile-step-panel"
                data-testid="mobile-subsidy-step"
              >
                <header
                  v-if="props.mobile"
                  class="mobile-step-heading"
                >
                  <h2>范围与补助</h2>
                  <p>
                    所属公司、预算、类别和日期来自已关联审批，只需核对补助。
                  </p>
                </header>
                <TripSubsidyCard
                  :mobile="props.mobile"
                  :readonly="formReadOnly"
                  :approvals="selectedSubsidyApprovals"
                  :approval-trip-type="selectedSubsidyTripType"
                />
              </section>
              <section
                v-show="!props.mobile || mobileStep === materialStep"
                :id="`reimbursement-step-${materialStep}`"
                class="mobile-step-panel"
                data-testid="mobile-material-step"
              >
                <header
                  v-if="props.mobile"
                  class="mobile-step-heading"
                >
                  <h2>费用与材料</h2>
                  <p>
                    可从文件管理器或系统相册上传票据和证明材料。
                  </p>
                  <p v-if="!isGeneral">
                    仅申请出差补助时无需添加费用或上传材料。
                  </p>
                </header>
                <ExpenseItemsCard
                  ref="expenseItemsCard"
                  :mobile="props.mobile"
                  :readonly="formReadOnly"
                  :refreshing-material-status="refreshingMaterialStatus"
                  @refresh-draft="openSavedReimbursement(drafts.currentDraft.id)"
                  @refresh-material-status="refreshMaterialStatus"
                  @material-revision-changed="reconcileMaterialRevision"
                />
              </section>
            </fieldset>
            <section
              v-if="!submittedResult"
              v-show="!props.mobile || mobileStep === reviewStep"
              :id="`reimbursement-step-${reviewStep}`"
              class="mobile-step-panel"
              data-testid="mobile-review-step"
            >
              <header
                v-if="props.mobile"
                class="mobile-step-heading"
              >
                <h2>核对并提交</h2>
                <p>确认金额、票据张数、材料完整性和即将生成的 OA 附件。</p>
              </header>
              <ReimbursementReviewOverview
                :mobile="props.mobile"
                :general="isGeneral"
                :company="displayedCompanyLabel"
                :budget="displayedBudgetLabel"
                :department="drafts.currentDraft?.department.name ?? ''"
                :periods="selectedTravelPeriods"
                :subjects="usedSubjects.map(option => option.label)"
                :approval-count="isGeneral ? generalApprovalIds.length : selectedRelatedApprovals.length"
                @approvals="focusReviewApprovals"
              />
              <ExpenseSummaryCard
                :mobile="props.mobile"
                :readonly="formReadOnly"
                :preview-disabled-reason="previewDisabledReason"
                :before-preview="props.mobile || formReadOnly ? undefined : prepareExcelPreview"
              >
                <template #submission>
                  <div
                    v-if="!formReadOnly && (localUploadFailureReason || drafts.pendingUploads.length || missingMaterialItems.length || materialSubmissionIssues.length)"
                    class="material-checklist"
                    data-testid="material-checklist"
                    role="status"
                  >
                    <div
                      v-if="localUploadFailureReason"
                      class="material-checklist-row"
                      data-testid="local-upload-failure"
                    >
                      <span>{{ localUploadFailureReason }}</span>
                      <el-button
                        link
                        type="primary"
                        @click="focusMobileMaterial()"
                      >
                        去处理失败文件
                      </el-button>
                    </div>
                    <div
                      v-if="drafts.pendingUploads.length"
                      class="material-checklist-row"
                    >
                      <strong>还有 {{ drafts.pendingUploads.length }} 份材料尚未完成上传</strong>
                      <el-button
                        link
                        type="primary"
                        @click="focusMobileMaterial()"
                      >
                        去查看上传进度
                      </el-button>
                    </div>
                    <strong v-if="missingMaterialItems.length">还有 {{ missingMaterialItems.length }} 笔费用需补材料</strong>
                    <div
                      v-for="entry in missingMaterialItems"
                      :key="entry.item.id"
                      class="material-checklist-row"
                    >
                      <span>{{ entry.item.description || '费用明细' }} · ¥{{ entry.item.amount }} · 缺{{ entry.missing.join('、') }}</span>
                      <el-button
                        link
                        type="primary"
                        @click="focusMobileMaterial(entry.item.id)"
                      >
                        去补齐
                      </el-button>
                    </div>
                    <strong v-if="materialSubmissionIssues.length">
                      还有 {{ materialSubmissionIssues.length }} 份材料需处理后才能提交 OA
                    </strong>
                    <div
                      v-for="issue in materialSubmissionIssues"
                      :key="issue.file.id"
                      class="material-checklist-row"
                    >
                      <span>{{ issue.file.name }} · {{ issue.reason }}</span>
                      <el-button
                        link
                        type="primary"
                        @click="focusMobileMaterial(issue.itemId, issue.itemId ? undefined : issue.file.id)"
                      >
                        去处理
                      </el-button>
                    </div>
                    <p>请先处理上面列出的材料，再提交 OA。</p>
                  </div>
                  <div class="submission-actions">
                    <div
                      class="submission-attachments"
                      data-testid="submission-attachments"
                    >
                      <strong>{{ drafts.currentDraft.status === 'LOCKED' ? 'OA 附件' : '预计 OA 附件' }}</strong>
                      <ul v-if="attachmentNames.length">
                        <li
                          v-for="name in attachmentNames"
                          :key="name"
                        >
                          {{ name }}
                        </li>
                      </ul>
                      <p v-else>
                        {{ attachmentPendingHint }}
                      </p>
                    </div>
                    <el-button
                      v-if="!props.mobile"
                      type="primary"
                      size="large"
                      :loading="submitFlowPending || submission.submitting"
                      :disabled="Boolean(submissionButtonReason)"
                      :title="submissionButtonReason"
                      @click="confirmAndSubmit"
                    >
                      提交 OA
                    </el-button>
                  </div>
                </template>
              </ExpenseSummaryCard>
            </section>
            <div
              v-if="props.mobile && !submittedResult"
              ref="mobileFooterDock"
              class="mobile-step-footer-dock"
            >
              <footer class="mobile-step-footer">
                <div class="mobile-footer-total">
                  <span>当前合计</span>
                  <strong>{{ expense.pendingTotalLabel || `¥${expense.displayTotal}` }}</strong>
                </div>
                <div class="mobile-footer-actions">
                  <el-button
                    v-if="mobileStep > 0"
                    @click="selectMobileStep(mobileStep - 1)"
                  >
                    上一步
                  </el-button>
                  <el-button
                    type="primary"
                    :loading="mobileStep === reviewStep && (canReplaceOutdatedForm
                      ? replacingTemplate
                      : submitFlowPending || submission.submitting)"
                    :disabled="mobileStep === reviewStep && (canReplaceOutdatedForm
                      ? drafts.busy
                      : !formReadOnly && Boolean(submissionButtonReason))"
                    :title="mobileStep === reviewStep && !formReadOnly ? submissionButtonReason : ''"
                    @click="handleMobilePrimaryAction"
                  >
                    {{ mobileStep === reviewStep && canReplaceOutdatedForm
                      ? '按新表单重新填写'
                      : formReadOnly ? '查看报销状态' : mobileNextLabel }}
                  </el-button>
                </div>
              </footer>
            </div>
          </template>
        </template>
      </div>
    </template>
    <el-card
      v-else
      shadow="never"
      class="content-card"
    >
      <el-result
        icon="error"
        title="无法进入报销工具"
        :sub-title="auth.errorMessage || '请从公司钉钉工作台重新进入本应用'"
      >
        <template #extra>
          <el-button
            type="primary"
            @click="auth.bootstrap(true)"
          >
            重新尝试
          </el-button>
        </template>
      </el-result>
    </el-card>
    <ReimbursementAttentionNotice
      :notice="pageAttention"
      :mobile="props.mobile"
      :footer-height="props.mobile && !submittedResult ? mobileFooterHeight || 116 : 0"
      :suspended="attentionSuspended"
      @reveal="revealAttention(undefined, true)"
    />
    <el-dialog
      v-model="historyVisible"
      title="报销记录"
      width="min(520px, calc(100% - 24px))"
      destroy-on-close
    >
      <p class="history-caption">
        本人最近 10 笔成功提交的报销
      </p>
      <el-skeleton
        v-if="historyLoading"
        :rows="3"
        animated
        aria-label="正在加载报销记录"
      />
      <div
        v-else-if="historyError"
        class="history-state"
        role="alert"
      >
        <span>{{ historyError }}</span>
        <el-button @click="loadHistory()">
          重新加载记录
        </el-button>
      </div>
      <p
        v-else-if="!historyRecords.length"
        class="history-state"
      >
        暂无成功提交 OA 的报销记录。
      </p>
      <ul
        v-else
        class="record-list"
        data-testid="reimbursement-history"
      >
        <li
          v-for="draft in historyRecords"
          :key="draft.id"
          class="record-row"
        >
          <div class="record-description">
            <div class="record-mainline">
              <strong
                v-if="draft.totalAmount !== undefined"
                class="record-amount"
              >¥{{ draft.totalAmount }}</strong>
              <strong class="record-budget">{{ draft.budgetLabel || '报销明细' }}</strong>
            </div>
            <small>
              <template v-if="draft.kind">{{ draft.kind === 'general' ? '普通费用报销' : '差旅报销' }} · </template>
              <template v-if="draft.beneficiaryName">代报 {{ draft.beneficiaryName }} · </template>
              <template v-if="draft.department">{{ draft.department.name }} · </template>
              <time :datetime="draft.lockedAt">{{ formatRecordTime(draft.lockedAt) }}</time>
              <template v-if="draft.relatedApprovalCount"> · {{ draft.relatedApprovalCount }} 张审批</template>
            </small>
          </div>
          <span
            v-if="draft.id === drafts.currentDraft?.id"
            class="record-action record-current"
          >当前记录</span>
          <el-button
            v-else
            class="record-action"
            link
            type="primary"
            :disabled="drafts.busy || saving || Boolean(historyOpeningId)"
            :loading="historyOpeningId === draft.id"
            @click="chooseHistoryRecord(draft.id)"
          >
            查看明细
          </el-button>
        </li>
      </ul>
      <p
        v-if="historyDetailError"
        class="history-state"
        role="alert"
      >
        {{ historyDetailError }}
      </p>
    </el-dialog>
    <el-dialog
      :model-value="Boolean(pendingDepartmentApproval)"
      title="确认本次报销部门"
      :width="props.mobile ? 'calc(100% - 24px)' : '480px'"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :show-close="false"
      data-testid="department-choice-dialog"
    >
      <p class="department-choice-copy">
        “{{ pendingDepartmentApprovalTitle }}”的发起部门已不在你当前可用的部门中，请选择本次报销归属的当前部门。
      </p>
      <el-select
        v-model="selectedDepartmentChoiceId"
        placeholder="请选择当前部门"
        class="department-choice-select"
        data-testid="department-choice-select"
      >
        <el-option
          v-for="department in selectableDepartments"
          :key="department.id"
          :label="department.name"
          :value="department.id"
        />
      </el-select>
      <template #footer>
        <el-button
          :disabled="bindingApprovalDepartment"
          @click="cancelDepartmentChoice"
        >
          返回重新选择
        </el-button>
        <el-button
          type="primary"
          :loading="bindingApprovalDepartment"
          :disabled="!selectedDepartmentChoiceId"
          @click="confirmDepartmentChoice"
        >
          确认部门
        </el-button>
      </template>
    </el-dialog>
    <ReimbursementEmployeePicker
      v-model="employeePickerOpen"
      :switching="switchingReimbursement"
      :switch-error="reimbursementSwitchError"
      @choose="chooseReimbursementEmployee"
    />
  </main>
</template>

<style scoped>
.workspace-content { display: contents; }
.reimbursement-kind-selector { margin: 10px 0 12px; }
.general-accounting-form { margin-top: 20px; }
.hero-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.hero-intro { flex: 1; min-width: 0; }
.hero-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.hero-links { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
.hero-links .el-button { margin: 0; }
.reimbursement-subject { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; margin-top: 9px; }
.reimbursement-subject > .el-button { margin: 0; }
.reimbursement-switch-note { color: var(--el-text-color-secondary); margin: 0; }
p.reimbursement-switch-note { margin: 8px 0 0; }
.hero-link {
  display: inline-flex;
  flex: none;
  align-items: center;
  min-height: 32px;
  padding: 4px 0;
  border: 0;
  background: transparent;
  color: var(--el-color-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
  white-space: nowrap;
  cursor: pointer;
}
.hero-link:disabled { color: var(--el-text-color-placeholder); cursor: not-allowed; text-decoration: none; }
.hero-link:not(:disabled):hover { color: var(--el-color-primary-dark-2); text-decoration: underline; text-underline-offset: 3px; }
.hero-title { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 12px; margin: 4px 0 12px; }
.hero-title h1 { margin: 0; }
.save-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #e9eef4;
  color: #48576a;
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
}
.save-indicator::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #69778b; }
.save-indicator[data-state='saved']::before { background: #21845a; }
.save-indicator[data-state='saving']::before { background: var(--el-color-primary); }
.eyebrow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.current-user-id {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}
.reimbursement-page {
  --workspace-gap: 18px;
  display: flex;
  flex-direction: column;
  gap: var(--workspace-gap);
  padding-top: 24px;
}
.reimbursement-page > .hero,
.reimbursement-page :is(.reimbursement-card, .summary-card) { margin-block: 0; }
.reimbursement-page :deep(.el-alert) { border-radius: 10px; }
.save-recovery-detail { margin: 4px 0; }
.record-mainline { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; }
.record-budget { overflow-wrap: anywhere; }
.record-mainline .record-amount { color: var(--expense-amount-color); white-space: nowrap; }
.history-caption { margin: 0 0 12px; color: var(--el-text-color-secondary); font-size: 13px; }
.history-state { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 0; color: var(--el-text-color-secondary); }
.record-list {
  max-height: min(62dvh, 460px);
  overflow-y: auto;
  padding: 0;
  margin: 0;
  list-style: none;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
}
.record-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; }
.record-row + .record-row { border-top: 1px solid var(--el-border-color-lighter); }
.record-description { display: grid; gap: 5px; }
.record-description strong { color: var(--el-text-color-primary); }
.record-description small { color: var(--el-text-color-secondary); }
.record-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 64px;
  box-sizing: border-box;
  height: 32px;
  min-height: 32px;
  padding: 2px;
  font-size: 14px;
  font-weight: 500;
  line-height: 20px;
  white-space: nowrap;
}
.record-current { color: var(--el-text-color-secondary); }
.submitted-receipt {
  padding: 24px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 16px;
  background: var(--el-bg-color);
}
.receipt-heading { display: flex; justify-content: space-between; align-items: center; gap: 18px; }
.receipt-heading-copy { display: flex; align-items: flex-start; gap: 12px; }
.receipt-check {
  width: 36px;
  height: 36px;
  padding: 8px;
  flex: none;
  box-sizing: border-box;
  border-radius: 50%;
  background: #e6f4ec;
  color: #17784c;
}
.receipt-heading h2 { margin: 0; color: var(--el-text-color-primary); font-size: 21px; line-height: 1.4; }
.receipt-heading p { margin: 3px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
.receipt-amount { display: grid; gap: 3px; min-width: 0; text-align: right; }
.receipt-amount span, .receipt-metrics dt, .receipt-info dt { color: var(--el-text-color-secondary); font-size: 12px; }
.receipt-amount strong { color: var(--el-text-color-primary); font-size: 30px; overflow-wrap: anywhere; }
.receipt-amount strong, .receipt-metrics dd { font-variant-numeric: tabular-nums; }
.receipt-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 16px 0 0;
  padding: 10px 0;
  border-radius: 10px;
  background: var(--el-fill-color-light);
}
.receipt-metrics > div { padding: 0 12px; min-width: 0; }
.receipt-metrics--general { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.receipt-metrics > div + div { border-left: 1px solid var(--el-border-color-lighter); }
.receipt-metrics dd, .receipt-info dd { margin: 4px 0 0; color: var(--el-text-color-primary); overflow-wrap: anywhere; }
.receipt-metrics dd { font-weight: 650; }
.submitted-receipt-toolbar { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 10px 20px; margin-top: 16px; }
.submitted-receipt-actions { display: flex; align-items: flex-start; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-left: auto; }
.receipt-next-note, .receipt-attachment-summary { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
.receipt-info {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px 18px;
  padding-top: 16px;
  margin: 18px 0 0;
  border-top: 1px solid var(--el-border-color-lighter);
}
.receipt-info dd { font-size: 14px; line-height: 1.5; }
.receipt-approvals { margin-top: 16px; }
.receipt-approvals h3 { margin: 0 0 6px; color: var(--el-text-color-secondary); font-size: 12px; font-weight: 400; }
.receipt-approvals ul { display: grid; gap: 12px; padding: 0; margin: 0; list-style: none; }
.receipt-approvals li { display: grid; gap: 6px; overflow-wrap: anywhere; }
.receipt-approvals li + li { padding-top: 12px; border-top: 1px solid var(--el-border-color-lighter); }
.receipt-approvals li strong { color: var(--el-text-color-primary); font-size: 14px; font-weight: 550; }
.receipt-approval-meta { margin: 0; color: var(--el-text-color-regular); font-size: 13px; line-height: 1.6; }
.receipt-approval-dates { white-space: nowrap; }
.receipt-approval-number { margin: 0; color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; }
.receipt-attachment-summary { margin: 14px 0 0; }
.submitted-receipt-toolbar > .receipt-attachment-summary { margin: 6px 0 0; }
.department-choice-copy { margin: 0 0 16px; color: var(--el-text-color-regular); line-height: 1.7; }
.department-choice-select { width: 100%; }
.department-gate-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.department-switch-help { margin: 0 0 12px; }
.department-identity { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
.travel-department-confirm { margin-top: 12px; }
.content-card .department-gate-approvals { margin-top: 0; padding-top: 0; border-top: 0; }
.identity-grid { margin-bottom: 0; }
.form-options-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.form-options-actions .el-button { margin: 0; }
.page-shell--mobile .form-options-actions .el-button { flex: 1 1 160px; min-height: 44px; }
.plain-fieldset, .editor-fieldset { min-width: 0; padding: 0; margin: 0; border: 0; }
.editor-fieldset { display: flex; flex-direction: column; gap: var(--workspace-gap); }
.accounting-verification-alert {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 16px;
  margin-top: 18px;
  padding: 12px 14px;
  border: 1px solid var(--el-color-warning-light-7);
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
}
.accounting-verification-alert p { flex: 1 1 240px; margin: 0; color: var(--el-text-color-regular); font-size: 13px; line-height: 1.6; }
.accounting-verification-alert .el-button { margin-left: auto; }
.reimbursement-page > .accounting-verification-alert { margin: 0; }
.derived-accounting-section { margin-top: 22px; padding-top: 22px; border-top: 1px solid var(--el-border-color-lighter); }
.derived-accounting-heading h2 { margin: 0; color: var(--el-text-color-primary); font-size: 16px; }
.derived-accounting-heading p { margin: 6px 0 14px; color: var(--el-text-color-secondary); font-size: 13px; }
.derived-accounting-grid { margin-bottom: 12px; }
.derived-accounting-grid :deep(.el-descriptions__table) { table-layout: fixed; }
.derived-accounting-grid :deep(.el-descriptions__content) { overflow-wrap: anywhere; }
.derived-accounting-list {
  overflow: hidden;
  margin: 0 0 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-fill-color-blank);
}
.derived-accounting-list__row { padding: 11px 12px; border-bottom: 1px solid var(--el-border-color-lighter); }
.derived-accounting-list__row:last-child { border-bottom: 0; }
.derived-accounting-list dt {
  margin: 0 0 5px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
}
.derived-accounting-list dd {
  min-width: 0;
  margin: 0;
  color: var(--el-text-color-primary);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.related-approval-verification-error { margin-top: 16px; margin-bottom: 12px; }
.travel-periods { display: grid; justify-items: start; gap: 5px; }
.travel-periods__count { color: var(--el-text-color-secondary); font-size: 12px; }
.travel-periods__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  color: var(--el-text-color-primary);
  white-space: nowrap;
}
.travel-periods__item::before {
  width: 5px;
  height: 5px;
  flex: none;
  border-radius: 50%;
  background: var(--el-color-primary-light-3);
  content: '';
}
.travel-periods--mobile { width: 100%; gap: 7px; }
.travel-periods--mobile .travel-periods__item {
  flex-wrap: wrap;
  gap: 4px;
  font-size: 13px;
  white-space: normal;
}
.travel-periods--mobile .travel-periods__item time { white-space: nowrap; }
.submission-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.submission-actions { align-items: flex-end; margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--el-border-color-lighter); }
.submission-attachments { color: var(--el-text-color-regular); font-size: 13px; line-height: 1.6; }
.submission-attachments strong { color: var(--el-text-color-primary); }
.submission-attachments ul { margin: 4px 0 0; padding-left: 18px; }
.submission-attachments p { margin: 4px 0 0; color: var(--el-text-color-secondary); }
.template-recovery-panel {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  padding: 16px;
  border: 1px solid var(--el-color-warning-light-5);
  border-radius: 10px;
  background: var(--el-color-warning-light-9);
}
.template-recovery-panel strong { color: var(--el-text-color-primary); }
.template-recovery-panel p { margin: 6px 0 0; color: var(--el-text-color-regular); font-size: 13px; line-height: 1.6; }
.template-recovery-panel .el-button { flex: none; }
.manual-review-panel, .submission-feedback-panel {
  padding: clamp(16px, 3vw, 24px);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  background: var(--el-bg-color);
}
.manual-review-panel { border-color: var(--el-color-warning-light-7); }
.manual-review-heading, .submission-feedback-heading { display: flex; align-items: flex-start; gap: 12px; }
.submission-feedback-heading > div { min-width: 0; }
.submission-feedback-icon {
  flex: none;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  font-size: 20px;
}
.submission-feedback-panel--error .submission-feedback-icon {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.submission-feedback-panel--warning .submission-feedback-icon {
  color: var(--el-color-warning-dark-2);
  background: var(--el-color-warning-light-9);
}
.manual-review-icon {
  flex: none;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  font-size: 19px;
}
.manual-review-heading h2, .submission-feedback-heading h2 { margin: 0; font-size: 18px; line-height: 1.4; }
.submission-feedback-heading p { margin: 6px 0 0; color: var(--el-text-color-regular); font-size: 14px; line-height: 1.65; overflow-wrap: anywhere; text-wrap: pretty; }
.manual-review-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.manual-review-details {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 8px 16px;
  margin: 16px 0;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  font-size: 13px;
  line-height: 1.5;
}
.manual-review-details dt { color: var(--el-text-color-secondary); }
.manual-review-details dd { margin: 0; overflow-wrap: anywhere; }
.manual-review-error { margin: 0 0 12px; color: var(--el-color-danger-dark-2); font-size: 13px; }
.submission-feedback-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--el-border-color-lighter);
}
.submission-feedback-actions .el-button { min-height: 36px; margin: 0; }
.submission-status .el-progress { margin: 16px 0; }
.material-checklist { margin-top: 16px; padding: 14px 16px; border-radius: 10px; background: #fff8ed; color: #8b5a16; font-size: 13px; }
.material-checklist > strong { display: block; }
.material-checklist > strong:not(:first-child) { margin-top: 12px; }
.material-checklist-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.material-checklist-row > span { min-width: 0; overflow-wrap: anywhere; }
.material-checklist-row .el-button { flex-shrink: 0; }
.material-checklist p { margin: 8px 0 0; color: #667085; }
.page-shell--mobile {
  --workspace-gap: 14px;
  --workspace-card-padding: 16px;
  min-height: 100vh;
  min-height: 100dvh;
  width: min(100%, 560px);
  padding: 16px 12px var(--mobile-footer-height, 116px);
}
.hero--mobile {
  align-items: stretch;
}
.page-shell--mobile :is(.submitted-receipt, .manual-review-panel, .submission-feedback-panel, .template-recovery-panel) { padding: var(--workspace-card-padding); }
.page-shell--mobile .hero-meta { gap: 8px; }
.page-shell--mobile .hero-links { flex: none; flex-wrap: nowrap; gap: 10px; }
.page-shell--mobile .hero-link { min-height: 40px; padding-block: 8px; }
.page-shell--mobile .submission-feedback-heading h2 { font-size: 17px; }
.page-shell--mobile .submission-feedback-actions { gap: 8px; margin-top: 16px; padding-top: 14px; }
.page-shell--mobile .submission-feedback-actions .el-button { flex: 1; min-width: 0; min-height: 44px; padding: 10px 12px; white-space: normal; height: auto; }
.page-shell--mobile .submission-feedback-actions .el-button--primary { flex: 1.9; }
.page-shell--mobile .submission-feedback-actions .submission-recheck-action { flex: 1 0 100%; }
.page-shell--mobile.page-shell--submitted { padding-bottom: 24px; }
.page-shell--mobile .receipt-heading { align-items: flex-start; flex-direction: column; gap: 12px; }
.page-shell--mobile .receipt-heading h2 { font-size: 19px; }
.page-shell--mobile .receipt-amount { text-align: left; }
.page-shell--mobile .receipt-info { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.page-shell--mobile .submitted-receipt-toolbar { display: block; }
.page-shell--mobile .submitted-receipt-actions > * { flex: 1 1 120px; }
.page-shell--mobile .submitted-receipt-actions :deep(.el-button) { width: 100%; min-height: 44px; margin: 0; }
.hero--mobile .hero-title { margin: 2px 0 6px; }
.hero--mobile .reimbursement-kind-selector { margin: 6px 0 12px; }
.hero--mobile .reimbursement-kind-buttons { display: flex; }
.hero--mobile .reimbursement-kind-buttons .el-button { flex: 1; min-width: 0; min-height: 44px; }
.hero--mobile h1 { font-size: 22px; }
.hero--mobile .eyebrow { font-size: 12px; letter-spacing: .04em; }
.hero--mobile .summary { font-size: 13px; line-height: 1.5; }
.hero--mobile .hero-actions { flex-shrink: 0; }
.mobile-step-nav {
  position: sticky;
  top: 0;
  z-index: 9;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(0, 1fr);
  gap: 4px;
  margin: 0 -4px;
  padding: 8px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: rgb(244 247 251 / 94%);
  backdrop-filter: blur(12px);
}
.mobile-step-nav button {
  min-width: 0;
  min-height: 52px;
  padding: 6px 2px;
  border: 0;
  border-radius: 10px;
  color: var(--el-text-color-secondary);
  background: transparent;
}
.mobile-step-nav button span {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  margin: 0 auto 3px;
  border: 1px solid var(--el-border-color);
  border-radius: 50%;
  background: var(--el-bg-color);
  font-size: 11px;
  font-weight: 700;
}
.mobile-step-nav button small { display: block; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.mobile-step-nav button.is-active { color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
.mobile-step-nav button.is-active span { border-color: var(--el-color-primary); color: #fff; background: var(--el-color-primary); }
.mobile-step-nav button.is-done { color: var(--el-color-success); }
.mobile-step-nav button.is-done span { border-color: var(--el-color-success); color: #fff; background: var(--el-color-success); }
.mobile-step-panel { min-width: 0; }
.mobile-step-heading { padding: 2px 2px 12px; scroll-margin-block-start: 76px; }
.mobile-step-heading h2 { margin: 0; font-size: 20px; }
.mobile-step-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.55; }
.page-shell--mobile :deep(.content-card) { border-radius: 16px; }
.page-shell--mobile :deep(.content-card > .el-card__header),
.page-shell--mobile :deep(.content-card:not(.summary-card--preview-only) > .el-card__body) { padding: var(--workspace-card-padding); }
.page-shell--mobile :deep(.totals-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.page-shell--mobile .template-recovery-panel { align-items: stretch; flex-direction: column; }
.page-shell--mobile .template-recovery-panel .el-button { width: 100%; margin: 0; }
.mobile-step-footer-dock {
  position: fixed;
  left: 50%;
  bottom: 0;
  z-index: 10;
  width: min(100%, 560px);
  padding-top: 18px;
  transform: translateX(-50%);
}
.mobile-step-footer {
  padding: 10px 12px max(12px, env(safe-area-inset-bottom));
  border-top: 1px solid var(--el-border-color-lighter);
  background: rgb(255 255 255 / 96%);
  box-shadow: 0 -8px 24px rgb(23 32 51 / 8%);
  backdrop-filter: blur(12px);
}
.mobile-footer-total { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.mobile-footer-total span { color: var(--el-text-color-secondary); font-size: 12px; }
.mobile-footer-total strong { font-size: 18px; }
.mobile-footer-actions { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 10px; }
.mobile-footer-actions .el-button { width: 100%; min-height: 46px; margin: 0; }
.mobile-footer-actions .el-button:only-child { grid-column: 1 / -1; }
@media (max-width: 420px) {
  .page-shell--mobile { width: 100%; padding-inline: 8px; }
  .mobile-step-nav button small { font-size: 12px; }
}
</style>

<script setup lang="ts">
import { adjustmentPayload } from '@/utils/subsidyAdjustments'
import { ArrowLeft, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'

import ExpenseItemsCard from '@/components/reimbursement/ExpenseItemsCard.vue'
import ExpenseSummaryCard from '@/components/reimbursement/ExpenseSummaryCard.vue'
import TravelApprovalSelector from '@/components/reimbursement/TravelApprovalSelector.vue'
import TripSubsidyCard from '@/components/reimbursement/TripSubsidyCard.vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { listReimbursementSubmissionHistory } from '@/api/reimbursements'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useHealthStore } from '@/stores/health'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'
import { isForeignExpense, type TripInput, type TripType } from '@/types/expenses'
import type {
  ReimbursementDraft,
  ReimbursementDraftInput,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
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
const expense = useExpenseStore()
const health = useHealthStore()
const drafts = useReimbursementDraftStore()
const submission = useReimbursementSubmissionStore()
const expenseItemsCard = ref<InstanceType<typeof ExpenseItemsCard> | null>(null)
const approvalSelector = ref<InstanceType<typeof TravelApprovalSelector> | null>(null)
const mobileFooterDock = ref<HTMLElement | null>(null)
const mobileFooterHeight = ref(0)
const mobileStep = ref(0)
const mobileSteps = ['关联审批', '范围补助', '费用材料', '核对提交'] as const
const companyValue = ref('')
const budgetCodeValue = ref('')
const selectedRelatedApprovals = ref<ReimbursementRelatedApprovalSelection[]>([])
const initializingWorkspace = ref(false)
const initializationError = ref('')
const departmentBindingError = ref('')
const bindingApprovalDepartment = ref(false)
const pendingDepartmentApproval = ref<ReimbursementRelatedApprovalSelection | null>(null)
const selectedDepartmentChoiceId = ref('')
const saving = ref(false)
const refreshingMaterialStatus = ref(false)
const deferredMaterialRevision = ref<string | null>(null)
const saveError = ref('')
const saveRequiresReload = ref(false)
const expiredDraftId = ref('')
const workspaceActionError = ref('')
const confirmingDraftReload = ref(false)
const pendingNewApproval = ref<ReimbursementRelatedApprovalSelection | null>(null)
const historyVisible = ref(false)
const historyRecords = ref<ReimbursementSubmissionHistoryEntry[]>([])
const historyError = ref('')
const historyLoading = ref(false)
const hydratedLockedDraftId = ref('')
const receiptLoadError = ref('')
let historyRequest: AbortController | undefined
const historyOrigin = ref<{
  draftId: string
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
let autosaveTimer: ReturnType<typeof setTimeout> | undefined
let initializationPromise: Promise<void> | null = null
let savePromise: Promise<void> | null = null
let initializationScope = ''
let initializedScope = ''
let disposed = false
let mobileFooterResizeObserver: ResizeObserver | undefined

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
const templateNeedsAdmin = computed(() => [
  'OA_TEMPLATE_CONFIRMATION_REQUIRED', 'OA_TEMPLATE_SCHEMA_CHANGED', 'OA_TEMPLATE_NOT_CONFIGURED',
  'INVALID_SYSTEM_CONFIGURATION',
].includes(drafts.reimbursementOptionsErrorCode))
const draftExpired = computed(() => Boolean(drafts.currentDraft
  && (drafts.currentDraft.status === 'EXPIRED' || drafts.currentDraft.id === expiredDraftId.value)))
const canReplaceOutdatedForm = computed(() => templateMismatch.value && Boolean(drafts.currentDraft
  && ['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status))
  && !trackedSubmission.value && !submitFlowPending.value && !initializingWorkspace.value)
const formReadOnly = computed(() => !drafts.currentDraft || submitFlowPending.value
  || drafts.currentDraft.status === 'LOCKED' || draftExpired.value || trackedSubmission.value
  || templateMismatch.value || reimbursementOptionsUnavailable.value || saveRequiresReload.value)
const formReadOnlyReason = computed(() => {
  if (!drafts.currentDraft) return '正在准备报销表单'
  if (submitFlowPending.value) return '正在提交，请稍候'
  if (draftExpired.value) return '本次填写内容已过期，请重新填写'
  if (drafts.currentDraft.status === 'LOCKED' || trackedSubmission.value) return '本次报销内容已锁定，请查看提交进度'
  if (saveRequiresReload.value) return '报销内容已更新，请核对最新报销内容'
  if (reimbursementOptionsUnavailable.value) return '当前 OA 模板或选项不可用，请重新加载后继续填写'
  if (templateMismatch.value) return 'OA 表单已更新，请按新表单重新填写'
  return ''
})
const currentFormInput = computed<ReimbursementDraftInput>(() => {
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
  : expense.items.length ? '请先上传报销材料。' : '填写费用明细或核对出差补助后会显示附件清单。')
const submissionButtonReason = computed(() => formReadOnlyReason.value
  || (accountingOptionsChanged.value ? '请重新关联出差审批' : '')
  || (needsApprovalReconfirmation.value ? '请先重新确认出差审批' : '')
  || subsidyInputError.value || submissionServiceReason.value
  || (relatedApprovalVerificationPending.value ? '正在核验出差审批，请稍候' : '')
  || (drafts.busy ? '请等待材料处理完成' : '')
  || (drafts.pendingUploads.length ? '有材料尚未完成上传，请查看上传进度' : '')
  || (materialSubmissionIssues.value.length
    ? `还有 ${materialSubmissionIssues.value.length} 份材料需处理后才能提交 OA` : '')
  || (missingMaterialItems.value.length ? `还有 ${missingMaterialItems.value.length} 笔费用需补材料` : ''))
const mobileNextLabel = computed(() => mobileStep.value === mobileSteps.length - 1
  ? '提交 OA'
  : `下一步：${mobileSteps[mobileStep.value + 1]}`)
const missingMaterialItems = computed(() => expense.sortedItems.map((item) => ({ item, missing: missingExpenseMaterials(item, drafts.files) }))
  .filter((entry) => entry.missing.length))
const previewDisabledReason = computed(() => expense.itemReadinessError
  || (saveRequiresReload.value ? '请先核对最新报销内容' : '')
  || (accountingOptionsChanged.value ? '请重新关联出差审批' : '')
  || subsidyInputError.value
  || (!formReadOnly.value && drafts.pendingUploads.length ? '请等待材料上传完成后再预览 Excel' : '')
  || (!formReadOnly.value && materialSubmissionIssues.value.length
    ? '请先处理全部待确认或未完成的材料，再预览 Excel' : '')
  || (!budgetCodeValue.value ? '请先选择预算代码' : '')
  || (relatedApprovalVerificationPending.value ? '正在核验出差审批，请稍候' : '')
  || (props.mobile && !formReadOnly.value && saveError.value ? '保存失败，请重试保存后再打开 Excel' : '')
  || (props.mobile && !formReadOnly.value && (saving.value || formDirty.value) ? '正在保存当前内容，请稍候' : '')
  || (submitFlowPending.value ? '正在提交，请稍候' : ''))
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
const canStartNewReimbursement = computed(() => Boolean(drafts.currentDraft
  && !historyOrigin.value
  && (draftExpired.value && !trackedSubmission.value
    || (submission.activeDraftId === drafts.currentDraft.id
      && (submission.status === 'SUBMITTED' || submission.status === 'FAILED_FINAL')))))
const submittedResult = computed(() => trackedSubmission.value && submission.succeeded
  ? submission.submission : null)
const submittedReimbursement = computed(() => drafts.currentDraft?.status === 'LOCKED'
  && hydratedLockedDraftId.value === drafts.currentDraft.id ? submittedResult.value : null)
const receiptDetails = computed(() => drafts.currentDraft?.submittedDetails)
const canOpenHistory = computed(() => Boolean(drafts.currentDraft && !historyOrigin.value
  && (canStartNewReimbursement.value
    || (!trackedSubmission.value && drafts.currentDraft.status !== 'LOCKED' && !submitFlowPending.value))))
const reimbursementTime = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', dateStyle: 'medium', timeStyle: 'short',
})

function formatRecordTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : reimbursementTime.format(date)
}

function sessionScope(): string {
  const user = auth.session?.user.userId
  const department = auth.session?.selectedDepartment
  return auth.status === 'authenticated' && user && department
    ? JSON.stringify([user, department.id, department.name]) : ''
}
function tripSignature(trip: TripInput | null | undefined): object | null {
  if (!trip) return null
  return {
    relatedApprovalId: trip.relatedApprovalId,
    tripType: trip.tripType,
    startDate: trip.startDate,
    startTime: trip.startTime,
    endDate: trip.endDate,
    endTime: trip.endTime,
    policyConfirmed: trip.policyConfirmed ?? false,
    attendanceDates: trip.attendanceDates,
    confirmedEffectiveDays: trip.confirmedEffectiveDays ?? '',
    noSubsidyException: trip.noSubsidyException ?? false,
    excludedPeriods: (trip.excludedPeriods ?? []).map(adjustmentPayload),
  }
}
function inputSignature(input: ReimbursementDraftInput): string {
  // Project is server-derived from budget, never a second employee input.
  return JSON.stringify({
    ocrDispositionVersion: input.ocrDispositionVersion,
    companyValue: input.companyValue,
    budgetCodeValue: input.budgetCodeValue,
    trip: tripSignature(input.trip),
    trips: (input.trips ?? []).map(tripSignature),
    editingState: input.editingState
      ? {
          includeSubsidy: input.editingState.includeSubsidy,
          trip: tripSignature(input.editingState.trip),
          trips: (input.editingState.trips ?? []).map(tripSignature),
        }
      : undefined,
    items: input.items.map((item) => ({
      sourceFileId: item.sourceFileId,
      transportType: item.transportType,
      itineraryFileIds: item.itineraryFileIds ?? [],
      itineraryAutoMatchDisabled: item.itineraryAutoMatchDisabled ?? false,
      paymentProofFileIds: item.paymentProofFileIds ?? [],
      hotelBillFileIds: item.hotelBillFileIds ?? [],
      receiptAttachmentFileIds: item.receiptAttachmentFileIds ?? [],
      railType: item.railType ?? 'unknown',
      requiresItinerary: item.requiresItinerary ?? false,
      originalCurrency: item.originalCurrency,
      originalAmount: item.originalAmount,
      originalDetailsEdited: item.originalDetailsEdited ?? false,
      cnyAmountConfirmed: item.cnyAmountConfirmed ?? false,
      requiresCnyConfirmation: item.requiresCnyConfirmation ?? false,
      category: item.category,
      date: item.date,
      displayDate: item.displayDate,
      description: item.description,
      amount: item.amount,
      receiptCount: item.receiptCount,
    })),
    dismissedOcrFileIds: input.dismissedOcrFileIds,
  })
}
function relatedSignature(selections: ReimbursementRelatedApprovalSelection[]): string { return JSON.stringify(selections) }
function acceptDerivedAccounting(): void {
  const input = drafts.currentDraft?.input
  if (!input) return
  companyValue.value = input.companyValue
  budgetCodeValue.value = input.budgetCodeValue
  // Merge only server-owned values; expense edits made during the request stay live.
  if (savedInputSignature.value) {
    const saved = JSON.parse(savedInputSignature.value) as ReimbursementDraftInput
    savedInputSignature.value = inputSignature({ ...saved, companyValue: input.companyValue, budgetCodeValue: input.budgetCodeValue })
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
async function runSerializedSave<T>(operation: () => Promise<T>): Promise<T> {
  if (savePromise) {
    await savePromise
    return runSerializedSave(operation)
  }
  const running = operation()
  const lock = running.then(() => undefined, () => undefined)
  savePromise = lock
  try {
    return await running
  } finally {
    if (savePromise === lock) savePromise = null
  }
}
async function verifyRelatedApprovals(
  selections: ReimbursementRelatedApprovalSelection[],
): Promise<'accepted' | 'restored' | 'conflict' | 'stale'> {
  const requestedSelections = cloneRelatedApprovalSelections(selections)
  const targetSignature = relatedSignature(requestedSelections)
  const draftId = drafts.currentDraft?.id ?? ''
  const scope = sessionScope()
  if (autosaveTimer) clearTimeout(autosaveTimer)
  relatedApprovalVerificationError.value = ''
  relatedApprovalRetry.value = null
  verifyingRelatedApprovals.value = true
  selectedRelatedApprovals.value = requestedSelections
  try {
    await runSerializedSave(() => drafts.saveRelatedApprovals(requestedSelections))
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
    verifyingRelatedApprovals.value = false
    scheduleAutosave()
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
function hydrate(draft: ReimbursementDraft): void {
  deferredMaterialRevision.value = null
  hydratedLockedDraftId.value = draft.status === 'LOCKED' ? draft.id : ''
  companyValue.value = draft.input.companyValue
  budgetCodeValue.value = draft.input.budgetCodeValue
  expense.hydrateFromDraft(draft, drafts.files)
  selectedRelatedApprovals.value = draft.relatedApprovals.map(approvalSelection)
  savedInputSignature.value = inputSignature(draft.input)
  savedRelatedSignature.value = relatedSignature(selectedRelatedApprovals.value)
  saveError.value = ''
  saveRequiresReload.value = false
  drafts.revisionConflict = false
  expiredDraftId.value = ''
  relatedApprovalVerificationError.value = ''
  relatedApprovalRetry.value = null
}
async function loadSubmittedReceipt(): Promise<void> {
  const result = submittedResult.value
  const scope = sessionScope()
  if (!result || !scope || drafts.loadingCurrentDraft || submittedReimbursement.value) return
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
async function openEditableReimbursement(): Promise<void> {
  const scope = sessionScope()
  const created = await drafts.createDraft({
    ocrDispositionVersion: 1,
    companyValue: '',
    budgetCodeValue: '',
    trip: null,
    trips: [],
    editingState: {
      includeSubsidy: false,
      trip: {
        tripType: 'business', startDate: '', startTime: '09:00', endDate: '', endTime: '18:00',
        policyConfirmed: false, confirmedEffectiveDays: '', noSubsidyException: false,
      },
      trips: [],
    },
    items: [],
    dismissedOcrFileIds: [],
  }, true, auth.session?.selectedDepartment ?? undefined)
  if (disposed || sessionScope() !== scope || drafts.currentDraft?.id !== created.id) return
  expense.reset()
  submission.abort()
  hydrate(created)
  if (created.status === 'LOCKED') {
    try {
      await submission.restore(created.id, { discoverByDraft: true, expectedRevision: created.revision })
    } catch { /* The submission panel shows the recovery action. */ }
  }
  if (templateMismatch.value) await drafts.loadReimbursementOptions()
}
async function runWorkspaceInitialization(scope: string): Promise<void> {
  initializingWorkspace.value = true
  initializationError.value = ''
  try {
    await Promise.all([
      expense.loadCategories(), drafts.loadReimbursementOptions(),
      drafts.loadDrafts(0, 50, auth.session?.selectedDepartment ?? undefined),
    ])
    if (disposed || sessionScope() !== scope) return
    if (drafts.listError) throw new Error(drafts.listError)
    // Restore a submission first: reloading must not create a duplicate approval.
    const recent = drafts.drafts.find((draft) => draft.status !== 'EXPIRED')
    if (recent) {
      await drafts.loadDraft(recent.id)
      const opened = drafts.currentDraft
      if (!opened || opened.id !== recent.id) throw new Error(drafts.loadError || '报销内容加载失败')
      if (sessionScope() !== scope || disposed) return
      hydrate(opened)
      try {
        await submission.restore(opened.id, opened.status === 'LOCKED'
          ? { discoverByDraft: true, expectedRevision: opened.revision } : undefined)
      } catch { /* The submission card renders the recoverable error. */ }
    } else {
      if (drafts.reimbursementOptionsError) throw new Error(drafts.reimbursementOptionsError)
      await openEditableReimbursement()
    }
    if (sessionScope() === scope) initializedScope = scope
  } catch (error) {
    if (sessionScope() === scope) initializationError.value = error instanceof Error ? error.message : '报销表单加载失败，请重试'
  } finally {
    if (sessionScope() === scope) { initializingWorkspace.value = false; scheduleAutosave() }
  }
}
function initializeWorkspace(force = false): Promise<void> {
  const scope = sessionScope()
  if (!scope || disposed || (!force && initializedScope === scope)) return Promise.resolve()
  if (initializationPromise && initializationScope === scope) return initializationPromise
  initializationScope = scope
  const running = runWorkspaceInitialization(scope).finally(() => {
    if (initializationPromise === running) initializationPromise = null
  })
  initializationPromise = running
  return running
}
function scheduleAutosave(): void {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  if (disposed || !sessionScope() || initializingWorkspace.value || formReadOnly.value
    || drafts.busy || confirmingDraftReload.value || verifyingRelatedApprovals.value || bindingApprovalDepartment.value
    || pendingDepartmentApproval.value || !formDirty.value) return
  autosaveTimer = setTimeout(() => { void flushAutosave().catch(() => undefined) }, 600)
}
async function flushAutosave(options: { includeRelated?: boolean } = {}): Promise<void> {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  const includeRelated = options.includeRelated !== false
  await runSerializedSave(async () => {
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
          acceptDerivedAccounting()
        }
      }
    } catch (error) {
      if (error !== fileOperationPause && drafts.currentDraft?.id === draftId && sessionScope() === scope) {
        if (includeRelated && relatedDirty.value) acceptApprovalReadback(drafts.currentDraft)
        if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_EXPIRED') expiredDraftId.value = draftId
        saveError.value = drafts.mutationError || (error instanceof Error ? error.message : '自动保存失败')
        void refreshOptionsAfterError(error)
      }
      throw error
    } finally { saving.value = false }
  })
}
onBeforeRouteLeave(async () => {
  if (submitFlowPending.value || bindingApprovalDepartment.value || replacingTemplate.value
    || initializingWorkspace.value || verifyingRelatedApprovals.value || pendingDepartmentApproval.value) {
    ElMessage.warning('请等待当前操作完成，或先完成部门选择')
    return false
  }
  if (!sessionScope() || drafts.currentDraft?.status === 'LOCKED' || trackedSubmission.value
    || (!formDirty.value && !savePromise)) return true
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
function validateSubmission(): string {
  if (!companyOptions.value.some((option) => option.value === companyValue.value)) return '请选择所属公司'
  if (!budgetOptions.value.some((option) => option.value === budgetCodeValue.value)) return '请选择预算代码'
  if (expense.includeSubsidy && expense.tripPayloads().length !== selectedRelatedApprovals.value.length) {
    return expense.policyInputError || '请完整确认每个补助项的出发和返回时段'
  }
  if (expense.items.length && (expense.categoryLoadError || !expense.categories.length)) return '请先加载费用类别'
  if (!expense.items.length && !hasValidSubsidy.value) return '请添加费用明细或申请有效的出差补助'
  if (expense.itemReadinessError) return expense.itemReadinessError
  if (materialSubmissionIssues.value.length) {
    const issue = materialSubmissionIssues.value[0]!
    return `请先处理“${issue.file.name}”：${issue.reason}`
  }
  if (!selectedRelatedApprovals.value.length) return '请至少关联一张已通过的出差审批'
  if (!hasActiveMaterials.value && !excelOnlySubmission.value) return '请上传报销材料'
  for (const item of expense.items) {
    if (item.category === 'lodging' && !item.hotelBillFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'hotel_bill')),
    )) return `“${item.description || '住宿费用'}”缺少住宿明细，请补充后提交`
    if (item.transportType !== 'taxi' && (item.requiresItinerary || item.transportType === 'ride_hailing') && !item.itineraryFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'itinerary')),
    )) return `“${item.description || '网约车费用'}”缺少对应行程单，请点击编辑补齐`
    if (isForeignExpense(item) && !item.cnyAmountConfirmed) {
      return `请确认“${item.description || '国外票据'}”的人民币报销金额`
    }
    if (requiresPaymentProof(item) && !item.paymentProofFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'payment_proof')),
    )) return `“${item.description || '本行费用'}”超过 500 元，请补充付款凭证`
  }
  return ''
}
async function confirmAndSubmit(): Promise<void> {
  if (submissionButtonReason.value) return
  const error = validateSubmission()
  if (error) { ElMessage.warning(error); return }
  submitFlowPending.value = true
  try {
    await ElMessageBox.confirm(
      excelOnlySubmission.value
        ? '核对无误后将生成报销单 Excel，并正式提交钉钉 OA。提交后内容将锁定。'
        : '核对无误后将生成票据汇总 PDF 和报销单 Excel，并正式提交钉钉 OA。提交后内容将锁定。',
      '提交报销申请', { confirmButtonText: '确认提交', cancelButtonText: '返回检查', type: 'warning' },
    )
    await flushAutosave()
    const ready = await drafts.markReviewReady()
    await submission.submit(ready.id, ready.revision)
    ElMessage.success('正在生成材料并提交 OA')
  } catch (error) {
    if (apiErrorCode(error) === 'REIMBURSEMENT_DRAFT_EXPIRED' && drafts.currentDraft) {
      expiredDraftId.value = drafts.currentDraft.id
    }
    void refreshOptionsAfterError(error)
    if (error !== 'cancel' && error !== 'close') ElMessage.error(submission.errorMessage || saveError.value
      || drafts.mutationError || (error instanceof Error ? error.message : '提交失败，请重试'))
  } finally { submitFlowPending.value = false; scheduleAutosave() }
}
async function newReimbursement(): Promise<void> {
  // Only a definitive outcome permits a new record; uncertain OA creation must
  // retain its existing submission identity until reconciliation completes.
  if (!canStartNewReimbursement.value || initializingWorkspace.value || drafts.busy
    || reimbursementOptionsUnavailable.value || confirmingDraftReload.value) return
  const scope = sessionScope()
  const draftId = drafts.currentDraft?.id
  if (draftExpired.value && formDirty.value) {
    confirmingDraftReload.value = true
    try {
      await ElMessageBox.confirm(
        '这笔报销已过期。继续填写新的报销前，请先留存本页修改。确认后，本页尚未保存的内容会被舍弃。',
        '确认重新填写',
        { confirmButtonText: '舍弃并重新填写', cancelButtonText: '保留本页', type: 'warning' },
      )
    } catch { return }
    finally { confirmingDraftReload.value = false }
    if (disposed || scope !== sessionScope() || draftId !== drafts.currentDraft?.id
      || !canStartNewReimbursement.value || drafts.busy || reimbursementOptionsUnavailable.value) return
  }
  const selection = pendingNewApproval.value
  workspaceActionError.value = ''
  initializingWorkspace.value = true
  try {
    await openEditableReimbursement()
    if (sessionScope() !== scope || disposed) return
    await returnToApprovalStep()
    const opened = drafts.currentDraft
    if (opened?.status !== 'DRAFT' && opened?.status !== 'REVIEW_READY') return
    pendingNewApproval.value = null
    if (selection && !opened.relatedApprovals.length) {
      await verifyRelatedApprovals([selection])
    } else if (selection && !opened.relatedApprovals.some((approval) => approval.processInstanceId === selection.processInstanceId)) {
      ElMessage.warning('已打开这笔部门草稿并保留原有关联，请在出差审批中核对后再调整。')
    }
  }
  catch (error) {
    workspaceActionError.value = error instanceof Error ? error.message : '新报销准备失败'
    ElMessage.error(workspaceActionError.value)
    void refreshOptionsAfterError(error)
  }
  finally { if (sessionScope() === scope) initializingWorkspace.value = false }
}
async function returnToApprovalStep(): Promise<void> {
  mobileStep.value = 0
  await nextTick()
  scrollToWorkspaceStart()
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
  if (scope === sessionScope() && !disposed && !formReadOnly.value) {
    await approvalSelector.value?.search?.()
  }
}

async function recoverAccountingOptions(): Promise<void> {
  await returnToApprovalStep()
  await refreshFormOptions()
}

async function refreshOptionsAfterError(error: unknown): Promise<void> {
  if ([
    'OA_TEMPLATE_CONFIGURATION_CHANGED', 'OA_TEMPLATE_SCHEMA_CHANGED', 'OA_TEMPLATE_CONFIRMATION_REQUIRED',
    'OA_TEMPLATE_NOT_CONFIGURED',
    'REIMBURSEMENT_DRAFT_TEMPLATE_CHANGED', 'REIMBURSEMENT_COMPANY_OPTION_INVALID',
    'REIMBURSEMENT_BUDGET_OPTION_INVALID',
  ].includes(apiErrorCode(error) ?? '')) await refreshFormOptions()
}

async function openSavedReimbursement(draftId: string, action?: 'history' | 'return'): Promise<void> {
  const scope = sessionScope()
  const previousId = drafts.currentDraft?.id
  if (!scope || initializingWorkspace.value || drafts.busy || saving.value
    || submitFlowPending.value || confirmingDraftReload.value || submission.submitting) return
  const origin = historyOrigin.value
  if (action === 'history' && (!previousId || !canOpenHistory.value || bindingApprovalDepartment.value
    || verifyingRelatedApprovals.value || pendingDepartmentApproval.value)) return
  if (action === 'return' && origin?.draftId !== draftId) return
  if (!action && draftId !== previousId) return
  if (action === 'history' && !trackedSubmission.value && formDirty.value) {
    try {
      await flushAutosave()
      if (formDirty.value) throw new Error('报销内容尚未保存，请核对后再查看记录')
    } catch (error) {
      workspaceActionError.value = error instanceof Error ? error.message : '保存失败，请重试'
      return
    }
  }
  if (previousId && formDirty.value && !trackedSubmission.value) {
    confirmingDraftReload.value = true
    if (autosaveTimer) clearTimeout(autosaveTimer)
    try {
      await ElMessageBox.confirm(
        '本页有尚未保存的修改。继续将读取服务器保存的内容，本页未保存的修改会被舍弃。',
        '读取已保存的报销',
        { confirmButtonText: '读取已保存内容', cancelButtonText: '保留本页', type: 'warning' },
      )
    } catch { return }
    finally { confirmingDraftReload.value = false; scheduleAutosave() }
  }
  if (disposed || scope !== sessionScope() || previousId !== drafts.currentDraft?.id
    || drafts.busy || saving.value || (action === 'history' && !canOpenHistory.value)) return
  if (autosaveTimer) clearTimeout(autosaveTimer)
  initializingWorkspace.value = true
  workspaceActionError.value = ''
  try {
    await drafts.loadDraft(draftId, true)
    if (disposed || scope !== sessionScope()) return
    const opened = drafts.currentDraft
    if (!opened || opened.id !== draftId || drafts.loadError) {
      if (action === 'return' && !drafts.loadError && !drafts.drafts.some((draft) => draft.id === draftId)) {
        historyOrigin.value = null
        await runWorkspaceInitialization(scope)
        return
      }
      workspaceActionError.value = drafts.loadError || '这笔报销已不可用，请重新加载报销列表'
      return
    }
    if (action === 'history' && previousId) {
      historyOrigin.value = { draftId: previousId, mobileStep: mobileStep.value, pendingApproval: pendingNewApproval.value }
    } else if (action === 'return') {
      historyOrigin.value = null
    }
    if (action) pendingNewApproval.value = null
    drafts.mutationError = ''
    hydrate(opened)
    try {
      await submission.restore(opened.id, opened.status === 'LOCKED'
        ? { discoverByDraft: true, expectedRevision: opened.revision } : undefined)
    } catch { /* The submission panel shows the recovery action. */ }
    if (action === 'return' && origin) {
      pendingNewApproval.value = origin.pendingApproval
      mobileStep.value = origin.mobileStep
      await nextTick()
      scrollToWorkspaceStart()
    } else if (previousId !== draftId) {
      await returnToApprovalStep()
    }
  } finally {
    if (scope === sessionScope()) { initializingWorkspace.value = false; scheduleAutosave() }
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
    refreshingMaterialStatus.value = false
  }
}
function openHistory(): void {
  historyVisible.value = true
  void loadHistory()
}
async function loadHistory(): Promise<void> {
  if (!historyVisible.value || historyLoading.value) return
  const scope = sessionScope()
  const request = new AbortController()
  historyRequest = request
  historyRecords.value = []
  historyLoading.value = true
  historyError.value = ''
  try {
    const records = await listReimbursementSubmissionHistory({
      signal: request.signal, expectedDepartment: auth.session?.selectedDepartment ?? undefined,
    })
    if (request.signal.aborted || scope !== sessionScope()) return
    historyRecords.value = records
  } catch (error) {
    if (!request.signal.aborted && scope === sessionScope()) {
      historyError.value = apiErrorMessage(error, '报销记录加载失败，请重试')
    }
  } finally {
    if (historyRequest === request) {
      historyRequest = undefined
      historyLoading.value = false
    }
  }
}
function chooseHistoryRecord(draftId: string): void {
  historyVisible.value = false
  void openSavedReimbursement(draftId, 'history')
}
function returnFromHistory(): void {
  if (historyOrigin.value) void openSavedReimbursement(historyOrigin.value.draftId, 'return')
}

async function replaceOutdatedForm(): Promise<void> {
  if (!canReplaceOutdatedForm.value || replacingTemplate.value || drafts.busy) return
  const draftId = drafts.currentDraft?.id
  if (!draftId) return
  const scope = sessionScope()
  replacingTemplate.value = true
  try {
    await ElMessageBox.confirm(
      '将打开新版 OA 表单。旧记录和已上传材料会保留，但本页内容不会自动转入新表单，需要重新填写和上传。',
      '按新表单重新填写',
      { confirmButtonText: '重新填写', cancelButtonText: '返回查看', type: 'warning' },
    )
    if (disposed || sessionScope() !== scope || drafts.currentDraft?.id !== draftId
      || !canReplaceOutdatedForm.value || drafts.busy) return
    await drafts.loadDraft(draftId)
    if (disposed || sessionScope() !== scope) return
    const latest = drafts.currentDraft
    if (!latest || latest.id !== draftId || drafts.loadError) {
      throw new Error(drafts.loadError || '无法核对原报销状态，请重试')
    }
    if (latest.status === 'LOCKED') {
      hydrate(latest)
      try {
        await submission.restore(draftId, { discoverByDraft: true, expectedRevision: latest.revision })
      } catch { /* The submission panel shows the recovery action. */ }
      return
    }
    if (!canReplaceOutdatedForm.value || drafts.busy) return
    initializingWorkspace.value = true
    await openEditableReimbursement()
    if (!disposed && sessionScope() === scope) await returnToApprovalStep()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') {
      ElMessage.error(error instanceof Error ? error.message : '新报销准备失败')
    }
  } finally {
    replacingTemplate.value = false
    if (sessionScope() === scope) initializingWorkspace.value = false
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
  const requestedSelections = [selection]
  if (autosaveTimer) clearTimeout(autosaveTimer)
  relatedApprovalVerificationError.value = ''
  verifyingRelatedApprovals.value = true
  selectedRelatedApprovals.value = requestedSelections
  bindingApprovalDepartment.value = true
  departmentBindingError.value = ''
  try {
    if (auth.status === 'authenticated' && drafts.currentDraft && inputDirty.value) {
      // Preserve edits in the previous department, but never attach the newly
      // selected approval to that department while its destination is unresolved.
      await flushAutosave({ includeRelated: false })
    }
    const previousScope = sessionScope()
    const resolution = await auth.selectDepartmentFromTravelApproval(
      selection,
      selectedDepartmentId,
    )
    if (resolution.selectionRequired || !resolution.selectedDepartment) {
      pendingDepartmentApproval.value = selection
      selectedDepartmentChoiceId.value = ''
      return
    }
    pendingDepartmentApproval.value = null
    selectedDepartmentChoiceId.value = ''
    if (!drafts.currentDraft || sessionScope() !== previousScope) {
      initializedScope = ''
      await initializeWorkspace(true)
    }
    const opened = drafts.currentDraft
    if (!opened) throw new Error(initializationError.value || '报销表单加载失败，请重试')
    const alreadyLinked = opened.relatedApprovals.some(
      (item) => item.processInstanceId === selection.processInstanceId,
    )
    if (alreadyLinked) {
      acceptAuthoritativeRelatedApprovals()
      drafts.mutationError = ''
      saveError.value = ''
      return
    }
    if (!['DRAFT', 'REVIEW_READY'].includes(opened.status)) {
      if (canStartNewReimbursement.value) pendingNewApproval.value = selection
      return
    }
    if (opened.relatedApprovals.length) {
      acceptAuthoritativeRelatedApprovals()
      ElMessage.warning('已打开这笔部门草稿并保留原有关联，请在出差审批中核对后再调整。')
      return
    }
    await verifyRelatedApprovals(requestedSelections)
  } catch (error) {
    acceptAuthoritativeRelatedApprovals()
    verifyingRelatedApprovals.value = false
    const message = error instanceof Error
      ? error.message
      : '无法读取出差审批的所在部门，请重试'
    if (auth.status === 'authenticated' && !drafts.currentDraft) initializationError.value = message
    else if (!saveError.value && !saveRequiresReload.value) departmentBindingError.value = message
  } finally {
    bindingApprovalDepartment.value = false
    if (!pendingDepartmentApproval.value) verifyingRelatedApprovals.value = false
    scheduleAutosave()
  }
}
function updateRelatedApprovals(
  selections: ReimbursementRelatedApprovalSelection[],
): void {
  const firstSelection = selections.length === 1 && selectedRelatedApprovals.value.length === 0
    ? selections[0]
    : undefined
  if (!firstSelection || selectableDepartments.value.length <= 1) {
    void persistRelatedApprovalsImmediately(selections)
    return
  }
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === firstSelection.processInstanceId,
  )
  const currentDepartmentId = auth.session?.selectedDepartment?.id
  if (candidate?.originatorDepartmentId === currentDepartmentId) {
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
  selectedDepartmentChoiceId.value = ''
  acceptAuthoritativeRelatedApprovals()
  verifyingRelatedApprovals.value = false
  relatedApprovalVerificationError.value = ''
  scheduleAutosave()
}
async function selectMobileStep(step: number): Promise<void> {
  if (!props.mobile || step < 0 || step >= mobileSteps.length) return
  mobileStep.value = step
  await nextTick()
  if (mobileStep.value !== step) return
  document.querySelector<HTMLElement>(`#reimbursement-step-${step} .mobile-step-heading`)
    ?.scrollIntoView?.({ block: 'start' })
}
async function advanceMobileStep(): Promise<void> {
  if (mobileStep.value === 0 && !selectedRelatedApprovals.value.length) {
    ElMessage.warning('请至少关联一张已通过的出差审批')
    return
  }
  if (mobileStep.value < mobileSteps.length - 1) {
    await selectMobileStep(mobileStep.value + 1)
    return
  }
  await confirmAndSubmit()
}
async function handleMobilePrimaryAction(): Promise<void> {
  if (mobileStep.value === mobileSteps.length - 1 && canReplaceOutdatedForm.value) {
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
    mobileStep.value = 2
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
  refreshingServiceStatus.value = true
  try {
    const results = await Promise.allSettled([
      auth.refreshPublicConfig(),
      ...(refreshProgress ? [submission.pollNow()] : [health.check()]),
    ])
    const failed = results.find((result) => result.status === 'rejected')
    if (failed?.status === 'rejected') throw failed.reason
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '服务状态刷新失败，请重试')
  } finally { refreshingServiceStatus.value = false }
}
onMounted(async () => { void health.check(); await auth.bootstrap(); await initializeWorkspace() })
watch(() => sessionScope(), () => {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  pendingNewApproval.value = null
  historyVisible.value = false
  historyOrigin.value = null
  workspaceActionError.value = ''
  deferredMaterialRevision.value = null
  if (sessionScope()) void initializeWorkspace()
  else { initializedScope = ''; initializationScope = '' }
})
watch(() => expenseItemsCard.value?.hasOpenMaterialEditor, (open) => {
  if (open === false && deferredMaterialRevision.value === drafts.currentDraft?.id) {
    void reconcileMaterialRevision()
  }
})
watch([currentFormInput, selectedRelatedApprovals], scheduleAutosave, { deep: true })
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
  if (drafts.currentDraft && drafts.currentDraft.status !== 'LOCKED' && !trackedSubmission.value && formDirty.value) {
    event.preventDefault()
    event.returnValue = ''
  }
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
onMounted(() => window.addEventListener('beforeunload', warnBeforeUnload))
onBeforeUnmount(() => {
  disposed = true
  historyRequest?.abort()
  if (calculationTimer) clearTimeout(calculationTimer)
  if (autosaveTimer) clearTimeout(autosaveTimer)
  mobileFooterResizeObserver?.disconnect()
  window.removeEventListener('beforeunload', warnBeforeUnload)
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
            <el-button
              v-if="auth.status === 'authenticated' && historyOrigin"
              :icon="ArrowLeft"
              :loading="initializingWorkspace"
              :disabled="submission.submitting || refreshingServiceStatus"
              @click="returnFromHistory"
            >
              返回报销
            </el-button>
            <button
              v-else-if="auth.status === 'authenticated' && !initializingWorkspace && canOpenHistory"
              type="button"
              class="hero-link"
              aria-haspopup="dialog"
              @click="openHistory"
            >
              报销记录
            </button>
            <RouterLink
              v-if="auth.isAdmin"
              :to="props.mobile ? '/m/settings' : '/admin/settings'"
              class="hero-link"
            >
              系统设置
            </RouterLink>
          </div>
        </div>
        <div class="hero-title">
          <h1 id="page-title">
            {{ auth.appTitle }}
          </h1>
          <span
            v-if="auth.status === 'authenticated' && !initializingWorkspace && !formReadOnly && !saveError"
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
          {{ props.mobile
            ? '关联审批、核对补助、整理材料，最后统一提交 OA。'
            : '关联审批、核对补助和费用，自动生成报销单并提交 OA。' }}
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
      v-else-if="auth.status === 'department_required'"
      shadow="never"
      class="content-card"
    >
      <template #header>
        <strong>选择本次出差申请</strong>
      </template>
      <p class="field-help">
        请选择本次报销对应的已通过出差审批。系统优先匹配审批的发起部门，无法匹配当前部门时再请你确认。
      </p>
      <TravelApprovalSelector
        :model-value="selectedRelatedApprovals"
        :mobile="props.mobile"
        :busy="bindingApprovalDepartment"
        single
        @update:model-value="chooseTravelApprovalForDepartment"
      />
      <el-alert
        v-if="departmentBindingError"
        :title="departmentBindingError"
        type="error"
        :closable="false"
        show-icon
        class="workspace-alert"
      />
    </el-card>
    <template v-else-if="auth.status === 'authenticated'">
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
          v-if="initializationError || departmentBindingError"
          :title="initializationError || departmentBindingError"
          type="error"
          :closable="false"
          class="workspace-alert"
        >
          <template #default>
            <el-button
              v-if="initializationError"
              link
              type="primary"
              @click="drafts.currentDraft ? openSavedReimbursement(drafts.currentDraft.id) : initializeWorkspace(true)"
            >
              重新加载
            </el-button>
          </template>
        </el-alert>
        <el-alert
          v-if="workspaceActionError"
          :title="workspaceActionError"
          type="error"
          :closable="false"
          class="workspace-alert"
        />
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
          v-if="drafts.currentDraft && reimbursementOptionsUnavailable"
          :title="templateNeedsAdmin ? '请联系管理员确认 OA 表单' : '暂时无法读取报销表单'"
          :description="drafts.reimbursementOptionsError"
          type="warning"
          :closable="false"
          class="workspace-alert"
        >
          <el-button
            type="primary"
            :loading="drafts.loadingReimbursementOptions"
            @click="refreshFormOptions"
          >
            {{ templateNeedsAdmin ? '重新检查表单' : '刷新表单与审批' }}
          </el-button>
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
            v-if="draftExpired && !trackedSubmission"
            title="本次填写内容已过期"
            description="可以开始新的报销，请重新填写并上传材料。"
            type="warning"
            :closable="false"
            class="workspace-alert"
          >
            <el-button
              type="primary"
              :disabled="drafts.busy || reimbursementOptionsUnavailable"
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
              <p>当前记录不能继续保存或提交。请按新表单重新填写；原记录和已上传材料仍会保留。</p>
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
            v-if="submissionServiceReason"
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
            v-if="saveError || saveRequiresReload"
            :title="saveRequiresReload ? '报销内容已更新，请核对最新报销内容' : '保存失败，内容仍保留在本页'"
            type="error"
            :closable="false"
            show-icon
            class="workspace-alert"
            data-testid="autosave-error"
          >
            <div
              v-if="saveError"
              class="save-recovery-detail"
            >
              {{ saveError }}
            </div>
            <el-button
              v-if="saveRequiresReload || (saveError && formReadOnly && !trackedSubmission)"
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
                  <p>后续审批进度可在钉钉查看。</p>
                </div>
              </div>
              <div class="receipt-amount">
                <span>报销合计</span>
                <strong>¥{{ drafts.currentDraft.totals.totalAmount }}</strong>
              </div>
            </header>
            <dl class="receipt-metrics">
              <div><dt>票据金额</dt><dd>¥{{ drafts.currentDraft.totals.expenseTotal }}</dd></div>
              <div><dt>出差补助</dt><dd>¥{{ drafts.currentDraft.totals.subsidyTotal }}</dd></div>
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
                  v-if="props.mobile && !historyOrigin"
                  type="primary"
                  :disabled="drafts.busy || reimbursementOptionsUnavailable"
                  @click="newReimbursement"
                >
                  返回报销
                </el-button>
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
                  :preview-disabled-reason="previewDisabledReason"
                  readonly
                  preview-only
                />
                <el-button
                  v-if="!props.mobile && !historyOrigin"
                  type="primary"
                  :disabled="drafts.busy || reimbursementOptionsUnavailable"
                  @click="newReimbursement"
                >
                  返回报销
                </el-button>
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
                <dt>所属公司</dt><dd>{{ receiptDetails.companyLabel }}</dd>
              </div>
              <div v-if="receiptDetails?.budgetLabel">
                <dt>预算代码 / 项目</dt><dd>{{ receiptDetails.budgetLabel }}</dd>
              </div>
              <div v-if="receiptDetails?.travelTypeLabel">
                <dt>出差类别</dt><dd>{{ receiptDetails.travelTypeLabel }}</dd>
              </div>
            </dl>
            <div
              v-if="drafts.currentDraft.relatedApprovals.length"
              class="receipt-approvals"
            >
              <h3>关联出差审批</h3>
              <ul>
                <li
                  v-for="approval in drafts.currentDraft.relatedApprovals"
                  :key="approval.processInstanceId"
                >
                  <strong>{{ approval.title }}</strong>
                  <small>{{ approval.startDate }} 至 {{ approval.endDate }}<template v-if="approval.businessId"> · {{ approval.businessId }}</template></small>
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
              <el-button
                v-if="!historyOrigin"
                :disabled="drafts.busy || reimbursementOptionsUnavailable"
                @click="newReimbursement"
              >
                返回报销
              </el-button>
            </template>
          </el-alert>
          <section
            v-else-if="submission.activeDraftId === drafts.currentDraft.id && submission.submission?.status === 'MANUAL_REVIEW'"
            class="submission-status manual-review-panel"
            aria-labelledby="manual-review-title"
            aria-live="polite"
            data-testid="submission-status"
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
            <div class="manual-review-actions">
              <el-button
                v-if="submission.submission.processInstanceId"
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
            class="submission-status"
            aria-live="polite"
            data-testid="submission-status"
          >
            <el-alert
              :title="submission.progressLabel || '提交需要处理'"
              :description="submission.errorMessage || undefined"
              :type="submissionAlertType"
              :closable="false"
              show-icon
            />
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
            <div class="submission-status-actions">
              <el-button
                v-if="canStartNewReimbursement"
                type="primary"
                :disabled="drafts.busy || reimbursementOptionsUnavailable"
                @click="newReimbursement"
              >
                重新填写
              </el-button>
              <el-button
                v-if="submission.submission && !submission.terminal"
                :loading="refreshingServiceStatus"
                :disabled="submission.submitting"
                @click="refreshSubmissionService(true)"
              >
                刷新提交进度
              </el-button>
              <el-button
                v-if="submission.requestError && !submission.submission && submission.requestAction"
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
            <el-button
              :loading="submission.polling"
              @click="retrySubmissionRecovery"
            >
              重新查找提交记录
            </el-button>
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
              <h2>关联出差审批</h2>
              <p>先选择本次报销对应的审批，报销范围和补助将自动生成。</p>
            </header>
            <el-card
              shadow="never"
              class="content-card reimbursement-card"
              data-testid="reimbursement-basics-card"
            >
              <template #header>
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
                  {{ auth.session?.selectedDepartment?.name }}
                </el-descriptions-item>
              </el-descriptions>

              <TravelApprovalSelector
                ref="approvalSelector"
                :model-value="selectedRelatedApprovals"
                :mobile="props.mobile"
                :linked-approvals="drafts.currentDraft.relatedApprovals"
                :readonly="formReadOnly"
                :busy="drafts.processingFiles || verifyingRelatedApprovals || bindingApprovalDepartment"
                @update:model-value="updateRelatedApprovals"
              />
              <section
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
            class="editor-fieldset"
            :disabled="formReadOnly || drafts.processingFiles"
          >
            <section
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
                <p>所属公司、预算、类别和日期来自已关联审批，只需核对补助。</p>
              </header>
              <TripSubsidyCard
                :mobile="props.mobile"
                :readonly="formReadOnly"
                :approvals="selectedSubsidyApprovals"
                :approval-trip-type="selectedSubsidyTripType"
              />
            </section>
            <section
              v-show="!props.mobile || mobileStep === 2"
              id="reimbursement-step-2"
              class="mobile-step-panel"
              data-testid="mobile-material-step"
            >
              <header
                v-if="props.mobile"
                class="mobile-step-heading"
              >
                <h2>费用与材料</h2>
                <p>仅申请出差补助时无需添加费用或上传材料；其他费用可从文件管理器或系统相册添加。</p>
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
            v-show="!props.mobile || mobileStep === 3"
            id="reimbursement-step-3"
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
            <ExpenseSummaryCard
              :mobile="props.mobile"
              :readonly="formReadOnly"
              :preview-disabled-reason="previewDisabledReason"
              :before-preview="props.mobile || formReadOnly ? undefined : flushAutosave"
            >
              <template #submission>
                <div
                  v-if="!formReadOnly && (drafts.pendingUploads.length || missingMaterialItems.length || materialSubmissionIssues.length)"
                  class="material-checklist"
                  data-testid="material-checklist"
                  role="status"
                >
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
                <strong>{{ expense.missingCalendarYears.length ? '待核对' : `¥${expense.displayTotal}` }}</strong>
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
                  :loading="mobileStep === 3 && (canReplaceOutdatedForm
                    ? replacingTemplate
                    : submitFlowPending || submission.submitting)"
                  :disabled="mobileStep === 3 && (canReplaceOutdatedForm
                    ? drafts.busy
                    : !formReadOnly && Boolean(submissionButtonReason))"
                  :title="mobileStep === 3 && !formReadOnly ? submissionButtonReason : ''"
                  @click="handleMobilePrimaryAction"
                >
                  {{ mobileStep === 3 && canReplaceOutdatedForm
                    ? '按新表单重新填写'
                    : formReadOnly ? '查看报销状态' : mobileNextLabel }}
                </el-button>
              </div>
            </footer>
          </div>
        </template>
      </template>
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
    <el-dialog
      v-model="historyVisible"
      title="报销记录"
      width="min(520px, calc(100% - 24px))"
      destroy-on-close
    >
      <p class="history-caption">
        {{ auth.session?.selectedDepartment?.name }} · 最近 10 笔
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
        <el-button @click="loadHistory">
          重新加载记录
        </el-button>
      </div>
      <p
        v-else-if="!historyRecords.length"
        class="history-state"
      >
        当前部门还没有报销提交记录。
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
            <strong><time :datetime="draft.lockedAt">{{ formatRecordTime(draft.lockedAt) }}</time></strong>
            <small v-if="draft.relatedApprovalCount">关联 {{ draft.relatedApprovalCount }} 张出差审批</small>
          </div>
          <span
            v-if="draft.id === drafts.currentDraft?.id"
            class="record-current"
          >当前记录</span>
          <el-button
            v-else
            link
            type="primary"
            :disabled="drafts.busy || saving"
            @click="chooseHistoryRecord(draft.id)"
          >
            查看提交
          </el-button>
        </li>
      </ul>
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
  </main>
</template>

<style scoped>
.hero-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.hero-intro { flex: 1; min-width: 0; }
.hero-meta { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.hero-links { display: flex; justify-content: flex-end; align-items: center; flex-wrap: wrap; gap: 4px 12px; }
.hero-links .el-button { margin: 0; }
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
.hero-link:hover { color: var(--el-color-primary-dark-2); text-decoration: underline; text-underline-offset: 3px; }
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
}
.reimbursement-page > .hero,
.reimbursement-page :is(.reimbursement-card, .summary-card) { margin-block: 0; }
.save-recovery-detail { margin: 4px 0; }
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
.record-row .el-button { flex: none; min-height: 32px; }
.record-current { flex: none; color: var(--el-text-color-secondary); font-size: 13px; }
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
.receipt-approvals ul { display: grid; gap: 6px; padding: 0; margin: 0; list-style: none; }
.receipt-approvals li { display: grid; gap: 3px; overflow-wrap: anywhere; }
.receipt-approvals li strong { color: var(--el-text-color-primary); font-size: 14px; font-weight: 550; }
.receipt-approvals li small { color: var(--el-text-color-secondary); font-size: 12px; }
.receipt-attachment-summary { margin: 14px 0 0; }
.submitted-receipt-toolbar > .receipt-attachment-summary { margin: 6px 0 0; }
.department-choice-copy { margin: 0 0 16px; color: var(--el-text-color-regular); line-height: 1.7; }
.department-choice-select { width: 100%; }
.identity-grid { margin-bottom: 0; }
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
.related-approval-verification-error { margin-bottom: 12px; }
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
.submission-actions, .submission-status-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
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
.manual-review-panel {
  padding: clamp(16px, 3vw, 24px);
  border: 1px solid var(--el-color-warning-light-7);
  border-radius: 12px;
  background: var(--el-bg-color);
}
.manual-review-heading { display: flex; align-items: flex-start; gap: 12px; }
.manual-review-icon {
  flex: none;
  width: 36px;
  height: 36px;
  border-radius: 9px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
  font-size: 19px;
}
.manual-review-heading h2 { margin: 0; font-size: 18px; line-height: 1.4; }
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
.manual-review-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.manual-review-actions .el-button { margin: 0; }
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
.page-shell--mobile :is(.submitted-receipt, .manual-review-panel, .template-recovery-panel) { padding: var(--workspace-card-padding); }
.page-shell--mobile.page-shell--submitted { padding-bottom: 24px; }
.page-shell--mobile .receipt-heading { align-items: flex-start; flex-direction: column; gap: 12px; }
.page-shell--mobile .receipt-heading h2 { font-size: 19px; }
.page-shell--mobile .receipt-amount { text-align: left; }
.page-shell--mobile .receipt-info { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.page-shell--mobile .submitted-receipt-toolbar { display: block; }
.page-shell--mobile .submitted-receipt-actions > * { flex: 1 1 120px; }
.page-shell--mobile .submitted-receipt-actions :deep(.el-button) { width: 100%; min-height: 44px; margin: 0; }
.hero--mobile .hero-title { margin: 2px 0 6px; }
.hero--mobile h1 { font-size: 22px; }
.hero--mobile .eyebrow { font-size: 12px; letter-spacing: .04em; }
.hero--mobile .summary { font-size: 13px; line-height: 1.5; }
.hero--mobile .hero-actions { flex-shrink: 0; }
.mobile-step-nav {
  position: sticky;
  top: 0;
  z-index: 9;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
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
.mobile-step-nav button small { display: block; overflow: hidden; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
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
  .mobile-step-nav button small { font-size: 9px; }
}
</style>

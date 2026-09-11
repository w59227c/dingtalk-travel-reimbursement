<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ExpenseItemsCard from '@/components/reimbursement/ExpenseItemsCard.vue'
import ExpenseSummaryCard from '@/components/reimbursement/ExpenseSummaryCard.vue'
import TravelApprovalSelector from '@/components/reimbursement/TravelApprovalSelector.vue'
import TripSubsidyCard from '@/components/reimbursement/TripSubsidyCard.vue'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useHealthStore } from '@/stores/health'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'
import { isForeignExpense, type TripType } from '@/types/expenses'
import type {
  ReimbursementDraft,
  ReimbursementDraftInput,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
  ReimbursementSubmissionStatus,
} from '@/types/reimbursements'
import { isActiveProof, missingExpenseMaterials, needsMaterialConfirmation, requiresPaymentProof } from '@/utils/expenseProofs'
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
const mobileStep = ref(0)
const mobileSteps = ['关联审批', '范围补助', '费用材料', '核对提交'] as const
const companyValue = ref('')
const budgetCodeValue = ref('')
const selectedRelatedApprovals = ref<ReimbursementRelatedApprovalSelection[]>([])
const initializingWorkspace = ref(false)
const initializationError = ref('')
const departmentBindingError = ref('')
const bindingApprovalDepartment = ref(false)
const saving = ref(false)
const saveError = ref('')
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

const companyOptions = computed(() => drafts.reimbursementOptions?.companyOptions ?? [])
const budgetOptions = computed(() => drafts.reimbursementOptions?.budgetCodeOptions ?? [])
const companyLabel = computed(() => companyOptions.value.find((option) => option.value === companyValue.value)?.label ?? companyValue.value)
const budgetLabel = computed(() => budgetOptions.value.find((option) => option.value === budgetCodeValue.value)?.label ?? budgetCodeValue.value)
const selectedTravelTypeLabel = computed(() => {
  const first = selectedRelatedApprovals.value[0]
  if (!first) return ''
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === first.processInstanceId,
  )
  if (candidate) return candidate.travelTypeOption.label
  const linked = drafts.currentDraft?.relatedApprovals.find(
    (approval) => approval.processInstanceId === first.processInstanceId,
  )
  const profile = drafts.reimbursementOptions?.travelProfiles.find(
    (item) => item.profileKey === first.profileKey,
  )
  return mappedTravelTypeOption(profile, linked?.sourceTravelTypeValue)?.label ?? ''
})
const selectedSubsidyApprovals = computed(() => selectedRelatedApprovals.value.flatMap((selection) => {
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === selection.processInstanceId,
  )
  const linked = drafts.currentDraft?.relatedApprovals.find(
    (approval) => approval.processInstanceId === selection.processInstanceId,
  )
  const approval = candidate ?? linked
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
  const candidate = drafts.travelApprovals.find(
    (approval) => approval.processInstanceId === first.processInstanceId,
  )
  if (candidate?.subsidyTripType) return candidate.subsidyTripType
  const linked = drafts.currentDraft?.relatedApprovals.find(
    (approval) => approval.processInstanceId === first.processInstanceId,
  )
  const profile = drafts.reimbursementOptions?.travelProfiles.find(
    (item) => item.profileKey === first.profileKey,
  )
  return subsidyTripTypeForProfile(profile, linked?.sourceTravelTypeValue)
    ?? subsidyTripTypeForTravelLabel(selectedTravelTypeLabel.value)
})
const selectedTravelPeriod = computed(() => {
  const periods = groupOverlappingSubsidyTrips(expense.subsidyTrips)
  if (!periods.length) return ''
  const labels = periods.map((item) => item.startDate === item.endDate
    ? item.startDate
    : `${item.startDate} 至 ${item.endDate}`)
  return labels.length === 1 ? labels[0]! : `共 ${labels.length} 个时间段：${labels.join('；')}`
})
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
const canReplaceOutdatedForm = computed(() => templateMismatch.value && Boolean(drafts.currentDraft
  && ['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status))
  && !trackedSubmission.value && !submitFlowPending.value && !initializingWorkspace.value)
const formReadOnly = computed(() => !drafts.currentDraft || submitFlowPending.value
  || ['LOCKED', 'EXPIRED'].includes(drafts.currentDraft.status) || trackedSubmission.value
  || templateMismatch.value || reimbursementOptionsUnavailable.value)
const formReadOnlyReason = computed(() => {
  if (!drafts.currentDraft) return '正在准备报销表单'
  if (submitFlowPending.value) return '正在提交，请稍候'
  if (drafts.currentDraft.status === 'EXPIRED') return '本次填写内容已过期，请重新填写'
  if (drafts.currentDraft.status === 'LOCKED' || trackedSubmission.value) return '本次报销内容已锁定，请查看提交进度'
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
      trips: expense.subsidyTrips.map((item) => ({ ...item })),
    },
    items: expense.buildDraftExpenseItems(),
    dismissedOcrFileIds: [...expense.dismissedOcrFileIds],
  }
})
const inputDirty = computed(() => inputSignature(currentFormInput.value) !== savedInputSignature.value)
const relatedDirty = computed(() => relatedSignature(selectedRelatedApprovals.value) !== savedRelatedSignature.value)
const formDirty = computed(() => inputDirty.value || relatedDirty.value)
const saveLabel = computed(() => trackedSubmission.value && submission.succeeded ? 'OA 已成功发起'
  : trackedSubmission.value && submission.status === 'QUEUED' ? '已排队，内容已锁定'
  : trackedSubmission.value || drafts.currentDraft?.status === 'LOCKED' ? '内容已锁定'
  : saveError.value ? '保存失败，内容仍保留在本页'
  : saving.value ? '正在保存…' : formDirty.value ? '等待保存…' : '已保存')
const submissionServiceReason = computed(() => submission.oaSubmissionEnabled === false
  ? (trackedSubmission.value || drafts.currentDraft?.status === 'LOCKED'
      ? 'OA提交服务未开启，请保留本次提交并稍后核对' : 'OA提交服务未开启，可继续填写')
  : submission.oaSubmissionEnabled === null ? '正在确认 OA 提交服务状态' : '')
const submissionButtonReason = computed(() => formReadOnlyReason.value || submissionServiceReason.value
  || (drafts.busy ? '请等待材料处理完成' : '')
  || (pendingMaterialFiles.value.length ? `还有 ${pendingMaterialFiles.value.length} 份材料待确认用途` : '')
  || (missingMaterialItems.value.length ? `还有 ${missingMaterialItems.value.length} 笔费用需补材料` : ''))
const mobileNextLabel = computed(() => mobileStep.value === mobileSteps.length - 1
  ? '提交 OA'
  : `下一步：${mobileSteps[mobileStep.value + 1]}`)
const missingMaterialItems = computed(() => expense.sortedItems.map((item) => ({ item, missing: missingExpenseMaterials(item, drafts.files) }))
  .filter((entry) => entry.missing.length))
const pendingMaterialFiles = computed(() => drafts.files.filter(needsMaterialConfirmation))
const previewDisabledReason = computed(() => expense.itemReadinessError
  || (!budgetCodeValue.value ? '请先选择预算代码' : '')
  || (props.mobile && (saving.value || formDirty.value) ? '正在保存当前内容，请稍候' : '')
  || (submitFlowPending.value ? '正在提交，请稍候' : ''))
const unresolvedOcrFiles = computed(() => drafts.files.filter((file) =>
  file.status === 'ACTIVE' && file.role === 'EXPENSE_SOURCE'
  && !expense.items.some((item) => item.sourceFileId === file.id)
  && !expense.dismissedOcrFileIds.includes(file.id)))
const submissionProgress: ReimbursementSubmissionStatus[] = [
  'QUEUED', 'VALIDATING', 'GENERATING_EXCEL', 'UPLOADING', 'OA_CREATING', 'VERIFYING', 'SUBMITTED',
]
const submissionProgressPercentage = computed(() => {
  if (submission.terminal) return 100
  const index = submissionProgress.indexOf(submission.status!)
  return index < 0 ? 0 : Math.round(index / (submissionProgress.length - 1) * 100)
})
const submissionAlertType = computed(() => submission.status === 'SUBMITTED' ? 'success'
  : submission.status === 'FAILED_FINAL' ? 'error'
    : submission.status === 'MANUAL_REVIEW' || submission.status === 'FAILED_RETRYABLE' ? 'warning' : 'info')
const canStartNewReimbursement = computed(() => submission.status === 'SUBMITTED'
  || submission.status === 'FAILED_FINAL')

function sessionScope(): string {
  const user = auth.session?.user.userId
  const department = auth.session?.selectedDepartment?.id
  return auth.status === 'authenticated' && user && department ? `${user}:${department}` : ''
}
function inputSignature(input: ReimbursementDraftInput): string {
  // Project is server-derived from budget, never a second employee input.
  return JSON.stringify({
    ocrDispositionVersion: input.ocrDispositionVersion,
    companyValue: input.companyValue,
    budgetCodeValue: input.budgetCodeValue,
    trip: input.trip,
    trips: input.trips,
    editingState: input.editingState,
    items: input.items,
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
async function reconfirmRelatedApprovals(): Promise<void> {
  try {
    await flushAutosave()
    const selections = JSON.parse(JSON.stringify(selectedRelatedApprovals.value)) as ReimbursementRelatedApprovalSelection[]
    await drafts.saveRelatedApprovals(selections)
    savedRelatedSignature.value = relatedSignature(selections)
    acceptDerivedAccounting()
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : '出差审批核验失败，请重试'
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
function hydrate(draft: ReimbursementDraft): void {
  companyValue.value = draft.input.companyValue
  budgetCodeValue.value = draft.input.budgetCodeValue
  expense.hydrateFromDraft(draft, drafts.files)
  selectedRelatedApprovals.value = draft.relatedApprovals.map(approvalSelection)
  savedInputSignature.value = inputSignature(draft.input)
  savedRelatedSignature.value = relatedSignature(selectedRelatedApprovals.value)
  saveError.value = ''
}
async function createBlankReimbursement(): Promise<void> {
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
  })
  // Keep the previous form visible and recoverable if creation fails.
  expense.reset()
  submission.abort()
  hydrate(created)
}
async function runWorkspaceInitialization(scope: string): Promise<void> {
  initializingWorkspace.value = true
  initializationError.value = ''
  try {
    await Promise.all([expense.loadCategories(), drafts.loadReimbursementOptions(), drafts.loadDrafts()])
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
      await createBlankReimbursement()
    }
    if (drafts.reimbursementOptionsError) initializationError.value = drafts.reimbursementOptionsError
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
  if (disposed || !sessionScope() || initializingWorkspace.value || formReadOnly.value || drafts.busy || !formDirty.value) return
  autosaveTimer = setTimeout(() => { void flushAutosave().catch(() => undefined) }, 600)
}
async function flushAutosave(): Promise<void> {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  if (savePromise) { await savePromise; return flushAutosave() }
  // Never enqueue a snapshot behind a file mutation: its references can already
  // be deleted by the time the queued save runs. The idle watcher retries using
  // the latest form; explicit preview/submit must not treat a skipped save as success.
  if (drafts.busy) throw new Error('请等待材料处理完成后重试')
  if (!drafts.currentDraft || !formDirty.value) return
  const draftId = drafts.currentDraft.id
  const scope = sessionScope()
  const fileOperationPause = new Error('请等待材料处理完成后重试')
  const run = async () => {
    saving.value = true
    saveError.value = ''
    try {
      // Keep typing enabled, save snapshots, and never hydrate an older response over live edits.
      while (formDirty.value && drafts.currentDraft?.id === draftId && sessionScope() === scope) {
        if (drafts.busy) throw fileOperationPause
        if (inputDirty.value) {
          const snapshot = JSON.parse(JSON.stringify(currentFormInput.value)) as ReimbursementDraftInput
          await drafts.saveDraft(snapshot)
          if (drafts.currentDraft?.id !== draftId || sessionScope() !== scope) return
          savedInputSignature.value = inputSignature(snapshot)
          acceptDerivedAccounting()
        }
        if (relatedDirty.value) {
          const selections = JSON.parse(JSON.stringify(selectedRelatedApprovals.value)) as ReimbursementRelatedApprovalSelection[]
          await drafts.saveRelatedApprovals(selections)
          if (drafts.currentDraft?.id !== draftId || sessionScope() !== scope) return
          savedRelatedSignature.value = relatedSignature(selections)
          acceptDerivedAccounting()
        }
      }
    } catch (error) {
      if (error !== fileOperationPause && drafts.currentDraft?.id === draftId && sessionScope() === scope) {
        saveError.value = drafts.mutationError || (error instanceof Error ? error.message : '自动保存失败')
      }
      throw error
    } finally { saving.value = false }
  }
  const running = run()
  savePromise = running
  try { await running } finally { if (savePromise === running) savePromise = null }
}
function validateSubmission(): string {
  if (!companyOptions.value.some((option) => option.value === companyValue.value)) return '请选择所属公司'
  if (!budgetOptions.value.some((option) => option.value === budgetCodeValue.value)) return '请选择预算代码'
  if (expense.includeSubsidy && expense.tripPayloads().length !== selectedRelatedApprovals.value.length) {
    return expense.policyInputError || '请完整确认每个补助项的出发和返回时段'
  }
  if (expense.categoryLoadError || !expense.categories.length) return '请先加载费用类别'
  if (!expense.items.length) return '请至少添加一条费用明细'
  if (expense.itemReadinessError) return expense.itemReadinessError
  if (unresolvedOcrFiles.value.length) return '请处理尚未加入费用明细的票据，或将其仅作为材料保留'
  if (pendingMaterialFiles.value.length) return '请先确认待处理材料的用途'
  if (!selectedRelatedApprovals.value.length) return '请至少关联一张已通过的出差审批'
  if (!drafts.files.some((file) => file.status === 'ACTIVE')) return '请上传报销材料'
  for (const item of expense.items) {
    if (item.category === 'lodging' && !item.hotelBillFileIds?.some((id) =>
      drafts.files.some((file) => file.id === id && isActiveProof(file, 'hotel_bill')),
    )) return `“${item.description || '住宿费用'}”缺少住宿明细，请补充后提交`
    if ((item.requiresItinerary || item.transportType === 'ride_hailing') && !item.itineraryFileIds?.some((id) =>
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
      '核对无误后将生成票据汇总 PDF 和报销单 Excel，并正式提交钉钉 OA。提交后内容将锁定。',
      '提交报销申请', { confirmButtonText: '确认提交', cancelButtonText: '返回检查', type: 'warning' },
    )
    await flushAutosave()
    const ready = await drafts.markReviewReady()
    await submission.submit(ready.id, ready.revision)
    ElMessage.success('正在生成材料并提交 OA')
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(submission.errorMessage || saveError.value
      || drafts.mutationError || (error instanceof Error ? error.message : '提交失败，请重试'))
  } finally { submitFlowPending.value = false; scheduleAutosave() }
}
async function newReimbursement(): Promise<void> {
  // Only a definitive outcome permits a new record; uncertain OA creation must
  // retain its existing submission identity until reconciliation completes.
  if (!canStartNewReimbursement.value) return
  initializingWorkspace.value = true
  try { await createBlankReimbursement() }
  catch (error) { ElMessage.error(error instanceof Error ? error.message : '新报销准备失败') }
  finally { initializingWorkspace.value = false }
}
async function replaceOutdatedForm(): Promise<void> {
  if (!canReplaceOutdatedForm.value || replacingTemplate.value || drafts.busy) return
  const draftId = drafts.currentDraft?.id
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
    initializingWorkspace.value = true
    await createBlankReimbursement()
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
  selectedRelatedApprovals.value = selection ? [selection] : []
  if (!selection) return
  bindingApprovalDepartment.value = true
  departmentBindingError.value = ''
  try {
    await auth.selectDepartmentFromTravelApproval(selection)
    initializedScope = ''
    await initializeWorkspace(true)
    let opened = drafts.currentDraft
    if (!opened) throw new Error(initializationError.value || '报销表单加载失败，请重试')
    const alreadyLinked = opened.relatedApprovals.some(
      (item) => item.processInstanceId === selection.processInstanceId,
    )
    if (alreadyLinked) return
    if (!['DRAFT', 'REVIEW_READY'].includes(opened.status)) {
      throw new Error('该部门有正在提交的报销，请先确认提交结果后再新建报销')
    }
    if (opened.relatedApprovals.length) {
      await createBlankReimbursement()
      opened = drafts.currentDraft
      if (!opened) throw new Error('新报销准备失败，请重试')
    }
    selectedRelatedApprovals.value = [selection]
    const updated = await drafts.saveRelatedApprovals([selection])
    hydrate(updated)
  } catch (error) {
    const message = error instanceof Error
      ? error.message
      : '无法读取出差审批的所在部门，请重试'
    departmentBindingError.value = message
    if (auth.status === 'authenticated') initializationError.value = message
  } finally {
    bindingApprovalDepartment.value = false
  }
}
function selectMobileStep(step: number): void {
  if (!props.mobile || step < 0 || step >= mobileSteps.length) return
  mobileStep.value = step
}
async function advanceMobileStep(): Promise<void> {
  if (mobileStep.value === 0 && !selectedRelatedApprovals.value.length) {
    ElMessage.warning('请至少关联一张已通过的出差审批')
    return
  }
  if (mobileStep.value < mobileSteps.length - 1) {
    mobileStep.value += 1
    return
  }
  await confirmAndSubmit()
}
async function focusMobileMaterial(itemId?: string): Promise<void> {
  if (props.mobile) {
    mobileStep.value = 2
    await nextTick()
  }
  expenseItemsCard.value?.focusMaterial(itemId)
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
    await auth.refreshPublicConfig()
    if (refreshProgress) await submission.pollNow()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '服务状态刷新失败，请重试')
  } finally { refreshingServiceStatus.value = false }
}
onMounted(async () => { void health.check(); await auth.bootstrap(); await initializeWorkspace() })
watch(() => sessionScope(), () => {
  if (autosaveTimer) clearTimeout(autosaveTimer)
  if (sessionScope()) void initializeWorkspace()
  else { initializedScope = ''; initializationScope = '' }
})
watch([currentFormInput, selectedRelatedApprovals], scheduleAutosave, { deep: true })
watch(() => drafts.busy, () => {
  // A file operation becoming idle should resume saving. Our own failed save
  // must keep the existing explicit-retry behavior, not retry every 600 ms.
  if (!saving.value) scheduleAutosave()
})
watch(budgetLabel, (label) => { expense.manualProjectText = label })
watch(selectedSubsidyApprovals, (approvals) => expense.syncSubsidyApprovals(approvals), {
  deep: true,
  immediate: true,
})
watch(selectedSubsidyTripType, (value) => {
  if (value && value !== 'overseas' && expense.trip.tripType !== value) expense.setTripType(value)
  if (value === 'overseas' && expense.includeSubsidy) expense.setSubsidyIncluded(false)
}, { immediate: true })
watch(() => [
  sessionScope(),
  expense.includeSubsidy,
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
  if (!formReadOnly.value && formDirty.value) { event.preventDefault(); event.returnValue = '' }
}
onMounted(() => window.addEventListener('beforeunload', warnBeforeUnload))
onBeforeUnmount(() => {
  disposed = true
  if (calculationTimer) clearTimeout(calculationTimer)
  if (autosaveTimer) clearTimeout(autosaveTimer)
  window.removeEventListener('beforeunload', warnBeforeUnload)
  submission.abort()
})
</script>

<template>
  <main
    class="page-shell"
    :class="{ 'page-shell--mobile': props.mobile }"
  >
    <section
      class="hero"
      :class="{ 'hero--mobile': props.mobile }"
      aria-labelledby="page-title"
    >
      <div>
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
        <h1 id="page-title">
          {{ auth.appTitle }}
        </h1>
        <p class="summary">
          {{ props.mobile
            ? '关联审批、核对补助、整理材料，最后统一提交 OA。'
            : '选预算、上传材料、核对费用，自动生成票据汇总和报销单并提交 OA。' }}
        </p>
      </div>
      <div class="hero-actions">
        <RouterLink
          v-if="auth.isAdmin"
          to="/admin/settings"
        >
          系统设置
        </RouterLink>
        <el-tag
          :type="health.available === true ? 'success' : health.available === false ? 'danger' : 'info'"
          round
        >
          {{ health.label }}
        </el-tag>
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
        请选择本次报销对应的已通过出差审批，系统将按审批中的所在部门自动填写，无需再选部门。
      </p>
      <TravelApprovalSelector
        :model-value="selectedRelatedApprovals"
        :mobile="props.mobile"
        :readonly="bindingApprovalDepartment"
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
          v-if="initializationError"
          :title="initializationError"
          type="error"
          :closable="false"
          class="workspace-alert"
        >
          <template #default>
            <el-button
              link
              type="primary"
              @click="initializeWorkspace(true)"
            >
              重新加载
            </el-button>
          </template>
        </el-alert>
        <template v-if="drafts.currentDraft">
          <nav
            v-if="props.mobile"
            class="mobile-step-nav"
            aria-label="报销填写步骤"
          >
            <button
              v-for="(step, index) in mobileSteps"
              :key="step"
              type="button"
              :class="{ 'is-active': mobileStep === index, 'is-done': mobileStep > index }"
              :aria-current="mobileStep === index ? 'step' : undefined"
              @click="selectMobileStep(index)"
            >
              <span>{{ index + 1 }}</span>
              <small>{{ step }}</small>
            </button>
          </nav>
          <section
            v-show="!props.mobile || mobileStep === 0"
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
                v-model="selectedRelatedApprovals"
                :mobile="props.mobile"
                :linked-approvals="drafts.currentDraft.relatedApprovals"
                :readonly="formReadOnly || drafts.processingFiles"
              />
              <el-alert
                v-if="drafts.currentDraft.relatedApprovals.length && !drafts.currentDraft.input.accountingSourceVerified && !formReadOnly"
                title="此报销需重新核验关联审批的所属公司和预算代码"
                type="warning"
                :closable="false"
                class="accounting-verification-alert"
              >
                <el-button
                  :disabled="drafts.busy || saving"
                  @click="reconfirmRelatedApprovals"
                >
                  重新确认出差审批
                </el-button>
              </el-alert>

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
                <el-descriptions
                  :column="props.mobile ? 1 : 2"
                  border
                  class="derived-accounting-grid"
                >
                  <el-descriptions-item label="所属公司">
                    {{ companyLabel || '选择出差审批后自动填入' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="预算代码 / 项目">
                    {{ budgetLabel || '选择出差审批后自动填入' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="出差类别">
                    {{ selectedTravelTypeLabel || '选择出差审批后自动填入' }}
                  </el-descriptions-item>
                  <el-descriptions-item label="出差日期">
                    {{ selectedTravelPeriod || '选择出差审批后自动填入' }}
                  </el-descriptions-item>
                </el-descriptions>
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
              <el-button
                v-if="canReplaceOutdatedForm"
                class="block-action"
                type="primary"
                :loading="replacingTemplate"
                :disabled="drafts.busy"
                @click="replaceOutdatedForm"
              >
                按新表单重新填写
              </el-button>
            </el-card>
          </section>
          <fieldset
            class="editor-fieldset"
            :disabled="formReadOnly || drafts.processingFiles"
          >
            <section
              v-show="!props.mobile || mobileStep === 1"
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
              class="mobile-step-panel"
              data-testid="mobile-material-step"
            >
              <header
                v-if="props.mobile"
                class="mobile-step-heading"
              >
                <h2>费用与材料</h2>
                <p>可从文件管理器或系统相册添加，失败文件可以单独重试。</p>
              </header>
              <ExpenseItemsCard
                ref="expenseItemsCard"
                :mobile="props.mobile"
                :readonly="formReadOnly"
              />
            </section>
          </fieldset>
          <section
            v-show="!props.mobile || mobileStep === 3"
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
              :preview-disabled-reason="previewDisabledReason"
              :before-preview="props.mobile ? undefined : flushAutosave"
            />
            <el-card
              shadow="never"
              class="content-card submission-card"
            >
              <div class="submission-actions">
                <div
                  role="status"
                  aria-live="polite"
                  data-testid="autosave-status"
                >
                  <span>{{ saveLabel }}</span><el-button
                    v-if="saveError"
                    link
                    type="primary"
                    @click="flushAutosave().catch(() => undefined)"
                  >
                    重试保存
                  </el-button>
                </div>
                <div class="primary-submit-area">
                  <p>OA 附件为两个文件：票据汇总.pdf（含行程单、付款凭证）＋报销单.xlsx。</p>
                  <el-button
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
              </div>
              <div
                v-if="!formReadOnly && (missingMaterialItems.length || pendingMaterialFiles.length)"
                class="material-checklist"
                data-testid="material-checklist"
                role="status"
              >
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
                <div
                  v-if="pendingMaterialFiles.length"
                  class="material-checklist-row"
                >
                  <span>{{ pendingMaterialFiles.length }} 份材料待确认用途</span>
                  <el-button
                    link
                    type="primary"
                    @click="focusMobileMaterial()"
                  >
                    去确认
                  </el-button>
                </div>
                <p>补齐后可提交 OA；你仍可继续编辑和预览报销单。</p>
              </div>
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
                v-if="saveError"
                :title="saveError"
                type="error"
                :closable="false"
                class="submission-status"
              />
              <section
                v-if="submission.activeDraftId === drafts.currentDraft.id && (submission.progressLabel || submission.errorMessage)"
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
                  :percentage="submissionProgressPercentage"
                  :status="submission.status === 'SUBMITTED' ? 'success' : submission.status === 'FAILED_FINAL' ? 'exception' : undefined"
                />
                <p v-if="submission.submission?.businessId">
                  审批编号：{{ submission.submission.businessId }}
                </p>
                <p v-if="submission.status === 'MANUAL_REVIEW'">
                  暂时无法确认提交结果，请联系管理员并提供提交编号 {{ submission.submission?.submissionId }}，不要重复发起报销。
                </p>
                <div class="submission-status-actions">
                  <el-button
                    v-if="submission.status === 'MANUAL_REVIEW' && submission.submission?.processInstanceId"
                    type="primary"
                    :loading="submission.submitting"
                    @click="submission.recheck()"
                  >
                    重新核对（不会重复提交）
                  </el-button>
                  <a
                    v-if="submission.status === 'SUBMITTED' && submission.submission?.approvalUrl?.trim()"
                    :href="submission.submission.approvalUrl"
                    target="_blank"
                    rel="noopener noreferrer"
                    data-testid="approval-link"
                  ><el-button type="primary">打开钉钉 OA</el-button></a>
                  <el-button
                    v-if="canStartNewReimbursement"
                    @click="newReimbursement"
                  >
                    {{ submission.status === 'FAILED_FINAL' ? '重新填写' : '再报销一笔' }}
                  </el-button>
                  <el-button
                    v-if="submission.submission && !submission.terminal"
                    :loading="refreshingServiceStatus"
                    @click="refreshSubmissionService(true)"
                  >
                    刷新提交进度
                  </el-button>
                  <el-button
                    v-if="submission.requestError && !submission.submission && submission.requestAction"
                    :loading="submission.submitting"
                    :disabled="submission.requestAction === 'retry' && submission.oaSubmissionEnabled !== true"
                    @click="submission.requestAction === 'retry' ? retrySameSubmission() : retrySubmissionRecovery()"
                  >
                    {{ submission.requestAction === 'retry' ? '重试本次提交' : '重新查找提交记录' }}
                  </el-button>
                </div>
              </section>
            </el-card>
          </section>
          <footer
            v-if="props.mobile"
            class="mobile-step-footer"
          >
            <div class="mobile-footer-total">
              <span>当前合计</span>
              <strong>¥{{ expense.displayTotal }}</strong>
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
                :loading="mobileStep === 3 && (submitFlowPending || submission.submitting)"
                :disabled="mobileStep === 3 && Boolean(submissionButtonReason)"
                :title="mobileStep === 3 ? submissionButtonReason : ''"
                @click="advanceMobileStep"
              >
                {{ mobileNextLabel }}
              </el-button>
            </div>
          </footer>
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
  </main>
</template>

<style scoped>
.hero-actions { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.eyebrow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.current-user-id {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  font-weight: 400;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}
.workspace-alert { margin-bottom: 18px; }
.identity-grid { margin-bottom: 0; }
.plain-fieldset, .editor-fieldset { min-width: 0; padding: 0; margin: 0; border: 0; }
.accounting-verification-alert { margin-top: 18px; }
.derived-accounting-section { margin-top: 22px; padding-top: 22px; border-top: 1px solid var(--el-border-color-lighter); }
.derived-accounting-heading h2 { margin: 0; color: var(--el-text-color-primary); font-size: 16px; }
.derived-accounting-heading p { margin: 6px 0 14px; color: var(--el-text-color-secondary); font-size: 13px; }
.derived-accounting-grid { margin-bottom: 12px; }
.derived-accounting-grid :deep(.el-descriptions__table) { table-layout: fixed; }
.derived-accounting-grid :deep(.el-descriptions__content) { overflow-wrap: anywhere; }
.submission-actions, .submission-status-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.primary-submit-area { text-align: right; }
.primary-submit-area p { color: var(--el-text-color-secondary); font-size: 13px; }
.submission-status { margin-top: 20px; }
.submission-status .el-progress { margin: 16px 0; }
.material-checklist { margin-top: 16px; padding: 14px 16px; border-radius: 10px; background: #fff8ed; color: #8b5a16; font-size: 13px; }
.material-checklist-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.material-checklist-row > span { min-width: 0; overflow-wrap: anywhere; }
.material-checklist-row .el-button { flex-shrink: 0; }
.material-checklist p { margin: 8px 0 0; color: #667085; }
.page-shell--mobile {
  width: min(100%, 560px);
  padding: 16px 12px 0;
}
.hero--mobile {
  align-items: center;
  margin-bottom: 14px;
}
.hero--mobile h1 { margin: 2px 0 6px; font-size: 22px; }
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
  margin: 0 -4px 14px;
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
.mobile-step-heading { padding: 2px 2px 12px; }
.mobile-step-heading h2 { margin: 0; font-size: 20px; }
.mobile-step-heading p { margin: 6px 0 0; color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.55; }
.page-shell--mobile :deep(.content-card) { border-radius: 16px; }
.page-shell--mobile :deep(.el-card__header) { padding: 14px; }
.page-shell--mobile :deep(.el-card__body) { padding: 14px; }
.page-shell--mobile :deep(.totals-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.page-shell--mobile .primary-submit-area .el-button { display: none; }
.mobile-step-footer {
  position: sticky;
  bottom: 0;
  z-index: 10;
  margin: 18px -12px 0;
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
@media (max-width: 640px) {
  .primary-submit-area { text-align: left; }
}
@media (max-width: 420px) {
  .page-shell--mobile { width: 100%; padding-inline: 8px; }
  .mobile-step-footer { margin-inline: -8px; }
  .mobile-step-nav button small { font-size: 9px; }
}
</style>

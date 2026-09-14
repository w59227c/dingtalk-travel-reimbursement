<script setup lang="ts">
import { Loading } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import ExpenseMaterialLinks from './ExpenseMaterialLinks.vue'
import ExpenseItinerarySuggestion from './ExpenseItinerarySuggestion.vue'

import {
  getReimbursementFileContent,
  requestReimbursementDraftFilePreviewTicket,
} from '@/api/reimbursements'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { ExpenseCategoryId, ExpenseItem } from '@/types/expenses'
import { isForeignExpense, isTaxiExpense } from '@/types/expenses'
import { isItineraryOcrResult } from '@/types/receipts'
import { receiptOcrResult } from '@/types/reimbursements'
import type {
  ReimbursementAttachmentKind,
  ReimbursementDraftFile,
  ReimbursementDraftFileRole,
} from '@/types/reimbursements'
import { downloadAndOpenDingTalkDocument } from '@/utils/dingtalk'
import { formatFileSize } from '@/utils/receiptFiles'
import {
  evidenceRailType,
  hasKnownNonRailEvidence,
  isActiveProof,
  isUnresolvedExpenseSourceFile,
  materialSubmissionBlockReason,
  needsMaterialConfirmation,
  requiresPaymentProof,
} from '@/utils/expenseProofs'
import { itineraryConfirmationWarnings, suggestItineraries } from '@/utils/itineraryMatching'
import { itineraryOptionPresentation } from '@/utils/itineraryPresentation'
import { centsToMoney, moneyToCents } from '@/utils/money'

const props = withDefaults(defineProps<{
  mobile?: boolean
  readonly?: boolean
}>(), {
  mobile: false,
  readonly: false,
})

const expense = useExpenseStore()
const drafts = useReimbursementDraftStore()
const auth = useAuthStore()
const durableExpenseInput = ref<HTMLInputElement | null>(null)
const durableAttachmentInput = ref<HTMLInputElement | null>(null)
const durableItineraryInput = ref<HTMLInputElement | null>(null)
const durablePaymentProofInput = ref<HTMLInputElement | null>(null)
const durableHotelBillInput = ref<HTMLInputElement | null>(null)
type ProofTarget = { itemId: string; draftId: string; departmentId: string; replaceId?: string; kind: 'payment_proof' | 'hotel_bill' }
const paymentTarget = ref<ProofTarget | null>(null)
const paymentUploadingItem = ref('')
const paymentErrors = reactive<Record<string, string>>({})
const proofPickerVisible = ref(false)
const proofPickerItemId = ref('')
const proofPickerSelection = ref<string[]>([])
const proofPickerKind = ref<'payment_proof' | 'hotel_bill'>('payment_proof')
const expandedItinerarySelection = ref(false)
const itineraryQuery = ref('')
const materialEditorVisible = ref(false)
const materialEditorFile = ref<ReimbursementDraftFile | null>(null)
const materialEditorKind = ref<ReimbursementAttachmentKind | 'expense'>('other')
const durableOperating = ref(false)
const retryingRecognition = ref<{
  fileId: string
  fileName: string
  generation: number
} | null>(null)
const durableErrors = reactive<Record<string, string>>({})
type BatchFileStatus = 'queued' | 'uploading' | 'uploaded' | 'recognizing' | 'done' | 'skipped' | 'failed'
interface BatchFile {
  key: string
  name: string
  source?: File
  role: ReimbursementDraftFileRole
  attachmentKind: ReimbursementAttachmentKind
  autoClassify: boolean
  target?: ProofTarget
  status: BatchFileStatus
  uploaded?: ReimbursementDraftFile
  recognized?: boolean
  error?: string
}
const batchFiles = ref<BatchFile[]>([])
const batchPhase = ref<'processing' | 'uploading' | 'recognizing' | 'done' | null>(null)
const batchNeedsOcr = ref(false)
let batchScope: DurableOperationScope | null = null
let batchPipelineId: number | null = null
let batchOriginalFileIds = new Set<string>()
const batchActive = computed(() => batchPhase.value !== null && batchPhase.value !== 'done')
const batchUploadedCount = computed(() => batchFiles.value.filter((file) => file.uploaded).length)
const batchRecognizedCount = computed(() => batchFiles.value.filter((file) => file.recognized).length)
const batchFailedCount = computed(() => batchFiles.value.filter((file) => file.status === 'failed').length)
const batchSkippedCount = computed(() => batchFiles.value.filter((file) => file.status === 'skipped').length)
const uploadOperationActive = computed(() => batchActive.value && batchFiles.value.some((file) =>
  ['queued', 'uploading'].includes(file.status)))
const visibleBatchFiles = computed(() => batchActive.value
  ? batchFiles.value
  : batchFiles.value.filter((file) => ['failed', 'skipped'].includes(file.status)))
const batchProgress = computed(() => {
  const stages = batchNeedsOcr.value ? 2 : 1
  const completed = batchFiles.value.reduce((sum, file) => sum
    + (['done', 'failed', 'skipped'].includes(file.status) ? stages
      : file.status === 'uploaded' || file.status === 'recognizing' ? 1 : 0), 0)
  return batchFiles.value.length ? Math.round(completed / (batchFiles.value.length * stages) * 100) : 0
})
const shouldPollDurableOcr = computed(() => Boolean(
  drafts.currentDraft
  && ['DRAFT', 'REVIEW_READY'].includes(drafts.currentDraft.status)
  && drafts.files.some((file) =>
    file.status === 'ACTIVE' && file.ocrStatus === 'RUNNING' && file.ocrStale !== true),
))
const batchStatusLabels: Record<BatchFileStatus, string> = {
  queued: '等待上传', uploading: '上传中', uploaded: '等待识别', recognizing: '识别中', done: '已完成', skipped: '重复，已跳过', failed: '需要处理',
}
let durableOperationGeneration = 0
let durableUnmounted = false
let activeDurableOperation: DurableOperationScope | null = null
const receiptPreviewVisible = ref(false)
const receiptPreviewUrl = ref('')
const receiptPreviewName = ref('')
const receiptPreviewKind = ref<'image' | 'pdf'>('image')
const previewLoading = ref(false)
let previewController: AbortController | null = null
let durableOcrPollTimer: number | null = null
const editorVisible = ref(false)
const editorRevision = ref(0)
const paymentExpenseContext = ref<{ fileId: string; draftId: string; departmentId: string } | null>(null)
const editor = reactive({
  id: '',
  category: '' as ExpenseCategoryId,
  date: '',
  description: '',
  amount: '',
  receiptCount: 1,
  transportType: 'other' as NonNullable<ExpenseItem['transportType']>,
  requiresItinerary: false,
  itineraryFileIds: [] as string[],
  itineraryAutoMatchDisabled: false,
  paymentProofFileIds: [] as string[],
  hotelBillFileIds: [] as string[],
  railType: 'unknown' as NonNullable<ExpenseItem['railType']>,
  originalCurrency: '',
  originalAmount: '',
  cnyAmountConfirmed: false,
})

const warningLabels: Record<string, string> = {
  MANUAL_REVIEW_REQUIRED: '需人工核对',
  MISSING_AMOUNT: '缺少金额',
  MISSING_DATE: '缺少日期',
  MISSING_DESCRIPTION: '缺少说明',
  MISSING_ROUTE: '缺少行程路线',
  LOW_CONFIDENCE: '识别置信度较低',
  LOW_OCR_CONFIDENCE: '识别置信度较低',
  QR_AMOUNT_REQUIRES_REVIEW: '金额来自二维码，请核对价税合计',
  QR_AMOUNT_MISMATCH: '二维码金额与票面金额不一致',
  QR_ISSUE_DATE_USED: '日期来自二维码开票日期，请核对发生日期',
  INVOICE_DATE_USED_AS_OCCURRENCE: '未识别到发生日期，当前使用开票日期',
  FOREIGN_CURRENCY_REQUIRES_CONFIRMATION: '请确认人民币报销金额',
  FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT: '请确认原票币种和人民币报销金额',
  MISSING_ITINERARY: '请补充对应行程单',
}
const durableRoleLabels: Record<ReimbursementDraftFileRole, string> = {
  EXPENSE_SOURCE: '票据/发票',
  ATTACHMENT_ONLY: '行程单/证明材料',
}
const attachmentKindLabels: Record<ReimbursementAttachmentKind, string> = {
  itinerary: '行程单', payment_proof: '付款凭证', hotel_bill: '住宿明细', other: '其他材料',
}

const categoryNames = computed<Record<string, string>>(() =>
  Object.fromEntries(expense.categories.map((item) => [item.id, item.name])),
)
const showLockedFileMetadata = computed(() => props.readonly && drafts.currentDraft?.status === 'LOCKED')
const durableFiles = computed(() => drafts.files.filter((file) =>
  file.status !== 'PURGED' || showLockedFileMetadata.value))
const activeRecognitionStatus = computed(() => {
  if (batchActive.value) return null
  const retrying = retryingRecognition.value
  if (retrying) {
    return {
      fileId: retrying.fileId,
      title: `正在重新识别“${retrying.fileName}”`,
      description: 'OCR 正在重新解析票据信息，完成后会更新识别结果；如明细已被人工修改，系统会保留你的人工修改。',
    }
  }
  const running = durableFiles.value.find((file) =>
    file.status === 'ACTIVE' && file.ocrStatus === 'RUNNING' && file.ocrStale !== true)
  return running
    ? {
        fileId: running.id,
        title: `正在识别“${running.name}”`,
        description: 'OCR 任务仍在处理中，页面会自动同步最新结果。',
      }
    : null
})
const hasVisibleDurableFiles = computed(() => durableFiles.value.some((file) =>
  !batchActive.value || batchOriginalFileIds.has(file.id),
))
const clearableDurableFiles = computed(() => durableFiles.value.filter((file) => ['ACTIVE', 'DELETING'].includes(file.status)))
const unlinkedDurableFiles = computed(() => durableFiles.value.filter((file) =>
  (!batchActive.value || batchOriginalFileIds.has(file.id))
  && !expense.items.some((item) => item.sourceFileId === file.id || item.itineraryFileIds?.includes(file.id)
    || item.paymentProofFileIds?.includes(file.id) || item.hotelBillFileIds?.includes(file.id)),
))
const blockingUnlinkedDurableFiles = computed(() => unlinkedDurableFiles.value.filter((file) =>
  Boolean(durableSubmissionBlockReason(file)),
))
const optionalUnlinkedDurableFiles = computed(() => unlinkedDurableFiles.value.filter((file) =>
  !durableSubmissionBlockReason(file),
))
const orderedUnlinkedDurableFiles = computed(() => [
  ...blockingUnlinkedDurableFiles.value,
  ...optionalUnlinkedDurableFiles.value,
])
const itineraryOptions = computed(() => durableFiles.value.filter((file) =>
  isActiveProof(file, 'itinerary'),
))
const itinerarySuggestions = computed(() => !batchActive.value
  ? suggestItineraries(expense.items, durableFiles.value) : [])
const editorItineraryOptions = computed(() => {
  const item = expense.items.find((entry) => entry.id === editor.id)
  const suggestion = itinerarySuggestions.value.find((entry) => entry.sourceFileId === item?.sourceFileId)
  const query = itineraryQuery.value.trim().toLowerCase()
  return itineraryOptions.value.map((file) => ({ file,
    recommended: suggestion?.itineraryFileId === file.id,
    ...itineraryOptionPresentation(file, expense.items, suggestion?.itineraryFileId === file.id ? suggestion : undefined),
  })).filter((option) => !query || option.searchText.toLowerCase().includes(query))
    .sort((a, b) => (Number(editor.itineraryFileIds.includes(b.file.id)) * 2 + Number(b.recommended))
      - (Number(editor.itineraryFileIds.includes(a.file.id)) * 2 + Number(a.recommended)))
})
const paymentProofOptions = computed(() => durableFiles.value.filter((file) => isActiveProof(file, 'payment_proof')))
const hotelBillOptions = computed(() => durableFiles.value.filter((file) => isActiveProof(file, 'hotel_bill')))
const currentProofOptions = computed(() => proofPickerKind.value === 'hotel_bill' ? hotelBillOptions.value : paymentProofOptions.value)
const foreignEditor = computed(() => Boolean(editor.originalCurrency && editor.originalCurrency.toUpperCase() !== 'CNY')
  || expense.items.find((item) => item.id === editor.id)?.requiresCnyConfirmation
  || editorEvidence.value?.type === 'foreign_receipt'
  || editorEvidence.value?.warnings.includes('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'))
const editorSource = computed(() => durableFiles.value.find((file) =>
  file.id === expense.items.find((item) => item.id === editor.id)?.sourceFileId,
))
const editorEvidence = computed(() => receiptOcrResult(editorSource.value))
const sourceRequiresItinerary = computed(() => editorEvidence.value?.requiresItinerary
  || editorEvidence.value?.transportType === 'ride_hailing')
const editorSourceInvoice = computed(() => {
  const item = expense.items.find((entry) => entry.id === editor.id)
  return Boolean(item?.sourceFileId || item?.source === 'ocr')
})
const editorIsTaxi = computed(() => isTaxiExpense(editor) || Boolean(sourceRequiresItinerary.value))
const editorCanSelectRailType = computed(() => editor.category === 'rail_fare'
  && !hasKnownNonRailEvidence(editorEvidence.value))
const sourceRailType = computed(() => editorEvidence.value?.railType)
const editorRailType = computed(() => evidenceRailType(editor.category, editor.railType, editorEvidence.value))
const editorNeedsPaymentProof = computed(() => requiresPaymentProof({ ...editor, railType: editorRailType.value }))
const unresolvedDurableOcrFiles = computed(() => durableFiles.value.filter(
  (file) => (!batchActive.value || batchOriginalFileIds.has(file.id)) && isUnresolvedDurableOcrFile(file),
))
const durableBusy = computed(() => durableOperating.value || drafts.busy)
const receiptOperationStatus = computed(() => {
  if (activeRecognitionStatus.value) return activeRecognitionStatus.value.title
  if (uploadOperationActive.value) return `正在上传本批 ${batchFiles.value.length} 个文件`
  return durableBusy.value ? '请等待当前文件操作完成' : ''
})
const durableMutationDisabledReason = computed(() => {
  const current = drafts.currentDraft
  if (!current) return '正在准备报销表单'
  if (current.status === 'LOCKED') return '当前报销已进入提交处理，不能再修改附件'
  if (current.status === 'EXPIRED') return '当前报销已过期，不能再修改附件'
  if (props.readonly) return '当前操作进行中，费用明细和附件暂不可修改'
  if (current.status === 'DRAFT' || current.status === 'REVIEW_READY') return ''
  return '当前报销不能再修改附件'
})
const durableActionDisabledReason = computed(() => durableMutationDisabledReason.value
  || (durableBusy.value ? receiptOperationStatus.value : ''))
const canAddExpenseItem = computed(
  () =>
    !props.readonly
    && !batchActive.value
    && expense.items.length < expense.maxExpenseItems
    && expense.manualCategories.length > 0
    && !expense.categoryLoadError,
)
const receiptUploadConstraintReason = computed(() => {
  if (durableMutationDisabledReason.value) return durableMutationDisabledReason.value
  if (durableFiles.value.length >= expense.receiptUploadLimits.maxFiles) {
    return `本次报销已达到 ${expense.receiptUploadLimits.maxFiles} 个附件上限`
  }
  return ''
})
const receiptUploadDisabledReason = computed(() => receiptUploadConstraintReason.value || receiptOperationStatus.value)
const newItemDisabledReason = computed(() => {
  if (durableMutationDisabledReason.value) return durableMutationDisabledReason.value
  if (props.readonly) return '当前操作进行中，费用明细暂不可修改'
  if (batchActive.value) return '请等待本批文件处理完成'
  if (expense.items.length >= expense.maxExpenseItems) {
    return `费用明细已达到 ${expense.maxExpenseItems} 条上限`
  }
  if (expense.categoriesLoading) return '费用类别正在加载'
  if (expense.categoryLoadError) return expense.categoryLoadError
  if (!expense.manualCategories.length) return '暂无可手工选择的费用类别'
  return ''
})

interface DurableOperationScope {
  draftId: string
  departmentId: string
  generation: number
}

onBeforeUnmount(() => {
  if (batchPipelineId !== null) drafts.cancelFilePipeline(batchPipelineId)
  durableUnmounted = true
  durableOperationGeneration += 1
  activeDurableOperation = null
  retryingRecognition.value = null
  if (batchScope?.generation === durableOperationGeneration - 1) drafts.processingFiles = false
  releaseReceiptPreview()
  previewController?.abort()
  if (durableOcrPollTimer !== null) window.clearTimeout(durableOcrPollTimer)
})

function scheduleDurableOcrPoll(): void {
  if (durableOcrPollTimer !== null) window.clearTimeout(durableOcrPollTimer)
  durableOcrPollTimer = null
  if (durableUnmounted || !shouldPollDurableOcr.value) return
  durableOcrPollTimer = window.setTimeout(async () => {
    durableOcrPollTimer = null
    if (!shouldPollDurableOcr.value || durableUnmounted) return
    try {
      await drafts.refreshCurrentFiles()
    } catch {
      // A later poll retries transient status-read failures without blocking edits.
    } finally {
      scheduleDurableOcrPoll()
    }
  }, 2500)
}

watch(shouldPollDurableOcr, scheduleDurableOcrPoll, { immediate: true })

watch(
  () => [
    drafts.currentDraft?.id,
    drafts.currentDraft?.status,
    auth.session?.selectedDepartment?.id,
    props.readonly,
  ] as const,
  () => {
    materialEditorVisible.value = false
    materialEditorFile.value = null
    proofPickerVisible.value = false
    if (paymentExpenseContext.value) {
      editorVisible.value = false
      paymentExpenseContext.value = null
    }
    if (batchScope && batchPipelineId !== null && (drafts.currentDraft?.id !== batchScope.draftId
      || (auth.session?.selectedDepartment?.id ?? '') !== batchScope.departmentId || !isDurableDraftEditable())) {
      drafts.cancelFilePipeline(batchPipelineId)
      batchPipelineId = null
    }
    if (batchScope && (drafts.currentDraft?.id !== batchScope.draftId
      || (auth.session?.selectedDepartment?.id ?? '') !== batchScope.departmentId)) {
      batchScope = null
      batchFiles.value = []
      batchPhase.value = null
      drafts.processingFiles = false
    }
    const active = activeDurableOperation
    if (
      !active
      || (
        drafts.currentDraft?.id === active.draftId
        && (auth.session?.selectedDepartment?.id ?? '') === active.departmentId
        && isDurableDraftEditable()
      )
    ) return
    durableOperationGeneration += 1
    activeDurableOperation = null
    retryingRecognition.value = null
    durableOperating.value = false
    batchFiles.value = []
    batchPhase.value = null
    drafts.processingFiles = false
  },
  { flush: 'sync' },
)

watch(() => editor.category, (category) => {
  if (!editorSourceInvoice.value && category !== 'local_transport') editor.transportType = 'other'
})

const itineraryEvidenceSignature = computed(() => JSON.stringify([
  drafts.currentDraft?.id, auth.session?.selectedDepartment?.id,
  drafts.files.map((file) => [file.id, file.role, file.attachmentKind, file.status, file.ocrResult]),
  expense.items.map((item) => [
    item.sourceFileId ?? item.id, item.transportType, item.amount, item.date, item.description, item.itineraryAutoMatchDisabled,
  ]),
]))
let processedItineraryEvidenceSignature: string | null = null
watch([
  itineraryEvidenceSignature,
  () => [durableOperating.value, drafts.pendingMutations, drafts.loadingCurrentDraft, props.readonly, drafts.currentDraft?.status],
], ([signature]) => {
  if (!isDurableDraftEditable() || durableOperating.value
    || drafts.pendingMutations || drafts.loadingCurrentDraft) return
  // A save/re-render is not new evidence: keep an employee's cleared link empty.
  // Do not mark busy evidence processed, so the completed batch still matches.
  if (signature === processedItineraryEvidenceSignature) return
  processedItineraryEvidenceSignature = signature
  expense.reconcileDraftProofs(drafts.files)
  expense.matchDraftItineraries(drafts.files)
}, { immediate: true, flush: 'post' })

function beginDurableOperation(): DurableOperationScope | null {
  const current = drafts.currentDraft
  if (durableUnmounted || !current || !isDurableDraftEditable()) return null
  const scope = {
    draftId: current.id,
    departmentId: auth.session?.selectedDepartment?.id ?? '',
    generation: ++durableOperationGeneration,
  }
  activeDurableOperation = scope
  return scope
}

function acceptsDurableOperation(scope: DurableOperationScope): boolean {
  return !durableUnmounted
    && scope.generation === durableOperationGeneration
    && drafts.currentDraft?.id === scope.draftId
    && (auth.session?.selectedDepartment?.id ?? '') === scope.departmentId
    && isDurableDraftEditable()
}

function isDurableDraftEditable(): boolean {
  return !props.readonly && (
    drafts.currentDraft?.status === 'DRAFT'
    || drafts.currentDraft?.status === 'REVIEW_READY'
  )
}

function finishDurableOperation(scope: DurableOperationScope): void {
  if (scope.generation !== durableOperationGeneration) return
  if (retryingRecognition.value?.generation === scope.generation) retryingRecognition.value = null
  activeDurableOperation = null
  durableOperating.value = false
}

function openNewItem(): void {
  if (props.readonly || batchActive.value) return
  const firstCategory = expense.manualCategories[0]
  if (!firstCategory) {
    ElMessage.error(expense.categoryLoadError || '费用类别尚未加载，请稍后重试')
    return
  }
  paymentExpenseContext.value = null
  Object.assign(editor, {
    id: '',
    category: firstCategory.id,
    date: '',
    description: '',
    amount: '',
    receiptCount: 1,
    transportType: 'other',
    requiresItinerary: false,
    itineraryFileIds: [],
    itineraryAutoMatchDisabled: false,
    paymentProofFileIds: [],
    hotelBillFileIds: [],
    railType: 'unknown',
    originalCurrency: '',
    originalAmount: '',
    cnyAmountConfirmed: false,
  })
  editorRevision.value += 1
  expandedItinerarySelection.value = false
  itineraryQuery.value = ''
  editorVisible.value = true
}

function openEditItem(item: ExpenseItem): void {
  if (props.readonly || batchActive.value) return
  paymentExpenseContext.value = null
  Object.assign(editor, {
    id: item.id,
    category: item.category,
    date: item.date ?? '',
    description: item.description,
    amount: item.amount,
    receiptCount: item.sourceFileId || item.source === 'ocr' ? 1 : item.receiptCount,
    transportType: item.transportType ?? (item.requiresItinerary ? 'ride_hailing' : 'other'),
    requiresItinerary: item.requiresItinerary || item.transportType === 'ride_hailing' || false,
    itineraryFileIds: [...(item.itineraryFileIds ?? [])],
    itineraryAutoMatchDisabled: item.itineraryAutoMatchDisabled ?? false,
    paymentProofFileIds: [...(item.paymentProofFileIds ?? [])],
    hotelBillFileIds: [...(item.hotelBillFileIds ?? [])],
    railType: item.railType ?? 'unknown',
    originalCurrency: item.originalCurrency ?? '',
    originalAmount: item.originalAmount ?? '',
    cnyAmountConfirmed: item.cnyAmountConfirmed ?? false,
  })
  editorRevision.value += 1
  expandedItinerarySelection.value = (item.itineraryFileIds?.length ?? 0) > 1
  itineraryQuery.value = ''
  editorVisible.value = true
}

function canRecordPaymentExpense(file: ReimbursementDraftFile): boolean {
  return isActiveProof(file, 'payment_proof') && file.ocrStatus !== 'RUNNING'
    && !expense.items.some((item) => item.sourceFileId === file.id
      || item.itineraryFileIds?.includes(file.id) || item.paymentProofFileIds?.includes(file.id))
}

function recordPaymentExpense(file: ReimbursementDraftFile): void {
  if (durableActionDisabledReason.value || newItemDisabledReason.value) return
  const current = drafts.files.find((entry) => entry.id === file.id)
  if (!current || !canRecordPaymentExpense(current) || !isDurableDraftEditable()) return
  openNewItem()
  paymentExpenseContext.value = {
    fileId: current.id,
    draftId: drafts.currentDraft!.id,
    departmentId: auth.session?.selectedDepartment?.id ?? '',
  }
  const details = current.paymentDetails
  const amount = moneyToCents(details?.amount ?? '')
  // A payment screenshot is supporting evidence, never an invoice source or
  // an automatic expense. Its suggestions stay local until explicit save.
  Object.assign(editor, {
    category: '',
    date: details?.date && /^\d{4}-\d{2}-\d{2}$/.test(details.date) ? details.date : '',
    description: details?.description ?? '',
    amount: amount === null ? '' : centsToMoney(amount),
    paymentProofFileIds: [current.id],
  })
}

function durableStatusLabel(file: ReimbursementDraftFile): string {
  if (isRetryingDurableRecognition(file)) return '重新识别中'
  if (file.status === 'RESERVED') return '等待上传'
  if (file.status === 'WRITING') return '上传中'
  if (file.status === 'FAILED') return '上传失败'
  if (file.status === 'DELETING') return '删除中'
  if (file.status === 'PURGED') return '已删除'
  if (file.ocrStale) return '识别已中断'
  if (needsMaterialConfirmation(file)) return file.materialClassification?.status === 'pending' ? '等待分类识别' : '待确认用途'
  if (file.ocrStatus === 'RUNNING') return '识别中'
  if (file.ocrStatus === 'COMPLETE') return '识别完成'
  if (file.ocrStatus === 'FAILED') return '识别失败'
  if (file.role === 'ATTACHMENT_ONLY' && file.attachmentKind !== 'itinerary') return '已上传'
  return '等待识别'
}

function durableStatusType(
  file: ReimbursementDraftFile,
): 'success' | 'warning' | 'danger' | 'info' {
  if (isDurableRecognitionRunning(file)) return 'warning'
  if (file.status === 'FAILED' || file.ocrStatus === 'FAILED' || file.ocrStale) return 'danger'
  if (needsMaterialConfirmation(file)) return 'warning'
  if (file.status !== 'ACTIVE' || file.ocrStatus === 'RUNNING') return 'warning'
  if (file.role === 'ATTACHMENT_ONLY' || file.ocrStatus === 'COMPLETE') return 'success'
  return 'info'
}

function isRetryingDurableRecognition(file: ReimbursementDraftFile | undefined): boolean {
  return Boolean(file && retryingRecognition.value?.fileId === file.id)
}

function isDurableRecognitionRunning(file: ReimbursementDraftFile | undefined): boolean {
  return Boolean(file && (isRetryingDurableRecognition(file)
    || (file.status === 'ACTIVE' && file.ocrStatus === 'RUNNING' && file.ocrStale !== true)))
}

function durableFileByItemId(itemId: string): ReimbursementDraftFile | undefined {
  const sourceFileId = expense.items.find((item) => item.id === itemId)?.sourceFileId
  return sourceFileId
    ? durableFiles.value.find((file) => file.id === sourceFileId)
    : undefined
}

function linkedProofFiles(item: ExpenseItem): ReimbursementDraftFile[] {
  return durableFiles.value.filter((file) => item.itineraryFileIds?.includes(file.id))
}

function isPurgedFile(file: ReimbursementDraftFile | undefined): boolean {
  return file?.status === 'PURGED'
}

function openMaterialEditor(file: ReimbursementDraftFile): void {
  if (durableActionDisabledReason.value) return
  materialEditorFile.value = file
  materialEditorKind.value = file.role === 'EXPENSE_SOURCE' ? 'expense' : file.attachmentKind ?? 'other'
  materialEditorVisible.value = true
}

function openSourceMaterialEditor(itemId: string): void {
  const file = durableFileByItemId(itemId)
  if (file) openMaterialEditor(file)
}

async function saveMaterialKind(): Promise<void> {
  const file = materialEditorFile.value
  if (!file || durableActionDisabledReason.value) return
  const scope = beginDurableOperation()
  if (!scope) return
  const kind = materialEditorKind.value
  const expenseBeforeChange = expenseItemSnapshot(expense.items.find((item) => item.sourceFileId === file.id))
  const dismissedBeforeChange = expense.dismissedOcrFileIds.includes(file.id)
  const preservesExistingExpense = kind === 'expense'
    && expenseBeforeChange !== null
  durableOperating.value = true
  try {
    const result = await drafts.updateFile(file.id, { role: kind === 'expense' ? 'EXPENSE_SOURCE' : 'ATTACHMENT_ONLY',
      attachmentKind: kind === 'expense' ? 'other' : kind })
    if (!acceptsDurableOperation(scope)) return
    delete durableErrors[file.id]
    expense.reconcileDraftProofs(drafts.files)
    if (kind !== 'expense') {
      const existing = expense.items.find((item) => item.sourceFileId === file.id)
      if (existing) expense.removeItem(existing.id)
    }
    if (kind === 'itinerary' || (kind === 'expense' && !preservesExistingExpense)) {
      const recognized = await recognizeDurableFile(result.file, scope, false)
      if (kind === 'expense' && recognized) {
        if (expenseItemSnapshot(expense.items.find((item) => item.sourceFileId === file.id)) !== expenseBeforeChange
          || (!dismissedBeforeChange && expense.dismissedOcrFileIds.includes(file.id))) {
          ElMessage.warning('识别结果已更新；保留你刚才对费用的修改，未自动新增或覆盖')
        } else if (!expense.upsertDraftOcrItem(recognized)) {
          durableErrors[file.id] = '识别结果不完整，请重试或手工添加费用明细'
        }
      }
    }
    if (acceptsDurableOperation(scope)) materialEditorVisible.value = false
  } catch (error) {
    if (acceptsDurableOperation(scope)) ElMessage.error(readableOperationError(error, '材料用途修改失败，请重试'))
  } finally { finishDurableOperation(scope) }
}

function durableFileError(file: ReimbursementDraftFile | undefined): string {
  if (!file) return ''
  if (isRetryingDurableRecognition(file)) return ''
  return durableErrors[file.id]
    ?? file.ocrResult?.error?.message
    ?? (file.ocrStale ? '材料识别因服务中断未完成，请重新识别或修改用途' : '')
}

function durableOcrSummary(file: ReimbursementDraftFile): string {
  const result = file.ocrResult
  if (!result) return ''
  if (isItineraryOcrResult(result)) {
    const summary = result.summary
    const tripCount = result.trips.length ? `${result.trips.length} 次行程` : '行程明细未识别'
    return [summary.startDate, tripCount, summary.amount ? `${summary.amount} ${summary.currency ?? ''}` : null,
      !result.complete || result.warnings.length ? '识别不完整或存在疑问，请手动核对关联' : null].filter(Boolean).join(' · ')
  }
  return [
    result.date,
    result.description?.trim(),
    result.amount ? `¥${result.amount}` : null,
  ].filter(Boolean).join(' · ')
}

function canRetryDurableRecognition(file: ReimbursementDraftFile | undefined): boolean {
  return Boolean(file
    && file.status === 'ACTIVE'
    && (file.role === 'EXPENSE_SOURCE' || isActiveProof(file, 'itinerary')
      || isActiveProof(file, 'hotel_bill') || needsMaterialConfirmation(file))
    && (file.ocrStatus !== 'RUNNING' || file.ocrStale === true))
}

function canAdoptDurableRecognition(file: ReimbursementDraftFile): boolean {
  return file.status === 'ACTIVE'
    && file.role === 'EXPENSE_SOURCE'
    && ['COMPLETE', 'FAILED'].includes(file.ocrStatus)
    && file.ocrResult !== null
    && !expense.items.some((item) => item.sourceFileId === file.id)
}

function isUnresolvedDurableOcrFile(file: ReimbursementDraftFile): boolean {
  return isUnresolvedExpenseSourceFile(file, expense.items, expense.dismissedOcrFileIds)
}

function durableSubmissionBlockReason(file: ReimbursementDraftFile): string {
  return materialSubmissionBlockReason(file, expense.items, expense.dismissedOcrFileIds)
}

function hasDurableFileFailure(file: ReimbursementDraftFile): boolean {
  return file.status === 'FAILED'
    || file.ocrStatus === 'FAILED'
    || file.ocrStale === true
    || Boolean(durableFileError(file))
}

function readableWarning(warning: string): string {
  return warningLabels[warning] ?? '请核对识别结果'
}

function mobileItemNeedsAttention(item: ExpenseItem): boolean {
  return moneyToCents(item.amount) === 0
    || Boolean(item.warnings?.length)
    || Boolean(durableFileError(durableFileByItemId(item.id)))
    || Boolean(isForeignExpense(item) && !item.cnyAmountConfirmed)
}

function meaningfulMobileDescription(item: ExpenseItem): string {
  const description = item.description.trim()
  const category = categoryNames.value[item.category] ?? item.category
  return description && description !== category ? description : ''
}

function readableMobileFileName(file: ReimbursementDraftFile | undefined): string {
  if (!file) return ''
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12,64}(?:\.[a-z0-9]+)?$/i.test(file.name)) {
    return file.name
  }
  const extension = file.name.includes('.') ? `.${file.name.split('.').at(-1)}` : ''
  const label = file.role === 'EXPENSE_SOURCE'
    ? '原始票据'
    : attachmentKindLabels[file.attachmentKind ?? 'other']
  return `${label}${extension}`
}

function chooseReceiptFiles(): void {
  if (props.readonly) return
  durableExpenseInput.value?.click()
}

function chooseItineraryFiles(): void {
  if (durableActionDisabledReason.value || receiptUploadDisabledReason.value) return
  durableItineraryInput.value?.click()
}

function choosePaymentProof(item: ExpenseItem, replaceId?: string, kind: 'payment_proof' | 'hotel_bill' = 'payment_proof'): void {
  if (durableActionDisabledReason.value || receiptUploadDisabledReason.value || !drafts.currentDraft) return
  paymentTarget.value = { itemId: item.id, draftId: drafts.currentDraft.id,
    departmentId: auth.session?.selectedDepartment?.id ?? '', replaceId, kind }
  delete paymentErrors[item.id]
  if (kind === 'hotel_bill') durableHotelBillInput.value?.click()
  else durablePaymentProofInput.value?.click()
}

function openPaymentPicker(item: ExpenseItem, kind: 'payment_proof' | 'hotel_bill' = 'payment_proof'): void {
  if (durableActionDisabledReason.value) return
  proofPickerItemId.value = item.id
  proofPickerKind.value = kind
  proofPickerSelection.value = [...((kind === 'hotel_bill' ? item.hotelBillFileIds : item.paymentProofFileIds) ?? [])]
  proofPickerVisible.value = true
}

function savePaymentSelection(): void {
  if (durableActionDisabledReason.value) return
  const item = expense.items.find((entry) => entry.id === proofPickerItemId.value)
  if (!item) return
  const ids = proofPickerSelection.value.filter((id) => currentProofOptions.value.some((file) => file.id === id))
  if (proofPickerKind.value === 'hotel_bill') item.hotelBillFileIds = ids
  else item.paymentProofFileIds = ids
  proofPickerVisible.value = false
}

function unlinkPaymentProof(item: ExpenseItem, fileId: string, kind: 'payment_proof' | 'hotel_bill' = 'payment_proof'): void {
  if (durableActionDisabledReason.value) return
  // This only removes the reference. The file may be used by another expense.
  if (kind === 'hotel_bill') item.hotelBillFileIds = (item.hotelBillFileIds ?? []).filter((id) => id !== fileId)
  else item.paymentProofFileIds = (item.paymentProofFileIds ?? []).filter((id) => id !== fileId)
}

function suggestionFor(item: ExpenseItem) {
  return itinerarySuggestions.value.find((suggestion) => suggestion.sourceFileId === item.sourceFileId)
}

function confirmItinerarySuggestion(item: ExpenseItem): void {
  if (durableActionDisabledReason.value) return
  // Re-evaluate current evidence at click time, never apply a stale proposal.
  const suggestion = suggestItineraries(expense.items, durableFiles.value).find((entry) => entry.sourceFileId === item.sourceFileId)
  if (!suggestion || item.itineraryFileIds?.length || item.itineraryAutoMatchDisabled) return
  item.transportType = suggestion.transportType
  item.requiresItinerary = true
  item.itineraryFileIds = [suggestion.itineraryFileId]
  item.itineraryAutoMatchDisabled = true
  Object.assign(item, suggestion.fill)
  item.warnings = itineraryConfirmationWarnings(item, durableFiles.value)
  ElMessage.success('已关联行程单')
}

function previewSuggestedItinerary(item: ExpenseItem): void {
  const file = durableFiles.value.find((entry) => entry.id === suggestionFor(item)?.itineraryFileId)
  if (file) void previewDurableFile(file)
}

function focusMaterial(itemId?: string, fileId?: string): void {
  const selector = itemId
    ? '[data-expense-material-id]'
    : fileId ? '[data-material-file-id]' : '[data-material-confirmation]'
  const element = [...document.querySelectorAll<HTMLElement>(selector)].find((entry) =>
    (!itemId || entry.dataset.expenseMaterialId === itemId)
    && (!fileId || entry.dataset.materialFileId === fileId)
    && entry.getClientRects().length > 0)
  element?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  element?.focus({ preventScroll: true })
}
defineExpose({ focusMaterial })

function releaseReceiptPreview(): void {
  previewController?.abort()
  previewController = null
  if (receiptPreviewUrl.value) URL.revokeObjectURL(receiptPreviewUrl.value)
  receiptPreviewUrl.value = ''
}

async function previewDurableFile(file: ReimbursementDraftFile | undefined): Promise<void> {
  const draft = drafts.currentDraft
  if (!draft || !file || file.status !== 'ACTIVE') return
  releaseReceiptPreview()
  const controller = new AbortController()
  previewController = controller
  previewLoading.value = true
  try {
    if (props.mobile && file.mediaType === 'application/pdf') {
      const ticket = await requestReimbursementDraftFilePreviewTicket(draft.id, file.id, {
        signal: controller.signal,
      })
      if (controller.signal.aborted || drafts.currentDraft?.id !== draft.id || durableUnmounted) return
      await downloadAndOpenDingTalkDocument({
        url: new URL(ticket.downloadUrl, window.location.origin).href,
        headers: { 'X-Reimbursement-Download-Token': ticket.downloadToken },
        fileType: ticket.fileType,
      })
      return
    }
    const blob = await getReimbursementFileContent(draft.id, file.id, { signal: controller.signal })
    if (controller.signal.aborted || drafts.currentDraft?.id !== draft.id || durableUnmounted) return
    receiptPreviewName.value = file.name
    receiptPreviewKind.value = file.mediaType === 'application/pdf' ? 'pdf' : 'image'
    receiptPreviewUrl.value = URL.createObjectURL(blob)
    receiptPreviewVisible.value = true
  } catch (error) {
    if (!controller.signal.aborted) ElMessage.error(apiErrorMessage(error, '材料预览失败，请重试'))
  } finally {
    if (previewController === controller) previewLoading.value = false
  }
}

async function previewItemReceipt(itemId: string): Promise<void> {
  await previewDurableFile(durableFileByItemId(itemId))
}

function durableTripYear(): number | undefined {
  return expense.includeSubsidy && /^\d{4}-/.test(expense.trip.startDate)
    ? Number(expense.trip.startDate.slice(0, 4))
    : undefined
}

function expenseItemSnapshot(item: ExpenseItem | undefined): string | null {
  if (!item) return null
  // Protect every employee-editable field, including proof links and foreign
  // currency confirmation, from a recognition response that arrives later.
  return JSON.stringify(item)
}

function readableOperationError(error: unknown, fallback: string): string {
  return drafts.mutationError
    || (error instanceof Error && error.message ? error.message : fallback)
}

function skipDuplicateBatchFile(entry: BatchFile, error: unknown): boolean {
  if (apiErrorCode(error) !== 'REIMBURSEMENT_FILE_DUPLICATE') return false
  entry.status = 'skipped'
  entry.source = undefined
  entry.error = apiErrorMessage(error, '该文件已在本次报销中上传，已跳过')
  ElMessage.warning(entry.error)
  return true
}

function recognitionFailure(file: ReimbursementDraftFile): string | null {
  if (file.ocrStatus !== 'FAILED' && file.ocrResult?.status !== 'failed') return null
  return file.ocrResult?.error?.message
    ?? file.materialClassification?.error?.message
    ?? file.materialClassification?.reason
    ?? '识别未完成，请重新识别或手动处理'
}

async function recognizeDurableFile(
  file: ReimbursementDraftFile,
  scope: DurableOperationScope,
  upsertItem: boolean,
  pipelineId?: number,
): Promise<ReimbursementDraftFile | null> {
  if (!acceptsDurableOperation(scope)) return null
  const result = await drafts.recognizeFile(file.id, durableTripYear(), pipelineId)
  if (!acceptsDurableOperation(scope) || result.draftId !== scope.draftId) return null
  if (result.file.id !== file.id) throw new Error('识别结果与材料不一致，请刷新后重试')
  delete durableErrors[file.id]
  if (upsertItem && !expense.upsertDraftOcrItem(result.file)) {
    durableErrors[file.id] = '识别结果不完整，请重试或手工添加费用明细'
  }
  if (upsertItem) void expense.refreshCalculations()
  return result.file
}

async function onDurableSelection(
  event: Event,
  role: ReimbursementDraftFileRole,
  attachmentKind: ReimbursementAttachmentKind = 'other',
  autoClassify = false,
): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = [...(input.files ?? [])]
  input.value = ''
  const target = ['payment_proof', 'hotel_bill'].includes(attachmentKind) ? paymentTarget.value : null
  paymentTarget.value = null
  if (props.readonly || !files.length || durableBusy.value) return
  if (target && (target.draftId !== drafts.currentDraft?.id
    || target.departmentId !== (auth.session?.selectedDepartment?.id ?? '')
    || !expense.items.some((item) => item.id === target.itemId))) {
    ElMessage.warning('费用或报销表单已变化，请在当前费用下重新选择证明材料')
    return
  }
  const scope = beginDurableOperation()
  if (!scope) return
  let pipelineId: number | undefined
  try {
    if (autoClassify) pipelineId = drafts.beginFilePipeline()
  } catch (error) {
    finishDurableOperation(scope)
    ElMessage.error(error instanceof Error ? error.message : '请等待当前文件操作完成')
    return
  }

  durableOperating.value = true
  if (target) paymentUploadingItem.value = target.itemId
  drafts.processingFiles = true
  batchScope = scope
  batchPipelineId = pipelineId ?? null
  batchOriginalFileIds = new Set(drafts.files.map((file) => file.id))
  batchNeedsOcr.value = autoClassify || role === 'EXPENSE_SOURCE' || ['itinerary', 'hotel_bill'].includes(attachmentKind)
  batchPhase.value = pipelineId === undefined ? 'uploading' : 'processing'
  batchFiles.value = files.map((file, index) => ({
    key: `${scope.generation}-${index}`,
    name: file.name,
    source: file,
    role,
    attachmentKind,
    autoClassify,
    ...(target ? { target: { ...target } } : {}),
    status: 'queued',
  }))
  const entries = batchFiles.value
  const recognized: ReimbursementDraftFile[] = []
  const active = () => acceptsDurableOperation(scope)
    && (pipelineId === undefined || drafts.isFilePipelineActive(pipelineId))
  let recognitionQueue = Promise.resolve()
  const recognizeEntry = async (entry: BatchFile): Promise<void> => {
    if (!active() || !entry.uploaded) return
    entry.status = 'recognizing'
    try {
      const result = await recognizeDurableFile(entry.uploaded, scope, false, pipelineId)
      if (!result || !active()) return
      recognized.push(result)
      const failure = recognitionFailure(result)
      entry.recognized = failure === null
      entry.status = failure === null ? 'done' : 'failed'
      if (failure) entry.error = failure
    } catch (error) {
      if (!active()) return
      entry.status = 'failed'
      entry.error = apiErrorMessage(error, '票据识别失败，请重试')
    }
  }
  try {
    for (const [index, file] of files.entries()) {
      if (!active()) break
      const entry = entries[index]!
      entry.status = 'uploading'
      try {
        const uploaded = await drafts.uploadFile(file, role, attachmentKind, autoClassify, pipelineId)
        if (!active() || uploaded.draftId !== scope.draftId) break
        delete durableErrors[uploaded.file.id]
        entry.uploaded = uploaded.file
        entry.source = undefined
        entry.status = batchNeedsOcr.value ? 'uploaded' : 'done'
        if (pipelineId !== undefined) {
          // The OCR lane drains in upload order while the next upload runs.
          // Yield only admission, never await the heavy recognition here.
          recognitionQueue = recognitionQueue.then(() => recognizeEntry(entry))
          await Promise.resolve()
        }
      } catch (error) {
        if (!acceptsDurableOperation(scope)) break
        if (!skipDuplicateBatchFile(entry, error)) {
          entry.status = 'failed'
          entry.error = apiErrorMessage(error, '文件上传失败，请重试')
          if (target) paymentErrors[target.itemId] = entry.error
        }
      }
    }
    if (pipelineId !== undefined) {
      await recognitionQueue
    } else if (active() && batchNeedsOcr.value) {
      batchPhase.value = 'recognizing'
      for (const entry of entries) {
        await recognizeEntry(entry)
      }
    }
    const synchronized = pipelineId === undefined || await drafts.finishFilePipeline(pipelineId)
    if (acceptsDurableOperation(scope) && synchronized) {
      // Publish together; the parent autosave/calculation watchers observe one update.
      for (const result of recognized) {
        const file = drafts.files.find((current) => current.id === result.id)
        if (!file || file.status !== 'ACTIVE') continue
        if (file.role !== 'EXPENSE_SOURCE') continue
        if (!expense.upsertDraftOcrItem(file)) durableErrors[file.id] = '识别结果不完整，请重试或手工添加费用明细'
      }
      if (target) {
        const item = expense.items.find((candidate) => candidate.id === target.itemId)
        const proofIds = entries.flatMap((entry) => entry.uploaded && isActiveProof(entry.uploaded, target.kind) ? [entry.uploaded.id] : [])
        if (item && proofIds.length) {
          const field = target.kind === 'hotel_bill' ? 'hotelBillFileIds' : 'paymentProofFileIds'
          item[field] = [...new Set([...(item[field] ?? []).filter((id) => id !== target.replaceId), ...proofIds])]
        }
      }
      batchPhase.value = 'done'
    }
  } finally {
    if (scope.generation === durableOperationGeneration) {
      if (pipelineId !== undefined) drafts.cancelFilePipeline(pipelineId)
      batchPipelineId = null
      if (batchPhase.value !== 'done') {
        for (const entry of entries) {
          if (!['done', 'failed', 'skipped'].includes(entry.status)) {
            entry.status = 'failed'
            entry.error = '本批处理已中断，请检查已上传材料后重试'
          }
        }
        batchPhase.value = 'done'
      }
      drafts.processingFiles = false
      paymentUploadingItem.value = ''
    }
    finishDurableOperation(scope)
  }
}

function removeFailedBatchFile(entry: BatchFile): void {
  if (durableBusy.value || entry.status !== 'failed' || entry.uploaded) return
  batchFiles.value = batchFiles.value.filter((candidate) => candidate.key !== entry.key)
  if (!batchFiles.value.length) batchPhase.value = null
}

async function retryFailedBatchUpload(entry: BatchFile): Promise<void> {
  if (props.readonly || durableBusy.value || entry.status !== 'failed' || entry.uploaded || !entry.source) return
  const source = entry.source
  const target = entry.target
  if (target && (target.draftId !== drafts.currentDraft?.id
    || target.departmentId !== (auth.session?.selectedDepartment?.id ?? '')
    || !expense.items.some((item) => item.id === target.itemId))) {
    entry.error = '费用或报销表单已变化，请在当前费用下重新选择证明材料'
    return
  }
  const scope = beginDurableOperation()
  if (!scope) return
  let pipelineId: number | undefined
  try {
    if (entry.autoClassify) pipelineId = drafts.beginFilePipeline()
  } catch (error) {
    finishDurableOperation(scope)
    entry.error = readableOperationError(error, '请等待当前文件操作完成')
    return
  }

  durableOperating.value = true
  drafts.processingFiles = true
  batchScope = scope
  batchPipelineId = pipelineId ?? null
  batchOriginalFileIds = new Set(drafts.files.map((file) => file.id))
  batchPhase.value = pipelineId === undefined ? 'uploading' : 'processing'
  entry.status = 'uploading'
  entry.error = undefined
  const needsOcr = entry.autoClassify || entry.role === 'EXPENSE_SOURCE'
    || ['itinerary', 'hotel_bill'].includes(entry.attachmentKind)
  try {
    const uploaded = await drafts.uploadFile(
      source,
      entry.role,
      entry.attachmentKind,
      entry.autoClassify,
      pipelineId,
    )
    if (!acceptsDurableOperation(scope) || uploaded.draftId !== scope.draftId) return
    entry.uploaded = uploaded.file
    entry.source = undefined
    entry.status = needsOcr ? 'uploaded' : 'done'
    let recognized: ReimbursementDraftFile | null = null
    if (needsOcr) {
      entry.status = 'recognizing'
      recognized = await recognizeDurableFile(uploaded.file, scope, false, pipelineId)
      if (!recognized || !acceptsDurableOperation(scope)) return
      const failure = recognitionFailure(recognized)
      entry.recognized = failure === null
      entry.status = failure === null ? 'done' : 'failed'
      if (failure) entry.error = failure
    }
    const synchronized = pipelineId === undefined || await drafts.finishFilePipeline(pipelineId)
    if (!acceptsDurableOperation(scope) || !synchronized) return
    const current = drafts.files.find((file) => file.id === uploaded.file.id) ?? recognized
    if (current?.role === 'EXPENSE_SOURCE' && !expense.upsertDraftOcrItem(current)) {
      durableErrors[current.id] = '识别结果不完整，请重试或手工添加费用明细'
    }
    if (current?.role === 'EXPENSE_SOURCE') void expense.refreshCalculations()
    if (target && current && isActiveProof(current, target.kind)) {
      const item = expense.items.find((candidate) => candidate.id === target.itemId)
      if (item) {
        const field = target.kind === 'hotel_bill' ? 'hotelBillFileIds' : 'paymentProofFileIds'
        item[field] = [...new Set([...(item[field] ?? []).filter((id) => id !== target.replaceId), current.id])]
      }
    }
  } catch (error) {
    if (!acceptsDurableOperation(scope)) return
    if (!skipDuplicateBatchFile(entry, error)) {
      entry.status = 'failed'
      entry.error = readableOperationError(error, '文件上传失败，请重试')
    }
  } finally {
    if (scope.generation === durableOperationGeneration) {
      if (pipelineId !== undefined) drafts.cancelFilePipeline(pipelineId)
      batchPipelineId = null
      batchPhase.value = 'done'
      drafts.processingFiles = false
      batchScope = null
    }
    finishDurableOperation(scope)
  }
}

async function retryDurableRecognition(file: ReimbursementDraftFile): Promise<void> {
  if (props.readonly || durableBusy.value || !canRetryDurableRecognition(file)) return
  const scope = beginDurableOperation()
  if (!scope) return
  const linkedItemSnapshot = expenseItemSnapshot(
    expense.items.find((item) => item.sourceFileId === file.id),
  )
  const previouslyDismissed = expense.dismissedOcrFileIds.includes(file.id)
  retryingRecognition.value = {
    fileId: file.id,
    fileName: file.name,
    generation: scope.generation,
  }
  durableOperating.value = true
  try {
    const recognized = await recognizeDurableFile(file, scope, false)
    if (!recognized) return
    if (recognized.role !== 'EXPENSE_SOURCE' || !recognized.ocrResult || isItineraryOcrResult(recognized.ocrResult)) {
      if (recognized.ocrResult && !recognitionFailure(recognized)) {
        ElMessage.success(`已更新“${file.name}”的识别结果`)
      }
      return
    }
    if (
      previouslyDismissed || expense.dismissedOcrFileIds.includes(file.id)
      || expenseItemSnapshot(expense.items.find((item) => item.sourceFileId === file.id))
        !== linkedItemSnapshot
    ) {
      ElMessage.warning('OCR 结果已更新；现有费用明细可能已人工修改，未自动新增或覆盖')
      return
    }
    if (!expense.upsertDraftOcrItem(recognized)) {
      durableErrors[file.id] = '识别结果不完整，请重试或手工添加费用明细'
      return
    }
    void expense.refreshCalculations()
    if (!recognitionFailure(recognized) && !durableErrors[recognized.id]) {
      ElMessage.success('已用新的 OCR 结果更新明细')
    }
  } catch (error) {
    if (!acceptsDurableOperation(scope)) return
    durableErrors[file.id] = readableOperationError(error, '票据识别失败，请重试')
  } finally {
    finishDurableOperation(scope)
  }
}

async function clearDurableFiles(): Promise<void> {
  if (durableActionDisabledReason.value || !clearableDurableFiles.value.length) return
  const scope = beginDurableOperation()
  if (!scope) return
  const targets = [...clearableDurableFiles.value]
  const sourceIds = new Set(targets.map((file) => file.id))
  const linkedCount = expense.items.filter((item) => sourceIds.has(item.sourceFileId ?? '')).length
  let deletedCount = 0
  durableOperating.value = true
  try {
    try {
      await ElMessageBox.confirm(
        `将删除当前报销的 ${targets.length} 个已上传文件（含发票、行程单和付款凭证），同时移除 ${linkedCount} 条由这些票据生成的费用明细。手工费用、基本信息和出差审批关联会保留。此操作不能撤销，需要重新上传文件。`,
        '清空已上传文件',
        { confirmButtonText: '确认清空', cancelButtonText: '取消', type: 'warning' },
      )
    } catch { return }
    if (!acceptsDurableOperation(scope)) return
    batchScope = scope
    drafts.processingFiles = true
    try {
      const result = await drafts.clearFiles()
      if (!acceptsDurableOperation(scope)) return
      if (result.draftId !== scope.draftId) throw new Error('清空结果与当前报销不一致')
    } catch (error) {
      if (!acceptsDurableOperation(scope)) return
      // The store reloads authoritative state after an uncertain/partial deletion.
      for (const file of targets) {
        const remaining = drafts.files.find((current) => current.id === file.id)
        if (!remaining || remaining.status === 'DELETING') {
          expense.removeDraftFileAssociation(file.id)
          deletedCount += 1
        }
      }
      ElMessage.error(readableOperationError(error, '清空未完成，请重试；剩余文件已保留'))
      return
    }
    for (const file of targets) {
      expense.removeDraftFileAssociation(file.id)
      delete durableErrors[file.id]
      batchFiles.value = batchFiles.value.filter((entry) => entry.uploaded?.id !== file.id)
      deletedCount += 1
    }
    batchFiles.value = []
    batchPhase.value = null
    ElMessage.success(`已清空 ${deletedCount} 个上传文件，手工费用和基本信息已保留`)
  } finally {
    if (scope.generation === durableOperationGeneration) {
      drafts.processingFiles = false
      batchScope = null
      if (deletedCount) void expense.refreshCalculations()
    }
    finishDurableOperation(scope)
  }
}

async function removeDurableFile(file: ReimbursementDraftFile): Promise<void> {
  if (props.readonly || durableBusy.value || !['ACTIVE', 'DELETING'].includes(file.status)) return
  const scope = beginDurableOperation()
  if (!scope) return
  const linked = expense.items.some((item) => item.sourceFileId === file.id)
  durableOperating.value = true
  try {
    if (linked && file.status === 'ACTIVE') {
      try {
        await ElMessageBox.confirm(
          '该票据已关联一条费用明细。删除文件会同时移除当前费用明细，是否继续？',
          '确认删除票据',
          {
            confirmButtonText: '删除',
            cancelButtonText: '取消',
            type: 'warning',
          },
        )
      } catch {
        return
      }
    }
    if (!acceptsDurableOperation(scope)) return
    const result = await drafts.removeFile(file.id)
    if (
      !acceptsDurableOperation(scope)
      || result.draftId !== scope.draftId
      || result.deletedFileId !== file.id
    ) return
    expense.removeDraftFileAssociation(file.id)
    delete durableErrors[file.id]
    ElMessage.success('已从本次报销移除该文件')
    void expense.refreshCalculations()
  } catch (error) {
    if (!acceptsDurableOperation(scope)) return
    const stillExists = drafts.files.some((current) => current.id === file.id)
    if (!stillExists) {
      expense.removeDraftFileAssociation(file.id)
      delete durableErrors[file.id]
      drafts.mutationError = ''
      ElMessage.success('服务端已确认文件删除，当前费用明细已同步移除')
      void expense.refreshCalculations()
      return
    }
    durableErrors[file.id] = readableOperationError(error, '文件删除失败，请重试')
  } finally {
    finishDurableOperation(scope)
  }
}

function adoptDurableRecognition(file: ReimbursementDraftFile): void {
  if (
    props.readonly
    || durableActionDisabledReason.value
    || !canAdoptDurableRecognition(file)
  ) return
  if (!expense.upsertDraftOcrItem(file)) {
    durableErrors[file.id] = '识别结果不完整，请手工添加费用明细'
    return
  }
  delete durableErrors[file.id]
  ElMessage.success('已将识别结果添加到费用明细')
  void expense.refreshCalculations()
}

async function ignoreDurableRecognition(file: ReimbursementDraftFile): Promise<void> {
  if (
    props.readonly
    || durableActionDisabledReason.value
    || !isUnresolvedDurableOcrFile(file)
  ) return
  const scope = beginDurableOperation()
  if (!scope) return
  durableOperating.value = true
  try {
    try {
      await ElMessageBox.confirm(
        '忽略后该文件仍作为 OA 附件保留，但识别结果不计入费用金额。是否继续？',
        '忽略此票据',
        {
          confirmButtonText: '确认忽略',
          cancelButtonText: '返回检查',
          type: 'warning',
        },
      )
    } catch {
      return
    }
    if (!acceptsDurableOperation(scope)) return
    const current = drafts.files.find((candidate) => candidate.id === file.id)
    if (!current || !isUnresolvedDurableOcrFile(current)) return
    if (expense.dismissDraftOcrFile(file.id)) {
      ElMessage.success('已忽略该票据的识别结果，原文件仍会作为附件提交')
    }
  } finally {
    finishDurableOperation(scope)
  }
}

function chooseEditorItineraries(value: string[] | string | null | undefined): void {
  editor.itineraryFileIds = Array.isArray(value) ? [...value] : value ? [value] : []
  editor.itineraryAutoMatchDisabled = true
}

function retryEditorItineraryMatching(): void {
  if (props.readonly || !isDurableDraftEditable() || editor.itineraryFileIds.length) return
  editor.itineraryAutoMatchDisabled = false
  saveItem()
}

function saveItem(): void {
  if (props.readonly || batchActive.value || !editorVisible.value) return
  try {
    const paymentContext = paymentExpenseContext.value
    if (paymentContext) {
      if (durableActionDisabledReason.value || !isDurableDraftEditable()
        || drafts.currentDraft?.id !== paymentContext.draftId
        || (auth.session?.selectedDepartment?.id ?? '') !== paymentContext.departmentId) return
      const proof = drafts.files.find((file) => file.id === paymentContext.fileId)
      if (!proof || !canRecordPaymentExpense(proof)) {
        throw new Error('此付款凭证已被关联或用途发生变化，请重新核对材料')
      }
      if (!editor.paymentProofFileIds.includes(proof.id)) {
        throw new Error('请保留本次录入所依据的付款凭证；普通手工费用可从“手动添加”录入')
      }
      if (!editor.category) throw new Error('请选择费用类别，并核对人民币金额后确认录入')
    }
    if (editor.originalCurrency.trim() && !/^[A-Z]{3}$/.test(editor.originalCurrency.trim().toUpperCase())) {
      throw new Error('请填写票面原币币种，例如 VND、USD 或 EUR')
    }
    expense.upsertManualItem({
      id: editor.id || undefined,
      category: editor.category,
      date: editor.date,
      displayDate: editor.date,
      description: editor.description,
      amount: editor.amount,
      receiptCount: editorSourceInvoice.value ? 1 : editor.receiptCount,
      requiresItinerary: editor.transportType === 'ride_hailing' || Boolean(sourceRequiresItinerary.value),
      transportType: sourceRequiresItinerary.value ? 'ride_hailing' : editor.transportType,
      itineraryFileIds: editorIsTaxi.value ? [...editor.itineraryFileIds] : [],
      itineraryAutoMatchDisabled: editor.itineraryAutoMatchDisabled,
      paymentProofFileIds: [...editor.paymentProofFileIds],
      hotelBillFileIds: [...editor.hotelBillFileIds],
      railType: editorRailType.value,
      originalCurrency: editor.originalCurrency.trim().toUpperCase() || undefined,
      originalAmount: editor.originalAmount.trim() || undefined,
      cnyAmountConfirmed: foreignEditor.value && editor.cnyAmountConfirmed,
      requiresCnyConfirmation: Boolean(foreignEditor.value),
      warnings: [],
    })
    editorVisible.value = false
    void expense.refreshCalculations()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '费用明细填写不正确')
  }
}

async function removeItem(id: string): Promise<void> {
  if (props.readonly || batchActive.value) return
  const item = expense.items.find((entry) => entry.id === id)
  const file = durableFileByItemId(id)
  if (item?.sourceFileId && file) {
    await removeDurableFile(file)
  } else {
    expense.removeItem(id)
    void expense.refreshCalculations()
  }
}

async function retryItemRecognition(id: string): Promise<void> {
  if (props.readonly) return
  const file = durableFileByItemId(id)
  if (file) await retryDurableRecognition(file)
}
</script>

<template>
  <el-card
    shadow="never"
    class="content-card reimbursement-card"
    :class="{ 'reimbursement-card--mobile': props.mobile }"
  >
    <template #header>
      <div class="card-header">
        <div>
          <strong>费用明细</strong>
          <span class="section-note">OCR 结果会直接填入，发现不准确时直接编辑</span>
        </div>
        <div
          class="receipt-header-actions"
          :class="{ 'receipt-header-actions--mobile': props.mobile }"
        >
          <el-button
            class="clear-files-button"
            type="danger"
            :plain="!props.mobile"
            :link="props.mobile"
            :disabled="Boolean(durableActionDisabledReason) || !clearableDurableFiles.length"
            :title="durableActionDisabledReason || '清空当前报销已上传的文件，保留手工费用和基本信息'"
            @click="clearDurableFiles"
          >
            清空文件
          </el-button>
          <el-button
            class="manual-item-button"
            :loading="expense.categoriesLoading"
            :disabled="!canAddExpenseItem"
            :title="newItemDisabledReason"
            @click="openNewItem"
          >
            手动添加
          </el-button>
          <el-button
            :class="props.mobile ? 'mobile-upload-button' : 'receipt-upload-button'"
            type="primary"
            :loading="uploadOperationActive"
            :disabled="Boolean(receiptUploadDisabledReason)"
            :title="receiptUploadDisabledReason"
            :data-testid="props.mobile ? 'mobile-file-upload-button' : undefined"
            @click="chooseReceiptFiles()"
          >
            上传报销材料
          </el-button>
        </div>
        <input
          ref="durableExpenseInput"
          data-testid="durable-expense-input"
          class="visually-hidden"
          type="file"
          :accept="props.mobile ? undefined : '.jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf'"
          multiple
          :disabled="Boolean(durableActionDisabledReason)"
          @change="onDurableSelection($event, 'ATTACHMENT_ONLY', 'other', true)"
        >
        <input
          ref="durableItineraryInput"
          data-testid="durable-itinerary-input"
          class="visually-hidden"
          type="file"
          :accept="props.mobile ? undefined : '.jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf'"
          multiple
          :disabled="Boolean(durableActionDisabledReason)"
          @change="onDurableSelection($event, 'ATTACHMENT_ONLY', 'itinerary')"
        >
        <input
          ref="durableHotelBillInput"
          data-testid="durable-hotel-bill-input"
          class="visually-hidden"
          type="file"
          :accept="props.mobile ? undefined : '.jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf'"
          multiple
          :disabled="Boolean(durableActionDisabledReason)"
          @change="onDurableSelection($event, 'ATTACHMENT_ONLY', 'hotel_bill')"
        >
        <input
          ref="durablePaymentProofInput"
          data-testid="durable-payment-proof-input"
          class="visually-hidden"
          type="file"
          :accept="props.mobile ? undefined : '.jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf'"
          multiple
          :disabled="Boolean(durableActionDisabledReason)"
          @change="onDurableSelection($event, 'ATTACHMENT_ONLY', 'payment_proof')"
        >
        <input
          ref="durableAttachmentInput"
          data-testid="durable-attachment-input"
          class="visually-hidden"
          type="file"
          :accept="props.mobile ? undefined : '.jpg,.jpeg,.png,.pdf,image/jpeg,image/png,application/pdf'"
          multiple
          :disabled="Boolean(durableActionDisabledReason)"
          @change="onDurableSelection($event, 'ATTACHMENT_ONLY')"
        >
        <p
          v-if="receiptUploadConstraintReason || newItemDisabledReason"
          class="field-help action-help"
          role="status"
        >
          {{ receiptUploadConstraintReason || newItemDisabledReason }}
        </p>
        <span
          class="visually-hidden receipt-operation-status"
          role="status"
          aria-live="polite"
        >{{ receiptOperationStatus }}</span>
      </div>
    </template>
    <el-alert
      :title="props.mobile ? '可一次上传全部报销材料' : '一次上传，自动整理报销材料'"
      :description="props.mobile
        ? '系统自动分类；住宿需附明细，超过 500 元需附付款凭证。'
        : '发票、打车行程单、住宿明细和付款凭证可一起上传。每笔住宿都须关联住宿明细，超过 500 元还须付款凭证；不确定的用途请确认。'"
      type="info"
      :closable="false"
      show-icon
      class="upload-guidance"
      :class="{ 'upload-guidance--mobile': props.mobile }"
    />
    <el-alert
      v-if="expense.ocrUnavailable"
      title="本地 OCR 当前不可用"
      description="对应条目已经保留，请直接编辑补充票据信息。系统不会转用付费或云端 OCR。"
      type="warning"
      :closable="false"
      show-icon
      class="receipt-alert"
    />
    <el-alert
      v-if="drafts.mutationError"
      :title="drafts.mutationError"
      type="error"
      :closable="false"
      show-icon
      class="receipt-alert"
    />
    <div
      v-if="activeRecognitionStatus"
      class="material-operation-status"
      role="status"
      aria-live="polite"
      aria-atomic="true"
      data-testid="material-operation-status"
    >
      <el-icon
        class="is-loading material-operation-status__icon"
        aria-hidden="true"
      >
        <Loading />
      </el-icon>
      <div>
        <strong>{{ activeRecognitionStatus.title }}</strong>
        <span>{{ activeRecognitionStatus.description }}</span>
      </div>
    </div>
    <el-alert
      v-if="unresolvedDurableOcrFiles.length > 0"
      :title="`${unresolvedDurableOcrFiles.length} 张票据的 OCR 结果待确认`"
      description="请逐张选择“添加到费用明细”或“仅作为材料保留”，处理完成后即可提交。"
      type="warning"
      :closable="false"
      show-icon
      class="receipt-alert"
    />
    <div
      v-if="batchFiles.length"
      class="batch-progress"
      role="status"
      aria-live="polite"
      data-testid="batch-progress"
    >
      <p>
        {{ batchPhase !== 'done'
          ? `正在处理本批 ${batchFiles.length} 个文件`
          : batchFailedCount
            ? `本批 ${batchFiles.length} 个文件处理结束，${batchFailedCount} 个需要处理`
            : `本批 ${batchFiles.length} 个文件已处理完成` }}
      </p>
      <p data-testid="batch-lane-counts">
        已上传 {{ batchUploadedCount }}/{{ batchFiles.length }}
        <template v-if="batchNeedsOcr">
          · 已识别 {{ batchRecognizedCount }}/{{ batchFiles.length }}
        </template>
        <template v-if="batchFailedCount">
          · 需要处理 {{ batchFailedCount }}
        </template>
        <template v-if="batchSkippedCount">
          · 已跳过 {{ batchSkippedCount }}
        </template>
      </p>
      <el-progress :percentage="batchProgress" />
      <div
        v-for="file in visibleBatchFiles"
        :key="file.key"
        class="batch-file"
        data-testid="batch-file"
      >
        <span>{{ file.name }}</span>
        <span class="batch-file-status">{{ batchStatusLabels[file.status] }}</span>
        <span
          v-if="file.status === 'failed' && !file.uploaded"
          class="batch-file-actions"
        >
          <el-button
            link
            type="primary"
            :disabled="durableBusy"
            @click="retryFailedBatchUpload(file)"
          >
            重试上传
          </el-button>
          <el-button
            link
            :disabled="durableBusy"
            @click="removeFailedBatchFile(file)"
          >
            移除
          </el-button>
        </span>
        <p
          v-if="file.error"
          class="field-error"
        >
          {{ file.error }}
        </p>
      </div>
    </div>
    <header
      v-if="unlinkedDurableFiles.length"
      class="material-workbench__heading"
      aria-live="polite"
      aria-label="待处理票据和未关联材料"
      data-testid="material-workbench"
    >
      <div>
        <strong>待处理材料</strong>
        <span>{{ unlinkedDurableFiles.length }} 份</span>
      </div>
      <p v-if="blockingUnlinkedDurableFiles.length">
        其中 {{ blockingUnlinkedDurableFiles.length }} 份处理后才能提交 OA；其余材料可按需关联。
      </p>
      <p v-else>
        这些材料尚未关联费用，当前不影响提交 OA。
      </p>
    </header>
    <div
      v-if="unlinkedDurableFiles.length"
      class="receipt-list"
      :class="{ 'receipt-list--mobile': props.mobile }"
    >
      <article
        v-for="file in orderedUnlinkedDurableFiles"
        :key="file.id"
        class="receipt-row"
        :class="{ 'receipt-row--mobile': props.mobile }"
        :data-material-file-id="file.id"
        :data-material-confirmation="needsMaterialConfirmation(file) ? file.id : undefined"
        tabindex="-1"
        :aria-label="`${file.name}：${durableStatusLabel(file)}`"
      >
        <div class="receipt-main">
          <div class="receipt-name-line">
            <button
              type="button"
              class="receipt-file-preview-link"
              :disabled="file.status !== 'ACTIVE' || previewLoading"
              :title="file.name"
              :aria-label="`预览材料 ${file.name}`"
              @click="previewDurableFile(file)"
            >
              {{ props.mobile ? file.name : `${file.name} · 预览` }}
            </button>
            <span class="receipt-tags">
              <el-tag
                v-if="!needsMaterialConfirmation(file)"
                size="small"
              >
                {{ file.role === 'ATTACHMENT_ONLY' ? attachmentKindLabels[file.attachmentKind ?? 'other'] : durableRoleLabels[file.role] }}
              </el-tag>
              <el-tag
                size="small"
                :type="durableStatusType(file)"
              >
                {{ durableStatusLabel(file) }}
              </el-tag>
            </span>
          </div>
          <span class="receipt-meta">
            {{ formatFileSize(file.sizeBytes) }}
            <template v-if="file.role === 'ATTACHMENT_ONLY'"> · 尚未关联费用</template>
          </span>
          <p
            v-if="needsMaterialConfirmation(file)"
            class="field-help receipt-guidance"
          >
            {{ file.materialClassification?.reason || '请确认材料用途；确认前不会计入费用或提交 OA。' }}
          </p>
          <p
            v-if="durableOcrSummary(file)"
            class="receipt-meta"
          >
            OCR：{{ durableOcrSummary(file) }}
          </p>
          <p
            v-if="durableFileError(file)"
            class="field-error receipt-error"
            role="alert"
          >
            {{ durableFileError(file) }}
          </p>
          <p
            class="receipt-submit-impact"
            :class="{
              'receipt-submit-impact--blocking': durableSubmissionBlockReason(file),
              'receipt-submit-impact--error': durableSubmissionBlockReason(file) && hasDurableFileFailure(file),
            }"
          >
            {{ durableSubmissionBlockReason(file)
              ? `提交前必须处理：${durableSubmissionBlockReason(file)}`
              : '尚未关联（可选处理） · 当前不影响提交 OA' }}
          </p>
        </div>
        <div class="receipt-actions">
          <el-button
            v-if="canRecordPaymentExpense(file)"
            class="receipt-action receipt-action--wide"
            :link="!props.mobile"
            type="primary"
            data-testid="record-payment-expense"
            :disabled="Boolean(durableActionDisabledReason || newItemDisabledReason)"
            :title="durableActionDisabledReason || newItemDisabledReason"
            @click="recordPaymentExpense(file)"
          >
            根据此付款凭证录入费用
          </el-button>
          <el-button
            class="receipt-action"
            :link="!props.mobile"
            :type="props.mobile && needsMaterialConfirmation(file) ? 'primary' : undefined"
            :disabled="Boolean(durableActionDisabledReason)"
            @click="openMaterialEditor(file)"
          >
            {{ needsMaterialConfirmation(file) ? '确认用途' : '修改用途' }}
          </el-button>
          <el-button
            v-if="canAdoptDurableRecognition(file)"
            class="receipt-action"
            :link="!props.mobile"
            :plain="props.mobile"
            type="primary"
            :disabled="Boolean(durableActionDisabledReason)"
            :title="durableActionDisabledReason"
            @click="adoptDurableRecognition(file)"
          >
            添加到费用明细
          </el-button>
          <el-button
            v-if="isUnresolvedDurableOcrFile(file)"
            class="receipt-action"
            :link="!props.mobile"
            :plain="props.mobile"
            type="warning"
            :disabled="Boolean(durableActionDisabledReason)"
            :title="durableActionDisabledReason"
            @click="ignoreDurableRecognition(file)"
          >
            仅作为材料保留
          </el-button>
          <el-button
            v-if="canRetryDurableRecognition(file)"
            class="receipt-action"
            :link="!props.mobile"
            :plain="props.mobile"
            type="primary"
            :loading="isRetryingDurableRecognition(file)"
            :disabled="Boolean(durableActionDisabledReason)"
            :title="durableActionDisabledReason"
            @click="retryDurableRecognition(file)"
          >
            重新识别
          </el-button>
          <el-button
            class="receipt-action"
            :link="!props.mobile"
            :plain="props.mobile"
            type="danger"
            :disabled="Boolean(durableActionDisabledReason)
              || !['ACTIVE', 'DELETING'].includes(file.status)"
            :title="durableActionDisabledReason"
            @click="removeDurableFile(file)"
          >
            {{ file.status === 'DELETING' ? '重试删除' : '删除文件' }}
          </el-button>
        </div>
      </article>
    </div>
    <div
      v-if="expense.categoryLoadError"
      class="category-load-error"
    >
      <el-alert
        :title="expense.categoryLoadError"
        type="error"
        show-icon
        :closable="false"
      />
      <el-button
        :loading="expense.categoriesLoading"
        :disabled="props.readonly"
        @click="expense.loadCategories(true)"
      >
        重新加载费用类别
      </el-button>
    </div>
    <el-empty
      v-if="expense.items.length === 0 && !hasVisibleDurableFiles"
      description="还没有费用明细"
      :image-size="80"
    />
    <header
      v-if="expense.items.length > 0 && (props.mobile || unlinkedDurableFiles.length > 0)"
      class="expense-items-group__heading"
      :class="{ 'expense-items-group__heading--desktop': !props.mobile }"
      data-testid="expense-items-group"
    >
      <div>
        <strong>已计入费用明细</strong>
        <span>{{ expense.items.length }} 笔</span>
      </div>
      <p>以下项目会计入本次报销金额，可继续编辑并补充关联材料。</p>
    </header>
    <el-table
      v-if="expense.items.length > 0"
      :data="expense.sortedItems"
      class="expense-table"
      :class="{ 'expense-table--hidden': props.mobile }"
    >
      <el-table-column
        label="类型"
        min-width="160"
      >
        <template #default="scope">
          <div class="expense-category-cell">
            <span>{{ categoryNames[scope.row.category] ?? scope.row.category }}</span>
            <el-tag
              v-if="scope.row.source === 'ocr'"
              size="small"
              type="info"
            >
              OCR
            </el-tag>
            <el-tag
              v-if="isDurableRecognitionRunning(durableFileByItemId(scope.row.id))"
              size="small"
              type="warning"
            >
              {{ isRetryingDurableRecognition(durableFileByItemId(scope.row.id)) ? '重新识别中' : '识别中' }}
            </el-tag>
            <button
              v-if="durableFileByItemId(scope.row.id)?.name
                && !isPurgedFile(durableFileByItemId(scope.row.id))"
              type="button"
              class="receipt-file-preview-link receipt-meta"
              :disabled="previewLoading"
              :aria-label="`预览票据 ${durableFileByItemId(scope.row.id)?.name}`"
              @click="previewItemReceipt(scope.row.id)"
            >
              {{ durableFileByItemId(scope.row.id)?.name }} · 预览
            </button>
            <span
              v-else-if="durableFileByItemId(scope.row.id)?.name"
              class="receipt-meta"
            >{{ durableFileByItemId(scope.row.id)?.name }}</span>
            <el-button
              v-if="durableFileByItemId(scope.row.id)
                && !isPurgedFile(durableFileByItemId(scope.row.id))"
              link
              :disabled="Boolean(durableActionDisabledReason)"
              @click="openSourceMaterialEditor(scope.row.id)"
            >
              修改用途
            </el-button>
            <span
              v-if="scope.row.warnings?.length"
              class="ocr-warning"
            >
              {{ scope.row.warnings.map(readableWarning).join('、') }}
            </span>
            <span
              v-if="durableFileError(durableFileByItemId(scope.row.id))"
              class="field-error"
            >
              {{ durableFileError(durableFileByItemId(scope.row.id)) }}
            </span>
          </div>
        </template>
      </el-table-column>
      <el-table-column
        label="日期"
        min-width="120"
      >
        <template #default="scope">
          {{ scope.row.displayDate || '待补充' }}
        </template>
      </el-table-column>
      <el-table-column
        prop="description"
        label="说明"
        min-width="290"
      >
        <template #default="scope">
          <div>{{ scope.row.description }}</div>
          <div
            v-for="file in linkedProofFiles(scope.row)"
            :key="file.id"
            class="linked-itinerary"
          >
            <button
              v-if="file.status !== 'PURGED'"
              type="button"
              class="receipt-file-preview-link"
              :disabled="previewLoading"
              @click="previewDurableFile(file)"
            >
              行程单：{{ file.name }} · 预览
            </button>
            <span v-else>行程单：{{ file.name }}</span>
            <span>{{ durableOcrSummary(file) }}</span>
            <el-button
              link
              :disabled="Boolean(durableActionDisabledReason)"
              @click="openEditItem(scope.row)"
            >
              更改关联
            </el-button>
            <el-button
              link
              :disabled="Boolean(durableActionDisabledReason)"
              @click="openMaterialEditor(file)"
            >
              修改用途
            </el-button>
          </div>
          <ExpenseItinerarySuggestion
            v-if="suggestionFor(scope.row)"
            :suggestion="suggestionFor(scope.row)!"
            :file-name="durableFiles.find((file) => file.id === suggestionFor(scope.row)?.itineraryFileId)?.name || '行程单'"
            :disabled="Boolean(durableActionDisabledReason)"
            :preview-loading="previewLoading"
            @confirm="confirmItinerarySuggestion(scope.row)"
            @choose="openEditItem(scope.row)"
            @preview="previewSuggestedItinerary(scope.row)"
          />
          <ExpenseMaterialLinks
            :item="scope.row"
            :files="durableFiles"
            :allow-purged="showLockedFileMetadata"
            :has-itinerary-suggestion="Boolean(suggestionFor(scope.row))"
            :disabled="Boolean(durableActionDisabledReason)"
            :preview-loading="previewLoading"
            :uploading="paymentUploadingItem === scope.row.id"
            :error="paymentErrors[scope.row.id]"
            @preview="previewDurableFile"
            @purpose="openMaterialEditor"
            @upload="choosePaymentProof(scope.row, $event)"
            @reuse="openPaymentPicker(scope.row)"
            @unlink="unlinkPaymentProof(scope.row, $event)"
            @hotel-upload="choosePaymentProof(scope.row, $event, 'hotel_bill')"
            @hotel-reuse="openPaymentPicker(scope.row, 'hotel_bill')"
            @hotel-unlink="unlinkPaymentProof(scope.row, $event, 'hotel_bill')"
            @itinerary="openEditItem(scope.row)"
          />
        </template>
      </el-table-column>
      <el-table-column
        label="金额"
        width="110"
        align="right"
      >
        <template #default="scope">
          {{ scope.row.amount ? `¥${scope.row.amount}` : '待补充' }}
          <div
            v-if="moneyToCents(scope.row.amount) === 0"
            class="ocr-warning"
            data-testid="zero-amount-warning"
          >
            金额为 0，请核实原票据
          </div>
          <div
            v-if="isForeignExpense(scope.row)"
            class="receipt-meta"
          >
            原币 {{ scope.row.originalAmount || '待补充' }} {{ scope.row.originalCurrency || '币种待确认' }}
            <span
              v-if="!scope.row.cnyAmountConfirmed"
              class="field-error"
            >请确认人民币金额</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column
        prop="receiptCount"
        label="张数"
        width="72"
        align="center"
      />
      <el-table-column
        label="操作"
        width="190"
        fixed="right"
      >
        <template #default="scope">
          <el-button
            link
            type="primary"
            :disabled="props.readonly || batchActive"
            @click="openEditItem(scope.row)"
          >
            编辑
          </el-button>
          <el-button
            v-if="Boolean(durableFileByItemId(scope.row.id)
              && canRetryDurableRecognition(durableFileByItemId(scope.row.id)))"
            link
            type="primary"
            :loading="isRetryingDurableRecognition(durableFileByItemId(scope.row.id))"
            :disabled="props.readonly || Boolean(durableActionDisabledReason)"
            :title="durableActionDisabledReason"
            @click="retryItemRecognition(scope.row.id)"
          >
            重新识别
          </el-button>
          <el-button
            link
            type="danger"
            :disabled="props.readonly || batchActive || (Boolean(durableFileByItemId(scope.row.id))
              && Boolean(durableActionDisabledReason))"
            :title="durableFileByItemId(scope.row.id)
              ? durableActionDisabledReason
              : ''"
            @click="removeItem(scope.row.id)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <div
      v-if="expense.items.length > 0"
      class="expense-mobile-list"
      :class="{ 'expense-mobile-list--active': props.mobile }"
    >
      <article
        v-for="item in expense.sortedItems"
        :key="item.id"
        class="expense-mobile-card"
      >
        <div class="mobile-expense-heading">
          <div class="mobile-expense-title">
            <strong>{{ categoryNames[item.category] ?? item.category }}</strong>
            <el-tag
              v-if="mobileItemNeedsAttention(item)"
              size="small"
              type="warning"
            >
              待完善
            </el-tag>
            <el-tag
              v-if="isDurableRecognitionRunning(durableFileByItemId(item.id))"
              size="small"
              type="warning"
            >
              {{ isRetryingDurableRecognition(durableFileByItemId(item.id)) ? '重新识别中' : '识别中' }}
            </el-tag>
          </div>
          <span class="mobile-expense-amount">{{ item.amount ? `¥${item.amount}` : '金额待补充' }}</span>
        </div>
        <p class="mobile-expense-meta">
          {{ item.displayDate || '日期待补充' }} · {{ item.receiptCount }} 张
        </p>
        <p
          v-if="moneyToCents(item.amount) === 0"
          class="ocr-warning"
          data-testid="zero-amount-warning"
        >
          金额为 0，请核实原票据
        </p>
        <p v-if="meaningfulMobileDescription(item)">
          {{ meaningfulMobileDescription(item) }}
        </p>
        <p
          v-if="item.source === 'ocr'"
          class="ocr-warning"
        >
          OCR<template v-if="item.warnings?.length">
            · {{ item.warnings.map(readableWarning).join('、') }}
          </template>
        </p>
        <p
          v-if="durableFileByItemId(item.id)?.name"
          class="receipt-meta"
        >
          <button
            v-if="!isPurgedFile(durableFileByItemId(item.id))"
            type="button"
            class="receipt-file-preview-link"
            :disabled="previewLoading"
            @click="previewItemReceipt(item.id)"
          >
            {{ readableMobileFileName(durableFileByItemId(item.id)) }} · 预览
          </button>
          <span v-else>{{ readableMobileFileName(durableFileByItemId(item.id)) }}</span>
          <el-button
            v-if="!isPurgedFile(durableFileByItemId(item.id))"
            link
            :disabled="Boolean(durableActionDisabledReason)"
            @click="openSourceMaterialEditor(item.id)"
          >
            修改用途
          </el-button>
        </p>
        <p
          v-for="file in linkedProofFiles(item)"
          :key="file.id"
          class="receipt-meta"
        >
          <button
            v-if="file.status !== 'PURGED'"
            type="button"
            class="receipt-file-preview-link"
            :disabled="previewLoading"
            @click="previewDurableFile(file)"
          >
            {{ attachmentKindLabels[file.attachmentKind] }}：{{ readableMobileFileName(file) }} · 预览
          </button>
          <span v-else>{{ attachmentKindLabels[file.attachmentKind] }}：{{ readableMobileFileName(file) }}</span>
          <span
            v-if="durableOcrSummary(file)"
            class="linked-proof-summary"
          >{{ durableOcrSummary(file) }}</span>
          <el-button
            link
            :disabled="Boolean(durableActionDisabledReason)"
            @click="openEditItem(item)"
          >
            更改关联
          </el-button>
          <el-button
            link
            :disabled="Boolean(durableActionDisabledReason)"
            @click="openMaterialEditor(file)"
          >
            修改用途
          </el-button>
        </p>
        <ExpenseItinerarySuggestion
          v-if="suggestionFor(item)"
          :suggestion="suggestionFor(item)!"
          :file-name="durableFiles.find((file) => file.id === suggestionFor(item)?.itineraryFileId)?.name || '行程单'"
          :disabled="Boolean(durableActionDisabledReason)"
          :preview-loading="previewLoading"
          @confirm="confirmItinerarySuggestion(item)"
          @choose="openEditItem(item)"
          @preview="previewSuggestedItinerary(item)"
        />
        <ExpenseMaterialLinks
          :item="item"
          :files="durableFiles"
          :allow-purged="showLockedFileMetadata"
          :has-itinerary-suggestion="Boolean(suggestionFor(item))"
          :disabled="Boolean(durableActionDisabledReason)"
          :preview-loading="previewLoading"
          :uploading="paymentUploadingItem === item.id"
          :error="paymentErrors[item.id]"
          @preview="previewDurableFile"
          @purpose="openMaterialEditor"
          @upload="choosePaymentProof(item, $event)"
          @reuse="openPaymentPicker(item)"
          @unlink="unlinkPaymentProof(item, $event)"
          @hotel-upload="choosePaymentProof(item, $event, 'hotel_bill')"
          @hotel-reuse="openPaymentPicker(item, 'hotel_bill')"
          @hotel-unlink="unlinkPaymentProof(item, $event, 'hotel_bill')"
          @itinerary="openEditItem(item)"
        />
        <p
          v-if="isForeignExpense(item)"
          class="receipt-meta"
        >
          原币 {{ item.originalAmount || '待补充' }} {{ item.originalCurrency || '币种待确认' }}
          <span
            v-if="!item.cnyAmountConfirmed"
            class="field-error"
          >请确认人民币金额</span>
        </p>
        <p
          v-if="durableFileError(durableFileByItemId(item.id))"
          class="field-error"
        >
          {{ durableFileError(durableFileByItemId(item.id)) }}
        </p>
        <div class="mobile-actions">
          <el-button
            size="small"
            :disabled="props.readonly || batchActive"
            @click="openEditItem(item)"
          >
            编辑
          </el-button>
          <el-button
            v-if="canRetryDurableRecognition(durableFileByItemId(item.id))"
            size="small"
            :loading="isRetryingDurableRecognition(durableFileByItemId(item.id))"
            :disabled="props.readonly || Boolean(durableActionDisabledReason)"
            :title="durableActionDisabledReason"
            @click="retryItemRecognition(item.id)"
          >
            重新识别
          </el-button>
          <el-button
            size="small"
            type="danger"
            plain
            :disabled="props.readonly || batchActive || (Boolean(durableFileByItemId(item.id))
              && Boolean(durableActionDisabledReason))"
            :title="durableFileByItemId(item.id)
              ? durableActionDisabledReason
              : ''"
            @click="removeItem(item.id)"
          >
            删除
          </el-button>
        </div>
      </article>
    </div>
  </el-card>

  <el-dialog
    v-model="receiptPreviewVisible"
    :title="`票据预览：${receiptPreviewName}`"
    width="min(960px, calc(100% - 24px))"
    top="4vh"
    destroy-on-close
    @closed="releaseReceiptPreview"
  >
    <div class="receipt-preview-surface">
      <img
        v-if="receiptPreviewKind === 'image'"
        :src="receiptPreviewUrl"
        :alt="`${receiptPreviewName} 预览`"
      >
      <iframe
        v-else
        :src="receiptPreviewUrl"
        :title="`${receiptPreviewName} 预览`"
      />
    </div>
    <p class="field-help receipt-preview-help">
      <span>预览本次报销的原始材料，请核对金额、日期和票面内容。</span>
      <a
        v-if="receiptPreviewKind === 'pdf' && !props.mobile"
        :href="receiptPreviewUrl"
        target="_blank"
        rel="noopener noreferrer"
      >在新标签页打开 PDF</a>
    </p>
    <template #footer>
      <el-button @click="receiptPreviewVisible = false">
        关闭
      </el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="proofPickerVisible"
    :title="`选择已上传的${attachmentKindLabels[proofPickerKind]}`"
    width="min(560px, calc(100% - 24px))"
  >
    <p class="field-help">
      选中后关联到当前这笔费用，不会复制或删除原文件。
    </p>
    <el-select
      v-model="proofPickerSelection"
      multiple
      filterable
      class="full-width"
      :placeholder="`选择${attachmentKindLabels[proofPickerKind]}`"
      :disabled="durableBusy"
    >
      <el-option
        v-for="file in currentProofOptions"
        :key="file.id"
        :label="file.name"
        :value="file.id"
      >
        <span>{{ file.name }} · {{ formatFileSize(file.sizeBytes) }}</span>
      </el-option>
    </el-select>
    <template #footer>
      <el-button @click="proofPickerVisible = false">
        取消
      </el-button>
      <el-button
        type="primary"
        :disabled="Boolean(durableActionDisabledReason)"
        @click="savePaymentSelection"
      >
        确认关联
      </el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="materialEditorVisible"
    title="修改材料用途"
    width="min(520px, calc(100% - 24px))"
  >
    <p>{{ materialEditorFile?.name }}</p>
    <el-select
      v-model="materialEditorKind"
      aria-label="材料用途"
      :disabled="durableBusy"
      class="full-width"
    >
      <el-option
        value="expense"
        label="票据/发票（生成费用明细）"
      />
      <el-option
        v-for="(label, kind) in attachmentKindLabels"
        :key="kind"
        :value="kind"
        :label="label"
      />
    </el-select>
    <p class="field-help">
      发票生成费用，行程单、住宿明细及付款凭证仅作附件，不增加费用。更改用途后会移除不适用的旧关联，请重新核对。
    </p>
    <p
      v-if="materialEditorKind !== 'expense' && expense.items.some(item => item.sourceFileId === materialEditorFile?.id)"
      class="field-help"
    >
      这张票据已有费用明细；保存为证明材料后，将移除对应的费用明细，但会保留原文件。取消则不会改变任何内容。
    </p>
    <template #footer>
      <el-button @click="materialEditorVisible = false">
        取消
      </el-button>
      <el-button
        type="primary"
        :disabled="Boolean(durableActionDisabledReason)"
        @click="saveMaterialKind"
      >
        保存用途
      </el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="editorVisible"
    :title="paymentExpenseContext ? '根据付款凭证录入费用' : editor.id ? '编辑费用明细' : '新增费用明细'"
    width="min(520px, calc(100% - 24px))"
    :fullscreen="props.mobile"
    :class="{ 'expense-editor-dialog--mobile': props.mobile }"
    :header-class="props.mobile ? 'expense-editor-dialog__header--mobile' : ''"
    :body-class="props.mobile ? 'expense-editor-dialog__body--mobile' : ''"
    :footer-class="props.mobile ? 'expense-editor-dialog__footer--mobile' : ''"
  >
    <el-form
      class="expense-editor-form"
      :class="{ 'expense-editor-form--mobile': props.mobile }"
      label-position="top"
      :disabled="props.readonly || batchActive"
    >
      <p
        v-if="paymentExpenseContext"
        class="field-help"
      >
        根据付款凭证录入，请确认实际用途和费用类别，并核对日期、说明和人民币金额。凭证仅作为附件；取消不会计入费用。
      </p>
      <el-form-item label="费用类别">
        <el-select
          v-model="editor.category"
          class="full-width"
          placeholder="请选择费用类别"
        >
          <el-option
            v-for="category in expense.manualCategories"
            :key="category.id"
            :label="category.name"
            :value="category.id"
          />
        </el-select>
      </el-form-item>
      <el-form-item
        v-if="editorCanSelectRailType"
        label="铁路票种"
      >
        <el-select
          v-model="editor.railType"
          aria-label="铁路票种"
          class="full-width"
          :disabled="Boolean(sourceRailType && sourceRailType !== 'unknown')"
        >
          <el-option
            value="unknown"
            label="待确认"
          />
          <el-option
            value="high_speed"
            label="高铁"
          />
          <el-option
            value="emu"
            label="动车"
          />
          <el-option
            value="regular"
            label="普通列车"
          />
        </el-select>
        <p
          v-if="sourceRailType && sourceRailType !== 'unknown'"
          class="field-help"
        >
          票种来自原始票据识别；如有误请核对原件后重新识别。
        </p>
      </el-form-item>
      <p
        v-if="editor.category === 'rail_fare' && !editorCanSelectRailType"
        class="field-help"
      >
        原始票据已识别为非铁路费用，修改类别不会获得高铁付款凭证豁免；请核对原件。
      </p>
      <el-form-item label="发生日期">
        <el-date-picker
          v-model="editor.date"
          type="date"
          value-format="YYYY-MM-DD"
          class="full-width"
        />
      </el-form-item>
      <el-form-item label="说明">
        <el-input
          v-model="editor.description"
          type="textarea"
          :rows="3"
          maxlength="500"
          show-word-limit
        />
      </el-form-item>
      <div class="trip-grid">
        <el-form-item :label="foreignEditor ? '人民币报销金额（元）' : '金额（元）'">
          <el-input
            v-model="editor.amount"
            inputmode="decimal"
            placeholder="0.00"
            maxlength="15"
            @input="editor.cnyAmountConfirmed = false"
          />
        </el-form-item>
        <el-form-item
          v-if="!editorSourceInvoice"
          label="票据张数"
        >
          <el-input-number
            :key="editorRevision"
            v-model="editor.receiptCount"
            :min="1"
            :max="10000"
            :step="1"
            step-strictly
            class="full-width"
          />
          <p class="field-help">
            {{ paymentExpenseContext ? '本笔默认按 1 张报销凭据计数，关联的付款文件不再额外计数。' : '手工汇总多张票据时填写；行程单和证明材料不计入。' }}
          </p>
          <p
            v-if="editor.receiptCount > 1"
            class="field-help"
          >
            付款凭证按本行金额判断；如需按单张发票判断，请分别录入。
          </p>
        </el-form-item>
      </div>
      <div class="trip-grid">
        <el-form-item label="原票币种（不确定时可留空）">
          <el-input
            v-model="editor.originalCurrency"
            placeholder="例如 VND、USD、EUR"
            maxlength="3"
            @input="editor.cnyAmountConfirmed = false"
          />
        </el-form-item>
        <el-form-item
          v-if="foreignEditor"
          label="原币金额"
        >
          <el-input
            v-model="editor.originalAmount"
            placeholder="例如 97600000"
            inputmode="decimal"
            @input="editor.cnyAmountConfirmed = false"
          />
        </el-form-item>
      </div>
      <el-form-item v-if="foreignEditor">
        <el-checkbox v-model="editor.cnyAmountConfirmed">
          已核对原币金额，并确认上述人民币报销金额
        </el-checkbox>
        <p class="field-help">
          人民币金额请按实际报销金额填写，系统不会把原币金额直接当作人民币。
        </p>
      </el-form-item>
      <el-form-item
        v-if="!sourceRequiresItinerary && editor.category === 'local_transport'"
        label="市内交通类型"
      >
        <el-select
          v-model="editor.transportType"
          aria-label="打车类型"
          class="full-width"
        >
          <el-option
            value="other"
            label="其他市内交通"
          />
          <el-option
            value="taxi"
            label="出租车（行程单可选）"
          />
          <el-option
            value="ride_hailing"
            label="网约车（必须有行程单）"
          />
        </el-select>
      </el-form-item>
      <template v-if="editorIsTaxi">
        <p class="field-help">
          {{ editor.transportType === 'ride_hailing' || sourceRequiresItinerary ? '网约车费用必须有对应行程单。' : '出租车费用可按需关联行程单。' }}
        </p>
        <el-form-item label="对应行程单">
          <el-select
            :key="expandedItinerarySelection ? 'multiple' : 'single'"
            :model-value="expandedItinerarySelection ? editor.itineraryFileIds : editor.itineraryFileIds[0]"
            :multiple="expandedItinerarySelection"
            filterable
            clearable
            :filter-method="(query: string) => itineraryQuery = query"
            class="full-width"
            placeholder="按金额、日期、路线或文件名查找"
            @update:model-value="chooseEditorItineraries"
          >
            <el-option
              v-for="option in editorItineraryOptions"
              :key="option.file.id"
              :label="option.label"
              :value="option.file.id"
              class="itinerary-option"
            >
              <strong>{{ option.summary }} <span v-if="option.recommended">· 推荐</span></strong>
              <span>{{ option.route }}</span>
              <span v-if="option.matchedTripSummary">{{ option.matchedTripSummary }}</span>
              <small>{{ option.file.name }}<template v-if="option.association"> · {{ option.association }}</template></small>
            </el-option>
          </el-select>
          <p class="field-help">
            默认关联一个文件；一个文件可包含多次行程。只有材料分在多个文件中时才需补充。
          </p>
          <div class="itinerary-editor-actions">
            <el-button
              link
              type="primary"
              :disabled="Boolean(durableActionDisabledReason)"
              data-testid="editor-itinerary-upload"
              @click="chooseItineraryFiles"
            >
              ＋ 上传新的行程单
            </el-button>
            <el-button
              v-if="!expandedItinerarySelection && editorItineraryOptions.length > 1"
              link
              type="primary"
              @click="expandedItinerarySelection = true"
            >
              关联多个已上传文件
            </el-button>
            <span
              v-else-if="expandedItinerarySelection"
              class="field-help itinerary-multiple-status"
            >已开启多文件关联</span>
          </div>
          <template v-if="editor.itineraryAutoMatchDisabled && editorSourceInvoice">
            <p class="field-help">
              已保留你的手动选择，刷新后也不会自动改变。需要重新匹配时，请先清空选择；重新匹配将保存当前编辑。
            </p>
            <el-button
              v-if="!editor.itineraryFileIds.length"
              link
              type="primary"
              :disabled="props.readonly"
              @click="retryEditorItineraryMatching"
            >
              重新自动匹配
            </el-button>
          </template>
        </el-form-item>
      </template>
      <el-form-item
        v-if="editor.category === 'lodging' || editor.hotelBillFileIds.length"
        label="住宿明细"
      >
        <el-select
          v-model="editor.hotelBillFileIds"
          multiple
          clearable
          class="full-width"
          placeholder="选择已上传的住宿明细"
        >
          <el-option
            v-for="file in hotelBillOptions"
            :key="file.id"
            :label="file.name"
            :value="file.id"
          />
        </el-select>
        <p class="field-help">
          每笔住宿都须关联住宿明细，可选多份或与其他住宿费用共用；超过 500 元另须付款凭证。
        </p>
      </el-form-item>
      <el-form-item
        v-if="editorNeedsPaymentProof || editor.paymentProofFileIds.length"
        label="付款凭证"
      >
        <el-select
          v-model="editor.paymentProofFileIds"
          multiple
          class="full-width"
          placeholder="选择已上传的付款凭证"
        >
          <el-option
            v-for="file in paymentProofOptions"
            :key="file.id"
            :label="file.name"
            :value="file.id"
          />
        </el-select>
        <p class="field-help">
          除高铁外，本行确认人民币金额超过 500 元须有付款凭证；500 元无需补充。
        </p>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="editorVisible = false">
        取消
      </el-button>
      <el-button
        type="primary"
        :disabled="props.readonly || batchActive"
        @click="saveItem"
      >
        {{ paymentExpenseContext ? '确认并录入费用' : '保存' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.receipt-upload-button { min-width: 148px; }
.expense-table.expense-table--hidden { display: none; }
.expense-table :deep(.el-table__body td.el-table__cell) { vertical-align: top; }
.expense-table :deep(.el-table__body td.el-table__cell > .cell) { padding-block: 12px; }
.material-workbench__heading {
  margin-top: 18px;
  padding: 14px 14px 0;
  border: 1px solid var(--el-color-warning-light-7);
  border-bottom: 0;
  border-radius: 14px 14px 0 0;
  background: var(--el-color-warning-light-9);
}
.material-workbench__heading div,
.expense-items-group__heading div {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.material-workbench__heading strong,
.expense-items-group__heading strong { font-size: 16px; }
.material-workbench__heading span,
.expense-items-group__heading span { color: var(--el-text-color-secondary); font-size: 13px; }
.material-workbench__heading p,
.expense-items-group__heading p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.55;
}
.material-workbench__heading + .receipt-list {
  margin-top: 0;
  padding: 12px 14px 14px;
  border-top: 1px solid var(--el-color-warning-light-7);
  border-right: 1px solid var(--el-color-warning-light-7);
  border-bottom: 1px solid var(--el-color-warning-light-7);
  border-left: 1px solid var(--el-color-warning-light-7);
  border-radius: 0 0 14px 14px;
  background: var(--el-color-warning-light-9);
}
.material-workbench__heading + .receipt-list .receipt-row { background: #fff; }
.receipt-submit-impact { color: var(--el-text-color-secondary); font-size: 13px; }
.receipt-submit-impact--blocking { color: var(--el-color-warning-dark-2); }
.receipt-submit-impact--error { color: var(--el-color-danger); }
.expense-items-group__heading {
  margin-top: 20px;
  padding: 14px 14px 0;
  border: 1px solid #e4e7ed;
  border-bottom: 0;
  border-radius: 14px 14px 0 0;
  background: #f8fafc;
}
.material-workbench__heading + .receipt-list + .expense-items-group__heading { margin-top: 24px; }
.expense-items-group__heading--desktop { padding-bottom: 14px; }
.expense-mobile-list.expense-mobile-list--active {
  display: grid;
  gap: 12px;
  padding: 12px 14px 14px;
  border: 1px solid #e4e7ed;
  border-top: 0;
  border-radius: 0 0 14px 14px;
  background: #f8fafc;
}
.expense-mobile-list--active .expense-mobile-card {
  padding: 14px;
  border: 1px solid #e4e7ed;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgb(16 24 40 / 4%);
}
.expense-mobile-list--active .expense-mobile-card > div:first-child {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}
.expense-mobile-list--active .expense-mobile-card > div:first-child span {
  color: #0958d9;
  font-weight: 700;
  white-space: nowrap;
}
.expense-mobile-list--active .expense-mobile-card p {
  margin: 8px 0 0;
  color: #667085;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.expense-mobile-list--active .linked-proof-summary { display: block; margin-top: 4px; }
.expense-mobile-list--active .mobile-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid #f0f2f5;
}
.expense-mobile-list--active .mobile-actions :deep(.el-button) { margin-left: 0; }
.receipt-list--mobile { gap: 12px; }
.receipt-list--mobile .receipt-row--mobile {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  align-items: stretch;
  gap: 12px;
  padding: 14px;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 8px rgb(16 24 40 / 4%);
}
.receipt-list--mobile .receipt-name-line {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
}
.receipt-list--mobile .receipt-file-preview-link {
  display: -webkit-box;
  min-width: 0;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-height: 1.45;
}
.receipt-list--mobile .receipt-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  max-width: 132px;
}
.receipt-list--mobile .receipt-meta { display: block; margin-top: 7px; font-size: 13px; }
.receipt-list--mobile .receipt-main > p { margin: 8px 0 0; line-height: 1.55; }
.receipt-list--mobile .receipt-guidance {
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--el-color-warning-light-9);
  color: var(--el-color-warning-dark-2);
}
.receipt-list--mobile .receipt-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: stretch;
  gap: 8px;
  width: 100%;
  margin-top: 0;
  padding-top: 12px;
  border-top: 1px solid #f0f2f5;
}
.receipt-list--mobile .receipt-actions :deep(.el-button) {
  flex: 1 1 80px;
  min-height: 40px;
  margin: 0;
  padding-inline: 8px;
}
.receipt-list--mobile .receipt-actions :deep(.receipt-action--wide) { flex-basis: 100%; }
.expense-editor-form--mobile .trip-grid { grid-template-columns: minmax(0, 1fr); gap: 0; }
.expense-editor-form--mobile :deep(.el-input-number),
.expense-editor-form--mobile :deep(.el-date-editor) { width: 100%; }
.itinerary-editor-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 16px; width: 100%; }
.itinerary-editor-actions :deep(.el-button) { margin-left: 0; }
.itinerary-multiple-status { margin: 0; }
.receipt-header-actions--mobile {
  display: grid;
  grid-template-areas:
    "upload upload"
    "manual clear";
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px 12px;
  width: 100%;
}
.receipt-header-actions--mobile :deep(.el-button) { margin: 0; }
.receipt-header-actions--mobile .mobile-upload-button {
  grid-area: upload;
  width: 100%;
  min-height: 48px;
}
.receipt-header-actions--mobile .manual-item-button { grid-area: manual; width: 100%; }
.receipt-header-actions--mobile .clear-files-button { grid-area: clear; justify-self: end; width: auto; }
.reimbursement-card--mobile :deep(.el-card__header) { padding: 16px; }
.reimbursement-card--mobile :deep(.el-card__body) { padding: 16px; }
.reimbursement-card--mobile .section-note { display: none; }
.upload-guidance--mobile { margin-bottom: 14px; }
.upload-guidance--mobile :deep(.el-alert__content) { min-width: 0; }
.upload-guidance--mobile :deep(.el-alert__title) { font-size: 14px; }
.upload-guidance--mobile :deep(.el-alert__description) { margin-top: 2px; line-height: 1.5; }
.material-operation-status {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-top: 16px;
  padding: 14px 16px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 8px;
  background: var(--el-color-primary-light-9);
  color: var(--el-text-color-regular);
}
.material-operation-status__icon {
  flex: none;
  margin-top: 2px;
  color: var(--el-color-primary);
  font-size: 20px;
}
.material-operation-status > div { min-width: 0; }
.material-operation-status strong,
.material-operation-status span { display: block; overflow-wrap: anywhere; }
.material-operation-status strong { color: var(--el-color-primary); }
.material-operation-status span { margin-top: 4px; color: var(--el-text-color-secondary); line-height: 1.55; }
.expense-mobile-list--active .mobile-expense-heading { align-items: flex-start; }
.mobile-expense-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }
.mobile-expense-amount { flex: none; font-size: 18px; }
.expense-mobile-list--active .mobile-expense-meta { margin-top: 6px; color: #98a2b3; font-size: 13px; }
.batch-progress { margin-top: 16px; padding: 16px; border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.batch-file { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 8px 12px; padding-top: 12px; }
.batch-file > span:first-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.batch-file > p { grid-column: 1 / -1; margin: 0; }
.batch-file-status { white-space: nowrap; }
.batch-file-actions { display: flex; white-space: nowrap; }
.linked-itinerary { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; margin-top: 6px; color: #667085; font-size: 12px; }
.itinerary-option { height: auto; min-height: 72px; padding: 10px 20px; line-height: 1.55; display: flex; flex-direction: column; white-space: normal; overflow-wrap: anywhere; }
.itinerary-option > span, .itinerary-option > small { color: #667085; font-weight: 400; }
.itinerary-option > span { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
@media (max-width: 640px) {
  .receipt-header-actions > .el-button + .el-button { margin-left: 0; }
}
</style>

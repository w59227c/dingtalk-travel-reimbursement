import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'

import { calculateTotals, getExpenseCategories } from '@/api/expenses'
import type {
  ExpenseCategoryMetadata,
  ExpenseItem,
  ExpenseTotals,
  TripInput,
  TripType,
} from '@/types/expenses'
import type { ReceiptUploadLimits } from '@/types/auth'
import type {
  ReimbursementDraft,
  ReimbursementDraftExpenseItemInput,
  ReimbursementDraftFile,
} from '@/types/reimbursements'
import { receiptOcrResult } from '@/types/reimbursements'
import { evidenceRailType, isActiveProof } from '@/utils/expenseProofs'
import { itineraryConfirmationWarnings, matchItineraries } from '@/utils/itineraryMatching'
import { centsToMoney, moneyToCents } from '@/utils/money'
import { DEFAULT_RECEIPT_LIMITS } from '@/utils/receiptFiles'
import { TRIP_PERIOD_TIME, tripPeriodFromTime } from '@/utils/tripPeriod'

const SPECIAL_TRIP_TYPES = new Set<TripType>([
  'same_city_project',
])
const CALENDAR_DATE_PATTERN = /^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$/

function newItemId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `manual-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function isCalendarDate(value: string | null): value is string {
  if (!value || !CALENDAR_DATE_PATTERN.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const parsed = new Date(Date.UTC(year!, month! - 1, day!))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month! - 1
    && parsed.getUTCDate() === day
}

function calendarDays(startDate: string, endDate: string): number | null {
  if (!isCalendarDate(startDate) || !isCalendarDate(endDate)) return null
  const start = Date.parse(`${startDate}T00:00:00Z`)
  const end = Date.parse(`${endDate}T00:00:00Z`)
  const days = Math.floor((end - start) / 86_400_000) + 1
  return days > 0 ? days : null
}

export const useExpenseStore = defineStore('expense', () => {
  const manualProjectText = ref('')
  const trip = reactive({
    tripType: 'business' as TripType,
    startDate: '',
    startTime: '09:00',
    endDate: '',
    endTime: '18:00',
    policyConfirmed: false,
    confirmedEffectiveDays: '',
    noSubsidyException: false,
  })
  const subsidyTrips = ref<TripInput[]>([])
  const items = ref<ExpenseItem[]>([])
  const dismissedOcrFileIds = ref<string[]>([])
  const includeSubsidy = ref(false)
  const receiptUploadLimits = ref<ReceiptUploadLimits>({ ...DEFAULT_RECEIPT_LIMITS })
  const ocrUnavailable = ref(false)
  const categories = ref<ExpenseCategoryMetadata[]>([])
  const categoriesLoading = ref(false)
  const categoryLoadError = ref('')
  const totals = ref<ExpenseTotals | null>(null)
  const calculating = ref(false)
  const calculationError = ref('')
  let calculationVersion = 0
  const calculatedSignature = ref('')

  const projectCalendarDays = computed(() =>
    trip.tripType === 'project' ? calendarDays(trip.startDate, trip.endDate) : null,
  )
  const projectPolicyType = computed(() => {
    if (trip.tripType !== 'project' || projectCalendarDays.value === null) return null
    return projectCalendarDays.value > 30 ? 'long_term_project' : 'short_term_project'
  })
  const requiresPolicyConfirmation = computed(
    () => includeSubsidy.value && SPECIAL_TRIP_TYPES.has(trip.tripType),
  )
  const maxExpenseItems = ref(200)
  const policyInputError = computed(() => {
    if (!includeSubsidy.value) return ''
    if (trip.tripType === 'overseas') return '境外出差不申请出差补助'
    if (!subsidyTrips.value.length) {
      if (!trip.startDate || !trip.endDate) return '请完整填写出发和返回日期、时段'
      if (requiresPolicyConfirmation.value && !trip.policyConfirmed) {
        return '请勾选已按公司制度确认'
      }
      return ''
    }
    if (requiresPolicyConfirmation.value
      && subsidyTrips.value.some((item) => !item.policyConfirmed)) {
      return '请逐项勾选已按公司制度确认'
    }
    return ''
  })
  const manualCategories = computed(() => categories.value.filter((item) => item.manualSelectable))
  const sortedItems = computed(() =>
    items.value
      .map((item, index) => ({ item, index }))
      .sort((left, right) => {
        const leftDate = isCalendarDate(left.item.date ?? null) ? left.item.date! : null
        const rightDate = isCalendarDate(right.item.date ?? null) ? right.item.date! : null
        if (leftDate && rightDate) {
          const dateOrder = leftDate.localeCompare(rightDate)
          return dateOrder || left.index - right.index
        }
        if (leftDate) return -1
        if (rightDate) return 1
        return left.index - right.index
      })
      .map(({ item }) => item),
  )
  const localExpenseCents = computed(() =>
    items.value.reduce((sum, item) => sum + (moneyToCents(item.amount) ?? 0), 0),
  )
  const localReceiptCount = computed(() =>
    items.value.reduce((sum, item) => sum + item.receiptCount, 0),
  )
  const displayExpenseTotal = computed(
    () => totals.value?.expenseTotal ?? centsToMoney(localExpenseCents.value),
  )
  const displaySubsidyTotal = computed(() => {
    if (!includeSubsidy.value) return '0.00'
    return totals.value?.subsidyTotal ?? '0.00'
  })
  const displayReceiptCount = computed(() => totals.value?.receiptCount ?? localReceiptCount.value)
  const displayTotal = computed(() => {
    if (totals.value) return totals.value.totalAmount
    const subsidyCents = moneyToCents(displaySubsidyTotal.value) ?? 0
    return centsToMoney(localExpenseCents.value + subsidyCents)
  })
  const itemReadinessError = computed(() => {
    if (items.value.some((item) => !item.date || !CALENDAR_DATE_PATTERN.test(item.date))) {
      return '请补全每条费用明细的发生日期'
    }
    if (items.value.some((item) => moneyToCents(item.amount) === null)) {
      return '请补全每条费用明细的金额，最多两位小数'
    }
    if (items.value.some((item) => !item.displayDate.trim() || !item.description.trim())) {
      return '请补全每条费用明细的日期说明和用途说明'
    }
    return ''
  })

  function calculationSignature(payload: TripInput | readonly TripInput[] | null): string {
    return JSON.stringify({
      trips: Array.isArray(payload) ? payload : payload ? [payload] : [],
      items: items.value.map((item) => ({
        category: item.category,
        date: item.date,
        displayDate: item.displayDate,
        description: item.description,
        amount: item.amount,
        receiptCount: item.receiptCount,
      })),
    })
  }

  function tripPayload(): TripInput | null {
    const values = tripPayloads()
    return values.length === 1 ? values[0]! : null
  }

  function tripPayloads(): TripInput[] {
    if (!includeSubsidy.value) return []
    if (trip.tripType === 'overseas') return []
    if (subsidyTrips.value.length) {
      return subsidyTrips.value.flatMap((item) => {
        const startPeriod = tripPeriodFromTime(item.startTime)
        const endPeriod = tripPeriodFromTime(item.endTime)
        if (!item.relatedApprovalId || !item.startDate || !item.endDate || !startPeriod || !endPeriod) {
          return []
        }
        const payload: TripInput = {
          relatedApprovalId: item.relatedApprovalId,
          tripType: trip.tripType,
          startDate: item.startDate,
          startTime: TRIP_PERIOD_TIME[startPeriod],
          endDate: item.endDate,
          endTime: TRIP_PERIOD_TIME[endPeriod],
        }
        if (trip.tripType === 'same_city_project') payload.policyConfirmed = Boolean(item.policyConfirmed)
        return [payload]
      })
    }
    const startPeriod = tripPeriodFromTime(trip.startTime)
    const endPeriod = tripPeriodFromTime(trip.endTime)
    if (
      !trip.startDate ||
      !trip.endDate ||
      !startPeriod ||
      !endPeriod
    ) return []
    // Normalize both ends so exact source times cannot conflict within the same half-day.
    const payload: TripInput = {
      tripType: trip.tripType,
      startDate: trip.startDate,
      startTime: TRIP_PERIOD_TIME[startPeriod],
      endDate: trip.endDate,
      endTime: TRIP_PERIOD_TIME[endPeriod],
    }
    if (requiresPolicyConfirmation.value) payload.policyConfirmed = trip.policyConfirmed
    return [payload]
  }

  function syncSubsidyApprovals(
    approvals: readonly { processInstanceId: string; startDate: string; endDate: string }[],
  ): void {
    const previous = new Map(subsidyTrips.value.map((item) => [item.relatedApprovalId, item]))
    const ordered = [...approvals].sort((left, right) =>
      left.startDate.localeCompare(right.startDate)
      || left.endDate.localeCompare(right.endDate)
      || left.processInstanceId.localeCompare(right.processInstanceId))
    const lastIndex = ordered.length - 1
    const nextTrips = ordered.map((approval, index) => {
      const current = previous.get(approval.processInstanceId)
      return {
        relatedApprovalId: approval.processInstanceId,
        tripType: trip.tripType,
        startDate: approval.startDate,
        startTime: current?.startTime ?? (index === 0 ? trip.startTime : '09:00'),
        endDate: approval.endDate,
        endTime: current?.endTime ?? (index === lastIndex ? trip.endTime : '18:00'),
        policyConfirmed: current?.policyConfirmed ?? false,
      }
    })
    const changed = JSON.stringify(nextTrips) !== JSON.stringify(subsidyTrips.value)
    if (changed) subsidyTrips.value = nextTrips
    if (!nextTrips.length) {
      if (!changed) return
      totals.value = null
      calculatedSignature.value = ''
      return
    }
    const first = nextTrips[0]
    const last = nextTrips.at(-1)
    trip.startDate = first?.startDate ?? ''
    trip.startTime = first?.startTime ?? '09:00'
    trip.endDate = last?.endDate ?? ''
    trip.endTime = last?.endTime ?? '18:00'
    if (!changed) return
    totals.value = null
    calculatedSignature.value = ''
  }

  async function loadCategories(force = false): Promise<boolean> {
    if (categoriesLoading.value) return categories.value.length > 0
    if (categories.value.length && !force) return true
    categoriesLoading.value = true
    categoryLoadError.value = ''
    try {
      const loaded = await getExpenseCategories()
      if (!loaded.some((item) => item.manualSelectable)) {
        throw new Error('no manually selectable expense category')
      }
      categories.value = loaded
      return true
    } catch {
      categories.value = []
      categoryLoadError.value = '费用类别加载失败，请重试后再添加费用明细'
      return false
    } finally {
      categoriesLoading.value = false
    }
  }

  function upsertManualItem(input: Omit<ExpenseItem, 'id' | 'source'> & { id?: string }): string {
    if (!manualCategories.value.some((category) => category.id === input.category)) {
      throw new Error('费用类别不可用，请重新加载类别后选择')
    }
    if (items.value.length >= maxExpenseItems.value && !input.id) {
      throw new Error(`当前最多添加 ${maxExpenseItems.value} 条票据费用明细`)
    }
    const amountCents = moneyToCents(input.amount)
    if (amountCents === null) throw new Error('金额必须为非负数，且最多两位小数')
    if (!Number.isInteger(input.receiptCount) || input.receiptCount < 1) {
      throw new Error('票据张数至少为 1')
    }
    const id = input.id ?? newItemId()
    const existing = items.value.find((item) => item.id === id)
    const source = existing?.source === 'ocr' ? 'ocr' : 'manual'
    const item: ExpenseItem = {
      ...input,
      receiptCount: existing?.source === 'ocr' || existing?.sourceFileId ? 1 : input.receiptCount,
      id,
      source,
      sourceFileId: existing?.sourceFileId,
      itineraryAutoMatchDisabled: input.itineraryAutoMatchDisabled ?? existing?.itineraryAutoMatchDisabled ?? false,
      amount: centsToMoney(amountCents),
      displayDate: input.displayDate.trim(),
      description: input.description.trim(),
      confidence: existing?.confidence,
      warnings: input.warnings,
    }
    if (!item.displayDate || !item.description) throw new Error('日期和说明不能为空')
    const index = items.value.findIndex((existing) => existing.id === id)
    if (index >= 0) items.value[index] = item
    else items.value.push(item)
    totals.value = null
    calculatedSignature.value = ''
    return id
  }

  function draftOcrExpenseItem(file: ReimbursementDraftFile): ExpenseItem | null {
    const candidate = receiptOcrResult(file)
    if (
      file.role !== 'EXPENSE_SOURCE'
      || file.status !== 'ACTIVE'
      || !['COMPLETE', 'FAILED'].includes(file.ocrStatus)
      || !candidate
      || candidate.fileId !== file.id
    ) return null

    const categoryId = typeof candidate.categoryId === 'string'
      ? candidate.categoryId.trim()
      : ''
    if (!categoryId) return null
    const confidenceNumber = Number(candidate.confidence)
    const confidence = Number.isFinite(confidenceNumber)
      ? Math.min(1, Math.max(0, confidenceNumber)).toFixed(2)
      : '0.00'
    const warnings = new Set(
      (Array.isArray(candidate.warnings) ? candidate.warnings : [])
        .filter((warning): warning is string => typeof warning === 'string' && Boolean(warning.trim())),
    )
    const validDate = isCalendarDate(candidate.date) ? candidate.date : undefined
    if (!validDate) warnings.add('MISSING_DATE')
    const foreign = Boolean(candidate.originalCurrency && candidate.originalCurrency !== 'CNY')
      || candidate.type === 'foreign_receipt'
      || candidate.warnings.includes('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
    if (foreign) warnings.add('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
    const amountCents = foreign || candidate.amount === null ? null : moneyToCents(candidate.amount)
    if (amountCents === null) warnings.add('MISSING_AMOUNT')
    const description = candidate.description?.trim()
      || (candidate.status === 'failed' ? file.name : candidate.categoryName.trim())
    if (!candidate.description?.trim()) warnings.add('MISSING_DESCRIPTION')
    if (warnings.size || candidate.status === 'failed') warnings.add('MANUAL_REVIEW_REQUIRED')
    return {
      id: `ocr-${file.id}`,
      sourceFileId: file.id,
      transportType: candidate.transportType,
      requiresItinerary: candidate.requiresItinerary || candidate.transportType === 'ride_hailing',
      itineraryFileIds: [],
      itineraryAutoMatchDisabled: false,
      paymentProofFileIds: [],
      hotelBillFileIds: [],
      railType: evidenceRailType(categoryId, undefined, candidate),
      originalCurrency: candidate.originalCurrency ?? undefined,
      originalAmount: candidate.originalAmount ?? undefined,
      cnyAmountConfirmed: false,
      requiresCnyConfirmation: foreign,
      category: categoryId,
      date: validDate,
      displayDate: validDate ?? '',
      description,
      amount: amountCents === null ? '' : centsToMoney(amountCents),
      receiptCount: 1,
      source: 'ocr',
      confidence,
      warnings: [...warnings],
    }
  }

  function upsertDraftOcrItem(file: ReimbursementDraftFile): boolean {
    const item = draftOcrExpenseItem(file)
    if (!item) return false
    const index = items.value.findIndex((existing) => existing.id === item.id)
    if (index < 0 && items.value.length >= maxExpenseItems.value) return false
    if (index >= 0) {
      item.itineraryFileIds = items.value[index]?.itineraryFileIds ?? []
      item.itineraryAutoMatchDisabled = items.value[index]?.itineraryAutoMatchDisabled ?? false
      item.paymentProofFileIds = items.value[index]?.paymentProofFileIds ?? []
      item.hotelBillFileIds = items.value[index]?.hotelBillFileIds ?? []
      item.railType = evidenceRailType(item.category, items.value[index]?.railType, receiptOcrResult(file))
      items.value[index] = item
    }
    else items.value.push(item)
    dismissedOcrFileIds.value = dismissedOcrFileIds.value.filter((id) => id !== file.id)
    if (file.ocrResult?.error?.code === 'OCR_DISABLED') ocrUnavailable.value = true
    totals.value = null
    calculatedSignature.value = ''
    return true
  }

  function hydrateFromDraft(
    draft: ReimbursementDraft,
    draftFiles: readonly ReimbursementDraftFile[],
  ): void {
    ocrUnavailable.value = false

    const project = draft.input.project
    manualProjectText.value = project?.mode === 'manual' ? project.text : ''

    const persistedTrips = draft.input.editingState?.trips?.length
      ? draft.input.editingState.trips
      : (draft.input.trips?.length ? draft.input.trips : [])
    const persistedTrip = draft.input.editingState?.trip ?? draft.input.trip ?? persistedTrips[0]
    includeSubsidy.value = draft.input.editingState?.includeSubsidy
      ?? Boolean(draft.input.trip || draft.input.trips?.length)
    Object.assign(trip, {
      tripType: persistedTrip?.tripType ?? 'business',
      startDate: persistedTrip?.startDate ?? '',
      startTime: persistedTrip?.startTime ?? '09:00',
      endDate: persistedTrip?.endDate ?? '',
      endTime: persistedTrip?.endTime ?? '18:00',
      policyConfirmed: persistedTrip?.policyConfirmed ?? false,
      confirmedEffectiveDays: persistedTrip?.confirmedEffectiveDays ?? '',
      noSubsidyException: persistedTrip?.noSubsidyException ?? false,
    })
    subsidyTrips.value = persistedTrips.map((item) => ({ ...item }))

    const candidates = draftFiles
      .map((file) => ({ file, item: draftOcrExpenseItem(file) }))
      .filter((entry): entry is { file: ReimbursementDraftFile; item: ExpenseItem } =>
        entry.item !== null,
      )
      .sort((left, right) => left.file.sortOrder - right.file.sortOrder)
    const candidateByFileId = new Map(candidates.map((entry) => [entry.file.id, entry]))
    const dismissed = new Set(
      (Array.isArray(draft.input.dismissedOcrFileIds)
        ? draft.input.dismissedOcrFileIds
        : [])
        .filter((id): id is string => typeof id === 'string' && Boolean(id.trim()))
        .map((id) => id.trim()),
    )
    const linkedFileIds = new Set<string>()
    const persistedItems = Array.isArray(draft.input.items) ? draft.input.items : []
    items.value = persistedItems.map((persisted, index) => {
      const amountCents = moneyToCents(persisted.amount ?? '')
      const validDate = isCalendarDate(persisted.date) ? persisted.date : undefined
      const sourceFileId = typeof persisted.sourceFileId === 'string'
        ? persisted.sourceFileId.trim()
        : ''
      if (sourceFileId) linkedFileIds.add(sourceFileId)
      const candidate = sourceFileId ? candidateByFileId.get(sourceFileId)?.item : undefined
      return {
        id: sourceFileId ? `ocr-${sourceFileId}` : `draft-${draft.id}-item-${index}`,
        ...(sourceFileId ? { sourceFileId } : {}),
        transportType: persisted.transportType ?? candidate?.transportType,
        itineraryFileIds: [...(persisted.itineraryFileIds ?? [])],
        itineraryAutoMatchDisabled: persisted.itineraryAutoMatchDisabled ?? false,
        paymentProofFileIds: [...(persisted.paymentProofFileIds ?? [])],
        hotelBillFileIds: [...(persisted.hotelBillFileIds ?? [])],
        railType: evidenceRailType(persisted.category, persisted.railType, receiptOcrResult(
          draftFiles.find((file) => file.id === sourceFileId),
        )),
        requiresItinerary: persisted.requiresItinerary || candidate?.requiresItinerary || false,
        originalCurrency: persisted.originalCurrency ?? candidate?.originalCurrency,
        originalAmount: persisted.originalAmount ?? candidate?.originalAmount,
        cnyAmountConfirmed: persisted.cnyAmountConfirmed ?? false,
        requiresCnyConfirmation: persisted.requiresCnyConfirmation ?? candidate?.requiresCnyConfirmation ?? false,
        category: typeof persisted.category === 'string' ? persisted.category : '',
        date: validDate,
        displayDate: typeof persisted.displayDate === 'string'
          ? persisted.displayDate.trim()
          : '',
        description: typeof persisted.description === 'string'
          ? persisted.description.trim()
          : '',
        amount: amountCents === null ? '' : centsToMoney(amountCents),
        receiptCount: !(sourceFileId && ['DRAFT', 'REVIEW_READY'].includes(draft.status))
          && Number.isSafeInteger(persisted.receiptCount)
          && persisted.receiptCount > 0
          ? persisted.receiptCount
          : 1,
        source: sourceFileId ? 'ocr' : 'manual',
        confidence: candidate?.confidence,
        warnings: candidate?.warnings ?? [],
      }
    })
    dismissedOcrFileIds.value = [...dismissed]
    const undecidedCandidates = candidates.filter(
      ({ file }) => !linkedFileIds.has(file.id) && !dismissed.has(file.id),
    )
    const recovered = undecidedCandidates.map(({ item }) => item)
    items.value.push(...recovered)
    for (const item of items.value) item.warnings = itineraryConfirmationWarnings(item, draftFiles)
    ocrUnavailable.value = draftFiles.some(
      (file) => file.ocrResult?.error?.code === 'OCR_DISABLED',
    )
    totals.value = recovered.length
      ? null
      : {
          ...draft.totals,
          subsidy: draft.totals.subsidy ? { ...draft.totals.subsidy } : null,
          subsidies: (draft.totals.subsidies ?? (draft.totals.subsidy ? [draft.totals.subsidy] : []))
            .map((item) => ({ ...item })),
        }
    calculationError.value = ''
    calculating.value = false
    calculationVersion += 1
    calculatedSignature.value = recovered.length ? '' : calculationSignature(
      subsidyTrips.value.length ? tripPayloads() : tripPayload(),
    )
  }

  function removeItem(id: string): void {
    const removed = items.value.find((item) => item.id === id)
    if (
      removed?.sourceFileId
      && !dismissedOcrFileIds.value.includes(removed.sourceFileId)
    ) {
      dismissedOcrFileIds.value = [
        ...dismissedOcrFileIds.value,
        removed.sourceFileId,
      ]
    }
    items.value = items.value.filter((item) => item.id !== id)
    totals.value = null
    calculatedSignature.value = ''
  }

  function removeDraftFileAssociation(fileId: string): void {
    const previousLength = items.value.length
    items.value = items.value.filter((item) => item.sourceFileId !== fileId)
    for (const item of items.value) {
      item.itineraryFileIds = item.itineraryFileIds?.filter((id) => id !== fileId)
      item.paymentProofFileIds = item.paymentProofFileIds?.filter((id) => id !== fileId)
      item.hotelBillFileIds = item.hotelBillFileIds?.filter((id) => id !== fileId)
    }
    dismissedOcrFileIds.value = dismissedOcrFileIds.value.filter((id) => id !== fileId)
    if (items.value.length !== previousLength) {
      totals.value = null
      calculatedSignature.value = ''
    }
  }

  function dismissDraftOcrFile(fileId: string): boolean {
    const normalized = fileId.trim()
    if (!normalized || items.value.some((item) => item.sourceFileId === normalized)) return false
    if (!dismissedOcrFileIds.value.includes(normalized)) {
      dismissedOcrFileIds.value = [...dismissedOcrFileIds.value, normalized]
    }
    return true
  }

  function reconcileDraftProofs(draftFiles: readonly ReimbursementDraftFile[]): void {
    for (const item of items.value) {
      const itineraryIds = (item.itineraryFileIds ?? []).filter((id) => draftFiles.some((file) => file.id === id && isActiveProof(file, 'itinerary')))
      const paymentIds = (item.paymentProofFileIds ?? []).filter((id) => draftFiles.some((file) => file.id === id && isActiveProof(file, 'payment_proof')))
      const hotelIds = (item.hotelBillFileIds ?? []).filter((id) => draftFiles.some((file) => file.id === id && isActiveProof(file, 'hotel_bill')))
      if (itineraryIds.length !== (item.itineraryFileIds?.length ?? 0)) item.itineraryFileIds = itineraryIds
      if (paymentIds.length !== (item.paymentProofFileIds?.length ?? 0)) item.paymentProofFileIds = paymentIds
      if (hotelIds.length !== (item.hotelBillFileIds?.length ?? 0)) item.hotelBillFileIds = hotelIds
    }
  }

  function matchDraftItineraries(draftFiles: readonly ReimbursementDraftFile[]): number {
    const matches = matchItineraries(items.value, draftFiles)
    for (const match of matches) {
      const item = items.value.find((item) => item.sourceFileId === match.sourceFileId)
      if (item && !item.itineraryFileIds?.length) item.itineraryFileIds = [match.itineraryFileId]
    }
    return matches.length
  }

  async function refreshCalculations(): Promise<void> {
    const payload = subsidyTrips.value.length ? tripPayloads() : tripPayload()
    const version = ++calculationVersion
    calculatedSignature.value = ''
    calculationError.value = includeSubsidy.value ? policyInputError.value : ''
    if (includeSubsidy.value && (policyInputError.value || !payload || (Array.isArray(payload)
      && payload.length !== subsidyTrips.value.length))) {
      totals.value = null
      calculating.value = false
      return
    }
    if (itemReadinessError.value) {
      totals.value = null
      calculating.value = false
      calculationError.value = itemReadinessError.value
      return
    }
    calculating.value = true
    try {
      const result = await calculateTotals(payload, items.value)
      if (version === calculationVersion) {
        totals.value = result
        calculatedSignature.value = calculationSignature(payload)
      }
    } catch (error) {
      if (version !== calculationVersion) return
      totals.value = null
      calculationError.value = axios.isAxiosError(error)
        ? (error.response?.data as { error?: { message?: string } } | undefined)?.error?.message ??
          '金额计算失败，请检查填写内容'
        : '金额计算失败，请检查填写内容'
    } finally {
      if (version === calculationVersion) calculating.value = false
    }
  }

  const calculationsCurrent = computed(() => {
    const payload = subsidyTrips.value.length ? tripPayloads() : tripPayload()
    if (includeSubsidy.value && (policyInputError.value || !payload || (Array.isArray(payload)
      && payload.length !== subsidyTrips.value.length))) return false
    return Boolean(
      totals.value && calculatedSignature.value === calculationSignature(payload),
    )
  })

  function buildDraftExpenseItems(): ReimbursementDraftExpenseItemInput[] {
    return items.value.map((item) => ({
      ...(item.sourceFileId ? { sourceFileId: item.sourceFileId } : {}),
      transportType: item.transportType,
      itineraryFileIds: [...(item.itineraryFileIds ?? [])],
      itineraryAutoMatchDisabled: item.itineraryAutoMatchDisabled ?? false,
      paymentProofFileIds: [...(item.paymentProofFileIds ?? [])],
      hotelBillFileIds: [...(item.hotelBillFileIds ?? [])],
      railType: item.railType ?? 'unknown',
      requiresItinerary: item.requiresItinerary ?? false,
      originalCurrency: item.originalCurrency,
      originalAmount: item.originalAmount,
      cnyAmountConfirmed: item.cnyAmountConfirmed ?? false,
      requiresCnyConfirmation: item.requiresCnyConfirmation ?? false,
      category: item.category,
      date: item.date || null,
      displayDate: item.displayDate,
      description: item.description,
      amount: item.amount || null,
      receiptCount: item.receiptCount,
    }))
  }

  function setTripType(value: TripType): void {
    trip.tripType = value
    trip.policyConfirmed = false
    trip.confirmedEffectiveDays = ''
    trip.noSubsidyException = false
    subsidyTrips.value = subsidyTrips.value.map((item) => ({
      ...item,
      tripType: value,
      policyConfirmed: false,
      confirmedEffectiveDays: undefined,
      noSubsidyException: undefined,
    }))
    totals.value = null
    calculatedSignature.value = ''
  }

  function setSubsidyIncluded(value: boolean): void {
    includeSubsidy.value = value
    totals.value = null
    calculatedSignature.value = ''
    calculationError.value = ''
  }

  function setReceiptUploadLimits(limits: ReceiptUploadLimits): void {
    receiptUploadLimits.value = { ...limits }
  }

  function setExpenseItemLimit(maxItems: number): void {
    if (Number.isSafeInteger(maxItems) && maxItems > 0) {
      maxExpenseItems.value = maxItems
    }
  }

  async function removeExpenseItem(id: string): Promise<boolean> {
    removeItem(id)
    return true
  }

  function reset(): void {
    manualProjectText.value = ''
    Object.assign(trip, {
      tripType: 'business' as TripType,
      startDate: '',
      startTime: '09:00',
      endDate: '',
      endTime: '18:00',
      policyConfirmed: false,
      confirmedEffectiveDays: '',
      noSubsidyException: false,
    })
    subsidyTrips.value = []
    items.value = []
    dismissedOcrFileIds.value = []
    includeSubsidy.value = false
    ocrUnavailable.value = false
    totals.value = null
    calculatedSignature.value = ''
    calculating.value = false
    calculationError.value = ''
    calculationVersion += 1
  }

  return {
    manualProjectText,
    trip,
    subsidyTrips,
    items,
    dismissedOcrFileIds,
    sortedItems,
    includeSubsidy,
    receiptUploadLimits,
    ocrUnavailable,
    categories,
    categoriesLoading,
    categoryLoadError,
    totals,
    calculating,
    calculationError,
    requiresPolicyConfirmation,
    projectCalendarDays,
    projectPolicyType,
    maxExpenseItems,
    policyInputError,
    itemReadinessError,
    manualCategories,
    localReceiptCount,
    displayExpenseTotal,
    displaySubsidyTotal,
    displayReceiptCount,
    displayTotal,
    calculationsCurrent,
    tripPayload,
    tripPayloads,
    syncSubsidyApprovals,
    loadCategories,
    upsertManualItem,
    upsertDraftOcrItem,
    hydrateFromDraft,
    removeItem,
    removeDraftFileAssociation,
    dismissDraftOcrFile,
    reconcileDraftProofs,
    matchDraftItineraries,
    removeExpenseItem,
    refreshCalculations,
    buildDraftExpenseItems,
    setTripType,
    setSubsidyIncluded,
    setReceiptUploadLimits,
    setExpenseItemLimit,
    reset,
  }
})

import { isForeignExpense, type ExpenseItem } from '@/types/expenses'
import { isItineraryOcrResult, type OcrReceiptCandidate } from '@/types/receipts'
import { receiptOcrResult, type ReimbursementDraftFile } from '@/types/reimbursements'
import { isActiveProof } from './expenseProofs'
import { moneyToCents } from './money'

export type ExpenseReviewField = 'category' | 'date' | 'description' | 'amount' | 'itinerary'
export interface ExpenseReviewHint { code: string; field: ExpenseReviewField; message: string }
const DATE_PLACEHOLDERS = ['INVOICE_DATE_USED_AS_OCCURRENCE', 'QR_ISSUE_DATE_USED']
const DERIVED_WARNINGS = ['ITINERARY_AMOUNT_MISMATCH', 'ITINERARY_DATE_MISMATCH', 'ITINERARY_IDENTIFIER_MISMATCH']

function calendarDate(value: string | undefined | null): boolean {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
}

function itineraryEvidence(item: ExpenseItem, files: readonly ReimbursementDraftFile[], source: OcrReceiptCandidate | null, allowPurged: boolean) {
  const ids = item.itineraryFileIds ?? []
  if (new Set(ids).size !== ids.length) return null
  const results = ids.map(id => {
    const file = files.find(file => file.id === id)
    const result = file?.ocrResult
    const recorded = file && (isActiveProof(file, 'itinerary') || (allowPurged && file.status === 'PURGED'
      && file.role === 'ATTACHMENT_ONLY' && file.attachmentKind === 'itinerary'))
    return recorded && file.ocrStatus === 'COMPLETE' && file.ocrStale !== true
      && isItineraryOcrResult(result) && result.fileId === file.id
      && result.status === 'recognized' && result.complete && !result.error
      && Array.isArray(result.warnings) && !result.warnings.length
      && result.pageCount > 0 && result.processedPageCount === result.pageCount
      && Array.isArray(result.trips) && result.trips.length
      ? result : null
  })
  if (!ids.length || results.some(result => !result)) return null
  const clean = results.filter(result => result !== null)
  const trips = clean.flatMap(result => result.trips)
  const conflict = (['invoiceNumbers', 'orderNumbers'] as const).some(field => {
    const normalized = (values: readonly string[]) => new Set(values.map(value => value.trim().toUpperCase()).filter(Boolean))
    const left = normalized(source?.[field] ?? [])
    return clean.some(result => {
      // Compare each file independently. A shared PDF can contain more than
      // one invoice, but a matching file cannot excuse another unrelated file.
      const right = normalized([...(result.summary[field] ?? []), ...result.trips.flatMap(trip => trip[field] ?? [])])
      return left.size > 0 && right.size > 0 && ![...left].some(value => right.has(value))
    })
  })
  const dates = new Set(trips.map(trip => trip.date))
  const date = dates.size === 1 && calendarDate(trips[0]?.date) ? trips[0]!.date : null
  // A multi-trip PDF can be shared by several expenses: its whole total is not
  // proof of the amount belonging to any one expense.
  const singleTripFiles = clean.every(result => result.trips.length === 1 && result.summary.currency === 'CNY'
    && moneyToCents(result.summary.amount ?? '') !== null
    && moneyToCents(result.trips[0]!.amount ?? '') === moneyToCents(result.summary.amount ?? ''))
  const amount = singleTripFiles ? trips.reduce((total, trip) => total + BigInt(moneyToCents(trip.amount ?? '')!), 0n) : null
  return { trips, date, amount, conflict }
}

export function expenseRequiresCnyConfirmation(
  item: Pick<ExpenseItem, 'originalCurrency' | 'requiresCnyConfirmation' | 'warnings'>,
  source: OcrReceiptCandidate | null,
): boolean {
  // With durable evidence, transient display warnings are never an input.
  // Original-currency fields remain optional, even when CNY needs confirming.
  return Boolean(item.requiresCnyConfirmation)
    || Boolean(item.originalCurrency && item.originalCurrency.toUpperCase() !== 'CNY')
    || Boolean(source && (source.type === 'foreign_receipt'
      || (source.originalCurrency && source.originalCurrency.toUpperCase() !== 'CNY')
      || source.warnings.includes('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
      || source.warnings.includes('FOREIGN_CURRENCY_REQUIRES_CONFIRMATION')))
    || (!source && isForeignExpense(item))
}

/** Rebuild display warnings from immutable OCR evidence and the current input. */
export function expenseReviewWarnings(item: ExpenseItem, files: readonly ReimbursementDraftFile[]): string[] {
  const sourceFile = files.find(file => file.id === item.sourceFileId && file.role === 'EXPENSE_SOURCE')
  const candidate = receiptOcrResult(sourceFile)
  const source = candidate && sourceFile && candidate.fileId === sourceFile.id ? candidate : null
  // item.warnings is a compatibility fallback only. Old derived review flags
  // must not outlive their original cause, nor conceal a later OCR result.
  const warnings = new Set((source ? source.warnings : item.warnings ?? []).filter(code => !DERIVED_WARNINGS.includes(code)))
  if (source?.status === 'failed') warnings.add('MANUAL_REVIEW_REQUIRED')
  const validDate = calendarDate(item.date)
  const completeDate = validDate && Boolean(item.displayDate.trim())
  const amount = moneyToCents(item.amount)
  const foreign = expenseRequiresCnyConfirmation(item, source)
  const proof = itineraryEvidence(item, files, source, sourceFile?.status === 'PURGED')
  const confirmed = item.itineraryAutoMatchDisabled && proof && !proof.conflict
  const confirmedDate = Boolean(confirmed && proof.date && validDate && item.date === proof.date && item.displayDate === item.date)
  const editedDate = Boolean(item.dateEdited && completeDate)
  const description = item.description.trim()
  const editedDescription = Boolean(item.descriptionEdited && description)
  const confirmedRoute = Boolean(confirmed && proof.trips.length === 1 && proof.trips[0]!.origin && proof.trips[0]!.destination
    && description === `${proof.trips[0]!.origin} → ${proof.trips[0]!.destination}`)
  if (completeDate) warnings.delete('MISSING_DATE')
  if (confirmedDate || editedDate) for (const code of DATE_PLACEHOLDERS) warnings.delete(code)
  if (description) warnings.delete('MISSING_DESCRIPTION')
  if (confirmedRoute || editedDescription) warnings.delete('MISSING_ROUTE')
  if (amount !== null) warnings.delete('MISSING_AMOUNT')
  if (!foreign || (item.cnyAmountConfirmed && amount !== null)) {
    warnings.delete('FOREIGN_CURRENCY_REQUIRES_CONFIRMATION')
    warnings.delete('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
  }
  if (item.source === 'ocr') {
    if (!completeDate) warnings.add('MISSING_DATE')
    if (amount === null) warnings.add('MISSING_AMOUNT')
  }
  if (foreign && !item.cnyAmountConfirmed) warnings.add('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT')
  if (proof && item.transportType === 'ride_hailing') {
    if (proof.conflict) warnings.add('ITINERARY_IDENTIFIER_MISMATCH')
    if (proof.date && validDate && item.date !== proof.date) warnings.add('ITINERARY_DATE_MISMATCH')
    if (!foreign && proof.amount !== null && amount !== null && BigInt(amount) !== proof.amount) warnings.add('ITINERARY_AMOUNT_MISMATCH')
  }
  return [...warnings]
}

const HINTS: Record<string, readonly [ExpenseReviewField, string]> = {
  MANUAL_REVIEW_REQUIRED: ['category', '识别依据不足，请核对费用类别和原件'],
  MISSING_AMOUNT: ['amount', '请填写人民币报销金额'],
  MISSING_DATE: ['date', '请补充发生日期'],
  MISSING_DESCRIPTION: ['description', '请补充费用说明'],
  MISSING_ROUTE: ['description', '行程路线识别不完整，请核对起终点'],
  LOW_CONFIDENCE: ['description', '识别置信度较低，请对照原件核对'],
  LOW_OCR_CONFIDENCE: ['description', '识别置信度较低，请对照原件核对'],
  QR_AMOUNT_REQUIRES_REVIEW: ['amount', '金额取自二维码，请核对价税合计'],
  QR_AMOUNT_MISMATCH: ['amount', '二维码与票面金额不一致，请核对'],
  QR_ISSUE_DATE_USED: ['date', '发生日期暂用二维码开票日期，请核对'],
  INVOICE_DATE_USED_AS_OCCURRENCE: ['date', '发生日期暂用开票日期，请核对'],
  FOREIGN_CURRENCY_REQUIRES_CONFIRMATION: ['amount', '请确认人民币金额'],
  FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT: ['amount', '请确认人民币金额'],
  ITINERARY_AMOUNT_MISMATCH: ['amount', '金额与关联行程不一致，请核对'],
  ITINERARY_DATE_MISMATCH: ['date', '费用日期与关联行程不一致，请核对'],
  ITINERARY_IDENTIFIER_MISMATCH: ['itinerary', '发票或订单编号与关联行程不一致，请核对关联'],
}

export function expenseReviewHints(item: ExpenseItem, files: readonly ReimbursementDraftFile[]): ExpenseReviewHint[] {
  const seen = new Set<string>()
  const warnings = expenseReviewWarnings(item, files)
  return warnings.flatMap(code => {
    // The material component already provides an actionable missing-file hint.
    if (code === 'MISSING_ITINERARY') return []
    if (DATE_PLACEHOLDERS.includes(code) && (warnings.includes('MISSING_DATE') || warnings.includes('ITINERARY_DATE_MISMATCH'))) return []
    if (code === 'INVOICE_DATE_USED_AS_OCCURRENCE' && warnings.includes('QR_ISSUE_DATE_USED')) return []
    if (code === 'QR_AMOUNT_REQUIRES_REVIEW' && warnings.includes('QR_AMOUNT_MISMATCH')) return []
    const [field, message] = HINTS[code] ?? ['description', '识别内容存在疑点，请对照原件核对']
    if (seen.has(message)) return []
    seen.add(message)
    return [{ code, field, message }]
  })
}

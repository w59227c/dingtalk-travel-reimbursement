import type { ExpenseItem, RailType } from '@/types/expenses'
import type { OcrReceiptCandidate } from '@/types/receipts'
import type { ReimbursementAttachmentKind, ReimbursementDraftFile } from '@/types/reimbursements'
import { moneyToCents } from '@/utils/money'

export const expenseProofFields = {
  itinerary: 'itineraryFileIds',
  payment_proof: 'paymentProofFileIds',
  hotel_bill: 'hotelBillFileIds',
  waybill: 'waybillFileIds',
  other: 'receiptAttachmentFileIds',
} as const satisfies Record<ReimbursementAttachmentKind, keyof ExpenseItem>

type ExpenseProofReferences = Pick<ExpenseItem, typeof expenseProofFields[ReimbursementAttachmentKind]>

export function isRailExpense(
  item: Pick<ExpenseItem, 'category'> & Partial<Pick<ExpenseItem, 'transportType'>>,
  generalMode = false,
): boolean {
  return item.category === 'rail_fare'
    || (generalMode && item.category === 'local_transport' && item.transportType === 'rail')
}

export function requiresPaymentProof(item: Pick<ExpenseItem, 'amount' | 'category' | 'railType'>
  & Partial<Pick<ExpenseItem, 'receiptCount' | 'sourceFileId' | 'source' | 'subjectValue' | 'transportType'>>): boolean {
  const count = item.sourceFileId || item.source === 'ocr' ? 1 : item.receiptCount ?? 1
  const rail = isRailExpense(item, Boolean(item.subjectValue))
  return count === 1 && (moneyToCents(item.amount) ?? 0) > 50_000
    && !(rail && item.railType === 'high_speed')
}

export function isActiveProof(file: ReimbursementDraftFile, kind: ReimbursementAttachmentKind): boolean {
  return file.status === 'ACTIVE' && file.role === 'ATTACHMENT_ONLY' && file.attachmentKind === kind
    && !needsMaterialConfirmation(file)
}

export function isRecordedProof(
  file: ReimbursementDraftFile,
  kind: ReimbursementAttachmentKind,
  allowPurged = false,
): boolean {
  return (isActiveProof(file, kind) || (allowPurged && file.status === 'PURGED'
    && file.role === 'ATTACHMENT_ONLY' && file.attachmentKind === kind))
}

/** Keep file-list display order; callers reconciling references retain their own ID order. */
export function linkedExpenseProofs(
  item: ExpenseProofReferences,
  files: readonly ReimbursementDraftFile[],
  kind: ReimbursementAttachmentKind,
  options: { allowPurged?: boolean } = {},
): ReimbursementDraftFile[] {
  const ids = new Set(item[expenseProofFields[kind]] ?? [])
  if (!ids.size) return []
  return files.filter(file => ids.has(file.id) && isRecordedProof(file, kind, options.allowPurged))
}

export function needsMaterialConfirmation(file: ReimbursementDraftFile): boolean {
  return file.status !== 'PURGED' && ['pending', 'needs_confirmation'].includes(file.materialClassification?.status ?? '')
}

export function isUnresolvedExpenseSourceFile(
  file: ReimbursementDraftFile,
  items: ReadonlyArray<Pick<ExpenseItem, 'sourceFileId'>>,
  dismissedOcrFileIds: readonly string[],
): boolean {
  return file.status === 'ACTIVE'
    && file.role === 'EXPENSE_SOURCE'
    && ['COMPLETE', 'FAILED'].includes(file.ocrStatus)
    && !items.some((item) => item.sourceFileId === file.id)
    && !dismissedOcrFileIds.includes(file.id)
}

export function materialSubmissionBlockReason(
  file: ReimbursementDraftFile,
  items: ReadonlyArray<Pick<ExpenseItem, 'sourceFileId' | 'itineraryFileIds' | 'paymentProofFileIds' | 'hotelBillFileIds'>>,
  dismissedOcrFileIds: readonly string[],
): string {
  if (file.status === 'RESERVED') return '文件等待上传'
  if (file.status === 'WRITING') return '文件仍在上传'
  if (file.status === 'DELETING') return '文件正在删除'
  if (file.status !== 'ACTIVE') return ''
  if (file.ocrStatus === 'RUNNING') return '材料仍在识别'
  if (needsMaterialConfirmation(file)) {
    return file.materialClassification?.status === 'pending' ? '材料用途仍在识别' : '材料用途待确认'
  }
  if (file.role === 'EXPENSE_SOURCE' && file.ocrStatus === 'NOT_REQUESTED') return '票据尚未识别'
  if (isUnresolvedExpenseSourceFile(file, items, dismissedOcrFileIds)) return '票据尚未加入费用明细'
  const proof = unlinkedProofLabel(file, items)
  if (proof) return `${proof}尚未关联费用`
  return ''
}

export function unlinkedProofLabel(
  file: ReimbursementDraftFile,
  items: ReadonlyArray<Pick<ExpenseItem, 'itineraryFileIds' | 'paymentProofFileIds' | 'hotelBillFileIds'>>,
): string {
  if (file.status !== 'ACTIVE' || file.role !== 'ATTACHMENT_ONLY'
    || file.ocrStatus === 'RUNNING' || needsMaterialConfirmation(file)) return ''
  for (const [kind, label] of [
    ['itinerary', '行程单'],
    ['payment_proof', '付款凭证'],
    ['hotel_bill', '住宿明细'],
  ] as const) {
    const field = expenseProofFields[kind]
    if (file.attachmentKind === kind && !items.some(item => item[field]?.includes(file.id))) return label
  }
  return ''
}

export function missingExpenseMaterials(
  item: ExpenseItem,
  files: readonly ReimbursementDraftFile[],
  options: { allowPurged?: boolean } = {},
): string[] {
  const missing: string[] = []
  if (item.transportType !== 'taxi' && (item.requiresItinerary || item.transportType === 'ride_hailing')
    && !linkedExpenseProofs(item, files, 'itinerary', options).length) missing.push('行程单')
  if (requiresPaymentProof(item) && !linkedExpenseProofs(item, files, 'payment_proof', options).length) missing.push('付款凭证')
  if (item.category === 'lodging' && !linkedExpenseProofs(item, files, 'hotel_bill', options).length) missing.push('住宿明细')
  return missing
}

export function evidenceRailType(category: string, selected: RailType | undefined, evidence: OcrReceiptCandidate | null): RailType {
  if (category !== 'rail_fare' || hasKnownNonRailEvidence(evidence)) return 'unknown'
  const observed = evidence?.railType
  if (!observed || observed === 'unknown') return selected ?? 'unknown'
  if (!selected || selected === 'unknown') return observed
  // OCR evidence may grant the high-speed payment-proof exemption. Employees
  // may safely correct it to a stricter non-exempt type, but not upgrade
  // contrary non-high-speed evidence into an exemption.
  if (selected === 'high_speed' && observed !== 'high_speed') return observed
  return selected
}

export function hasKnownNonRailEvidence(evidence: OcrReceiptCandidate | null): boolean {
  return Boolean(evidence?.categoryId && !['rail_fare', 'other'].includes(evidence.categoryId))
}

export function isCourierExpense(item: Pick<ExpenseItem, 'category' | 'welfareType'>): boolean {
  return item.category === 'express' || (item.category === 'employee_welfare' && item.welfareType === 'express')
}

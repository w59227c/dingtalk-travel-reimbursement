import type { ExpenseItem, RailType } from '@/types/expenses'
import type { OcrReceiptCandidate } from '@/types/receipts'
import type { ReimbursementAttachmentKind, ReimbursementDraftFile } from '@/types/reimbursements'
import { moneyToCents } from '@/utils/money'

export function requiresPaymentProof(item: Pick<ExpenseItem, 'amount' | 'category' | 'railType'>): boolean {
  return (moneyToCents(item.amount) ?? 0) > 50_000
    && !(item.category === 'rail_fare' && item.railType === 'high_speed')
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
  items: ReadonlyArray<Pick<ExpenseItem, 'sourceFileId'>>,
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
  return ''
}

export function missingExpenseMaterials(
  item: ExpenseItem,
  files: ReimbursementDraftFile[],
  options: { allowPurged?: boolean } = {},
): string[] {
  const missing: string[] = []
  if ((item.requiresItinerary || item.transportType === 'ride_hailing') && !item.itineraryFileIds?.some((id) =>
    files.some((file) => file.id === id && isRecordedProof(file, 'itinerary', options.allowPurged)))) missing.push('行程单')
  if (requiresPaymentProof(item) && !item.paymentProofFileIds?.some((id) =>
    files.some((file) => file.id === id && isRecordedProof(file, 'payment_proof', options.allowPurged)))) missing.push('付款凭证')
  if (item.category === 'lodging' && !item.hotelBillFileIds?.some((id) =>
    files.some((file) => file.id === id && isRecordedProof(file, 'hotel_bill', options.allowPurged)))) missing.push('住宿明细')
  return missing
}

export function evidenceRailType(category: string, selected: RailType | undefined, evidence: OcrReceiptCandidate | null): RailType {
  if (category !== 'rail_fare' || hasKnownNonRailEvidence(evidence)) return 'unknown'
  return evidence?.railType && evidence.railType !== 'unknown' ? evidence.railType : selected ?? 'unknown'
}

export function hasKnownNonRailEvidence(evidence: OcrReceiptCandidate | null): boolean {
  return Boolean(evidence?.categoryId && !['rail_fare', 'other'].includes(evidence.categoryId))
}

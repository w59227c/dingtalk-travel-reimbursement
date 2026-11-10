import type { TripInput } from '@/types/expenses'
import type { ReimbursementDraftInput } from '@/types/reimbursements'
import { adjustmentPayload } from '@/utils/subsidyAdjustments'

function tripSignature(trip: TripInput | null | undefined): object | null {
  if (!trip) return null
  return {
    // The API writes an absent editingState approval ID as null.
    relatedApprovalId: trip.relatedApprovalId ?? undefined,
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
function amountSignature(value: string | null | undefined): string | null | undefined {
  if (typeof value !== 'string') return value
  const match = /^(0|[1-9]\d*)(?:\.(\d{1,2}))?$/.exec(value)
  if (!match) return value
  // Compare decimal strings without losing precision on large original amounts.
  const whole = match[1]!
  const fraction = (match[2] ?? '').replace(/0+$/, '')
  return fraction ? `${whole}.${fraction}` : whole
}
export function inputSignature(input: ReimbursementDraftInput): string {
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
      originalAmount: amountSignature(item.originalAmount),
      originalDetailsEdited: item.originalDetailsEdited ?? false,
      cnyAmountConfirmed: item.cnyAmountConfirmed ?? false,
      requiresCnyConfirmation: item.requiresCnyConfirmation ?? false,
      category: item.category,
      date: item.date,
      displayDate: item.displayDate,
      description: item.description,
      dateEdited: item.dateEdited ?? false,
      descriptionEdited: item.descriptionEdited ?? false,
      amount: amountSignature(item.amount),
      receiptCount: item.receiptCount,
    })),
    dismissedOcrFileIds: input.dismissedOcrFileIds,
  })
}

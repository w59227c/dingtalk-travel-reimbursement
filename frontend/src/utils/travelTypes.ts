import type { TripType } from '@/types/expenses'
import type { OaFormOption } from '@/types/oa'
import type { OaReimbursementTravelProfile } from '@/types/reimbursements'

export function mappedTravelTypeOption(
  profile: OaReimbursementTravelProfile | undefined,
  sourceTravelTypeValue: string | null | undefined,
): OaFormOption | null {
  if (!profile) return null
  if (profile.travelTypeMappings) {
    return sourceTravelTypeValue
      ? profile.travelTypeMappings[sourceTravelTypeValue] ?? null
      : null
  }
  return profile.travelTypeOption
}

export function subsidyTripTypeForProfile(
  profile: OaReimbursementTravelProfile | undefined,
  sourceTravelTypeValue: string | null | undefined,
): TripType | null {
  if (!profile) return null
  if (profile.subsidyTripTypeMappings) {
    return sourceTravelTypeValue
      ? profile.subsidyTripTypeMappings[sourceTravelTypeValue] ?? null
      : null
  }
  return profile.subsidyTripType
    ?? subsidyTripTypeForTravelLabel(sourceTravelTypeValue ?? '')
    ?? subsidyTripTypeForTravelLabel(
      mappedTravelTypeOption(profile, sourceTravelTypeValue)?.label ?? '',
    )
}

/** Compatibility fallback for API responses created before stable policy types existed. */
export function subsidyTripTypeForTravelLabel(label: string): TripType | null {
  const normalized = label.replace(/\s+/g, '')
  if (!normalized) return null
  const longTerm = normalized.includes('长期')
  const shortTerm = normalized.includes('短期')
  if (normalized.includes('境外')) {
    return longTerm ? 'long_term_overseas' : shortTerm ? 'short_term_overseas' : null
  }
  if (normalized.includes('同市') || normalized.includes('市内项目')) return 'same_city_project'
  if (normalized.includes('公司内部')) {
    return longTerm ? 'long_term_internal' : shortTerm ? 'short_term_internal' : null
  }
  if (normalized.includes('市外项目')) {
    return longTerm ? 'long_term_project' : shortTerm ? 'short_term_project' : null
  }
  if (normalized.includes('商务')) return 'business'
  return null
}

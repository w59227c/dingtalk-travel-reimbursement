import { isForeignExpense, isTaxiExpense, type ExpenseItem } from '@/types/expenses'
import { isItineraryOcrResult, type ItineraryOcrResult } from '@/types/receipts'
import { receiptOcrResult, type ReimbursementDraftFile } from '@/types/reimbursements'
import { isActiveProof, needsMaterialConfirmation } from '@/utils/expenseProofs'
import { moneyToCents } from '@/utils/money'

interface NumberedEvidence { invoiceNumbers?: string[]; orderNumbers?: string[] }
interface SourceInvoice { item: ExpenseItem; tokens: Set<string> }
interface ItineraryEvidence { fileId: string; result: ItineraryOcrResult; tokens: Set<string> }
export interface ItineraryMatch {
  sourceFileId: string
  itineraryFileId: string
  basis: 'identifier' | 'amount_date_route'
}
export interface ItinerarySuggestion {
  sourceFileId: string
  itineraryFileId: string
  basis: 'amount_date_route' | 'amount_only'
  transportType: 'ride_hailing'
  trip?: ItineraryOcrResult['trips'][number]
  documentSummary?: { amount: string; tripCount: number; startDate: string; endDate: string }
  /** Only applied after employee confirmation, and recomputed at click time. */
  fill?: Partial<Pick<ExpenseItem, 'date' | 'displayDate' | 'description'>>
}

function identifiers(evidence: NumberedEvidence): Set<string> {
  const tokens = new Set<string>()
  for (const [kind, numbers] of [['invoice', evidence.invoiceNumbers], ['order', evidence.orderNumbers]] as const) {
    for (const number of numbers ?? []) {
      const normalized = number.trim().toUpperCase()
      if (normalized) tokens.add(`${kind}:${normalized}`)
    }
  }
  return tokens
}

function conflictingIdentifiers(left: Set<string>, right: Set<string>): boolean {
  return ['invoice:', 'order:'].some((prefix) => {
    const leftNumbers = [...left].filter((token) => token.startsWith(prefix))
    const rightNumbers = [...right].filter((token) => token.startsWith(prefix))
    return leftNumbers.length > 0 && rightNumbers.length > 0
      && !leftNumbers.some((token) => right.has(token))
  })
}

function matchingTrip(invoice: SourceInvoice, trip: ItineraryOcrResult['trips'][number]): boolean {
  const amount = moneyToCents(invoice.item.amount)
  if (amount === null || amount <= 0 || moneyToCents(trip.amount ?? '') !== amount
    || !invoice.item.date || invoice.item.date !== trip.date) return false
  const normalize = (value: string) => value.replace(/[\s\p{P}|｜]/gu, '').toUpperCase()
  const origin = normalize(trip.origin ?? '')
  const destination = normalize(trip.destination ?? '')
  const description = normalize(invoice.item.description)
  if (origin.length < 2 || destination.length < 2
    || origin.includes(destination) || destination.includes(origin)) return false
  const start = description.indexOf(origin)
  const end = description.indexOf(destination)
  return start >= 0 && end >= start + origin.length
    && start === description.lastIndexOf(origin) && end === description.lastIndexOf(destination)
}

export function matchItineraries(items: readonly ExpenseItem[], files: readonly ReimbursementDraftFile[]): ItineraryMatch[] {
  const sources: SourceInvoice[] = items.flatMap((item) => {
    const file = files.find((candidate) => candidate.id === item.sourceFileId)
    const evidence = receiptOcrResult(file)
    return item.sourceFileId && item.transportType === 'ride_hailing' && file?.role === 'EXPENSE_SOURCE'
      && file.status === 'ACTIVE' && evidence?.status === 'recognized' && evidence.fileId === file.id
      ? [{ item, tokens: identifiers(evidence) }] : []
  })
  const itineraries: ItineraryEvidence[] = files.flatMap((file) => {
    const result = file.ocrResult
    if (!isActiveProof(file, 'itinerary') || !isItineraryOcrResult(result)
      || result.fileId !== file.id || result.status !== 'recognized' || !result.complete
      || result.processedPageCount !== result.pageCount || result.warnings.length || result.error) return []
    const tokens = identifiers(result.summary)
    for (const trip of result.trips) for (const token of identifiers(trip)) tokens.add(token)
    return [{ fileId: file.id, result, tokens }]
  })
  const matches: ItineraryMatch[] = []
  for (const source of sources) {
    if (source.item.itineraryAutoMatchDisabled || source.item.itineraryFileIds?.length) continue
    const compatible = itineraries.filter((proof) => !conflictingIdentifiers(source.tokens, proof.tokens))
    const numbered = compatible.filter((proof) => [...source.tokens].some((token) => proof.tokens.has(token)))
    if (numbered.length) {
      const proof = numbered[0]!
      if (numbered.length === 1 && [...source.tokens].some((token) => proof.tokens.has(token)
        && sources.filter((candidate) => candidate.tokens.has(token)).length === 1)) {
        matches.push({ sourceFileId: source.item.sourceFileId!, itineraryFileId: proof.fileId, basis: 'identifier' })
      }
      continue
    }
    const routed = compatible.filter((proof) => proof.result.summary.currency === 'CNY'
      && proof.result.trips.some((trip) => matchingTrip(source, trip)))
    if (routed.length !== 1) continue
    const proof = routed[0]!
    const unique = proof.result.trips.some((trip) => matchingTrip(source, trip)
      && sources.filter((candidate) => matchingTrip(candidate, trip)).length === 1)
    if (unique) matches.push({ sourceFileId: source.item.sourceFileId!, itineraryFileId: proof.fileId, basis: 'amount_date_route' })
  }
  return matches
}

/** Proposals only: uncertain transport types still need the employee's confirmation. */
function suggestExactItineraries(items: readonly ExpenseItem[], files: readonly ReimbursementDraftFile[]): ItinerarySuggestion[] {
  const sources: SourceInvoice[] = items.flatMap((item) => {
    const file = files.find((candidate) => candidate.id === item.sourceFileId)
    const evidence = receiptOcrResult(file)
    if (!item.sourceFileId || item.category !== 'local_transport' || isForeignExpense(item)
      || file?.role !== 'EXPENSE_SOURCE' || file.status !== 'ACTIVE' || file.ocrStatus !== 'COMPLETE'
      || evidence?.status !== 'recognized' || evidence.fileId !== file.id || evidence.error || evidence.warnings.length
      || evidence.categoryId !== 'local_transport'
      || (evidence.originalCurrency && evidence.originalCurrency.toUpperCase() !== 'CNY')) return []
    // Confirmed taxi lines participate in ambiguity checks, even when already linked.
    const uncertain = (!item.transportType || item.transportType === 'other')
      && (!evidence.transportType || evidence.transportType === 'other') && evidence.type === 'invoice'
    return isTaxiExpense(item) || uncertain ? [{ item, tokens: identifiers(evidence) }] : []
  })
  const proofs: ItineraryEvidence[] = files.flatMap((file) => {
    const result = file.ocrResult
    if (!isActiveProof(file, 'itinerary') || file.ocrStatus !== 'COMPLETE' || !isItineraryOcrResult(result)
      || result.fileId !== file.id || result.status !== 'recognized' || !result.complete
      || result.processedPageCount !== result.pageCount || result.pageCount < 1
      || result.warnings.length || result.error || result.summary.currency !== 'CNY'
      || (moneyToCents(result.summary.amount ?? '') ?? 0) <= 0) return []
    const tokens = identifiers(result.summary)
    for (const trip of result.trips) for (const token of identifiers(trip)) tokens.add(token)
    return [{ fileId: file.id, result, tokens }]
  })
  return sources.flatMap((source) => {
    if (isTaxiExpense(source.item) || source.item.itineraryAutoMatchDisabled || source.item.itineraryFileIds?.length) return []
    const candidates = proofs.flatMap((proof) => conflictingIdentifiers(source.tokens, proof.tokens) ? []
      : proof.result.trips.filter((trip) => matchingTrip(source, trip)
        && !conflictingIdentifiers(source.tokens, identifiers(trip))).map((trip) => ({ proof, trip })))
    // One candidate trip, not just one PDF: duplicate rows in a PDF are ambiguous too.
    if (candidates.length !== 1) return []
    const { proof, trip } = candidates[0]!
    if (sources.filter((candidate) => matchingTrip(candidate, trip)
      && !conflictingIdentifiers(candidate.tokens, proof.tokens)
      && !conflictingIdentifiers(candidate.tokens, identifiers(trip))).length !== 1) return []
    // Links are file-level today. Only reuse a linked multi-trip file when the
    // other expense can be located on exactly one different trip in that file.
    const occupied = items.some((item) => {
      if (item.id === source.item.id || !item.itineraryFileIds?.includes(proof.fileId)) return false
      const usedTrips = proof.result.trips.filter((candidate) => matchingTrip({ item, tokens: new Set() }, candidate))
      return usedTrips.length !== 1 || usedTrips[0] === trip
    })
    if (occupied) return []
    return [{
      sourceFileId: source.item.sourceFileId!, itineraryFileId: proof.fileId,
      basis: 'amount_date_route' as const, transportType: 'ride_hailing' as const,
      trip: { ...trip, invoiceNumbers: [...trip.invoiceNumbers], orderNumbers: [...trip.orderNumbers] },
    }]
  })
}

const MISSING_TRAVEL_WARNINGS = new Set([
  'INVOICE_DATE_USED_AS_OCCURRENCE', 'QR_ISSUE_DATE_USED', 'MISSING_DATE',
  'MISSING_DESCRIPTION', 'MISSING_ROUTE', 'MANUAL_REVIEW_REQUIRED',
])
const ISSUE_DATE_WARNINGS = ['INVOICE_DATE_USED_AS_OCCURRENCE', 'QR_ISSUE_DATE_USED']

/** Equal amounts are a weak hint, never an automatic link or evidence of travel. */
function suggestAmountItineraries(items: readonly ExpenseItem[], files: readonly ReimbursementDraftFile[]): ItinerarySuggestion[] {
  return items.flatMap((item): ItinerarySuggestion[] => {
    const file = files.find((candidate) => candidate.id === item.sourceFileId)
    const evidence = receiptOcrResult(file)
    const amount = moneyToCents(item.amount)
    if (!file || file.role !== 'EXPENSE_SOURCE' || file.status !== 'ACTIVE' || file.ocrStatus !== 'COMPLETE'
      || needsMaterialConfirmation(file) || !evidence || evidence.fileId !== file.id || evidence.status !== 'recognized'
      || evidence.type !== 'invoice' || evidence.error || evidence.warnings.some((warning) => !MISSING_TRAVEL_WARNINGS.has(warning))
      || item.category !== 'local_transport' || evidence.categoryId !== 'local_transport' || isForeignExpense(item)
      || (evidence.originalCurrency && evidence.originalCurrency.toUpperCase() !== 'CNY')
      || [item.transportType, evidence.transportType].some((type) => type && !['ride_hailing', 'taxi', 'other'].includes(type))
      || item.transportType === 'taxi' || item.itineraryAutoMatchDisabled || item.itineraryFileIds?.length
      || amount === null || amount <= 0 || moneyToCents(evidence.amount ?? '') !== amount) return []
    const issueDate = ISSUE_DATE_WARNINGS.some((warning) => evidence.warnings.includes(warning))
    // Do not relax contradictory, fully read travel evidence to amount-only.
    if (evidence.date && evidence.description?.trim() && !issueDate) return []
    // Count all competing local expenses, including edited, linked and warned
    // ones. Their exclusion from recommendations must not create false uniqueness.
    if (items.filter((other) => other.category === 'local_transport' && !isForeignExpense(other)
      && moneyToCents(other.amount) === amount).length !== 1) return []
    const candidates = files.filter((proof) => {
      const result = proof.ocrResult
      if (!isActiveProof(proof, 'itinerary') || proof.ocrStatus !== 'COMPLETE' || !isItineraryOcrResult(result)
        || result.fileId !== proof.id || result.status !== 'recognized' || !result.complete || result.error
        || result.warnings.length || result.pageCount < 1 || result.processedPageCount !== result.pageCount
        || result.summary.currency !== 'CNY' || moneyToCents(result.summary.amount ?? '') !== amount
        || result.trips.length < 1) return false
      const sourceTokens = identifiers(evidence)
      const amounts = result.trips.map((trip) => moneyToCents(trip.amount ?? ''))
      return amounts.every((value) => value !== null && value > 0)
        && amounts.reduce<number>((sum, value) => sum + (value ?? 0), 0) === amount
        && result.trips.every((trip) => Boolean(trip.date && trip.origin?.trim() && trip.destination?.trim())
          && !conflictingIdentifiers(sourceTokens, identifiers(trip)))
        && !conflictingIdentifiers(sourceTokens, identifiers(result.summary))
    })
    if (candidates.length !== 1) return []
    const proof = candidates[0]!
    if (items.some((other) => other.itineraryFileIds?.includes(proof.id))) return []
    const result = proof.ocrResult as ItineraryOcrResult
    if (result.trips.length > 1) {
      const dates = result.trips.map((trip) => trip.date!).sort()
      return [{ sourceFileId: file.id, itineraryFileId: proof.id, basis: 'amount_only',
        transportType: 'ride_hailing', fill: {}, documentSummary: {
          amount: result.summary.amount!, tripCount: result.trips.length,
          startDate: dates[0]!, endDate: dates[dates.length - 1]!,
        } }]
    }
    const trip = result.trips[0]!
    const fill: NonNullable<ItinerarySuggestion['fill']> = {}
    // Preserve edits: replace only an untouched invoice-date fallback or an
    // empty date, and only an empty description or the original category label.
    if ((!item.date && !item.displayDate) || (issueDate && item.date === evidence.date
      && (!item.displayDate || item.displayDate === evidence.date))) {
      fill.date = trip.date!
      fill.displayDate = trip.date!
    }
    if (!item.description.trim() || (!evidence.description?.trim() && item.description.trim() === evidence.categoryName.trim())) {
      fill.description = `${trip.origin} → ${trip.destination}`
    }
    return [{ sourceFileId: file.id, itineraryFileId: proof.id, basis: 'amount_only', transportType: 'ride_hailing', fill,
      trip: { ...trip, invoiceNumbers: [...trip.invoiceNumbers], orderNumbers: [...trip.orderNumbers] } }]
  })
}

export function suggestItineraries(items: readonly ExpenseItem[], files: readonly ReimbursementDraftFile[]): ItinerarySuggestion[] {
  const exact = suggestExactItineraries(items, files)
  return [...exact, ...suggestAmountItineraries(items, files)
    .filter((suggestion) => !exact.some((stronger) => stronger.sourceFileId === suggestion.sourceFileId))]
}

/** Derive resolved field hints from the persisted link, not from mutable OCR. */
export function itineraryConfirmationWarnings(item: ExpenseItem, files: readonly ReimbursementDraftFile[]): string[] {
  const warnings = item.warnings ?? []
  if (!item.itineraryAutoMatchDisabled || item.itineraryFileIds?.length !== 1) return warnings
  const proof = files.find((file) => file.id === item.itineraryFileIds![0] && isActiveProof(file, 'itinerary'))
  const result = proof?.ocrResult
  if (!proof || proof.ocrStatus !== 'COMPLETE' || !isItineraryOcrResult(result) || result.fileId !== proof.id
    || result.status !== 'recognized' || !result.complete || result.error || result.warnings.length
    || result.processedPageCount !== result.pageCount || result.trips.length !== 1) return warnings
  const trip = result.trips[0]!
  return warnings.filter((warning) => {
    if (trip.date && item.date === trip.date && item.displayDate === trip.date
      && ['MISSING_DATE', ...ISSUE_DATE_WARNINGS].includes(warning)) return false
    if (trip.origin && trip.destination && item.description === `${trip.origin} → ${trip.destination}`
      && ['MISSING_DESCRIPTION', 'MISSING_ROUTE'].includes(warning)) return false
    return true
  })
}

// Category identifiers are an opaque server-owned contract. The category API
// supplies both the identifiers and the options that may be selected manually.
export type ExpenseCategoryId = string

export interface ExpenseCategoryMetadata {
  id: ExpenseCategoryId
  name: string
  order: number
  manualSelectable: boolean
}

export type TripType =
  | 'business'
  | 'project'
  | 'same_city_project'
  | 'internal'
  | 'overseas'

export type SubsidyRateType =
  | 'business'
  | 'short_term_project'
  | 'long_term_project'
  | 'same_city_project'
  | 'internal'

export interface TripInput {
  tripType: TripType
  relatedApprovalId?: string
  startDate: string
  startTime: string
  endDate: string
  endTime: string
  policyConfirmed?: boolean
  confirmedEffectiveDays?: string
  noSubsidyException?: boolean
}

export type RailType = 'high_speed' | 'emu' | 'regular' | 'unknown'

export interface ExpenseItem {
  id: string
  /** Stable provenance for a line created from a persisted reimbursement draft file. */
  sourceFileId?: string
  itineraryFileIds?: string[]
  itineraryAutoMatchDisabled?: boolean
  paymentProofFileIds?: string[]
  hotelBillFileIds?: string[]
  railType?: RailType
  requiresItinerary?: boolean
  transportType?: 'ride_hailing' | 'taxi' | 'rail' | 'hotel' | 'other'
  originalCurrency?: string
  originalAmount?: string
  /** The employee explicitly corrected or cleared OCR-provided original-currency details. */
  originalDetailsEdited?: boolean
  cnyAmountConfirmed?: boolean
  requiresCnyConfirmation?: boolean
  category: ExpenseCategoryId
  date?: string
  displayDate: string
  description: string
  amount: string
  receiptCount: number
  source: 'ocr' | 'manual' | 'system'
  confidence?: string
  warnings?: string[]
}

export function isTaxiExpense(item: Pick<ExpenseItem, 'transportType'>): boolean {
  return item.transportType === 'ride_hailing' || item.transportType === 'taxi'
}

export function isForeignExpense(item: Pick<ExpenseItem, 'originalCurrency' | 'warnings' | 'requiresCnyConfirmation'>): boolean {
  return Boolean(item.requiresCnyConfirmation)
    || Boolean(item.originalCurrency && item.originalCurrency.toUpperCase() !== 'CNY')
    || Boolean(item.warnings?.includes('FOREIGN_CURRENCY_REQUIRES_CNY_AMOUNT'))
}

export interface SubsidyResult {
  /** Stable key used to keep each amount attached to its source approval. */
  relatedApprovalId?: string
  tripType: SubsidyRateType | 'overseas'
  calendarDays: number
  effectiveDays: string
  dailyRate: string
  total: string
}

export interface ExpenseTotals {
  expenseTotal: string
  subsidyTotal: string
  totalAmount: string
  receiptCount: number
  uppercaseAmount: string
  subsidy: SubsidyResult | null
  /** Present on current API responses; optional while hydrating pre-v6 saved drafts. */
  subsidies?: SubsidyResult[]
}

export type ExcelProjectInput = { mode: 'manual'; text: string }

export interface ExcelExpenseItemInput {
  category: ExpenseCategoryId
  date: string
  displayDate: string
  description: string
  amount: string
  receiptCount: number
}

export interface ExcelGeneratePayload {
  project: ExcelProjectInput
  trip: TripInput | null
  trips?: TripInput[]
  items: ExcelExpenseItemInput[]
}

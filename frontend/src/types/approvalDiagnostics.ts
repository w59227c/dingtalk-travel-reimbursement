export type ApprovalIdentifierKind = 'auto' | 'business_id' | 'instance_id'
export type ApprovalDiagnosticOutcome = 'visible' | 'unselectable' | 'excluded' | 'not_found' | 'incomplete' | 'blocked'
export type ApprovalDiagnosticCheckStatus = 'passed' | 'failed' | 'unknown' | 'skipped'

export interface ApprovalDiagnosticInput {
  userId: string
  approvalIdentifier: string
  identifierKind: ApprovalIdentifierKind
  from?: string
  to?: string
  keyword?: string
}

export interface ApprovalDiagnosticCheck {
  key: string
  label: string
  status: ApprovalDiagnosticCheckStatus
  message: string
}

export interface ApprovalDiagnosticCounts {
  templates: number
  listRequests: number
  listed: number
  detailsRead: number
  evaluated: number
  excluded: number
  returned: number | null
  unselectable: number | null
  exclusions: { code: string, reason: string, count: number }[]
}

export interface ApprovalDiagnosticReport {
  requestId: string
  checkedAt: string
  queryWindow: { from: string, to: string }
  identifierKind: Exclude<ApprovalIdentifierKind, 'auto'>
  configVersion?: number
  outcome: ApprovalDiagnosticOutcome
  summary: string
  checks: ApprovalDiagnosticCheck[]
  counts: ApprovalDiagnosticCounts | null
  record: {
    ownerMatches: boolean
    status: string
    result: string | null
    createdDate: string | null
    startDate: string | null
    endDate: string | null
    sameDay: boolean | null
  } | null
  hints: string[]
  error?: {
    code: string
    upstreamHttpStatus: number | null
    upstreamErrorCode: string | null
  }
}

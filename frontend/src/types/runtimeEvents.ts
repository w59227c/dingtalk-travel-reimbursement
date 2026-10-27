export type RuntimeModule = 'approvals' | 'materials' | 'submission' | 'auth' | 'system'
export type RuntimeResult = 'success' | 'empty' | 'failed' | 'incomplete' | 'info'
export interface RuntimeEvent {
  id: number
  occurredAt: string
  module: RuntimeModule
  event: 'request' | 'approval_query' | 'upstream_failure' | 'ocr_failure' | 'internal_failure' | 'collection_gap'
  result: RuntimeResult
  requestId: string | null
  details: {
    operation?: string
    stage?: string
    httpStatus?: number
    durationMs?: number
    errorCode?: string
    upstreamOperation?: string
    upstreamHttpStatus?: number
    upstreamErrorCode?: string
    exceptionType?: string
    failureKind?: 'timeout' | 'transport'
    configVersion?: number
    queryWindow?: { from: string, to: string }
    keywordPresent?: boolean
    templates?: number
    listRequests?: number
    listed?: number
    detailsRead?: number
    evaluated?: number
    excluded?: number
    returned?: number | null
    unselectable?: number | null
    exclusions?: { code: string, count: number }[]
    dropped?: number
  }
}
export interface RuntimeEventSearch {
  from?: string
  to?: string
  module?: RuntimeModule
  result?: RuntimeResult
  requestId?: string
  before?: number
}
export interface RuntimeEventPage {
  items: RuntimeEvent[]
  nextCursor: number | null
  coverage: { oldestAt: string | null, newestAt: string | null, records: number, bytes: number }
  limits: { retentionDays: number, maxRecords: number, maxBytes: number }
  collector: { startedAt: string, pending: number, droppedSinceStart: number, writeError: boolean }
}

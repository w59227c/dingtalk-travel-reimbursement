import axios, { type AxiosProgressEvent } from 'axios'

import { filenameFromContentDisposition } from './excel'
import { http, reimbursementContextHeaders } from './http'
import type { ApiEnvelope, Department } from '@/types/auth'
import type {
  OaReimbursementOptions,
  GeneralApproval,
  GeneralApprovalSearchResult,
  OaTravelApprovalList,
  ReimbursementDraft,
  ReimbursementDraftDepartment,
  ReimbursementDraftFile,
  ReimbursementDraftDeletion,
  ReimbursementDraftFileDeletion,
  ReimbursementDraftFileList,
  ReimbursementDraftFileMutation,
  ReimbursementDraftFileRole,
  ReimbursementDraftFileUpdate,
  ReimbursementDraftInput,
  ReimbursementDraftList,
  ReimbursementDraftOcrInput,
  ReimbursementExcelPreview,
  ReimbursementExcelPreviewTicket,
  ReimbursementRelatedApprovalSelection,
  ReimbursementSubmission,
  ReimbursementSubmissionHistoryEntry,
  ReimbursementDraftSummary,
  ReimbursementKind,
  SubmitReimbursementInput,
} from '@/types/reimbursements'

const DRAFT_EXCEL_PREVIEW_FILENAME = '差旅费报销单预览.xlsx'
const EXCEL_PREVIEW_REQUEST_TIMEOUT_MS = 60_000
// Keep the client attached through the OCR worker ceiling and its settlement window.
// Covers the bounded server queue, one OCR run, and process cleanup headroom.
const OCR_REQUEST_TIMEOUT_MS = 360_000

export interface ReimbursementRequestOptions {
  kind?: ReimbursementKind
  signal?: AbortSignal
  reimbursementContext?: string | null
  history?: boolean
}

function personalHistoryRequest(history?: boolean) {
  return history ? { params: { history: true }, headers: reimbursementContextHeaders(null) } : {}
}

export interface ListReimbursementDraftsOptions extends ReimbursementRequestOptions {
  offset?: number
  limit?: number
  expectedDepartment?: ReimbursementDraftDepartment
}

function expectedDepartmentParams(department?: ReimbursementDraftDepartment) {
  return department ? { expectedDepartmentId: department.id, expectedDepartmentName: department.name } : {}
}

export interface UploadReimbursementDraftFileOptions extends ReimbursementRequestOptions {
  role?: ReimbursementDraftFileRole
  attachmentKind?: ReimbursementDraftFile['attachmentKind']
  autoClassify?: boolean
  manualItineraryAssociation?: boolean
  onProgress?: (percent: number) => void
}

export interface ReimbursementPdfPreviewPage {
  blob: Blob
  pageNumber: number
  pageCount: number
}

export interface ListOaTravelApprovalsOptions extends ReimbursementRequestOptions {
  from?: string
  to?: string
  query?: string
}

function draftUrl(draftId: string): string {
  return `/reimbursements/drafts/${encodeURIComponent(draftId)}`
}

function fileUrl(draftId: string, fileId: string): string {
  return `${draftUrl(draftId)}/files/${encodeURIComponent(fileId)}`
}

function uploadPercent(event: AxiosProgressEvent): number {
  if (!event.total || event.total <= 0) return 0
  return Math.min(100, Math.max(0, Math.round((event.loaded / event.total) * 100)))
}

export async function getOaReimbursementOptions(
  options: ReimbursementRequestOptions = {},
): Promise<OaReimbursementOptions> {
  const response = await http.get<ApiEnvelope<OaReimbursementOptions>>(
    '/oa/reimbursements/options',
    { signal: options.signal, headers: reimbursementContextHeaders(options.reimbursementContext),
      ...(options.kind === 'general' ? { params: { kind: options.kind } } : {}) },
  )
  return response.data.data
}

export async function listOaTravelApprovals(
  options: ListOaTravelApprovalsOptions = {},
): Promise<OaTravelApprovalList> {
  const params: { from?: string; to?: string; q?: string } = {}
  if (options.from !== undefined) params.from = options.from
  if (options.to !== undefined) params.to = options.to
  if (options.query !== undefined) params.q = options.query
  const response = await http.get<ApiEnvelope<OaTravelApprovalList>>(
    '/oa/travel-approvals',
    {
      params,
      signal: options.signal,
      timeout: 60_000,
    },
  )
  return response.data.data
}

async function normalizeBlobApiError(error: unknown): Promise<never> {
  if (
    axios.isAxiosError(error)
    && error.response
    && error.response.data instanceof Blob
  ) {
    try {
      error.response.data = JSON.parse(await error.response.data.text()) as unknown
    } catch {
      // Keep the original Axios error when the response is not a JSON envelope.
    }
  }
  throw error
}

export async function createReimbursementDraft(
  input: ReimbursementDraftInput,
  options: ReimbursementRequestOptions & { replaceCurrent?: { id: string; revision: number }; reuseExisting?: boolean; expectedDepartment?: ReimbursementDraftDepartment } = {},
): Promise<ReimbursementDraft> {
  const response = await http.post<ApiEnvelope<ReimbursementDraft>>(
    '/reimbursements/drafts',
    { expectedRevision: 0, input, ...(options.reuseExisting ? { reuseExisting: true } : {}),
      ...(options.kind === 'general' ? { kind: options.kind } : {}),
      ...(options.replaceCurrent ? { replacesDraftId: options.replaceCurrent.id, replacesDraftRevision: options.replaceCurrent.revision } : {}),
      ...expectedDepartmentParams(options.expectedDepartment) },
    { signal: options.signal, headers: reimbursementContextHeaders(options.reimbursementContext) },
  )
  return response.data.data
}

export async function listReimbursementDrafts(
  options: ListReimbursementDraftsOptions = {},
): Promise<ReimbursementDraftList> {
  const response = await http.get<ApiEnvelope<ReimbursementDraftList>>(
    '/reimbursements/drafts',
    {
      params: {
        offset: options.offset ?? 0,
        limit: options.limit ?? 50,
        ...expectedDepartmentParams(options.expectedDepartment),
        ...(options.kind === 'general' ? { kind: options.kind } : {}),
      },
      signal: options.signal,
    },
  )
  return response.data.data
}

export async function listReimbursementSubmissionHistory(
  options: { signal?: AbortSignal } = {},
): Promise<ReimbursementSubmissionHistoryEntry[]> {
  const response = await http.get<ApiEnvelope<ReimbursementSubmissionHistoryEntry[]>>(
    '/reimbursements/submission-history',
    { signal: options.signal, headers: reimbursementContextHeaders(null) },
  )
  return response.data.data
}

export async function listWorkspaceReimbursements(
  options: ReimbursementRequestOptions & { expectedDepartment?: ReimbursementDraftDepartment; offset?: number } = {},
): Promise<{ items: ReimbursementDraftSummary[]; total: number }> {
  const response = await http.get<ApiEnvelope<{ items: ReimbursementDraftSummary[]; total: number }>>(
    '/reimbursements/workspace',
    {
      signal: options.signal,
      headers: reimbursementContextHeaders(options.reimbursementContext),
      params: { ...expectedDepartmentParams(options.expectedDepartment), offset: options.offset ?? 0,
        ...(options.kind === 'general' ? { kind: options.kind } : {}) },
    },
  )
  return response.data.data
}

export async function recoverReimbursementDraft(
  sourceId: string, expectedRevision: number, options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.post<ApiEnvelope<ReimbursementDraft>>(
    `${draftUrl(sourceId)}/recover`, { expectedRevision }, { signal: options.signal, timeout: OCR_REQUEST_TIMEOUT_MS },
  )
  return response.data.data
}

export async function listGeneralApprovals(
  filters: { from?: string; to?: string; query?: string; cursor?: string; processCode?: string },
  signal: AbortSignal,
) {
  const response = await http.get<ApiEnvelope<GeneralApprovalSearchResult>>(
    '/oa/general-approvals',
    { params: { from: filters.from, to: filters.to, q: filters.query, cursor: filters.cursor, processCode: filters.processCode }, timeout: 120_000, signal },
  )
  return response.data.data
}

export async function getSavedGeneralApprovals(draftId: string, signal: AbortSignal): Promise<GeneralApproval[]> {
  const response = await http.get<ApiEnvelope<GeneralApproval[]>>(`${draftUrl(draftId)}/general-approvals`, { signal })
  return response.data.data
}

export async function replaceGeneralApprovals(draftId: string, expectedRevision: number, instanceIds: string[], signal?: AbortSignal) {
  const response = await http.put<ApiEnvelope<ReimbursementDraft>>(
    `${draftUrl(draftId)}/general-approvals`, { expectedRevision, instanceIds }, { timeout: 60_000, signal },
  )
  return response.data.data
}

export async function getReimbursementDraft(
  draftId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.get<ApiEnvelope<ReimbursementDraft>>(
    draftUrl(draftId),
    { signal: options.signal, headers: reimbursementContextHeaders(options.reimbursementContext), ...personalHistoryRequest(options.history) },
  )
  return response.data.data
}

export async function updateReimbursementDraft(
  draftId: string,
  expectedRevision: number,
  input: ReimbursementDraftInput,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.put<ApiEnvelope<ReimbursementDraft>>(
    draftUrl(draftId),
    { expectedRevision, input },
    { signal: options.signal },
  )
  return response.data.data
}

export async function deleteReimbursementDraft(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraftDeletion> {
  const response = await http.delete<ApiEnvelope<ReimbursementDraftDeletion>>(
    draftUrl(draftId),
    {
      params: { expectedRevision },
      signal: options.signal,
    },
  )
  return response.data.data
}

export async function replaceReimbursementRelatedApprovals(
  draftId: string,
  expectedRevision: number,
  selections: ReimbursementRelatedApprovalSelection[],
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.put<ApiEnvelope<ReimbursementDraft>>(
    `${draftUrl(draftId)}/related-approvals`,
    { expectedRevision, selections },
    { signal: options.signal, timeout: 60_000 },
  )
  return response.data.data
}

export async function markReimbursementDraftReviewReady(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.post<ApiEnvelope<ReimbursementDraft>>(
    `${draftUrl(draftId)}/review`,
    { expectedRevision },
    { signal: options.signal },
  )
  return response.data.data
}

export async function listReimbursementDraftFiles(
  draftId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraftFileList> {
  const response = await http.get<ApiEnvelope<ReimbursementDraftFileList>>(
    `${draftUrl(draftId)}/files`,
    { signal: options.signal, headers: reimbursementContextHeaders(options.reimbursementContext), ...personalHistoryRequest(options.history) },
  )
  return response.data.data
}

export async function uploadReimbursementDraftFile(
  draftId: string,
  expectedRevision: number,
  file: File,
  options: UploadReimbursementDraftFileOptions = {},
): Promise<ReimbursementDraftFileMutation> {
  const form = new FormData()
  form.append('files[]', file, file.name)
  const response = await http.post<ApiEnvelope<ReimbursementDraftFileMutation>>(
    `${draftUrl(draftId)}/files`,
    form,
    {
      params: {
        expectedRevision,
        role: options.role ?? 'EXPENSE_SOURCE',
        attachmentKind: options.attachmentKind ?? 'other',
        ...(options.autoClassify ? { autoClassify: true } : {}),
        ...(options.manualItineraryAssociation ? { manualItineraryAssociation: true } : {}),
      },
      signal: options.signal,
      timeout: 120_000,
      onUploadProgress: (event) => options.onProgress?.(uploadPercent(event)),
    },
  )
  return response.data.data
}

export async function updateReimbursementDraftFile(
  draftId: string,
  fileId: string,
  input: ReimbursementDraftFileUpdate,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraftFileMutation> {
  const response = await http.patch<ApiEnvelope<ReimbursementDraftFileMutation>>(
    fileUrl(draftId, fileId),
    input,
    { signal: options.signal },
  )
  return response.data.data
}

export async function clearReimbursementDraftFiles(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<{ draftId: string; revision: number; deletedFileIds: string[] }> {
  const response = await http.post<ApiEnvelope<{ draftId: string; revision: number; deletedFileIds: string[] }>>(
    `/reimbursements/drafts/${encodeURIComponent(draftId)}/files/clear`,
    { expectedRevision },
    { signal: options.signal },
  )
  return response.data.data
}

export async function deleteReimbursementDraftFile(
  draftId: string,
  fileId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraftFileDeletion> {
  const response = await http.delete<ApiEnvelope<ReimbursementDraftFileDeletion>>(
    fileUrl(draftId, fileId),
    {
      params: { expectedRevision },
      signal: options.signal,
    },
  )
  return response.data.data
}

export async function recognizeReimbursementDraftFile(
  draftId: string,
  fileId: string,
  input: ReimbursementDraftOcrInput,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraftFileMutation> {
  const response = await http.post<ApiEnvelope<ReimbursementDraftFileMutation>>(
    `${fileUrl(draftId, fileId)}/ocr`,
    input,
    { signal: options.signal, timeout: OCR_REQUEST_TIMEOUT_MS },
  )
  return response.data.data
}

export async function getReimbursementDraftExcelPreview(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementExcelPreview> {
  const response = await http.post<Blob>(
    `${draftUrl(draftId)}/excel-preview`,
    { expectedRevision },
    {
      responseType: 'blob',
      signal: options.signal,
      timeout: EXCEL_PREVIEW_REQUEST_TIMEOUT_MS,
      ...personalHistoryRequest(options.history),
    },
  ).catch(normalizeBlobApiError)
  return {
    blob: response.data,
    filename: filenameFromContentDisposition(
      response.headers['content-disposition'],
      DRAFT_EXCEL_PREVIEW_FILENAME,
    ),
  }
}

export async function requestReimbursementDraftExcelPreviewTicket(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementExcelPreviewTicket> {
  const response = await http.post<ApiEnvelope<ReimbursementExcelPreviewTicket>>(
    `${draftUrl(draftId)}/excel-preview-ticket`,
    { expectedRevision },
    { signal: options.signal, timeout: EXCEL_PREVIEW_REQUEST_TIMEOUT_MS, ...personalHistoryRequest(options.history) },
  )
  return response.data.data
}

export async function getReimbursementFileContent(
  draftId: string,
  fileId: string,
  options: ReimbursementRequestOptions = {},
): Promise<Blob> {
  const response = await http.get<Blob>(`${fileUrl(draftId, fileId)}/content`, {
    responseType: 'blob',
    signal: options.signal,
    timeout: 60_000,
  }).catch(normalizeBlobApiError)
  return response.data
}

export async function getReimbursementPdfPreviewPage(
  draftId: string,
  fileId: string,
  pageNumber: number,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementPdfPreviewPage> {
  if (!Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > 30) {
    throw new Error('PDF 预览页码无效')
  }
  const response = await http.get<Blob>(
    `${fileUrl(draftId, fileId)}/preview/pages/${pageNumber}`,
    {
      responseType: 'blob',
      signal: options.signal,
      timeout: 60_000,
    },
  ).catch(normalizeBlobApiError)
  const pageCount = Number(response.headers['x-pdf-page-count'])
  const returnedPage = Number(response.headers['x-pdf-page-number'])
  if (
    response.data.type !== 'image/png'
    || !Number.isInteger(pageCount)
    || pageCount < pageNumber
    || pageCount > 30
    || returnedPage !== pageNumber
  ) {
    throw new Error('PDF 预览响应无效，请重试')
  }
  return { blob: response.data, pageNumber, pageCount }
}

export async function submitOaReimbursement(
  draftId: string,
  expectedRevision: number,
  idempotencyKey: string,
  options: ReimbursementRequestOptions & { expectedTotalAmount?: string } = {},
): Promise<ReimbursementSubmission> {
  const body: SubmitReimbursementInput = { expectedRevision,
    ...(options.expectedTotalAmount === undefined ? {} : { expectedTotalAmount: options.expectedTotalAmount }) }
  const response = await http.post<ApiEnvelope<ReimbursementSubmission>>(
    `/oa/reimbursements/${encodeURIComponent(draftId)}/submit`,
    body,
    {
      headers: { 'Idempotency-Key': idempotencyKey },
      signal: options.signal,
    },
  )
  return response.data.data
}

export async function getOaReimbursementSubmission(
  submissionId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementSubmission> {
  const response = await http.get<ApiEnvelope<ReimbursementSubmission>>(
    `/oa/reimbursements/submissions/${encodeURIComponent(submissionId)}`,
    { signal: options.signal },
  )
  return response.data.data
}

export async function recheckOaReimbursementSubmission(
  submissionId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementSubmission> {
  const response = await http.post<ApiEnvelope<ReimbursementSubmission>>(
    `/oa/reimbursements/submissions/${encodeURIComponent(submissionId)}/recheck`,
    {}, { signal: options.signal },
  )
  return response.data.data
}

export async function getOaReimbursementSubmissionForDraft(
  draftId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementSubmission> {
  const response = await http.get<ApiEnvelope<ReimbursementSubmission>>(
    `/oa/reimbursements/drafts/${encodeURIComponent(draftId)}/submission`,
    { signal: options.signal, ...personalHistoryRequest(options.history) },
  )
  return response.data.data
}


export interface WorkspaceContextResult {
  selectedDepartment: Department
  reimbursementContext?: string
}

export async function openReimbursementWorkspace(
  draftId: string, candidates: { id: string; revision: number }[],
  options: ReimbursementRequestOptions = {},
): Promise<WorkspaceContextResult> {
  const response = await http.post<ApiEnvelope<WorkspaceContextResult>>('/reimbursements/workspace/open',
    { draftId, candidates, kind: options.kind ?? 'travel' },
    { signal: options.signal, headers: reimbursementContextHeaders(options.reimbursementContext) })
  return response.data.data
}

export async function correctReimbursementDepartment(
  draftId: string, expectedRevision: number,
  departmentId?: string, selection?: ReimbursementRelatedApprovalSelection,
): Promise<WorkspaceContextResult & { draft: ReimbursementDraft }> {
  const response = await http.put<ApiEnvelope<WorkspaceContextResult & { draft: ReimbursementDraft }>>(
    `${draftUrl(draftId)}/department`, { expectedRevision, departmentId, selection }, { timeout: 60_000 })
  return response.data.data
}

import axios, { type AxiosProgressEvent } from 'axios'

import { downloadBlob, filenameFromContentDisposition } from './excel'
import { http } from './http'
import type { ApiEnvelope } from '@/types/auth'
import type {
  OaReimbursementOptions,
  OaTravelApprovalList,
  ReimbursementDraft,
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
  ReimbursementFilePreviewTicket,
  ReimbursementRelatedApprovalSelection,
  ReimbursementSubmission,
  SubmitReimbursementInput,
} from '@/types/reimbursements'

const DRAFT_EXCEL_PREVIEW_FILENAME = '差旅费报销单预览.xlsx'
// Keep the client attached through the OCR worker ceiling and its settlement window.
// Covers the bounded server queue, one OCR run, and process cleanup headroom.
const OCR_REQUEST_TIMEOUT_MS = 360_000

export interface ReimbursementRequestOptions {
  signal?: AbortSignal
}

export interface ListReimbursementDraftsOptions extends ReimbursementRequestOptions {
  offset?: number
  limit?: number
}

export interface UploadReimbursementDraftFileOptions extends ReimbursementRequestOptions {
  role?: ReimbursementDraftFileRole
  attachmentKind?: ReimbursementDraftFile['attachmentKind']
  autoClassify?: boolean
  onProgress?: (percent: number) => void
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
    { signal: options.signal },
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
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.post<ApiEnvelope<ReimbursementDraft>>(
    '/reimbursements/drafts',
    { expectedRevision: 0, input },
    { signal: options.signal },
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
      },
      signal: options.signal,
    },
  )
  return response.data.data
}

export async function getReimbursementDraft(
  draftId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementDraft> {
  const response = await http.get<ApiEnvelope<ReimbursementDraft>>(
    draftUrl(draftId),
    { signal: options.signal },
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
    { signal: options.signal },
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
      timeout: 60_000,
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
    { signal: options.signal },
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

export async function requestReimbursementDraftFilePreviewTicket(
  draftId: string,
  fileId: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementFilePreviewTicket> {
  const response = await http.post<ApiEnvelope<ReimbursementFilePreviewTicket>>(
    `${fileUrl(draftId, fileId)}/preview-ticket`,
    undefined,
    { signal: options.signal },
  )
  return response.data.data
}

export async function downloadReimbursementDraftExcelPreview(
  draftId: string,
  expectedRevision: number,
  options: ReimbursementRequestOptions = {},
): Promise<void> {
  const preview = await getReimbursementDraftExcelPreview(
    draftId,
    expectedRevision,
    options,
  )
  downloadBlob(preview.blob, preview.filename)
}

export async function submitOaReimbursement(
  draftId: string,
  expectedRevision: number,
  idempotencyKey: string,
  options: ReimbursementRequestOptions = {},
): Promise<ReimbursementSubmission> {
  const body: SubmitReimbursementInput = { expectedRevision }
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
    { signal: options.signal },
  )
  return response.data.data
}

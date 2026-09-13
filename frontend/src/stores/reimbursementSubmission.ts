import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import {
  getOaReimbursementSubmission,
  getOaReimbursementSubmissionForDraft,
  recheckOaReimbursementSubmission,
  submitOaReimbursement,
} from '@/api/reimbursements'
import type {
  ReimbursementSubmission,
  ReimbursementSubmissionStatus,
} from '@/types/reimbursements'

const STORAGE_KEY = 'dingtalk-travel-reimbursement.reimbursement-submissions.v1'
const DEFAULT_POLL_AFTER_MS = 1_500
const MAX_POLL_AFTER_MS = 60_000
const MAX_PERSISTED_DRAFTS = 100
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

const TERMINAL_STATUSES = new Set<ReimbursementSubmissionStatus>([
  'SUBMITTED',
  'FAILED_FINAL',
  'MANUAL_REVIEW',
])

const KNOWN_STATUSES = new Set<ReimbursementSubmissionStatus>([
  'QUEUED',
  'VALIDATING',
  'GENERATING_EXCEL',
  'UPLOADING',
  'OA_CREATING',
  'VERIFYING',
  'FAILED_RETRYABLE',
  'RECONCILING',
  'ORPHAN_CLEANUP',
  'SUBMITTED',
  'FAILED_FINAL',
  'MANUAL_REVIEW',
])

export const REIMBURSEMENT_SUBMISSION_PROGRESS_LABELS: Readonly<
  Record<ReimbursementSubmissionStatus, string>
> = {
  QUEUED: '本系统已接收，等待后台处理',
  VALIDATING: '正在校验报销信息',
  GENERATING_EXCEL: '正在生成报销 Excel',
  UPLOADING: '正在上传审批附件',
  OA_CREATING: '正在发起钉钉审批',
  VERIFYING: '审批已发起，正在核验',
  FAILED_RETRYABLE: '遇到临时问题，等待自动重试',
  RECONCILING: '正在确认审批是否已创建',
  ORPHAN_CLEANUP: '审批未创建，正在清理附件',
  SUBMITTED: '审批已成功发起',
  FAILED_FINAL: '审批未发起',
  MANUAL_REVIEW: '提交结果需要人工核对',
}

interface PersistedSubmission {
  draftId: string
  expectedRevision: number
  idempotencyKey: string
  submissionId: string | null
}

interface SubmitFlight {
  draftId: string
  promise: Promise<ReimbursementSubmission>
}

interface PollFlight {
  submissionId: string
  promise: Promise<ReimbursementSubmission | null>
}

interface DiscoveryFlight {
  draftId: string
  promise: Promise<ReimbursementSubmission | null>
}

export interface RestoreReimbursementSubmissionOptions {
  discoverByDraft?: boolean
  expectedRevision?: number
}

function browserSessionStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}

function isPersistedSubmission(value: unknown): value is PersistedSubmission {
  if (value === null || typeof value !== 'object') return false
  const record = value as Partial<PersistedSubmission>
  return typeof record.draftId === 'string'
    && record.draftId.length > 0
    && Number.isInteger(record.expectedRevision)
    && (record.expectedRevision ?? 0) > 0
    && typeof record.idempotencyKey === 'string'
    && UUID_PATTERN.test(record.idempotencyKey)
    && (record.submissionId === null
      || (typeof record.submissionId === 'string' && record.submissionId.length > 0))
}

function readPersistedSubmissions(): PersistedSubmission[] {
  const storage = browserSessionStorage()
  if (storage === null) return []
  try {
    const raw = storage.getItem(STORAGE_KEY)
    if (raw === null) return []
    const value: unknown = JSON.parse(raw)
    if (!Array.isArray(value)) return []
    return value.filter(isPersistedSubmission).slice(-MAX_PERSISTED_DRAFTS)
  } catch {
    return []
  }
}

function persistedSubmission(draftId: string): PersistedSubmission | null {
  const records = readPersistedSubmissions()
  for (let index = records.length - 1; index >= 0; index -= 1) {
    if (records[index]?.draftId === draftId) return records[index]!
  }
  return null
}

function writePersistedSubmission(record: PersistedSubmission): void {
  const storage = browserSessionStorage()
  if (storage === null) return
  const records = readPersistedSubmissions().filter((item) => item.draftId !== record.draftId)
  records.push(record)
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(records.slice(-MAX_PERSISTED_DRAFTS)))
  } catch {
    // A submission remains usable in memory when browser storage is unavailable.
  }
}

function clearPersistedSubmissions(): void {
  try {
    browserSessionStorage()?.removeItem(STORAGE_KEY)
  } catch {
    // Reset must still clear memory when browser storage is unavailable.
  }
}

function removePersistedSubmission(draftId: string): void {
  try {
    browserSessionStorage()?.setItem(STORAGE_KEY, JSON.stringify(
      readPersistedSubmissions().filter((record) => record.draftId !== draftId),
    ))
  } catch {
    // In-memory state remains usable when browser storage is unavailable.
  }
}

function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return [
    value.slice(0, 8),
    value.slice(8, 12),
    value.slice(12, 16),
    value.slice(16, 20),
    value.slice(20),
  ].join('-')
}

function isCancellation(error: unknown): boolean {
  return axios.isCancel(error)
    || (error instanceof DOMException && error.name === 'AbortError')
}

function isDefiniteClientRejection(error: unknown): boolean {
  if (!axios.isAxiosError(error)) return false
  const status = error.response?.status
  return status !== undefined && status >= 400 && status < 500
}

function isTerminal(status: ReimbursementSubmissionStatus): boolean {
  return TERMINAL_STATUSES.has(status)
}

function pollDelay(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return DEFAULT_POLL_AFTER_MS
  return Math.min(value, MAX_POLL_AFTER_MS)
}

function validateSubmission(
  value: ReimbursementSubmission,
  expectedDraftId: string,
  expectedSubmissionId?: string,
): void {
  if (
    value === null
    || typeof value !== 'object'
    || !value.submissionId
    || value.draftId !== expectedDraftId
    || (expectedSubmissionId !== undefined && value.submissionId !== expectedSubmissionId)
    || !KNOWN_STATUSES.has(value.status)
    || !Number.isInteger(value.statusVersion)
    || value.statusVersion < 1
  ) {
    throw new Error('提交任务响应无效，请刷新后重试')
  }
}

function statusErrorMessage(value: ReimbursementSubmission | null): string {
  const serverMessage = value?.error?.message.trim()
  if (serverMessage) return serverMessage
  if (value?.status === 'FAILED_RETRYABLE') return '提交处理遇到临时问题，系统将自动重试'
  if (value?.status === 'FAILED_FINAL') return '审批未发起，请检查报销内容后重试'
  if (value?.status === 'MANUAL_REVIEW') return '提交结果需要人工核对，请联系管理员'
  return ''
}

export const useReimbursementSubmissionStore = defineStore(
  'reimbursementSubmission',
  () => {
    const activeDraftId = ref<string | null>(null)
    const idempotencyKey = ref<string | null>(null)
    const submission = ref<ReimbursementSubmission | null>(null)
    const submitting = ref(false)
    const polling = ref(false)
    const requestError = ref('')
    const requestAction = ref<'retry' | 'discover' | null>(null)
    const oaSubmissionEnabled = ref<boolean | null>(null)

    let generation = 0
    let activeController: AbortController | null = null
    let pollTimer: ReturnType<typeof setTimeout> | null = null
    let submitFlight: SubmitFlight | null = null
    let pollFlight: PollFlight | null = null
    let discoveryFlight: DiscoveryFlight | null = null
    const memoryRecords = new Map<string, PersistedSubmission>()

    const status = computed<ReimbursementSubmissionStatus | null>(
      () => submission.value?.status ?? null,
    )
    const terminal = computed(() => status.value !== null && isTerminal(status.value))
    const succeeded = computed(() => status.value === 'SUBMITTED')
    const progressLabel = computed(() => {
      if (submission.value !== null) {
        if (submission.value.status === 'QUEUED' && oaSubmissionEnabled.value === false) {
          return '等待 OA 提交服务开启，尚未发起审批'
        }
        return REIMBURSEMENT_SUBMISSION_PROGRESS_LABELS[submission.value.status]
      }
      return submitting.value ? '正在创建提交任务' : ''
    })
    const errorMessage = computed(
      () => requestError.value || statusErrorMessage(submission.value),
    )

    function clearPollTimer(): void {
      if (pollTimer !== null) clearTimeout(pollTimer)
      pollTimer = null
    }

    function invalidateRequests(): void {
      generation += 1
      clearPollTimer()
      activeController?.abort()
      activeController = null
      submitFlight = null
      pollFlight = null
      discoveryFlight = null
      submitting.value = false
      polling.value = false
    }

    function activateDraft(draftId: string): number {
      if (activeDraftId.value !== draftId) {
        invalidateRequests()
        activeDraftId.value = draftId
        idempotencyKey.value = null
        submission.value = null
        requestError.value = ''
        requestAction.value = null
      }
      return generation
    }

    function accepts(requestGeneration: number, draftId: string): boolean {
      return requestGeneration === generation && activeDraftId.value === draftId
    }

    function saveRecord(
      draftId: string,
      expectedRevision: number,
      key: string,
      submissionId: string | null,
    ): void {
      const record = {
        draftId,
        expectedRevision,
        idempotencyKey: key,
        submissionId,
      }
      memoryRecords.set(draftId, record)
      writePersistedSubmission(record)
    }

    function recordForDraft(draftId: string): PersistedSubmission | null {
      const inMemory = memoryRecords.get(draftId)
      if (inMemory !== undefined) return inMemory
      const stored = persistedSubmission(draftId)
      if (stored !== null) memoryRecords.set(draftId, stored)
      return stored
    }

    function schedulePoll(value: ReimbursementSubmission): void {
      clearPollTimer()
      if (oaSubmissionEnabled.value === false || isTerminal(value.status) || activeDraftId.value !== value.draftId) {
        polling.value = false
        return
      }
      const requestGeneration = generation
      polling.value = true
      pollTimer = setTimeout(() => {
        pollTimer = null
        if (!accepts(requestGeneration, value.draftId)) return
        void pollNow().catch(() => undefined)
      }, pollDelay(value.pollAfterMs))
    }

    watch(oaSubmissionEnabled, () => {
      if (submission.value !== null) schedulePoll(submission.value)
    }, { flush: 'sync' })

    function adoptSubmission(
      value: ReimbursementSubmission,
      record: PersistedSubmission,
      requestGeneration: number,
    ): boolean {
      validateSubmission(value, record.draftId, record.submissionId ?? undefined)
      if (!accepts(requestGeneration, record.draftId)) return false
      if (
        submission.value?.submissionId === value.submissionId
        && submission.value.statusVersion > value.statusVersion
      ) {
        schedulePoll(submission.value)
        return false
      }
      submission.value = value
      idempotencyKey.value = record.idempotencyKey
      requestError.value = ''
      requestAction.value = null
      saveRecord(
        record.draftId,
        record.expectedRevision,
        record.idempotencyKey,
        value.submissionId,
      )
      schedulePoll(value)
      return true
    }

    async function postSubmission(
      record: PersistedSubmission,
      requestGeneration: number,
    ): Promise<ReimbursementSubmission> {
      const controller = new AbortController()
      activeController = controller
      submitting.value = true
      requestError.value = ''
      requestAction.value = null
      try {
        const value = await submitOaReimbursement(
          record.draftId,
          record.expectedRevision,
          record.idempotencyKey,
          { signal: controller.signal },
        )
        adoptSubmission(value, record, requestGeneration)
        return value
      } catch (error) {
        if (accepts(requestGeneration, record.draftId) && !isCancellation(error)) {
          requestError.value = apiErrorMessage(error, '报销提交失败，请重试')
          const disabled = apiErrorCode(error) === 'OA_SUBMISSION_DISABLED'
          if (disabled || isDefiniteClientRejection(error)) {
            // A definite server rejection guarantees no task or draft lock was created.
            requestAction.value = null
            if (disabled) oaSubmissionEnabled.value = false
            memoryRecords.delete(record.draftId)
            removePersistedSubmission(record.draftId)
            idempotencyKey.value = null
          } else {
            requestAction.value = 'retry'
          }
        }
        throw error
      } finally {
        if (requestGeneration === generation && activeController === controller) {
          activeController = null
          submitting.value = false
        }
      }
    }

    function submit(
      draftId: string,
      expectedRevision: number,
    ): Promise<ReimbursementSubmission> {
      if (!draftId || !Number.isInteger(expectedRevision) || expectedRevision < 1) {
        return Promise.reject(new Error('报销内容或版本无效'))
      }
      const requestGeneration = activateDraft(draftId)
      if (submitFlight?.draftId === draftId) return submitFlight.promise
      if (submission.value?.draftId === draftId) {
        if (!isTerminal(submission.value.status)) schedulePoll(submission.value)
        return Promise.resolve(submission.value)
      }

      const stored = recordForDraft(draftId)
      if (stored?.submissionId) {
        return restore(draftId, expectedRevision).then((value) => {
          if (value === null) throw new Error('未找到可恢复的提交任务')
          return value
        })
      }
      if (oaSubmissionEnabled.value === false) {
        requestError.value = 'OA提交服务未开启，可继续填写'
        return Promise.reject(new Error(requestError.value))
      }
      const record: PersistedSubmission = {
        draftId,
        expectedRevision: stored?.expectedRevision ?? expectedRevision,
        idempotencyKey: stored?.idempotencyKey ?? newIdempotencyKey(),
        submissionId: null,
      }
      idempotencyKey.value = record.idempotencyKey
      saveRecord(draftId, record.expectedRevision, record.idempotencyKey, null)
      const promise = postSubmission(record, requestGeneration).finally(() => {
        if (submitFlight?.promise === promise) submitFlight = null
      })
      submitFlight = { draftId, promise }
      return promise
    }

    async function fetchSubmission(
      record: PersistedSubmission,
      requestGeneration: number,
    ): Promise<ReimbursementSubmission | null> {
      if (record.submissionId === null) return null
      const controller = new AbortController()
      activeController = controller
      polling.value = true
      try {
        const value = await getOaReimbursementSubmission(record.submissionId, {
          signal: controller.signal,
        })
        adoptSubmission(value, record, requestGeneration)
        return accepts(requestGeneration, record.draftId) ? submission.value : null
      } catch (error) {
        if (accepts(requestGeneration, record.draftId) && !isCancellation(error)) {
          requestError.value = apiErrorMessage(
            error,
            '提交进度获取失败，请刷新重试',
          )
          if (submission.value !== null) schedulePoll(submission.value)
        }
        throw error
      } finally {
        if (requestGeneration === generation && activeController === controller) {
          activeController = null
          if (submission.value === null || isTerminal(submission.value.status)) {
            polling.value = false
          }
        }
      }
    }

    function pollNow(): Promise<ReimbursementSubmission | null> {
      const current = submission.value
      if (current === null || isTerminal(current.status)) {
        polling.value = false
        clearPollTimer()
        return Promise.resolve(current)
      }
      if (pollFlight?.submissionId === current.submissionId) return pollFlight.promise
      clearPollTimer()
      const record = recordForDraft(current.draftId) ?? {
        draftId: current.draftId,
        expectedRevision: 1,
        idempotencyKey: idempotencyKey.value ?? newIdempotencyKey(),
        submissionId: current.submissionId,
      }
      record.submissionId = current.submissionId
      const requestGeneration = generation
      const promise = fetchSubmission(record, requestGeneration).finally(() => {
        if (pollFlight?.promise === promise) pollFlight = null
      })
      pollFlight = { submissionId: current.submissionId, promise }
      return promise
    }

    async function recheck(): Promise<void> {
      const current = submission.value
      if (!current || current.status !== 'MANUAL_REVIEW' || !current.processInstanceId || submitting.value) return
      const requestGeneration = generation
      const record = recordForDraft(current.draftId)
      if (!record) return
      const controller = new AbortController()
      activeController = controller
      submitting.value = true
      requestError.value = ''
      try {
        const value = await recheckOaReimbursementSubmission(current.submissionId, { signal: controller.signal })
        adoptSubmission(value, record, requestGeneration)
      } catch (error) {
        if (accepts(requestGeneration, current.draftId) && !isCancellation(error)) {
          requestError.value = apiErrorMessage(error, '重新核对失败，请稍后重试')
        }
      } finally {
        if (requestGeneration === generation && activeController === controller) {
          activeController = null
          submitting.value = false
        }
      }
    }

    function restore(
      draftId: string,
      options: RestoreReimbursementSubmissionOptions | number = {},
    ): Promise<ReimbursementSubmission | null> {
      if (!draftId) return Promise.reject(new Error('报销内容无效'))
      const requestGeneration = activateDraft(draftId)
      const record = recordForDraft(draftId)
      if (record === null || (record.submissionId === null && oaSubmissionEnabled.value === false)) {
        const restoreOptions = typeof options === 'number' ? {} : options
        if (record === null && restoreOptions.discoverByDraft !== true) return Promise.resolve(null)
        if (discoveryFlight?.draftId === draftId) return discoveryFlight.promise
        if (record !== null) idempotencyKey.value = record.idempotencyKey
        const promise = discoverSubmissionForDraft(
          draftId,
          record?.expectedRevision ?? restoreOptions.expectedRevision,
          requestGeneration,
        ).then((value) => {
          if (record !== null && value === null && accepts(requestGeneration, draftId)) {
            requestError.value = '尚未找到本次提交记录，服务恢复后可重试本次提交'
            requestAction.value = 'retry'
          }
          return value
        }).finally(() => {
          if (discoveryFlight?.promise === promise) discoveryFlight = null
        })
        discoveryFlight = { draftId, promise }
        return promise
      }
      idempotencyKey.value = record.idempotencyKey
      if (record.submissionId === null) {
        return submit(draftId, record.expectedRevision)
      }
      if (
        submission.value?.submissionId === record.submissionId
        && submission.value.draftId === draftId
      ) {
        if (!isTerminal(submission.value.status)) schedulePoll(submission.value)
        return Promise.resolve(submission.value)
      }
      if (pollFlight?.submissionId === record.submissionId) return pollFlight.promise
      const promise = fetchSubmission(record, requestGeneration).finally(() => {
        if (pollFlight?.promise === promise) pollFlight = null
      })
      pollFlight = { submissionId: record.submissionId, promise }
      return promise
    }

    async function discoverSubmissionForDraft(
      draftId: string,
      expectedRevision: number | undefined,
      requestGeneration: number,
    ): Promise<ReimbursementSubmission | null> {
      const controller = new AbortController()
      activeController = controller
      polling.value = true
      requestError.value = ''
      requestAction.value = null
      try {
        const value = await getOaReimbursementSubmissionForDraft(draftId, {
          signal: controller.signal,
        })
        const record: PersistedSubmission = {
          draftId,
          expectedRevision: Number.isInteger(expectedRevision) && (expectedRevision ?? 0) > 0
            ? expectedRevision!
            : 1,
          idempotencyKey: recordForDraft(draftId)?.idempotencyKey ?? newIdempotencyKey(),
          submissionId: value.submissionId,
        }
        adoptSubmission(value, record, requestGeneration)
        return accepts(requestGeneration, draftId) ? submission.value : null
      } catch (error) {
        if (apiErrorCode(error) === 'REIMBURSEMENT_SUBMISSION_NOT_FOUND') {
          if (accepts(requestGeneration, draftId)) {
            requestError.value = ''
            requestAction.value = null
          }
          return null
        }
        if (accepts(requestGeneration, draftId) && !isCancellation(error)) {
          requestError.value = apiErrorMessage(error, '提交记录恢复失败，请稍后重试')
          requestAction.value = 'discover'
        }
        throw error
      } finally {
        if (requestGeneration === generation && activeController === controller) {
          activeController = null
          if (submission.value === null || isTerminal(submission.value.status)) {
            polling.value = false
          }
        }
      }
    }

    function startPolling(): void {
      if (submission.value !== null) schedulePoll(submission.value)
    }

    function abort(): void {
      invalidateRequests()
      requestError.value = ''
      requestAction.value = null
    }

    function reset(): void {
      invalidateRequests()
      clearPersistedSubmissions()
      memoryRecords.clear()
      activeDraftId.value = null
      idempotencyKey.value = null
      submission.value = null
      requestError.value = ''
      requestAction.value = null
    }

    return {
      activeDraftId,
      idempotencyKey,
      submission,
      submitting,
      polling,
      requestError,
      requestAction,
      oaSubmissionEnabled,
      status,
      terminal,
      succeeded,
      progressLabel,
      errorMessage,
      submit,
      restore,
      pollNow,
      recheck,
      startPolling,
      abort,
      reset,
    }
  },
)

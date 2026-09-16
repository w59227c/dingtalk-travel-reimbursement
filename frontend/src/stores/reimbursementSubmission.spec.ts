import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  getOaReimbursementSubmission,
  getOaReimbursementSubmissionForDraft,
  recheckOaReimbursementSubmission,
  submitOaReimbursement,
} from '@/api/reimbursements'
import {
  REIMBURSEMENT_SUBMISSION_PROGRESS_LABELS,
  useReimbursementSubmissionStore,
} from '@/stores/reimbursementSubmission'
import type {
  ReimbursementSubmission,
  ReimbursementSubmissionStatus,
} from '@/types/reimbursements'

vi.mock('@/api/reimbursements', () => ({
  getOaReimbursementSubmission: vi.fn(),
  getOaReimbursementSubmissionForDraft: vi.fn(),
  recheckOaReimbursementSubmission: vi.fn(),
  submitOaReimbursement: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function task(
  status: ReimbursementSubmissionStatus = 'QUEUED',
  statusVersion = 1,
  overrides: Partial<ReimbursementSubmission> = {},
): ReimbursementSubmission {
  return {
    submissionId: 'submission-1',
    draftId: 'draft-1',
    status,
    statusVersion,
    attemptCount: 0,
    processInstanceId: status === 'SUBMITTED' ? 'process-1' : null,
    businessId: status === 'SUBMITTED' ? '202609040001' : null,
    approvalUrl: status === 'SUBMITTED' ? 'dingtalk://approval/process-1' : null,
    error: null,
    pollAfterMs: status === 'SUBMITTED' ? 0 : 1_500,
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
    submittedAt: status === 'SUBMITTED' ? '2026-09-04T00:01:00Z' : null,
    ...overrides,
  }
}

describe('reimbursement submission store', () => {
  it('rechecks an existing manual-review OA then polls without creating another approval', async () => {
    const store = useReimbursementSubmissionStore()
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(task('MANUAL_REVIEW', 4, { processInstanceId: 'existing-oa' }))
    await store.restore('draft-1', { discoverByDraft: true })
    const pending = deferred<ReimbursementSubmission>()
    vi.mocked(recheckOaReimbursementSubmission).mockReturnValue(pending.promise)
    const first = store.recheck()
    await store.recheck()
    expect(recheckOaReimbursementSubmission).toHaveBeenCalledOnce()
    pending.resolve(task('VERIFYING', 5, { processInstanceId: 'existing-oa' }))
    await first
    expect(store.status).toBe('VERIFYING')
    vi.mocked(getOaReimbursementSubmission).mockResolvedValue(task('SUBMITTED', 6, { processInstanceId: 'existing-oa' }))
    await store.pollNow()
    expect(store.status).toBe('SUBMITTED')
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    store.abort()
  })
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    window.sessionStorage.clear()
    vi.clearAllMocks()
  })

  afterEach(() => {
    window.sessionStorage.clear()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('coalesces repeated clicks into one POST with one stable UUID', async () => {
    const pending = deferred<ReimbursementSubmission>()
    vi.mocked(submitOaReimbursement).mockReturnValue(pending.promise)
    const store = useReimbursementSubmissionStore()

    const first = store.submit('draft-1', 7)
    const second = store.submit('draft-1', 7)

    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    const key = vi.mocked(submitOaReimbursement).mock.calls[0]![2]
    expect(key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    pending.resolve(task())
    await expect(first).resolves.toEqual(task())
    await expect(second).resolves.toEqual(task())

    expect(store.idempotencyKey).toBe(key)
    expect(store.progressLabel).toBe('本系统已接收，等待后台处理')
    expect(store.polling).toBe(true)
  })

  it('submits with a UUID idempotency key when Web Crypto is unavailable', async () => {
    vi.stubGlobal('crypto', undefined)
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    const store = useReimbursementSubmissionStore()

    await store.submit('draft-1', 7)

    expect(vi.mocked(submitOaReimbursement).mock.calls[0]![2]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    expect(store.status).toBe('QUEUED')
    store.abort()
  })

  it('stops automatic polling when OA is disabled, preserves identity and permits a manual refresh', async () => {
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    vi.mocked(getOaReimbursementSubmission).mockResolvedValue(task())
    const store = useReimbursementSubmissionStore()
    await store.submit('draft-1', 7)
    const key = store.idempotencyKey
    store.oaSubmissionEnabled = false
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getOaReimbursementSubmission).not.toHaveBeenCalled()
    expect(store.polling).toBe(false)
    expect(store.idempotencyKey).toBe(key)
    expect(store.status).toBe('QUEUED')
    await store.pollNow()
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    expect(store.polling).toBe(false)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    store.oaSubmissionEnabled = true
    await vi.advanceTimersByTimeAsync(1_500)
    expect(getOaReimbursementSubmission).toHaveBeenCalledTimes(2)
    store.abort()
  })

  it('releases an unaccepted submission identity after an explicit disabled-service response', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValue({
      isAxiosError: true,
      response: { status: 503, data: { error: { code: 'OA_SUBMISSION_DISABLED', message: 'OA提交服务未开启，可继续填写' } } },
    })
    const store = useReimbursementSubmissionStore()
    await expect(store.submit('draft-1', 7)).rejects.toBeDefined()
    expect(store.oaSubmissionEnabled).toBe(false)
    expect(store.submission).toBeNull()
    expect(store.idempotencyKey).toBeNull()
    expect(store.errorMessage).toBe('OA提交服务未开启，可继续填写')
    const rejectedKey = vi.mocked(submitOaReimbursement).mock.calls[0]![2]
    store.oaSubmissionEnabled = true
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    await store.submit('draft-1', 8)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.[1]).toBe(8)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.[2]).not.toBe(rejectedKey)
    store.abort()
  })

  it('releases a rejected submission identity after a definite validation response', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          error: {
            code: 'REIMBURSEMENT_SNAPSHOT_INVALID',
            message: '报销提交快照无法生成，请刷新后重试',
          },
        },
      },
    })
    const store = useReimbursementSubmissionStore()

    await expect(store.submit('draft-1', 7)).rejects.toBeDefined()

    const rejectedKey = vi.mocked(submitOaReimbursement).mock.calls[0]![2]
    expect(store.submission).toBeNull()
    expect(store.idempotencyKey).toBeNull()
    expect(store.requestAction).toBeNull()
    expect(store.errorMessage).toContain('快照无法生成')

    vi.mocked(submitOaReimbursement).mockResolvedValueOnce(task())
    await store.submit('draft-1', 8)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.[1]).toBe(8)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.[2]).not.toBe(rejectedKey)
    store.abort()
  })

  it('polls using pollAfterMs and stops at a terminal status', async () => {
    vi.mocked(submitOaReimbursement).mockResolvedValue(task('QUEUED', 1, {
      pollAfterMs: 20,
    }))
    vi.mocked(getOaReimbursementSubmission)
      .mockResolvedValueOnce(task('UPLOADING', 2, { pollAfterMs: 40 }))
      .mockResolvedValueOnce(task('SUBMITTED', 3))
    const store = useReimbursementSubmissionStore()

    await store.submit('draft-1', 7)
    expect(getOaReimbursementSubmission).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(20)
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    expect(store.status).toBe('UPLOADING')
    expect(store.progressLabel).toBe('正在上传审批附件')

    await vi.advanceTimersByTimeAsync(39)
    expect(getOaReimbursementSubmission).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(1)
    expect(getOaReimbursementSubmission).toHaveBeenCalledTimes(2)
    expect(store.status).toBe('SUBMITTED')
    expect(store.terminal).toBe(true)
    expect(store.succeeded).toBe(true)
    expect(store.polling).toBe(false)

    await vi.advanceTimersByTimeAsync(120_000)
    expect(getOaReimbursementSubmission).toHaveBeenCalledTimes(2)
  })

  it('restores a known submission after a fresh Pinia instance without another POST', async () => {
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    const firstStore = useReimbursementSubmissionStore()
    await firstStore.submit('draft-1', 7)
    const originalKey = firstStore.idempotencyKey
    firstStore.abort()

    setActivePinia(createPinia())
    vi.mocked(getOaReimbursementSubmission).mockResolvedValue(task('VALIDATING', 2))
    const restoredStore = useReimbursementSubmissionStore()

    await expect(restoredStore.restore('draft-1')).resolves.toEqual(task('VALIDATING', 2))

    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    expect(getOaReimbursementSubmission).toHaveBeenCalledWith(
      'submission-1',
      { signal: expect.any(AbortSignal) },
    )
    expect(restoredStore.idempotencyKey).toBe(originalKey)
    expect(restoredStore.status).toBe('VALIDATING')
  })

  it('reuses the persisted UUID when the first POST result was unknown', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValueOnce(new Error('connection lost'))
    const firstStore = useReimbursementSubmissionStore()

    await expect(firstStore.submit('draft-1', 7)).rejects.toThrow('connection lost')
    const originalKey = firstStore.idempotencyKey
    expect(originalKey).toBeTruthy()
    expect(firstStore.requestAction).toBe('retry')

    setActivePinia(createPinia())
    vi.mocked(submitOaReimbursement).mockResolvedValueOnce(task())
    const restoredStore = useReimbursementSubmissionStore()
    await restoredStore.restore('draft-1')

    expect(submitOaReimbursement).toHaveBeenCalledTimes(2)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.slice(0, 3)).toEqual([
      'draft-1',
      7,
      originalKey,
    ])
    expect(restoredStore.idempotencyKey).toBe(originalKey)
  })

  it('replays an uncertain POST with its persisted revision instead of a newer caller revision', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValueOnce(new Error('connection lost'))
    const firstStore = useReimbursementSubmissionStore()
    await expect(firstStore.submit('draft-1', 7)).rejects.toThrow('connection lost')
    const originalKey = firstStore.idempotencyKey

    setActivePinia(createPinia())
    vi.mocked(submitOaReimbursement).mockResolvedValueOnce(task())
    const restoredStore = useReimbursementSubmissionStore()
    await restoredStore.submit('draft-1', 99)

    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.slice(0, 3)).toEqual([
      'draft-1',
      7,
      originalKey,
    ])
  })

  it('discovers an existing task after an unknown POST even while OA submission is disabled', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValueOnce(new Error('response lost'))
    const firstStore = useReimbursementSubmissionStore()
    await expect(firstStore.submit('draft-1', 7)).rejects.toThrow('response lost')
    const originalKey = firstStore.idempotencyKey
    firstStore.abort()
    setActivePinia(createPinia())
    const store = useReimbursementSubmissionStore()
    store.oaSubmissionEnabled = false
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(task())
    await expect(store.restore('draft-1', { discoverByDraft: true, expectedRevision: 8 }))
      .resolves.toEqual(task())
    expect(store.idempotencyKey).toBe(originalKey)
    expect(store.status).toBe('QUEUED')
    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(getOaReimbursementSubmission).not.toHaveBeenCalled()
    store.abort()
  })

  it('retains an unknown submission identity when disabled-service discovery cannot find a task', async () => {
    vi.mocked(submitOaReimbursement).mockRejectedValueOnce(new Error('response lost'))
    const firstStore = useReimbursementSubmissionStore()
    await expect(firstStore.submit('draft-1', 7)).rejects.toThrow('response lost')
    const originalKey = firstStore.idempotencyKey
    firstStore.abort()
    setActivePinia(createPinia())
    const store = useReimbursementSubmissionStore()
    store.oaSubmissionEnabled = false
    vi.mocked(getOaReimbursementSubmissionForDraft).mockRejectedValue({
      isAxiosError: true,
      response: { status: 404, data: { error: { code: 'REIMBURSEMENT_SUBMISSION_NOT_FOUND' } } },
    })
    await expect(store.restore('draft-1')).resolves.toBeNull()
    expect(store.idempotencyKey).toBe(originalKey)
    expect(store.requestError).toContain('服务恢复后可重试本次提交')
    expect(submitOaReimbursement).toHaveBeenCalledOnce()
    store.oaSubmissionEnabled = true
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    await store.submit('draft-1', 8)
    expect(vi.mocked(submitOaReimbursement).mock.calls[1]?.slice(0, 3))
      .toEqual(['draft-1', 7, originalKey])
    store.abort()
  })

  it('does not create a submission while restoring a draft without a persisted record', async () => {
    const store = useReimbursementSubmissionStore()

    await expect(store.restore('draft-1', 7)).resolves.toBeNull()

    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expect(getOaReimbursementSubmission).not.toHaveBeenCalled()
    expect(getOaReimbursementSubmissionForDraft).not.toHaveBeenCalled()
    expect(store.idempotencyKey).toBeNull()
  })

  it('recovers a locked draft across devices through the read-only draft lookup', async () => {
    vi.mocked(getOaReimbursementSubmissionForDraft).mockResolvedValue(task('VERIFYING', 4))
    const store = useReimbursementSubmissionStore()

    await expect(store.restore('draft-1', {
      discoverByDraft: true,
      expectedRevision: 9,
    })).resolves.toEqual(task('VERIFYING', 4))

    expect(getOaReimbursementSubmissionForDraft).toHaveBeenCalledWith(
      'draft-1',
      { signal: expect.any(AbortSignal) },
    )
    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expect(store.status).toBe('VERIFYING')
    expect(store.polling).toBe(true)
  })

  it('treats a missing read-only draft lookup as no submission without posting', async () => {
    vi.mocked(getOaReimbursementSubmissionForDraft).mockRejectedValue(Object.assign(
      new Error('not found'),
      {
        isAxiosError: true,
        response: {
          status: 404,
          data: {
            error: {
              code: 'REIMBURSEMENT_SUBMISSION_NOT_FOUND',
              message: '提交记录不存在',
            },
          },
        },
      },
    ))
    const store = useReimbursementSubmissionStore()

    await expect(store.restore('draft-1', { discoverByDraft: true })).resolves.toBeNull()

    expect(submitOaReimbursement).not.toHaveBeenCalled()
    expect(store.requestError).toBe('')
    expect(store.polling).toBe(false)
  })

  it('isolates a late response after the user switches to another draft', async () => {
    const stale = deferred<ReimbursementSubmission>()
    vi.mocked(submitOaReimbursement)
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(task('QUEUED', 1, {
        submissionId: 'submission-2',
        draftId: 'draft-2',
      }))
    const store = useReimbursementSubmissionStore()

    const oldRequest = store.submit('draft-1', 1)
    await store.submit('draft-2', 2)
    stale.resolve(task('SUBMITTED', 5))
    await oldRequest

    expect(store.activeDraftId).toBe('draft-2')
    expect(store.submission?.submissionId).toBe('submission-2')
    expect(store.status).toBe('QUEUED')
  })

  it('aborts and ignores a stale poll response after reset', async () => {
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    const stale = deferred<ReimbursementSubmission>()
    let pollSignal: AbortSignal | undefined
    vi.mocked(getOaReimbursementSubmission).mockImplementation((_id, options) => {
      pollSignal = options?.signal
      return stale.promise
    })
    const store = useReimbursementSubmissionStore()
    await store.submit('draft-1', 7)
    const poll = store.pollNow()

    store.reset()
    expect(pollSignal?.aborted).toBe(true)
    stale.resolve(task('SUBMITTED', 2))
    await poll

    expect(store.activeDraftId).toBeNull()
    expect(store.submission).toBeNull()
    expect(store.polling).toBe(false)

    setActivePinia(createPinia())
    await expect(useReimbursementSubmissionStore().restore('draft-1')).resolves.toBeNull()
  })

  it('keeps the newest status when an older poll completes later', async () => {
    vi.mocked(submitOaReimbursement).mockResolvedValue(task())
    const stale = deferred<ReimbursementSubmission>()
    vi.mocked(getOaReimbursementSubmission)
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(task('VERIFYING', 3))
    const store = useReimbursementSubmissionStore()
    await store.submit('draft-1', 7)

    const oldPoll = store.pollNow()
    store.abort()
    await store.pollNow()
    stale.resolve(task('UPLOADING', 2))
    await oldPoll

    expect(store.status).toBe('VERIFYING')
    expect(store.submission?.statusVersion).toBe(3)
  })

  it('exposes stable progress and failure messages for every terminal outcome', () => {
    const store = useReimbursementSubmissionStore()
    store.submission = task('FAILED_RETRYABLE', 2)
    expect(store.terminal).toBe(false)
    expect(store.errorMessage).toContain('自动重试')

    store.submission = task('FAILED_FINAL', 3, {
      error: { code: 'OA_CREATE_REJECTED', message: '钉钉拒绝发起审批' },
    })
    expect(store.terminal).toBe(true)
    expect(store.progressLabel).toBe(REIMBURSEMENT_SUBMISSION_PROGRESS_LABELS.FAILED_FINAL)
    expect(store.errorMessage).toBe('钉钉拒绝发起审批')

    store.submission = task('MANUAL_REVIEW', 4)
    expect(store.terminal).toBe(true)
    expect(store.errorMessage).toContain('人工核对')
  })
})

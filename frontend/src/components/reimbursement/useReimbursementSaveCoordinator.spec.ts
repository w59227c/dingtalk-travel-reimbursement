import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { effectScope, type EffectScope } from 'vue'

import { useReimbursementSaveCoordinator } from './useReimbursementSaveCoordinator'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

describe('reimbursement save coordinator', () => {
  let scope: EffectScope
  let target: { scope: string; draftId: string | undefined }
  let allowed: boolean
  let autosave: ReturnType<typeof vi.fn<() => Promise<void>>>
  let coordinator: ReturnType<typeof useReimbursementSaveCoordinator>

  beforeEach(() => {
    vi.useFakeTimers()
    target = { scope: 'synthetic-session-1', draftId: 'synthetic-draft-1' }
    allowed = true
    autosave = vi.fn().mockResolvedValue(undefined)
    scope = effectScope()
    coordinator = scope.run(() => useReimbursementSaveCoordinator({
      target: () => target, canAutosave: () => allowed, autosave,
    }))!
  })

  afterEach(() => {
    scope.stop()
    vi.useRealTimers()
  })

  it('debounces edits for 600 ms and reads the latest form when saving', async () => {
    let input = 'first edit'
    autosave.mockImplementation(async () => { expect(input).toBe('latest edit') })
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(599)
    expect(autosave).not.toHaveBeenCalled()
    input = 'latest edit'
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(599)
    expect(autosave).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(autosave).toHaveBeenCalledOnce()
  })

  it('rechecks whether autosaving is allowed after the debounce wait', async () => {
    coordinator.schedule()
    allowed = false
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
    allowed = true
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).toHaveBeenCalledOnce()
  })

  it('does not schedule a blocked form and does not loop on a failed autosave', async () => {
    allowed = false
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
    allowed = true
    autosave.mockRejectedValueOnce(new Error('synthetic save failure'))
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(10_000)
    expect(autosave).toHaveBeenCalledOnce()
    coordinator.schedule()
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).toHaveBeenCalledTimes(2)
  })

  it.each(['scope', 'draftId'] as const)('does not apply a scheduled save to a changed %s', async (field) => {
    coordinator.schedule()
    target[field] = 'another-target'
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
  })

  it('manual saving cancels a pending background save', async () => {
    coordinator.schedule()
    const save = vi.fn().mockResolvedValue('saved')
    expect(await coordinator.run(save)).toBe('saved')
    await vi.advanceTimersByTimeAsync(600)
    expect(save).toHaveBeenCalledOnce()
    expect(autosave).not.toHaveBeenCalled()
    expect(coordinator.pending.value).toBe(false)
  })

  it('runs approval and form operations in order without concurrent writes', async () => {
    const approval = deferred<string>()
    const form = deferred<string>()
    const calls: string[] = []
    const first = coordinator.run(() => { calls.push('approval'); return approval.promise })
    const second = coordinator.run(() => { calls.push('form'); return form.promise })
    const third = coordinator.run(async () => { calls.push('navigation'); return 'ready' })
    expect(coordinator.pending.value).toBe(true)
    expect(calls).toEqual(['approval'])
    approval.resolve('verified')
    expect(await first).toBe('verified')
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toEqual(['approval', 'form'])
    expect(coordinator.pending.value).toBe(true)
    form.resolve('saved')
    expect(await second).toBe('saved')
    expect(await third).toBe('ready')
    expect(calls).toEqual(['approval', 'form', 'navigation'])
    expect(coordinator.pending.value).toBe(false)
  })

  it('releases a rejected operation for an explicit retry', async () => {
    const pending = deferred<void>()
    const failure = coordinator.run(() => pending.promise)
    const rejected = expect(failure).rejects.toThrow('synthetic write failure')
    const retry = vi.fn().mockResolvedValue('recovered')
    const recovery = coordinator.run(retry)
    expect(retry).not.toHaveBeenCalled()
    pending.reject(new Error('synthetic write failure'))
    await rejected
    expect(await recovery).toBe('recovered')
    expect(coordinator.pending.value).toBe(false)
  })

  it('releases synchronous failures without leaving a busy queue', async () => {
    await expect(coordinator.run(() => { throw new Error('synchronous failure') })).rejects.toThrow('synchronous failure')
    expect(coordinator.pending.value).toBe(false)
    expect(await coordinator.run(async () => 'recovered')).toBe('recovered')
  })

  it.each(['scope', 'draftId'] as const)('rejects queued work after a changed %s', async (field) => {
    const pending = deferred<void>()
    const first = coordinator.run(() => pending.promise)
    const waiting = vi.fn().mockResolvedValue(undefined)
    const queued = coordinator.run(waiting)
    const rejected = expect(queued).rejects.toThrow('当前报销已变化')
    target[field] = 'another-target'
    pending.resolve()
    await first
    await rejected
    expect(waiting).not.toHaveBeenCalled()
  })

  it.each(['resolve', 'reject'] as const)('keeps a new scope pending when an old operation later %s', async (settlement) => {
    const oldWrite = deferred<void>()
    const first = coordinator.run(() => oldWrite.promise)
    const firstResult = settlement === 'reject' ? expect(first).rejects.toThrow('old failure') : first
    const waiting = vi.fn().mockResolvedValue(undefined)
    const oldQueued = coordinator.run(waiting)
    const canceled = expect(oldQueued).rejects.toThrow('当前报销已变化')
    coordinator.schedule()
    // Reset invalidates even an A -> B -> A switch whose final target looks identical.
    coordinator.reset()
    const newWrite = deferred<void>()
    const current = coordinator.run(() => newWrite.promise)
    if (settlement === 'resolve') oldWrite.resolve()
    else oldWrite.reject(new Error('old failure'))
    await firstResult
    await canceled
    expect(waiting).not.toHaveBeenCalled()
    expect(coordinator.pending.value).toBe(true)
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
    newWrite.resolve()
    await current
    expect(coordinator.pending.value).toBe(false)
  })

  it('canceling the timer leaves an explicit save and its queued successor intact', async () => {
    const pending = deferred<void>()
    const first = coordinator.run(() => pending.promise)
    const next = coordinator.run(async () => 'saved')
    coordinator.schedule()
    coordinator.cancelScheduled()
    expect(coordinator.pending.value).toBe(true)
    pending.resolve()
    await first
    expect(await next).toBe('saved')
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
  })

  it('disposal invalidates queued work and prevents later scheduling or saving', async () => {
    const pending = deferred<void>()
    const first = coordinator.run(() => pending.promise)
    const operation = vi.fn().mockResolvedValue(undefined)
    const queued = coordinator.run(operation)
    const canceled = expect(queued).rejects.toThrow('当前报销已变化')
    coordinator.schedule()
    scope.stop()
    coordinator.schedule()
    pending.resolve()
    await first
    await canceled
    await expect(coordinator.run(operation)).rejects.toThrow('当前报销已变化')
    await vi.advanceTimersByTimeAsync(600)
    expect(autosave).not.toHaveBeenCalled()
    expect(operation).not.toHaveBeenCalled()
    expect(coordinator.pending.value).toBe(false)
  })
})

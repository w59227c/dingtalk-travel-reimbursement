import { onScopeDispose } from 'vue'

import {
  createReimbursementDraft, getOaReimbursementOptions, listWorkspaceReimbursements,
  openReimbursementWorkspace,
  type ReimbursementRequestOptions,
} from '@/api/reimbursements'
import { readConsistentDraft, type useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { AuthSession } from '@/types/auth'
import type {
  ReimbursementDraft, ReimbursementDraftInput, ReimbursementDraftSummary, ReimbursementKind,
} from '@/types/reimbursements'

type WorkspaceRequest = NonNullable<Parameters<typeof listWorkspaceReimbursements>[0]>
type DraftStore = Pick<ReturnType<typeof useReimbursementDraftStore>, 'loadDraft' | 'currentDraft' | 'loadError'>
export interface WorkspaceReadContext {
  readonly scope: string
  readonly generation: number
  readonly request: Readonly<WorkspaceRequest>
}
type WorkspaceResult = { kind: 'stale' } | { kind: 'empty' }
  | { kind: 'legacy'; items: ReimbursementDraftSummary[] }
  | { kind: 'current'; draft: ReimbursementDraftSummary }
type DraftResult = { kind: 'stale' } | { kind: 'rejected'; message: string }
  | { kind: 'loaded'; draft: ReimbursementDraft }

export function emptyReimbursementInput(kind: ReimbursementKind): ReimbursementDraftInput {
  const common = { ocrDispositionVersion: 1 as const, companyValue: '', budgetCodeValue: '',
    trip: null, items: [], dismissedOcrFileIds: [] }
  if (kind === 'general') return common
  return {
    ...common, trips: [],
    editingState: {
      includeSubsidy: false,
      trip: {
        tripType: 'business', startDate: '', startTime: '09:00', endDate: '', endTime: '18:00',
        policyConfirmed: false, confirmedEffectiveDays: '', noSubsidyException: false,
      },
      trips: [],
    },
  }
}

/** Coordinates server reads; the view retains dirty-input confirmation and UI state. */
export function useReimbursementWorkspaceReader(options: {
  scope: () => string
  request: () => WorkspaceRequest
  drafts: DraftStore
}) {
  let generation = 0
  let disposed = false

  function capture(): WorkspaceReadContext {
    const request = options.request()
    return { scope: options.scope(), generation, request: {
      ...request,
      ...(request?.expectedDepartment ? { expectedDepartment: { ...request.expectedDepartment } } : {}),
    } }
  }
  function invalidate(): void { generation += 1 }
  function begin(): WorkspaceReadContext { invalidate(); return capture() }
  function accepts(context: WorkspaceReadContext): boolean {
    return !disposed && Boolean(context.scope) && context.scope === options.scope() && context.generation === generation
  }

  async function list(context: WorkspaceReadContext): Promise<WorkspaceResult> {
    if (!accepts(context)) return { kind: 'stale' }
    try {
      const workspace = await listWorkspaceReimbursements(context.request)
      if (!accepts(context)) return { kind: 'stale' }
      if (workspace.items.length > 1) return { kind: 'legacy', items: workspace.items }
      return workspace.items[0] ? { kind: 'current', draft: workspace.items[0] } : { kind: 'empty' }
    } catch (error) {
      if (!accepts(context)) return { kind: 'stale' }
      throw error
    }
  }

  async function load(context: WorkspaceReadContext, draftId: string, policy: {
    source?: 'workspace' | 'saved' | 'history'
    accept?: (draft: ReimbursementDraft) => boolean
  } = {}): Promise<DraftResult> {
    if (!accepts(context)) return { kind: 'stale' }
    const source = policy.source ?? 'workspace'
    const adopted = await options.drafts.loadDraft(draftId, true, incoming => accepts(context)
      && (source === 'history' ? incoming.status === 'LOCKED' : source === 'saved' || !incoming.workspaceRetired)
      && (!policy.accept || policy.accept(incoming)), source === 'history')
    if (!accepts(context)) return { kind: 'stale' }
    const draft = options.drafts.currentDraft
    if (!adopted || !draft || draft.id !== draftId || options.drafts.loadError) {
      return { kind: 'rejected', message: options.drafts.loadError }
    }
    return { kind: 'loaded', draft }
  }

  // Prepare an employee's workspace before applying their authenticated context.
  // No original-page state is adopted here. A failed read keeps that page intact.
  async function prepareEmployee(target: AuthSession, signal: AbortSignal) {
    const context = capture()
    const acceptsTarget = () => accepts(context) && !signal.aborted
    const request: ReimbursementRequestOptions = { signal, reimbursementContext: target.reimbursementContext ?? null }
    if (!acceptsTarget()) return undefined
    const [workspace, oaOptions] = await Promise.allSettled([
      listWorkspaceReimbursements(request), getOaReimbursementOptions(request),
    ])
    if (!acceptsTarget()) return undefined
    if (workspace.status === 'rejected') throw workspace.reason
    if (workspace.value.items.length > 1) return null
    const current = workspace.value.items[0]
    if (!current && !target.selectedDepartment) return null
    if (current && (current.department.id !== target.selectedDepartment?.id
      || current.department.name !== target.selectedDepartment?.name)) {
      const restored = await openReimbursementWorkspace(current.id, [current], request)
      if (!acceptsTarget()) return undefined
      target.selectedDepartment = restored.selectedDepartment
      if (restored.reimbursementContext) {
        target.reimbursementContext = restored.reimbursementContext
        request.reimbursementContext = restored.reimbursementContext
      }
    }
    if (!current && oaOptions.status === 'rejected') throw oaOptions.reason
    const recent = current ?? await createReimbursementDraft(emptyReimbursementInput(context.request?.kind ?? 'travel'), {
      ...request, reuseExisting: true, expectedDepartment: target.selectedDepartment!,
    })
    if (!acceptsTarget()) return undefined
    const read = await readConsistentDraft(recent.id, request)
    if (!acceptsTarget()) return undefined
    if (read.draft.workspaceRetired) throw new Error('目标报销已发生变化，请重新选择报销人')
    return { read, options: oaOptions }
  }

  onScopeDispose(() => { disposed = true; invalidate() })
  return { capture, begin, accepts, invalidate, list, load, prepareEmployee }
}

import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'

import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraft, ReimbursementDraftFile } from '@/types/reimbursements'
import { expenseProofFields, isActiveProof } from '@/utils/expenseProofs'

export type ProofKind = 'payment_proof' | 'hotel_bill' | 'waybill'
export interface ProofUploadTarget {
  itemId: string
  draftId: string
  pickerGeneration: number
  kind: ProofKind
}
interface ItineraryEditor {
  id: string
  transportType: string
  itineraryFileIds: string[]
  itineraryAutoMatchDisabled: boolean
}

/** Owns material-selection transactions; uploading never confirms an association. */
export function useExpenseMaterialPicker(options: {
  draft: () => Pick<ReimbursementDraft, 'id' | 'status'> | null
  editable: () => boolean
  blocked: () => boolean
  items: () => ExpenseItem[]
  files: () => readonly ReimbursementDraftFile[]
  editor: { state: ItineraryEditor; visible: Ref<boolean>; pickerOnly: Ref<boolean>; revision: Ref<number> }
}) {
  let disposed = false
  const generation = ref(0)
  let proofDraftId: string | undefined
  let pendingProofUpload: ProofUploadTarget | null = null
  let baseline: string[] = []
  const visible = ref(false)
  const kind = ref<ProofKind>('payment_proof')
  const itemId = ref('')
  const selection = ref<string[]>([])
  const error = ref('')
  const item = computed(() => options.items().find(candidate => candidate.id === itemId.value))
  const candidates = computed(() => options.files().filter(file => isActiveProof(file, kind.value)))
  const selected = computed(() => selection.value.flatMap(id => {
    const file = candidates.value.find(candidate => candidate.id === id)
    return file ? [file] : []
  }))
  const changed = computed(() => JSON.stringify(selection.value) !== JSON.stringify(baseline))
  const canMutate = () => !disposed && options.editable() && !options.blocked()

  function closeProof(): void {
    visible.value = false
    pendingProofUpload = null
  }
  function openProof(expense: ExpenseItem, proofKind: ProofKind = 'payment_proof'): void {
    const draft = options.draft()
    if (!canMutate() || !draft) return
    generation.value += 1
    pendingProofUpload = null
    proofDraftId = draft.id
    itemId.value = expense.id
    kind.value = proofKind
    baseline = [...(expense[expenseProofFields[proofKind]] ?? [])]
    selection.value = [...baseline]
    error.value = ''
    visible.value = true
  }
  function confirmProof(): void {
    const expense = item.value
    if (!visible.value || !canMutate() || !expense || proofDraftId !== options.draft()?.id) return
    const field = expenseProofFields[kind.value]
    if (JSON.stringify(expense[field] ?? []) !== JSON.stringify(baseline)) {
      error.value = '本笔费用的材料关联已变化，请关闭后重新打开核对'
      return
    }
    if (selection.value.some(id => !candidates.value.some(file => file.id === id))) {
      error.value = '所选材料已删除或用途已变化，请重新选择'
      return
    }
    expense[field] = [...new Set(selection.value)]
    closeProof()
  }
  function acceptsUploadTarget(target: ProofUploadTarget): boolean {
    return !disposed && target.draftId === options.draft()?.id
      && options.items().some(expense => expense.id === target.itemId)
  }
  function isCurrentProofTarget(target: ProofUploadTarget): boolean {
    return target.pickerGeneration === generation.value && target.kind === kind.value
  }
  function armProofUpload(): boolean {
    const expense = item.value, draft = options.draft()
    if (!visible.value || !canMutate() || !expense || !draft || proofDraftId !== draft.id) return false
    pendingProofUpload = { itemId: expense.id, draftId: draft.id, pickerGeneration: generation.value, kind: kind.value }
    return true
  }
  function consumeProofUpload(proofKind: ProofKind): ProofUploadTarget | null {
    const target = pendingProofUpload
    pendingProofUpload = null
    return target && visible.value && target.kind === proofKind && isCurrentProofTarget(target)
      && acceptsUploadTarget(target) ? target : null
  }
  watch(visible, isVisible => { if (!isVisible) pendingProofUpload = null }, { flush: 'sync' })

  const mode = ref<'single' | 'multiple' | 'keep-one'>('single')
  const query = ref('')
  const itineraryChanged = ref(false)
  const uploadError = ref('')
  let uploadRevision: number | null = null
  const itineraryItem = computed(() => options.editor.pickerOnly.value && options.editor.visible.value
    ? options.items().find(expense => expense.id === options.editor.state.id) : undefined)
  const itineraryCandidates = computed(() => options.files().filter(file => isActiveProof(file, 'itinerary')))

  function resetItinerary(): void {
    mode.value = options.editor.state.itineraryFileIds.length > 1 ? 'multiple' : 'single'
    query.value = ''
    itineraryChanged.value = false
    uploadError.value = ''
  }
  function toggleItineraryMode(): void {
    query.value = ''
    mode.value = mode.value === 'multiple'
      ? options.editor.state.itineraryFileIds.length > 1 ? 'keep-one' : 'single' : 'multiple'
  }
  function chooseItineraries(value: string[] | string | null | undefined): void {
    const editor = options.editor.state
    if (editor.transportType !== 'ride_hailing') return
    if (mode.value === 'keep-one') {
      if (typeof value !== 'string' || !editor.itineraryFileIds.includes(value)) return
      query.value = ''
      mode.value = 'single'
    }
    editor.itineraryFileIds = Array.isArray(value) ? [...value] : value ? [value] : []
    editor.itineraryAutoMatchDisabled = true
    if (options.editor.pickerOnly.value) itineraryChanged.value = true
  }
  function confirmItinerary(automatic = false): string | null {
    if (!canMutate() || mode.value === 'keep-one') return null
    const expense = itineraryItem.value
    if (!expense || expense.category !== 'local_transport'
      || (expense.transportType ?? (expense.requiresItinerary ? 'ride_hailing' : 'other')) !== 'ride_hailing') return null
    if (options.editor.state.itineraryFileIds.some(id => !itineraryCandidates.value.some(file => file.id === id))) {
      return '所选行程单已删除或用途已变化，请重新选择'
    }
    if (automatic || itineraryChanged.value) {
      expense.itineraryFileIds = [...options.editor.state.itineraryFileIds]
      expense.itineraryAutoMatchDisabled = !automatic
    }
    options.editor.visible.value = false
    return null
  }
  function armItineraryUpload(): void { uploadRevision = options.editor.revision.value }
  function consumeItineraryUpload(): { revision: number | null; fromPicker: boolean } | null {
    const revision = uploadRevision
    uploadRevision = null
    if (disposed || (revision !== null && (!options.editor.visible.value || revision !== options.editor.revision.value))) return null
    const fromPicker = options.editor.pickerOnly.value && revision === options.editor.revision.value
    if (fromPicker) uploadError.value = ''
    return { revision, fromPicker }
  }
  function finishItineraryUpload(revision: number | null, message: string): void {
    if (!disposed && options.editor.pickerOnly.value && options.editor.visible.value
      && options.editor.revision.value === revision) uploadError.value = message
  }
  watch(() => itineraryItem.value?.itineraryFileIds, ids => {
    if (!itineraryItem.value || itineraryChanged.value) return
    options.editor.state.itineraryFileIds = [...(ids ?? [])]
    if (options.editor.state.itineraryFileIds.length > 1 && mode.value === 'single') mode.value = 'multiple'
  })
  watch(() => [options.draft()?.id, options.draft()?.status, options.editable()] as const,
    ([draftId, status], [previousId]) => {
      if (draftId !== previousId || !options.editable()) closeProof()
      if (options.editor.pickerOnly.value && (draftId !== previousId || status === 'LOCKED' || status === 'EXPIRED')) {
        options.editor.visible.value = false
      }
    }, { flush: 'sync' })
  onScopeDispose(() => { disposed = true; closeProof() })

  return {
    proof: { visible, kind, selection, error, item, candidates, selected, changed,
      open: openProof, close: closeProof, confirm: confirmProof, armUpload: armProofUpload,
      consumeUpload: consumeProofUpload, acceptsUploadTarget, isCurrentTarget: isCurrentProofTarget },
    itinerary: { mode, query, changed: itineraryChanged, uploadError, item: itineraryItem,
      candidates: itineraryCandidates, reset: resetItinerary, toggleMode: toggleItineraryMode,
      choose: chooseItineraries, confirm: confirmItinerary, armUpload: armItineraryUpload,
      consumeUpload: consumeItineraryUpload, finishUpload: finishItineraryUpload },
  }
}

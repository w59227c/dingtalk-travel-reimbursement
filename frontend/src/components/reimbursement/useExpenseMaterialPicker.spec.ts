import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { effectScope, nextTick, reactive, ref, type EffectScope, type Ref } from 'vue'

import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraft, ReimbursementDraftFile } from '@/types/reimbursements'
import { useExpenseMaterialPicker, type ProofKind } from './useExpenseMaterialPicker'

function item(overrides: Partial<ExpenseItem> = {}): ExpenseItem {
  return { id: 'synthetic-expense', category: 'local_transport', description: '合成费用',
    amount: '86.50', receiptCount: 1, date: '2026-11-01', displayDate: '2026-11-01',
    source: 'manual', transportType: 'ride_hailing', requiresItinerary: true, warnings: [], ...overrides }
}
function file(id: string, kind: ReimbursementDraftFile['attachmentKind']): ReimbursementDraftFile {
  return { id, name: '合成证明.pdf', role: 'ATTACHMENT_ONLY', attachmentKind: kind,
    sortOrder: 0, status: 'ACTIVE', mediaType: 'application/pdf', sizeBytes: 100,
    ocrStatus: 'NOT_REQUESTED', ocrResult: null }
}
const fields = { payment_proof: 'paymentProofFileIds', hotel_bill: 'hotelBillFileIds', waybill: 'waybillFileIds' } as const

describe('expense material selection transactions', () => {
  let scope: EffectScope
  let draft: Ref<Pick<ReimbursementDraft, 'id' | 'status'> | null>
  let editable: Ref<boolean>
  let blocked: Ref<boolean>
  let items: Ref<ExpenseItem[]>
  let files: Ref<ReimbursementDraftFile[]>
  let editor: { state: { id: string; transportType: string; itineraryFileIds: string[]; itineraryAutoMatchDisabled: boolean };
    visible: Ref<boolean>; pickerOnly: Ref<boolean>; revision: Ref<number> }
  let picker: ReturnType<typeof useExpenseMaterialPicker>

  beforeEach(() => {
    scope = effectScope()
    draft = ref({ id: 'synthetic-draft', status: 'DRAFT' })
    editable = ref(true); blocked = ref(false)
    items = ref([item(), item({ id: 'synthetic-shared-expense' })])
    files = ref([file('synthetic-payment', 'payment_proof'), file('synthetic-hotel', 'hotel_bill'),
      file('synthetic-waybill', 'waybill'), file('synthetic-itinerary-1', 'itinerary'), file('synthetic-itinerary-2', 'itinerary')])
    editor = { state: reactive({ id: items.value[0]!.id, transportType: 'ride_hailing', itineraryFileIds: [], itineraryAutoMatchDisabled: false }),
      visible: ref(false), pickerOnly: ref(false), revision: ref(1) }
    picker = scope.run(() => useExpenseMaterialPicker({
      draft: () => draft.value, editable: () => editable.value, blocked: () => blocked.value,
      items: () => items.value, files: () => files.value, editor,
    }))!
  })
  afterEach(() => scope.stop())

  describe.each(['payment_proof', 'hotel_bill', 'waybill'] as ProofKind[])('%s', kind => {
    const selectedId = `synthetic-${kind === 'payment_proof' ? 'payment' : kind === 'hotel_bill' ? 'hotel' : 'waybill'}`
    it('keeps selections local until confirmation and preserves shared files and other fields', () => {
      items.value[0]![fields[kind]] = [selectedId]
      items.value[1]![fields[kind]] = [selectedId]
      const originalFiles = [...files.value]
      picker.proof.open(items.value[0]!, kind)
      picker.proof.selection.value = []
      expect(items.value[0]![fields[kind]]).toEqual([selectedId])
      picker.proof.confirm()
      expect(items.value[0]![fields[kind]]).toEqual([])
      expect(items.value[1]![fields[kind]]).toEqual([selectedId])
      expect(items.value[0]!.amount).toBe('86.50')
      expect(files.value).toEqual(originalFiles)
      expect(picker.proof.visible.value).toBe(false)
    })
    it('cancels without changing associations, then opens the current saved selection', () => {
      picker.proof.open(items.value[0]!, kind)
      picker.proof.selection.value = [selectedId]
      picker.proof.visible.value = false
      picker.proof.confirm()
      expect(items.value[0]![fields[kind]]).toBeUndefined()
      picker.proof.open(items.value[0]!, kind)
      expect(picker.proof.selection.value).toEqual([])
      expect(picker.proof.changed.value).toBe(false)
    })
    it('requires reopening if that association changed while the picker was open', () => {
      picker.proof.open(items.value[0]!, kind)
      picker.proof.selection.value = [selectedId]
      items.value[0]![fields[kind]] = ['synthetic-concurrent-relation']
      picker.proof.confirm()
      expect(picker.proof.error.value).toContain('材料关联已变化')
      expect(items.value[0]![fields[kind]]).toEqual(['synthetic-concurrent-relation'])
      expect(picker.proof.visible.value).toBe(true)
    })
    it.each(['deleted', 'purpose', 'status'] as const)('rejects selected materials after %s changes', reason => {
      picker.proof.open(items.value[0]!, kind)
      picker.proof.selection.value = [selectedId]
      const selected = files.value.find(candidate => candidate.id === selectedId)!
      if (reason === 'deleted') files.value = files.value.filter(candidate => candidate !== selected)
      if (reason === 'purpose') selected.attachmentKind = 'other'
      if (reason === 'status') selected.status = 'PURGED'
      picker.proof.confirm()
      expect(picker.proof.error.value).toContain('所选材料已删除或用途已变化')
      expect(items.value[0]![fields[kind]]).toBeUndefined()
    })
    it('binds native file selection to the open picker and rejects a late callback after reopening', () => {
      picker.proof.open(items.value[0]!, kind)
      expect(picker.proof.armUpload()).toBe(true)
      const target = picker.proof.consumeUpload(kind)!
      expect(target.itemId).toBe(items.value[0]!.id)
      expect(picker.proof.acceptsUploadTarget(target)).toBe(true)
      expect(picker.proof.isCurrentTarget(target)).toBe(true)
      picker.proof.armUpload()
      picker.proof.close()
      picker.proof.open(items.value[0]!, kind)
      expect(picker.proof.consumeUpload(kind)).toBeNull()
      expect(picker.proof.isCurrentTarget(target)).toBe(false)
      // A failed file from the former picker can still be retried as a material,
      // without changing either the new selection or any expense association.
      expect(picker.proof.acceptsUploadTarget(target)).toBe(true)
      expect(picker.proof.selection.value).toEqual([])
      expect(items.value[0]![fields[kind]]).toBeUndefined()
    })
    it('does not transfer an upload target to another expense or reimbursement', () => {
      picker.proof.open(items.value[0]!, kind)
      picker.proof.armUpload()
      const target = picker.proof.consumeUpload(kind)!
      items.value.shift()
      expect(picker.proof.acceptsUploadTarget(target)).toBe(false)
      draft.value = { id: 'synthetic-other-draft', status: 'DRAFT' }
      expect(picker.proof.visible.value).toBe(false)
      expect(picker.proof.acceptsUploadTarget(target)).toBe(false)
    })
  })

  it.each(['blocked', 'readonly', 'dispose'] as const)('does not open or confirm a proof when %s', reason => {
    picker.proof.open(items.value[0]!)
    picker.proof.selection.value = ['synthetic-payment']
    if (reason === 'blocked') blocked.value = true
    if (reason === 'readonly') editable.value = false
    if (reason === 'dispose') scope.stop()
    picker.proof.confirm()
    expect(items.value[0]!.paymentProofFileIds).toBeUndefined()
    picker.proof.open(items.value[0]!)
    expect(picker.proof.armUpload()).toBe(false)
  })

  function openItinerary(ids: string[] = []) {
    items.value[0]!.itineraryFileIds = [...ids]
    editor.state.itineraryFileIds = [...ids]
    editor.visible.value = true; editor.pickerOnly.value = true
    picker.itinerary.reset()
  }
  it('requires choosing one previously selected file before changing multiple to single', () => {
    openItinerary(['synthetic-itinerary-1', 'synthetic-itinerary-2'])
    picker.itinerary.query.value = '合成路线'
    picker.itinerary.toggleMode()
    expect(picker.itinerary.mode.value).toBe('keep-one')
    expect(picker.itinerary.query.value).toBe('')
    picker.itinerary.confirm()
    expect(editor.visible.value).toBe(true)
    picker.itinerary.choose('synthetic-foreign')
    expect(picker.itinerary.mode.value).toBe('keep-one')
    picker.itinerary.choose('synthetic-itinerary-2')
    expect(picker.itinerary.mode.value).toBe('single')
    expect(items.value[0]!.itineraryFileIds).toEqual(['synthetic-itinerary-1', 'synthetic-itinerary-2'])
    picker.itinerary.confirm()
    expect(items.value[0]!.itineraryFileIds).toEqual(['synthetic-itinerary-2'])
    expect(items.value[0]!.itineraryAutoMatchDisabled).toBe(true)
    expect(editor.visible.value).toBe(false)
  })
  it('cancels the keep-one transition without discarding any selected files', () => {
    openItinerary(['synthetic-itinerary-1', 'synthetic-itinerary-2'])
    picker.itinerary.toggleMode(); picker.itinerary.toggleMode()
    expect(picker.itinerary.mode.value).toBe('multiple')
    expect(editor.state.itineraryFileIds).toHaveLength(2)
  })
  it('allows an explicit empty selection and an explicit retry of automatic matching', () => {
    openItinerary(['synthetic-itinerary-1'])
    picker.itinerary.choose(null)
    picker.itinerary.confirm()
    expect(items.value[0]!.itineraryFileIds).toEqual([])
    expect(items.value[0]!.itineraryAutoMatchDisabled).toBe(true)
    openItinerary()
    picker.itinerary.confirm(true)
    expect(items.value[0]!.itineraryAutoMatchDisabled).toBe(false)
  })
  it('adopts new automatic suggestions until an employee edits the selection', async () => {
    openItinerary()
    items.value[0]!.itineraryFileIds = ['synthetic-itinerary-1']
    await nextTick()
    expect(editor.state.itineraryFileIds).toEqual(['synthetic-itinerary-1'])
    picker.itinerary.choose('synthetic-itinerary-2')
    items.value[0]!.itineraryFileIds = ['synthetic-itinerary-1', 'synthetic-itinerary-2']
    await nextTick()
    expect(editor.state.itineraryFileIds).toEqual(['synthetic-itinerary-2'])
  })
  it('blocks confirmation if an itinerary was removed or the expense category changed', () => {
    openItinerary()
    picker.itinerary.choose('synthetic-itinerary-1')
    files.value = files.value.filter(candidate => candidate.id !== 'synthetic-itinerary-1')
    expect(picker.itinerary.confirm()).toContain('所选行程单已删除或用途已变化')
    expect(editor.visible.value).toBe(true)
    expect(items.value[0]!.itineraryFileIds).toEqual([])
    items.value[0]!.category = 'office'
    expect(picker.itinerary.confirm()).toBeNull()
    expect(items.value[0]!.itineraryFileIds).toEqual([])
  })
  it('isolates native itinerary upload callbacks and late errors by editor revision', () => {
    openItinerary()
    picker.itinerary.armUpload()
    const first = picker.itinerary.consumeUpload()!
    expect(first.fromPicker).toBe(true)
    editor.visible.value = false
    editor.revision.value += 1
    openItinerary()
    picker.itinerary.finishUpload(first.revision, '旧弹窗上传失败')
    expect(picker.itinerary.uploadError.value).toBe('')
    picker.itinerary.armUpload()
    editor.revision.value += 1
    expect(picker.itinerary.consumeUpload()).toBeNull()
  })
  it('closes a dedicated itinerary picker when the draft locks without changing its expense', () => {
    openItinerary()
    picker.itinerary.choose('synthetic-itinerary-1')
    draft.value!.status = 'LOCKED'
    expect(editor.visible.value).toBe(false)
    expect(items.value[0]!.itineraryFileIds).toEqual([])
  })
})

import ElementPlus from 'element-plus'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DefineComponent } from 'vue'

import { getSavedGeneralApprovals, listGeneralApprovals } from '@/api/reimbursements'
import type { GeneralApproval, GeneralApprovalSearchResult } from '@/types/reimbursements'
import GeneralApprovalPicker from './GeneralApprovalPicker.vue'

vi.mock('@/api/reimbursements', () => ({
  listGeneralApprovals: vi.fn(),
  getSavedGeneralApprovals: vi.fn(),
}))

const first: GeneralApproval = {
  processInstanceId: 'synthetic-first', title: '合成采购审批', businessId: 'SYNTHETIC-FIRST',
  status: 'COMPLETED', result: 'agree', createdAt: '2026-11-10T01:00:00Z',
}
const last: GeneralApproval = { ...first, processInstanceId: 'synthetic-last', title: '合成后续审批',
  businessId: 'SYNTHETIC-LAST', createdAt: '2026-11-11T01:00:00Z' }

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

const mounted: VueWrapper[] = []
function mountPicker(mobile: boolean) {
  // Keep props typed for the native TypeScript pass, which uses the generic Vue shim.
  const component = GeneralApprovalPicker as DefineComponent<{
    modelValue: string[]; draftId: string; savedApprovals: GeneralApproval[]; mobile: boolean
    active: boolean; readonly: boolean; busy: boolean; saveError: string; expanded: boolean
  }>
  const wrapper = mount(component, {
    props: { modelValue: [], draftId: 'synthetic-draft', savedApprovals: [], mobile,
      active: true, readonly: false, busy: false, saveError: '', expanded: false },
    global: { plugins: [ElementPlus] },
  })
  mounted.push(wrapper)
  return wrapper
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [], complete: true })
  vi.mocked(getSavedGeneralApprovals).mockResolvedValue([])
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})
afterEach(() => { for (const wrapper of mounted.splice(0)) wrapper.unmount() })

describe.each([false, true])('automatic general approval search, mobile=%s', (mobile) => {
  it('shows bounded plain descriptions and approved status beside the title in both modes', async () => {
    const entry = { ...first, summaryFields: [
      { label: '所属公司', value: '合成公司' },
      { label: '预算项目', value: 'SYNTHETIC-42' },
      { label: '申请事项', value: '<b>合成内容</b>' },
      { label: '额外', value: '不显示第四条' },
    ] }
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [entry], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const row = wrapper.get('.travel-approval-row')
    expect(row.get('.travel-approval-title').text()).toContain('已通过')
    expect(row.get('[data-testid="approval-summary-fields"]').findAll('small')).toHaveLength(3)
    expect(row.text()).toContain('预算项目：SYNTHETIC-42')
    expect(row.text()).toContain('<b>合成内容</b>')
    expect(row.find('b').exists()).toBe(false)
    expect(row.text()).not.toContain('不显示第四条')
    expect(row.text()).toContain('发起日期：2026-11-10')
    expect(wrapper.get('input[aria-label="搜索本人审批"]').attributes('placeholder')).toContain('说明')
  })

  it('restores saved descriptions outside the current query and keeps legacy entries selectable', async () => {
    const saved = { ...first, summaryFields: [{ label: '项目', value: '合成已存项目' }] }
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [first.processInstanceId], savedApprovals: [saved] })
    await flushPromises()
    expect(wrapper.text()).toContain('项目：合成已存项目')
    expect(wrapper.text()).toContain('已选 1 张')
    const legacyRow = wrapper.findAll('.travel-approval-row').find(row => row.text().includes(last.title))!
    expect(legacyRow.find('[data-testid="approval-summary-fields"]').exists()).toBe(false)
    expect(legacyRow.get('input[type="checkbox"]').attributes('disabled')).toBeUndefined()
  })

  it('starts on expansion and reaches later matches without a search or continuation click', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [], complete: false, nextCursor: 'synthetic-page-2' })
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    expect(listGeneralApprovals).not.toHaveBeenCalled()
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ cursor: 'synthetic-page-2' }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).toContain('当前范围已查完')
    expect(wrapper.text()).not.toContain('继续查找')
  })

  it('shows progress and deduplicates all pages while retaining a saved selection', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [first.processInstanceId], savedApprovals: [first] })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('正在查询')
    expect(wrapper.text()).not.toContain('当前范围已查完')
    expect(wrapper.text()).toContain('已选 1 张')
    pending.resolve({ items: [last, first], complete: true })
    await flushPromises()
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('已选 1 张')
    expect(wrapper.text()).toContain('当前范围已查完')
  })

  it('keeps successful results on failure and retries from the failed cursor', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockRejectedValueOnce(new Error('合成查询连接中断'))
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(wrapper.text()).toContain('合成查询连接中断')
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).not.toContain('当前范围已查完')
    await wrapper.findAll('button').find(button => button.text() === '重新加载')!.trigger('click')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(3)
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ cursor: 'synthetic-page-2' }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).toContain('当前范围已查完')
  })

  it('pauses on collapse, resumes on expansion, and ignores the cancelled response', async () => {
    const old = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    const signal = vi.mocked(listGeneralApprovals).mock.calls[1]![1]
    await wrapper.setProps({ expanded: false })
    expect(signal.aborted).toBe(true)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(3)
    old.resolve({ items: [{ ...last, title: '过时的合成结果' }], complete: false, nextCursor: 'stale-next' })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).not.toContain('过时的合成结果')
  })

  it('stops on filter edits and uses only the new query after applying them', async () => {
    const old = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    const signal = vi.mocked(listGeneralApprovals).mock.calls[1]![1]
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('后续审批')
    expect(signal.aborted).toBe(true)
    expect(wrapper.text()).toContain('筛选条件已修改')
    await wrapper.get('.travel-query-button').trigger('click')
    await flushPromises()
    old.resolve({ items: [{ ...first, title: '旧筛选条件的结果' }], complete: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ query: '后续审批' }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).not.toContain('旧筛选条件的结果')
    expect(wrapper.text()).not.toContain(first.title)
  })

  it('stops repeated cursors instead of making an endless request loop', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockResolvedValue({ items: [last], complete: false, nextCursor: 'synthetic-page-2' })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('查询进度异常')
    expect(wrapper.text()).not.toContain('当前范围已查完')
  })

  it('paginates the completed combined results without further API requests', async () => {
    const approvals = Array.from({ length: 25 }, (_, index) => ({ ...first,
      processInstanceId: `synthetic-${String(index).padStart(2, '0')}`, title: `合成审批 ${index}` }))
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: approvals.slice(0, 10), complete: false, nextCursor: 'synthetic-page-2' })
      .mockResolvedValueOnce({ items: approvals.slice(10, 20), complete: false, nextCursor: 'synthetic-page-3' })
      .mockResolvedValueOnce({ items: approvals.slice(20), complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [approvals[0]!.processInstanceId], savedApprovals: [approvals[0]!] })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).toContain('共 25 张已通过审批')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(10)
    await wrapper.get('.el-pagination .btn-next').trigger('click')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(10)
    expect(wrapper.text()).toContain('已选 1 张')
    expect(listGeneralApprovals).toHaveBeenCalledTimes(3)
  })

  it('retains earlier incomplete reasons until the entire range has been retried', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false,
      nextCursor: 'synthetic-page-2', incompleteReasons: ['detail'] })
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('部分审批详情未能读取')
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).not.toContain('当前范围已查完')
  })

  it('does not duplicate a running search when a selection is being saved', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'synthetic-page-2' })
      .mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, busy: true })
    expect(listGeneralApprovals).not.toHaveBeenCalled()
    await wrapper.setProps({ busy: false })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    const signal = vi.mocked(listGeneralApprovals).mock.calls[1]![1]
    await wrapper.setProps({ busy: true, modelValue: [first.processInstanceId], savedApprovals: [first] })
    await wrapper.setProps({ busy: false })
    expect(signal.aborted).toBe(false)
    pending.resolve({ items: [last], complete: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('已选 1 张')
    await wrapper.setProps({ expanded: false })
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
  })

  it('starts a new search for a different draft without adopting the old response', async () => {
    const old = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ items: [last], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    const signal = vi.mocked(listGeneralApprovals).mock.calls[0]![1]
    await wrapper.setProps({ draftId: 'synthetic-other-draft' })
    await flushPromises()
    expect(signal.aborted).toBe(true)
    old.resolve({ items: [first], complete: false, nextCursor: 'should-not-run' })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).not.toContain(first.title)
  })

  it.each(['readonly', 'active'] as const)('cancels when %s prevents further search', async (field) => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    const signal = vi.mocked(listGeneralApprovals).mock.calls[0]![1]
    await wrapper.setProps(field === 'readonly' ? { readonly: true } : { active: false })
    expect(signal.aborted).toBe(true)
    pending.resolve({ items: [first], complete: false, nextCursor: 'should-not-run' })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
    expect(wrapper.text()).not.toContain(first.title)
  })

  it('cancels an unfinished search when the component leaves the draft', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    const signal = vi.mocked(listGeneralApprovals).mock.calls[0]![1]
    wrapper.unmount()
    expect(signal.aborted).toBe(true)
    pending.resolve({ items: [first], complete: false, nextCursor: 'should-not-run' })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
  })
})

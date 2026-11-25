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
  processCode: 'synthetic-purchase',
  status: 'COMPLETED', result: 'agree', createdAt: '2026-11-10T01:00:00Z',
}
const last: GeneralApproval = { ...first, processInstanceId: 'synthetic-last', title: '合成后续审批',
  processCode: 'synthetic-travel',
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
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date('2026-11-25T08:00:00Z'))
  vi.resetAllMocks()
  vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [], complete: true })
  vi.mocked(getSavedGeneralApprovals).mockResolvedValue([])
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
})
afterEach(() => {
  for (const wrapper of mounted.splice(0)) wrapper.unmount()
  vi.useRealTimers()
})

describe.each([false, true])('automatic general approval search, mobile=%s', (mobile) => {
  it('instantly filters titles, numbers and descriptions without another query or changing types', async () => {
    const described = { ...first, summaryFields: [{ label: '预算项目', value: 'SYNTHETIC-42' }] }
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [described, last], complete: true,
      templates: [{ processCode: first.processCode!, name: '采购申请' },
        { processCode: last.processCode!, name: '境内出差申请' }] })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const input = wrapper.get('input[aria-label="搜索本人审批"]')
    for (const query of ['采购', 'synthetic-first', '  synthetic-42  ', '预算项目']) {
      await input.setValue(query)
      expect(wrapper.findAll('.travel-approval-row').map(row => row.get('strong').text())).toEqual([first.title])
      expect(wrapper.get('[data-testid="general-approval-query-complete"]').text())
        .toContain('共查到 2 张审批，当前筛选 1 张')
      await input.trigger('keyup', { key: 'Enter' })
      expect(listGeneralApprovals).toHaveBeenCalledOnce()
    }
    await input.setValue('没有匹配的合成条件')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(0)
    expect(wrapper.get('[data-testid="general-approval-query-complete"]').text()).toContain('当前筛选 0 张')
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    await input.setValue('')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(2)
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
  })

  it('keeps streaming while the keyword changes and filters newly arriving matches immediately', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false, nextCursor: 'keyword-page' })
      .mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const signal = vi.mocked(listGeneralApprovals).mock.calls[1]![1]
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('后续')
    expect(signal.aborted).toBe(false)
    expect(wrapper.text()).not.toContain('筛选条件已修改')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(0)
    expect(wrapper.get('[data-testid="general-approval-query-progress"]').text()).toContain('当前筛选 0 张')
    pending.resolve({ items: [last], complete: true })
    await flushPromises()
    expect(wrapper.findAll('.travel-approval-row').map(row => row.get('strong').text())).toEqual([last.title])
    expect(wrapper.get('[data-testid="general-approval-query-complete"]').text()).toContain('当前筛选 1 张')
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ cursor: 'keyword-page' }, expect.any(AbortSignal))
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
  })

  it('allows paging during streaming without moving the page and reorders when returning home', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    const entries = Array.from({ length: 20 }, (_, index) => ({ ...first,
      processInstanceId: `synthetic-browse-${String(index).padStart(2, '0')}`, title: `原合成审批 ${index}` }))
    const incoming = Array.from({ length: 5 }, (_, index) => ({ ...last,
      processInstanceId: `synthetic-incoming-${index}`, title: `新合成审批 ${index}` }))
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: entries, complete: false, nextCursor: 'browse-page' })
      .mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(wrapper.get<HTMLButtonElement>('.el-pagination .btn-next').element.disabled).toBe(false)
    await wrapper.get('.el-pagination .btn-next').trigger('click')
    const pageTwo = wrapper.findAll('.travel-approval-row').map(row => row.get('strong').text())
    expect(pageTwo).toHaveLength(10)
    pending.resolve({ items: incoming, complete: true })
    await flushPromises()
    expect(wrapper.findAll('.travel-approval-row').map(row => row.get('strong').text())).toEqual(pageTwo)
    await wrapper.get('.el-pagination .btn-next').trigger('click')
    expect(wrapper.findAll('.travel-approval-row').map(row => row.get('strong').text()).sort())
      .toEqual(incoming.map(row => row.title).sort())
    await wrapper.get('.el-pagination .btn-prev').trigger('click')
    await wrapper.get('.el-pagination .btn-prev').trigger('click')
    expect(wrapper.findAll('.travel-approval-row').slice(0, 5).every(row => row.text().includes('新合成审批'))).toBe(true)
    await wrapper.get('.el-pagination .btn-next').trigger('click')
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('新合成审批')
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(5)
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
  })

  it('labels selected approvals outside the filter without adding them to the match count', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [first, last], complete: true,
      templates: [{ processCode: first.processCode!, name: '采购申请' },
        { processCode: last.processCode!, name: '境内出差申请' }] })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [first.processInstanceId], savedApprovals: [first] })
    await flushPromises()
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', last.processCode)
    await flushPromises()
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('已关联 · 不符合当前筛选')
    expect(wrapper.get('[data-testid="general-approval-query-complete"]').text()).toContain('当前筛选 1 张')
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('没有匹配')
    const row = wrapper.get('.travel-approval-row')
    expect(row.get('strong').text()).toBe(first.title)
    expect(wrapper.get('[data-testid="general-approval-query-complete"]').text()).toContain('当前筛选 0 张')
    await row.get('input[type="checkbox"]').setValue(false)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[]])
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
  })

  it('keeps the category in the filter toolbar with only one selector in either mode', async () => {
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElSelect' })).toHaveLength(1)
    if (mobile) {
      const heading = wrapper.get('.approval-filter-heading')
      expect(heading.get('[aria-label="审批类别"]').isVisible()).toBe(true)
      expect(wrapper.get('fieldset').isVisible()).toBe(false)
      await heading.get('button[aria-label="筛选审批"]').trigger('click')
      expect(heading.get('button[aria-label="筛选审批"]').attributes('aria-expanded')).toBe('true')
      expect(wrapper.get('fieldset').attributes('style')).not.toContain('display: none')
      expect(wrapper.findAllComponents({ name: 'ElSelect' })).toHaveLength(1)
      expect(wrapper.find('fieldset [aria-label="审批类别"]').exists()).toBe(false)
    } else {
      const toolbar = wrapper.get('.travel-query-grid')
      expect(toolbar.classes()).toContain('travel-query-grid--category')
      expect(toolbar.get('.approval-query-category [aria-label="审批类别"]').isVisible()).toBe(true)
      expect(toolbar.get('input[aria-label="审批查询开始日期"]').isVisible()).toBe(true)
      expect(toolbar.get('input[aria-label="搜索本人审批"]').isVisible()).toBe(true)
    }
    await wrapper.setProps({ readonly: true })
    expect(wrapper.find('[aria-label="审批类别"]').exists()).toBe(false)
    expect(wrapper.get('.travel-query-grid').classes()).not.toContain('travel-query-grid--category')
  })

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

  it.each([
    { status: 'RUNNING', result: null, label: '审批中' },
    { status: 'TERMINATED', result: 'agree', label: '已终止' },
    { status: 'COMPLETED', result: 'refuse', label: '已拒绝' },
    { status: 'COMPLETED', result: null, label: '已完成' },
  ])('displays and allows associating an employee approval in $status / $result', async ({ status, result, label }) => {
    const entry = { ...first, status, result }
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [entry], complete: true,
      templates: [{ processCode: first.processCode!, name: '采购申请' }] })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const row = wrapper.get('.travel-approval-row')
    expect(row.get('.approval-row-status').text()).toBe(label)
    expect(wrapper.text()).not.toContain('请取消关联后再办理')
    expect(wrapper.text()).not.toContain('仅查询本人发起且已通过')
    await row.get('input[type="checkbox"]').setValue(true)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[entry.processInstanceId]])
  })

  it('replaces available types on a new date query and drops a selection with no records', async () => {
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: true,
      templates: [{ processCode: first.processCode!, name: '采购申请' }] })
      .mockResolvedValueOnce({ items: [last], complete: true,
        templates: [{ processCode: last.processCode!, name: '境内出差申请' }] })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', first.processCode)
    await flushPromises()
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-11-01')
    dates[1]!.vm.$emit('update:modelValue', '2026-11-15')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '查询审批')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '境内出差申请'])
    expect(wrapper.getComponent({ name: 'ElSelect' }).props('modelValue')).toBe('')
    expect(wrapper.text()).toContain(last.title)
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
  })

  it('retains types, keyword and category through a failed refresh and cursor retry', async () => {
    const purchase = { processCode: first.processCode!, name: '采购申请' }
    const travel = { processCode: last.processCode!, name: '境内出差申请' }
    vi.mocked(listGeneralApprovals)
      .mockResolvedValueOnce({ items: [first, last], templates: [purchase, travel], complete: true })
      .mockResolvedValueOnce({ items: [last], templates: [travel], complete: false, nextCursor: 'retry-types' })
      .mockRejectedValueOnce(new Error('合成续查失败'))
      .mockResolvedValueOnce({ items: [first], templates: [purchase], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const select = wrapper.getComponent({ name: 'ElSelect' })
    select.vm.$emit('update:modelValue', first.processCode)
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('合成')
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
    await wrapper.findAll('button').find(button => button.text() === '刷新审批')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('合成续查失败')
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    expect(select.props('modelValue')).toBe(first.processCode)
    select.vm.$emit('update:modelValue', last.processCode)
    await flushPromises()
    expect(wrapper.text()).toContain(last.title)
    await wrapper.get('input[aria-label="搜索本人审批"]').setValue('后续')
    await wrapper.findAll('button').find(button => button.text() === '重新加载')!.trigger('click')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ cursor: 'retry-types' }, expect.any(AbortSignal))
    expect(wrapper.get<HTMLInputElement>('input[aria-label="搜索本人审批"]').element.value).toBe('后续')
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '境内出差申请', '采购申请'])
    expect(wrapper.text()).not.toContain('合成续查失败')
  })

  it('keeps old types during a pending or incomplete date query and replaces them only after a complete retry', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    const purchase = { processCode: first.processCode!, name: '采购申请' }
    const travel = { processCode: last.processCode!, name: '境内出差申请' }
    vi.mocked(listGeneralApprovals)
      .mockResolvedValueOnce({ items: [first, last], templates: [purchase, travel], complete: true })
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ items: [last], templates: [travel], complete: true })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [first.processInstanceId], savedApprovals: [first] })
    await flushPromises()
    const select = wrapper.getComponent({ name: 'ElSelect' })
    select.vm.$emit('update:modelValue', first.processCode)
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-11-01')
    dates[1]!.vm.$emit('update:modelValue', '2026-11-15')
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === '查询审批')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    pending.resolve({ items: [last], templates: [travel], complete: false, incompleteReasons: ['detail'] })
    await flushPromises()
    expect(select.props('modelValue')).toBe(first.processCode)
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    await wrapper.findAll('button').find(button => button.text() === '重新加载')!.trigger('click')
    await flushPromises()
    expect(select.props('modelValue')).toBe('')
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '境内出差申请'])
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).toContain('已选 1 张')
    expect(wrapper.text()).toContain(last.title)
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

  it('stops on date edits, keeps the local keyword and ignores the previous range response', async () => {
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
    expect(signal.aborted).toBe(false)
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-11-01')
    dates[1]!.vm.$emit('update:modelValue', '2026-11-25')
    await flushPromises()
    expect(signal.aborted).toBe(true)
    expect(wrapper.text()).toContain('日期范围已修改')
    await wrapper.get('.travel-query-button').trigger('click')
    await flushPromises()
    old.resolve({ items: [{ ...first, title: '旧筛选条件的结果' }], complete: true })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({ from: '2026-11-01', to: '2026-11-25' }, expect.any(AbortSignal))
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
    expect(wrapper.text()).toContain('共查到 25 张审批，当前筛选 25 张')
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
  it('filters actual employee templates locally while retaining other selected approvals', async () => {
    const templates = [{ processCode: 'synthetic-purchase', name: '采购申请' },
      { processCode: 'synthetic-travel', name: '境内出差申请' }]
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first, last], complete: true, templates })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [first.processInstanceId], savedApprovals: [first] })
    await flushPromises()
    const select = wrapper.getComponent({ name: 'ElSelect' })
    expect(select.isVisible()).toBe(true)
    expect(select.props('placeholder')).toBe('全部审批单')
    expect(select.props('disabled')).toBe(false)
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    select.vm.$emit('update:modelValue', 'synthetic-travel')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).toContain(last.title)
    expect(wrapper.text()).toContain('已选 1 张')
    const selected = wrapper.findAll('.travel-approval-row').find(row => row.text().includes(first.title))!
    await selected.get('input[type="checkbox"]').setValue(false)
    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual([[]])
    select.vm.$emit('update:modelValue', '')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledOnce()
  })

  it('keeps automatic pagination running and accumulates employee types while filtering locally', async () => {
    const pending = deferred<GeneralApprovalSearchResult>()
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first], complete: false,
      nextCursor: 'old-page', templates: [{ processCode: 'synthetic-purchase', name: '采购申请' }] })
      .mockReturnValueOnce(pending.promise)
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    const oldSignal = vi.mocked(listGeneralApprovals).mock.calls[1]![1]
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'synthetic-purchase')
    await flushPromises()
    expect(oldSignal.aborted).toBe(false)
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    pending.resolve({ items: [last], complete: true,
      templates: [{ processCode: 'synthetic-travel', name: '境内出差申请' }] })
    await flushPromises()
    expect(wrapper.text()).toContain(first.title)
    expect(wrapper.text()).not.toContain(last.title)
    expect(wrapper.findAllComponents({ name: 'ElOption' }).map(option => option.props('label')))
      .toEqual(['全部审批单', '采购申请', '境内出差申请'])
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'synthetic-travel')
    await flushPromises()
    expect(wrapper.text()).not.toContain(first.title)
    expect(wrapper.text()).toContain(last.title)
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    await wrapper.setProps({ busy: true })
    expect(wrapper.getComponent({ name: 'ElSelect' }).props('disabled')).toBe(true)
    await wrapper.setProps({ readonly: true })
    expect(wrapper.findComponent({ name: 'ElSelect' }).exists()).toBe(false)
  })

  it('resets the category once when another draft opens without replaying its query', async () => {
    const templates = [{ processCode: 'synthetic-purchase', name: '采购申请' }]
    vi.mocked(listGeneralApprovals).mockResolvedValue({ items: [first], complete: true, templates })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true })
    await flushPromises()
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'synthetic-purchase')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(1)
    await wrapper.setProps({ draftId: 'another-synthetic-draft' })
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(2)
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({}, expect.any(AbortSignal))
    expect(wrapper.getComponent({ name: 'ElSelect' }).props('modelValue')).toBe('')
  })

  it('clears previous candidates on category changes with invalid dates while keeping saved associations', async () => {
    const templates = [{ processCode: 'synthetic-travel', name: '境内出差申请' }]
    const selected = { ...last, title: '已关联合成审批' }
    const match = { ...first, processCode: 'synthetic-travel', processInstanceId: 'new-travel-match', title: '合成出差审批' }
    vi.mocked(listGeneralApprovals).mockResolvedValueOnce({ items: [first, selected], complete: true, templates })
      .mockResolvedValueOnce({ items: [match], complete: true, templates })
    const wrapper = mountPicker(mobile)
    await wrapper.setProps({ expanded: true, modelValue: [selected.processInstanceId], savedApprovals: [selected] })
    await flushPromises()
    wrapper.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-11-01')
    await flushPromises()
    wrapper.getComponent({ name: 'ElSelect' }).vm.$emit('update:modelValue', 'synthetic-travel')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).not.toContain(first.title)
    expect(wrapper.findAll('.travel-approval-row')).toHaveLength(1)
    expect(wrapper.get('.travel-approval-row').text()).toContain(selected.title)
    expect(wrapper.get('.travel-approval-row').text()).toContain('已关联')
    expect(wrapper.text()).toContain('自定义日期范围需要同时填写开始和结束日期')
    expect(wrapper.find('[data-testid="general-approval-query-complete"]').exists()).toBe(false)
    wrapper.findAllComponents({ name: 'ElDatePicker' })[1]!.vm.$emit('update:modelValue', '2026-11-10')
    await wrapper.findAll('button').find(button => button.text() === '查询审批')!.trigger('click')
    await flushPromises()
    expect(listGeneralApprovals).toHaveBeenLastCalledWith({
      from: '2026-11-01', to: '2026-11-10',
    }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain(match.title)
    expect(wrapper.text()).toContain(selected.title)
  })

})

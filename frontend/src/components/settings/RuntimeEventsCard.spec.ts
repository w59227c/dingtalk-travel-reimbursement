import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listRuntimeEvents } from '@/api/runtimeEvents'
import { useAuthStore } from '@/stores/auth'
import type { RuntimeEventPage } from '@/types/runtimeEvents'
import RuntimeEventsCard from './RuntimeEventsCard.vue'

vi.mock('@/api/runtimeEvents', () => ({ listRuntimeEvents: vi.fn() }))
const response: RuntimeEventPage = {
  items: [{ id: 12, occurredAt: '2026-10-27T01:00:00Z', module: 'approvals', event: 'approval_query', result: 'empty', requestId: 'synthetic-request', details: { stage: 'complete', listed: 3, detailsRead: 3, excluded: 3, returned: 0, unselectable: 0, exclusions: [{ code: 'DATE_FIELDS_INVALID', count: 3 }] } }],
  nextCursor: null,
  coverage: { oldestAt: '2026-10-27T01:00:00Z', newestAt: '2026-10-27T01:00:00Z', records: 1, bytes: 700 },
  limits: { retentionDays: 7, maxRecords: 10000, maxBytes: 16777216 },
  collector: { startedAt: '2026-10-27T00:00:00Z', pending: 0, droppedSinceStart: 0, writeError: false },
}

describe.each([false, true])('RuntimeEventsCard mobile=%s', (mobile) => {
  let pinia: ReturnType<typeof createPinia>
  beforeEach(() => {
    vi.clearAllMocks()
    pinia = createPinia(); setActivePinia(pinia)
    vi.mocked(listRuntimeEvents).mockResolvedValue(response)
  })
  function render() { return mount(RuntimeEventsCard, { props: { mobile }, global: { plugins: [pinia, ElementPlus] } }) }
  it('loads retained evidence and displays real empty-result causes and retention limits', async () => {
    const wrapper = render(); await flushPromises()
    expect(listRuntimeEvents).toHaveBeenCalledWith({}, expect.any(AbortSignal))
    expect(wrapper.get('.runtime-filters').classes().includes('runtime-filters--mobile')).toBe(mobile)
    expect(wrapper.text()).toContain('员工审批查询过程')
    expect(wrapper.text()).toContain('日期无法解析 3 条')
    expect(wrapper.text()).toContain('10000 条')
    expect(wrapper.text()).toContain('16 MiB')
    expect(wrapper.text()).toContain('不代表期间记录完整')
    wrapper.unmount()
  })
  it('validates paired dates then sends module, result and exact request filters', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.get('[aria-label="运行记录开始日期"]').setValue('2026-10-25')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('请同时填写起止日期')
    expect(listRuntimeEvents).toHaveBeenCalledTimes(1)
    await wrapper.get('[aria-label="运行记录结束日期"]').setValue('2026-10-27')
    await wrapper.get('[aria-label="运行记录功能模块"]').setValue('approvals')
    await wrapper.get('[aria-label="运行记录结果"]').setValue('empty')
    await wrapper.get('[aria-label="运行记录排查编号"]').setValue(' synthetic-request ')
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(listRuntimeEvents).toHaveBeenLastCalledWith({ from: '2026-10-25', to: '2026-10-27', module: 'approvals', result: 'empty', requestId: 'synthetic-request' }, expect.any(AbortSignal))
    wrapper.unmount()
  })
  it('uses a stable cursor and preserves prior evidence when a later page fails', async () => {
    vi.mocked(listRuntimeEvents).mockResolvedValueOnce({ ...response, nextCursor: 12 })
    const wrapper = render(); await flushPromises()
    vi.mocked(listRuntimeEvents).mockRejectedValueOnce(new Error('合成读取失败'))
    await wrapper.findAll('button').find((button) => button.text() === '加载更早记录')!.trigger('click')
    await flushPromises()
    expect(listRuntimeEvents).toHaveBeenLastCalledWith({ before: 12 }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain('合成读取失败')
    expect(wrapper.find('[data-event-id="12"]').exists()).toBe(true)
    vi.mocked(listRuntimeEvents).mockResolvedValueOnce({ ...response, items: [{ ...response.items[0]!, id: 11 }] })
    await wrapper.findAll('button').find((button) => button.text() === '加载更早记录')!.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.runtime-event')).toHaveLength(2)
    wrapper.unmount()
  })
  it('does not turn request failures or partial collection into an empty success', async () => {
    vi.mocked(listRuntimeEvents).mockRejectedValueOnce(new Error('合成数据库不可读'))
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain('合成数据库不可读')
    expect(wrapper.text()).not.toContain('没有可展示')
    vi.mocked(listRuntimeEvents).mockResolvedValueOnce({ ...response, collector: { ...response.collector, writeError: true, droppedSinceStart: 2, pending: 1 }, items: [{ ...response.items[0]!, result: 'incomplete', details: { listed: 3, returned: null } }] })
    await wrapper.get('form').trigger('submit'); await flushPromises()
    expect(wrapper.text()).toContain('最近一次写入失败')
    expect(wrapper.text()).toContain('遗漏 2 条')
    expect(wrapper.text()).toContain('不能把未完成的数量当作零条结果')
    expect(wrapper.findAll('dd').some((node) => node.text() === '未完成')).toBe(true)
    wrapper.unmount()
  })
  it('cancels stale requests when filters change or the view unmounts', async () => {
    let resolve!: (value: RuntimeEventPage) => void
    vi.mocked(listRuntimeEvents).mockImplementation(() => new Promise((done) => { resolve = done }))
    const wrapper = render(); await flushPromises()
    const signal = vi.mocked(listRuntimeEvents).mock.calls[0]![1]!
    await wrapper.get('[aria-label="运行记录排查编号"]').setValue('new-request')
    expect(signal.aborted).toBe(true)
    resolve(response); await flushPromises()
    expect(wrapper.find('[aria-label="运行记录列表"]').exists()).toBe(false)
    await wrapper.get('form').trigger('submit')
    const nextSignal = vi.mocked(listRuntimeEvents).mock.calls[1]![1]!
    wrapper.unmount(); expect(nextSignal.aborted).toBe(true)
    resolve(response); await flushPromises()
  })
  it('clears reports when administrator identity or privileges change', async () => {
    const wrapper = render(); await flushPromises()
    const auth = useAuthStore()
    auth.session = { user: { userId: 'another-admin', name: '合成管理员' }, departments: [], selectedDepartment: null, csrfToken: 'synthetic-csrf', isAdmin: true }
    await flushPromises()
    expect(wrapper.find('[aria-label="运行记录列表"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it.each(['timeout', 'transport'] as const)('shows the safe %s classification in both modes', async (failureKind) => {
    vi.mocked(listRuntimeEvents).mockResolvedValueOnce({
      ...response,
      items: [{ ...response.items[0]!, module: 'submission', event: 'upstream_failure', result: 'failed', details: { errorCode: 'DINGTALK_OPENAPI_FAILED', failureKind } }],
    })
    const wrapper = render(); await flushPromises()
    expect(wrapper.text()).toContain(failureKind === 'timeout' ? '失败类别：请求超时' : '失败类别：网络通信失败')
    expect(wrapper.text()).toContain('业务状态')
    expect(wrapper.text()).not.toContain('稍后重试')
    wrapper.unmount()
  })
  it('searches the same request without restrictive old filters', async () => {
    const wrapper = render(); await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '查看同次请求')!.trigger('click')
    await flushPromises()
    expect(listRuntimeEvents).toHaveBeenLastCalledWith({ requestId: 'synthetic-request' }, expect.any(AbortSignal))
    wrapper.unmount()
  })
  it('provides selectable safe summary when HTTP clipboard access is unavailable', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('unavailable')) } })
    const wrapper = render(); await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '复制脱敏摘要')!.trigger('click')
    await flushPromises()
    const value = (wrapper.get('[aria-label="手动复制运行记录摘要"]').element as HTMLTextAreaElement).value
    expect(JSON.parse(value).details.returned).toBe(0)
    expect(JSON.parse(value).requestId).toBe('synthetic-request')
    wrapper.unmount()
  })
})

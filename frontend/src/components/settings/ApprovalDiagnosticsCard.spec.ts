import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { diagnoseTravelApproval } from '@/api/approvalDiagnostics'
import { useAuthStore } from '@/stores/auth'
import type { ApprovalDiagnosticReport } from '@/types/approvalDiagnostics'
import ApprovalDiagnosticsCard from './ApprovalDiagnosticsCard.vue'

vi.mock('@/api/approvalDiagnostics', () => ({ diagnoseTravelApproval: vi.fn() }))

const report: ApprovalDiagnosticReport = {
  requestId: 'synthetic-request-1', checkedAt: '2026-08-31T00:00:00Z',
  queryWindow: { from: '2026-08-01', to: '2026-08-31' }, identifierKind: 'business_id',
  configVersion: 7, outcome: 'visible', summary: '审批在当前条件下可以查询并选择',
  checks: [{ key: 'dates', label: '日期字段解析', status: 'passed', message: '日期可解析，属于同日行程；同日不会被排除' }],
  counts: { templates: 2, listRequests: 2, listed: 3, detailsRead: 3, evaluated: 3, excluded: 0, returned: 3, unselectable: 0, exclusions: [] },
  record: { ownerMatches: true, status: 'COMPLETED', result: 'agree', createdDate: '2026-08-01', startDate: '2026-08-10', endDate: '2026-08-10', sameDay: true },
  hints: ['请对照员工实际查询条件'],
}

describe.each([false, true])('ApprovalDiagnosticsCard, mobile=%s', (mobile) => {
  let pinia: ReturnType<typeof createPinia>
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-29T06:00:00Z'))
    vi.clearAllMocks()
    pinia = createPinia()
    setActivePinia(pinia)
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.mocked(diagnoseTravelApproval).mockResolvedValue(report)
  })
  afterEach(() => vi.useRealTimers())
  function render() {
    return mount(ApprovalDiagnosticsCard, { props: { mobile }, global: { plugins: [pinia, ElementPlus] } })
  }
  async function fill(wrapper: ReturnType<typeof render>) {
    await wrapper.get('[aria-label="诊断员工 userId"]').setValue(' synthetic-employee ')
    await wrapper.get('[aria-label="诊断审批编号或实例 ID"]').setValue(' 202608010800000000001 ')
  }

  it('rejects unavailable historical dates and constrains the diagnostic date controls', async () => {
    const wrapper = render(); await fill(wrapper)
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    expect(dates[0]!.props('disabledDate')(new Date(2025, 9, 29))).toBe(true)
    expect(dates[1]!.props('disabledDate')(new Date(2026, 9, 30))).toBe(true)
    dates[0]!.vm.$emit('update:modelValue', '2025-01-01')
    dates[1]!.vm.$emit('update:modelValue', '2025-05-31')
    await flushPromises(); await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('最近 365 个自然日')
    expect(diagnoseTravelApproval).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not query on mount and validates required inputs', async () => {
    const wrapper = render()
    expect(wrapper.get('.diagnostic-form-grid').classes().includes('diagnostic-form-grid--mobile')).toBe(mobile)
    expect(diagnoseTravelApproval).not.toHaveBeenCalled()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('请填写员工 userId')
    expect(diagnoseTravelApproval).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('runs the diagnosis and explains same-day visibility without exposing inputs in the summary', async () => {
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(diagnoseTravelApproval).toHaveBeenCalledWith({
      userId: 'synthetic-employee', approvalIdentifier: '202608010800000000001', identifierKind: 'auto',
    }, expect.any(AbortSignal))
    expect(wrapper.text()).toContain('审批在当前条件下可以查询并选择')
    expect(wrapper.text()).toContain('同日不会被排除')
    expect(wrapper.text()).toContain('synthetic-request-1')
    const summary = wrapper.get('[aria-label="脱敏诊断摘要"]').element as HTMLTextAreaElement
    expect(summary.value).not.toContain('synthetic-employee')
    expect(summary.value).not.toContain('202608010800000000001')
    expect(summary.value).not.toContain('2026-08-10')
    expect(JSON.parse(summary.value).counts.returned).toBe(3)
    wrapper.unmount()
  })

  it('replays explicit dates and keyword and rejects an incomplete date range', async () => {
    const wrapper = render()
    await fill(wrapper)
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-08-01')
    await flushPromises()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('同时填写开始和结束日期')
    expect(diagnoseTravelApproval).not.toHaveBeenCalled()
    dates[1]!.vm.$emit('update:modelValue', '2026-08-31')
    await wrapper.get('[aria-label="诊断查询关键词"]').setValue(' TARGET ')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(diagnoseTravelApproval).toHaveBeenCalledWith(expect.objectContaining({
      from: '2026-08-01', to: '2026-08-31', keyword: 'TARGET',
    }), expect.any(AbortSignal))
    wrapper.unmount()
  })

  it('shows incomplete checks and unknown counts instead of successful absence', async () => {
    vi.mocked(diagnoseTravelApproval).mockResolvedValue({
      ...report, outcome: 'incomplete', summary: '诊断超时，已有结果不完整', record: null,
      counts: { ...report.counts!, returned: null, unselectable: null },
      checks: [{ key: 'employee_details', label: '员工审批详情读取', status: 'unknown', message: '尚未完成全部检查' }],
      error: { code: 'DIAGNOSIS_TIMEOUT', upstreamHttpStatus: null, upstreamErrorCode: null },
    })
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('诊断超时')
    expect(wrapper.text()).toContain('未确认')
    expect(wrapper.text()).toContain('未完成')
    expect(wrapper.text()).toContain('DIAGNOSIS_TIMEOUT')
    expect(wrapper.text()).not.toContain('审批在当前条件下可以查询并选择')
    wrapper.unmount()
  })

  it('distinguishes unavailable options from an empty list', async () => {
    vi.mocked(diagnoseTravelApproval).mockResolvedValue({
      ...report, outcome: 'unselectable', summary: '审批会出现在列表中，但当前不可选择',
      checks: [{ key: 'accounting', label: '公司、预算和出差类别', status: 'failed', message: '出差审批的预算代码无法唯一对应来源模板选项' }],
    })
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('会出现在列表中，但当前不可选择')
    expect(wrapper.text()).toContain('预算代码无法唯一对应')
    expect(wrapper.text()).toContain('未通过')
    wrapper.unmount()
  })

  it('cancels through the visible action and discards late results after cancellation or unmount', async () => {
    let resolve!: (value: ApprovalDiagnosticReport) => void
    vi.mocked(diagnoseTravelApproval).mockImplementation(() => new Promise((done) => { resolve = done }))
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    const signal = vi.mocked(diagnoseTravelApproval).mock.calls[0]![1]!
    expect(wrapper.text()).toContain('尚未得出结论')
    expect(wrapper.get('[aria-label="诊断员工 userId"]').attributes('disabled')).toBeDefined()
    await wrapper.findAll('button').find((button) => button.text() === '取消并清空')!.trigger('click')
    expect(signal.aborted).toBe(true)
    resolve(report)
    await flushPromises()
    expect(wrapper.find('[aria-label="审批诊断结果"]').exists()).toBe(false)
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    const second = vi.mocked(diagnoseTravelApproval).mock.calls[1]![1]!
    wrapper.unmount()
    expect(second.aborted).toBe(true)
    resolve(report)
    await flushPromises()
  })

  it('clears previous results and renders request errors honestly', async () => {
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    vi.mocked(diagnoseTravelApproval).mockRejectedValue(new Error('管理员权限已撤销'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('管理员权限已撤销')
    expect(wrapper.find('[aria-label="审批诊断结果"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clears sensitive results when input changes and provides a manual-copy fallback on HTTP', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('unavailable')) } })
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await wrapper.findAll('button').find((button) => button.text() === '复制脱敏摘要')!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('请选中下方摘要手动复制')
    expect(wrapper.get('.diagnostic-summary').attributes('open')).toBeDefined()
    await wrapper.get('[aria-label="诊断审批编号或实例 ID"]').setValue('synthetic-other')
    expect(wrapper.find('[aria-label="审批诊断结果"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('cancels a diagnosis when the administrator identity changes', async () => {
    let resolve!: (value: ApprovalDiagnosticReport) => void
    vi.mocked(diagnoseTravelApproval).mockImplementation(() => new Promise((done) => { resolve = done }))
    const wrapper = render()
    await fill(wrapper)
    await wrapper.get('form').trigger('submit')
    const signal = vi.mocked(diagnoseTravelApproval).mock.calls[0]![1]!
    useAuthStore().session = {
      user: { userId: 'another-synthetic-admin', name: '合成管理员' }, departments: [],
      selectedDepartment: null, isAdmin: true, csrfToken: 'synthetic-csrf',
    }
    await flushPromises()
    expect(signal.aborted).toBe(true)
    resolve(report)
    await flushPromises()
    expect(wrapper.find('[aria-label="审批诊断结果"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

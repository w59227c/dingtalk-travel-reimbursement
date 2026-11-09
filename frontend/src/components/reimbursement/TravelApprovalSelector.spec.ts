import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type {
  OaTravelApproval,
  ReimbursementDraft,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
} from '@/types/reimbursements'
import TravelApprovalSelector from './TravelApprovalSelector.vue'

const candidate: OaTravelApproval = {
  processInstanceId: 'travel-1',
  profileKey: 'business',
  profileDisplayName: '境内出差',
  sourceProcessCode: 'PROC-TRAVEL',
  travelTypeOption: { value: 'business', label: '境内出差', key: null },
  companyOption: { value: 'company-1', label: '北京公司', key: null },
  budgetCodeOption: { value: 'budget-1', label: '预算1', key: null },
  unavailableReason: null,
  title: '合肥出差申请',
  businessId: 'TRAVEL-1',
  originatorDepartmentId: '100',
  startDate: '2026-09-01',
  endDate: '2026-09-03',
  createdAt: '2026-08-30T00:00:00Z',
  finishedAt: '2026-08-31T00:00:00Z',
}

const selection: ReimbursementRelatedApprovalSelection = {
  processInstanceId: 'travel-1',
  profileKey: 'business',
  queryWindow: { from: '2026-08-01', to: '2026-09-04' },
}

const linked: ReimbursementRelatedApproval = {
  processInstanceId: 'travel-1',
  profileKey: 'business',
  sourceProcessCode: 'PROC-TRAVEL',
  originatorDepartmentId: '100',
  title: '合肥出差申请',
  businessId: 'TRAVEL-1',
  startDate: '2026-09-01',
  endDate: '2026-09-03',
  queryWindow: {
    startTimeMs: Date.parse('2026-08-01T00:00:00+08:00'),
    endTimeMs: Date.parse('2026-09-04T23:59:59.999+08:00'),
  },
  verifiedAt: '2026-09-04T00:00:00Z',
}

function restoredDraft(): ReimbursementDraft {
  return {
    id: 'draft', status: 'DRAFT', revision: 1, department: { id: '1', name: '演示部门' },
    templateConfigVersion: 1, relatedApprovalCount: 1, expiresAt: '', createdAt: '', updatedAt: '', lockedAt: null,
    template: { processCode: 'PROC-R', configVersion: 1, schemaFingerprint: 'a'.repeat(64) },
    input: { companyValue: 'company-1', budgetCodeValue: 'budget-1', accountingSourceVerified: true,
      project: { mode: 'manual', text: '预算1 · 演示项目' }, trip: null, items: [],
      ocrDispositionVersion: 1, dismissedOcrFileIds: [] },
    totals: { expenseTotal: '0.00', subsidyTotal: '0.00', totalAmount: '0.00', receiptCount: 0, uppercaseAmount: '零元整', subsidy: null },
    relatedApprovals: [structuredClone(linked)], relatedApprovalSummary: null,
  }
}

describe.each([false, true])('approval query bounds, mobile=%s', (mobile) => {
  beforeEach(() => { setActivePinia(createPinia()); vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(new Date('2026-10-29T06:00:00Z')) })
  afterEach(() => vi.useRealTimers())
  it.each([
    ['2025-01-01', '2025-05-31', '365'],
    ['2026-10-29', '2026-10-30', '晚于今天'],
    ['2026-01-01', '2026-07-01', '180'],
  ])('rejects invalid dates %s through %s before sending a request', async (from, to, reason) => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate]
    const load = vi.spyOn(drafts, 'loadTravelApprovals').mockResolvedValue(undefined)
    const wrapper = mount(TravelApprovalSelector, { props: { modelValue: [], mobile }, global: { plugins: [ElementPlus] } })
    const pickers = wrapper.findAllComponents({ name: 'ElDatePicker' })
    pickers[0]!.vm.$emit('update:modelValue', from)
    pickers[1]!.vm.$emit('update:modelValue', to)
    await nextTick()
    await wrapper.get('.travel-query-button').trigger('click')
    expect(wrapper.text()).toContain(reason)
    expect(load).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('审批发起日期')
    wrapper.unmount()
  })
  it('uses the same allowed dates in both pickers and accepts exactly 180 days', async () => {
    const drafts = useReimbursementDraftStore(); drafts.travelApprovals = [candidate]
    const load = vi.spyOn(drafts, 'loadTravelApprovals').mockResolvedValue(undefined)
    const wrapper = mount(TravelApprovalSelector, { props: { modelValue: [], mobile }, global: { plugins: [ElementPlus] } })
    const pickers = wrapper.findAllComponents({ name: 'ElDatePicker' })
    expect(pickers[0]!.props('disabledDate')(new Date(2025, 9, 29))).toBe(true)
    expect(pickers[1]!.props('disabledDate')(new Date(2026, 9, 30))).toBe(true)
    pickers[0]!.vm.$emit('update:modelValue', '2025-10-30')
    pickers[1]!.vm.$emit('update:modelValue', '2026-04-27')
    await nextTick(); await wrapper.get('.travel-query-button').trigger('click')
    expect(load).toHaveBeenCalledWith({ from: '2025-10-30', to: '2026-04-27' })
    wrapper.unmount()
  })
  it('refreshes template options before retrying a stale approval search with the same filters', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovalsError = '审批模板配置已更新'
    drafts.travelApprovalsErrorCode = 'OA_TEMPLATE_CONFIGURATION_CHANGED'
    const calls: string[] = []
    vi.spyOn(drafts, 'loadReimbursementOptions').mockImplementation(async () => { calls.push('options') })
    const load = vi.spyOn(drafts, 'loadTravelApprovals').mockImplementation(async () => { calls.push('approvals') })
    const wrapper = mount(TravelApprovalSelector, { props: { modelValue: [], mobile }, global: { plugins: [ElementPlus] } })
    await wrapper.get('input[aria-label="搜索出差审批"]').setValue('合肥')
    await nextTick()
    await wrapper.get('.travel-query-alert button').trigger('click')
    await nextTick()
    expect(calls).toEqual(['options', 'approvals'])
    expect(load).toHaveBeenCalledWith({ query: '合肥' })
    wrapper.unmount()
  })

  it('keeps upstream date rejections actionable instead of offering blind retries', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovalsError = '钉钉拒绝当前查询日期，请调整日期'
    drafts.travelApprovalsErrorCode = 'DINGTALK_QUERY_TIME_INVALID'
    const wrapper = mount(TravelApprovalSelector, { props: { modelValue: [], mobile }, global: { plugins: [ElementPlus] } })
    expect(wrapper.text()).toContain('请调整查询日期后重新查询')
    expect(wrapper.text()).not.toContain('重新加载')
    expect(wrapper.findComponent({ name: 'ElEmpty' }).props('description')).toBe('请调整查询日期后重新查询')
    wrapper.unmount()
  })
})

describe('TravelApprovalSelector', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
  })

  it.each([false, true])('shows each candidate company and budget without borrowing the saved selection, mobile=%s', (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = restoredDraft()
    drafts.travelApprovals = [candidate, {
      ...candidate, processInstanceId: 'travel-2',
      companyOption: { value: 'company-2', label: '另一演示公司', key: null },
      budgetCodeOption: { value: 'budget-2', label: 'DEMO-02 · 另一演示项目', key: null },
    }, { ...candidate, processInstanceId: 'travel-missing', companyOption: null, budgetCodeOption: null }]
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], linkedApprovals: [linked], mobile },
      global: { plugins: [ElementPlus] },
    })
    const rows = wrapper.findAll('.travel-approval-row')
    expect(rows[0]!.get('.approval-accounting-line').text()).toBe('北京公司 · 预算1')
    expect(rows[1]!.get('.approval-accounting-line').text()).toBe('另一演示公司 · DEMO-02 · 另一演示项目')
    expect(rows[2]!.find('.approval-accounting-line').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('restores verified linked accounting without a query cache and hides unverified values, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = restoredDraft()
    drafts.travelApprovalsError = '暂不重新查询'
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], linkedApprovals: [linked], mobile },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.get('.approval-accounting-line').text()).toBe('company-1 · 预算1 · 演示项目')
    drafts.currentDraft.input.accountingSourceVerified = false
    await nextTick()
    expect(wrapper.find('.approval-accounting-line').exists()).toBe(false)
    drafts.currentDraft.status = 'LOCKED'
    drafts.currentDraft.submittedDetails = { employeeName: '合成员工', companyLabel: '提交时公司',
      budgetLabel: '提交时预算', travelTypeLabel: '提交时类别' }
    await nextTick()
    expect(wrapper.get('.approval-accounting-line').text()).toBe('提交时公司 · 提交时预算')
    drafts.currentDraft.relatedApprovals = []
    await nextTick()
    expect(wrapper.find('.approval-accounting-line').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('uses the matching approval header and query layout, mobile=%s', (mobile) => {
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], mobile },
      global: { plugins: [ElementPlus] },
    })

    const header = wrapper.get('.travel-approval-heading')
    expect(header.classes().includes('travel-approval-heading--mobile')).toBe(mobile)
    expect(header.get('h2').text()).toBe('关联已通过的出差审批')
    expect(header.get('.travel-approval-count').text()).toBe('已选 1 张')
    expect(wrapper.get('.travel-query-grid').classes().includes('travel-query-grid--mobile')).toBe(mobile)
    expect(wrapper.get('.travel-query-button').classes()).toContain('travel-query-button')
    wrapper.unmount()
  })

  it('disables approval interactions while busy without claiming the reimbursement is being submitted', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate]
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], busy: true },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.findComponent({ name: 'ElCheckbox' }).props('disabled')).toBe(true)
    expect(wrapper.text()).not.toContain('当前报销已进入提交阶段')
    expect(wrapper.text()).not.toContain('正在核验')
    expect(wrapper.text()).toContain('已选 1 张')
    wrapper.unmount()
  })

  it('emits the server profile and exact query window for a selected candidate', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate]
    drafts.travelApprovalQueryWindow = selection.queryWindow
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [] },
      global: { plugins: [ElementPlus] },
    })

    wrapper.findComponent({ name: 'ElCheckbox' }).vm.$emit('change', true)
    await nextTick()

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([[selection]])
    wrapper.unmount()
  })

  it('uses a single approval to derive the reimbursement department', async () => {
    const drafts = useReimbursementDraftStore()
    const other = {
      ...candidate,
      processInstanceId: 'travel-2',
      companyOption: { value: 'company-2', label: '无锡公司', key: null },
      budgetCodeOption: { value: 'budget-2', label: '预算2', key: null },
    }
    drafts.travelApprovals = [candidate, other]
    drafts.travelApprovalQueryWindow = selection.queryWindow
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], single: true },
      global: { plugins: [ElementPlus] },
    })

    const replacement = wrapper.findAllComponents({ name: 'ElCheckbox' })[1]!
    expect(replacement.props('disabled')).toBe(false)
    replacement.vm.$emit('change', true)
    await nextTick()

    expect(wrapper.text()).toContain('无法匹配当前部门时再请你确认')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([[
      { ...selection, processInstanceId: 'travel-2' },
    ]])
    wrapper.unmount()
  })

  it('keeps a restored linked approval visible even when it is outside the latest search', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovalsError = '暂不重新查询'
    const wrapper = mount(TravelApprovalSelector, {
      props: {
        modelValue: [selection],
        linkedApprovals: [linked],
        readonly: true,
      },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain('合肥出差申请')
    expect(wrapper.text()).toContain('出差类别：出差类别待重新核验')
    expect(wrapper.text()).toContain('已关联')
    expect(wrapper.text()).not.toContain('当前报销已进入提交阶段')
    expect(wrapper.findComponent({ name: 'ElCheckbox' }).props('disabled')).toBe(true)
    wrapper.unmount()
  })

  it('disables mismatched and unavailable approvals with reasons and keeps selections removable', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate,
      { ...candidate, processInstanceId: 'other-company', companyOption: { value: 'other', label: '另一公司', key: null } },
      { ...candidate, processInstanceId: 'other-budget', budgetCodeOption: { value: 'other', label: '另一预算', key: null } },
      { ...candidate, processInstanceId: 'other-type', travelTypeOption: { value: 'other', label: '另一类别', key: null } },
      { ...candidate, processInstanceId: 'other-department', originatorDepartmentId: '200' },
      { ...candidate, processInstanceId: 'missing', companyOption: null, unavailableReason: '出差审批的所属公司缺失' },
    ]
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection] }, global: { plugins: [ElementPlus] },
    })
    expect(wrapper.findAllComponents({ name: 'ElCheckbox' }).map((item) => item.props('disabled')))
      .toEqual([false, true, true, true, true, true])
    expect(wrapper.text()).toContain('所属公司与已选审批不同')
    expect(wrapper.text()).toContain('预算代码与已选审批不同')
    expect(wrapper.text()).toContain('出差类别与已选审批不同')
    expect(wrapper.text()).toContain('发起部门与已选审批不同')
    expect(wrapper.text()).toContain('出差审批的所属公司缺失')
    wrapper.findAllComponents({ name: 'ElCheckbox' })[1]!.vm.$emit('change', true)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    wrapper.findAllComponents({ name: 'ElCheckbox' })[0]!.vm.$emit('change', false)
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([[]])
    wrapper.unmount()
  })

  it('allows discontinuous approvals while preserving the optional date-overlap filter', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [
      candidate,
      { ...candidate, processInstanceId: 'adjacent', startDate: '2026-09-04', endDate: '2026-09-05' },
      { ...candidate, processInstanceId: 'gap', startDate: '2026-09-07', endDate: '2026-09-08' },
      { ...candidate, processInstanceId: 'unrelated', startDate: '2026-08-01', endDate: '2026-08-03' },
    ]
    const wrapper = mount(TravelApprovalSelector, {
      props: {
        modelValue: [selection],
        requiredStartDate: '2026-09-01',
        requiredEndDate: '2026-09-10',
      },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.findAllComponents({ name: 'ElCheckbox' }).map((item) => item.props('disabled')))
      .toEqual([false, false, false, true])
    expect(wrapper.text()).not.toContain('审批日期与已选审批不连续')
    expect(wrapper.text()).toContain('审批日期与出差补助日期不重合')
    wrapper.unmount()
  })

  it('allows removing a middle approval even when that creates a date gap', async () => {
    const drafts = useReimbursementDraftStore()
    const middle = { ...candidate, processInstanceId: 'travel-2', startDate: '2026-09-04', endDate: '2026-09-05' }
    const last = { ...candidate, processInstanceId: 'travel-3', startDate: '2026-09-06', endDate: '2026-09-07' }
    drafts.travelApprovals = [candidate, middle, last]
    const selections = [selection, middle, last].map((approval) => ({
      processInstanceId: approval.processInstanceId,
      profileKey: approval.profileKey,
      queryWindow: selection.queryWindow,
    }))
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: selections },
      global: { plugins: [ElementPlus] },
    })

    wrapper.findAllComponents({ name: 'ElCheckbox' })[1]!.vm.$emit('change', false)
    await nextTick()

    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([[selection, selections[2]]])
    expect(wrapper.text()).not.toContain('移除后审批日期会中断')
    wrapper.unmount()
  })

  it('loads candidates by an explicit date range and keyword', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate]
    const load = vi.spyOn(drafts, 'loadTravelApprovals').mockResolvedValue(undefined)
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [] },
      global: { plugins: [ElementPlus] },
    })
    const datePickers = wrapper.findAllComponents({ name: 'ElDatePicker' })
    const keyword = wrapper.findAllComponents({ name: 'ElInput' }).find(
      (item) => item.props('ariaLabel') === '搜索出差审批',
    )
    if (!datePickers[0] || !datePickers[1] || !keyword) throw new Error('Missing query inputs')
    datePickers[0].vm.$emit('update:modelValue', '2026-07-01')
    datePickers[1].vm.$emit('update:modelValue', '2026-09-04')
    keyword.vm.$emit('update:modelValue', '合肥')
    await nextTick()

    const query = wrapper.findAll('button').find((button) => button.text().trim() === '查询审批')
    if (!query) throw new Error('Missing query button')
    await query.trigger('click')

    expect(load).toHaveBeenCalledWith({
      from: '2026-07-01',
      to: '2026-09-04',
      query: '合肥',
    })
    wrapper.unmount()
  })

  it.each([true, false])('restores dynamic source type without falling back to a fixed option (%s)', (hasSource) => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [{
      ...candidate,
      processInstanceId: 'new-travel',
      sourceTravelTypeValue: 'source',
    }]
    drafts.reimbursementOptions = {
      templateConfigVersion: 1, reimbursementProcessCode: 'PROC-R', companyOptions: [], budgetCodeOptions: [],
      travelProfiles: [{ profileKey: 'business', displayName: '境内', processCode: 'PROC-TRAVEL',
        schemaFingerprint: 'a'.repeat(64), travelTypeOption: { value: 'wrong-fixed', label: '固定', key: null },
        travelTypeMappings: { source: candidate.travelTypeOption } }],
    }
    drafts.currentDraft = {
      id: 'draft', status: 'DRAFT', revision: 1, department: { id: '1', name: '部门' },
      templateConfigVersion: 1, relatedApprovalCount: 1, expiresAt: '', createdAt: '', updatedAt: '', lockedAt: null,
      template: { processCode: 'PROC-R', configVersion: 1, schemaFingerprint: 'a'.repeat(64) },
      input: { companyValue: 'company-1', budgetCodeValue: 'budget-1', trip: null, items: [],
        ocrDispositionVersion: 1, dismissedOcrFileIds: [] },
      totals: { expenseTotal: '0.00', subsidyTotal: '0.00', totalAmount: '0.00', receiptCount: 0, uppercaseAmount: '零元整', subsidy: null },
      relatedApprovals: [], relatedApprovalSummary: null,
    }
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection], linkedApprovals: [{ ...linked, sourceTravelTypeValue: hasSource ? 'source' : undefined }] },
      global: { plugins: [ElementPlus] },
    })
    const options = wrapper.findAllComponents({ name: 'ElCheckbox' })
    expect(options[0]?.props('disabled')).toBe(!hasSource)
    if (!hasSource) expect(wrapper.text()).toContain('出差类别来源失效')
    else expect(wrapper.text()).not.toContain('出差类别与已选审批不同')
    if (hasSource) expect(wrapper.text()).toContain('出差类别：境内出差')
    wrapper.unmount()
  })

  it('rejects different source categories even when both map to the same reimbursement option', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [
      { ...candidate, sourceTravelTypeValue: '公司内部出差（长期）' },
      {
        ...candidate,
        processInstanceId: 'internal-short',
        sourceTravelTypeValue: '公司内部出差（短期）',
      },
    ]
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection] },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.findAllComponents({ name: 'ElCheckbox' })[1]?.props('disabled')).toBe(true)
    expect(wrapper.text()).toContain('出差类别与已选审批不同')
    wrapper.unmount()
  })
  it.each([false, true])('distinguishes a failed approval query from an empty result, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovalsError = '合成上游查询超时'
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [], mobile }, global: { plugins: [ElementPlus] },
    })
    expect(wrapper.text()).toContain('合成上游查询超时')
    expect(wrapper.findComponent({ name: 'ElEmpty' }).props('description')).toBe('审批查询未完成，请重试')
    drafts.travelApprovalsError = ''
    await nextTick()
    expect(wrapper.findComponent({ name: 'ElEmpty' }).props('description')).toBe('当前查询范围内没有已通过的出差审批')
    wrapper.unmount()
  })

})

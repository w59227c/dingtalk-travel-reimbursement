import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type {
  OaTravelApproval,
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

describe('TravelApprovalSelector', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
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

    expect(wrapper.text()).toContain('无需再次选择部门')
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
    expect(wrapper.text()).toContain('当前报销已进入提交阶段')
    expect(wrapper.findComponent({ name: 'ElCheckbox' }).props('disabled')).toBe(true)
    wrapper.unmount()
  })

  it('disables mismatched and unavailable approvals with reasons and keeps selections removable', () => {
    const drafts = useReimbursementDraftStore()
    drafts.travelApprovals = [candidate,
      { ...candidate, processInstanceId: 'other-company', companyOption: { value: 'other', label: '另一公司', key: null } },
      { ...candidate, processInstanceId: 'other-budget', budgetCodeOption: { value: 'other', label: '另一预算', key: null } },
      { ...candidate, processInstanceId: 'other-type', travelTypeOption: { value: 'other', label: '另一类别', key: null } },
      { ...candidate, processInstanceId: 'missing', companyOption: null, unavailableReason: '出差审批的所属公司缺失' },
    ]
    const wrapper = mount(TravelApprovalSelector, {
      props: { modelValue: [selection] }, global: { plugins: [ElementPlus] },
    })
    expect(wrapper.findAllComponents({ name: 'ElCheckbox' }).map((item) => item.props('disabled')))
      .toEqual([false, true, true, true, true])
    expect(wrapper.text()).toContain('所属公司与已选审批不同')
    expect(wrapper.text()).toContain('预算代码与已选审批不同')
    expect(wrapper.text()).toContain('出差类别与已选审批不同')
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
})

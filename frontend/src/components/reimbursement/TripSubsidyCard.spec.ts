import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { nextTick } from 'vue'

import { useExpenseStore } from '@/stores/expense'
import type { TripType } from '@/types/expenses'
import TripSubsidyCard from './TripSubsidyCard.vue'

enableAutoUnmount(afterEach)

const approvals = [
  { processInstanceId: 'approval-1', title: '合肥出差', startDate: '2026-09-01', endDate: '2026-09-03' },
  { processInstanceId: 'approval-2', title: '南京出差', startDate: '2026-09-04', endDate: '2026-09-05' },
]

describe('TripSubsidyCard', () => {
  beforeEach(() => setActivePinia(createPinia()))

  function mountCard(
    tripType: TripType = 'business',
    selected = approvals,
    readonly = false,
  ) {
    const expense = useExpenseStore()
    expense.setTripType(tripType)
    expense.syncSubsidyApprovals(selected)
    expense.setSubsidyIncluded(true)
    return mount(TripSubsidyCard, {
      props: { readonly, approvals: selected, approvalTripType: tripType },
      global: { plugins: [ElementPlus] },
    })
  }

  it('creates one subsidy editor for every non-overlapping approval', () => {
    const wrapper = mountCard()
    expect(wrapper.findAll('.subsidy-item')).toHaveLength(2)
    expect(wrapper.text()).toContain('补助 1')
    expect(wrapper.text()).toContain('合肥出差')
    expect(wrapper.text()).toContain('补助 2')
    expect(wrapper.text()).toContain('南京出差')
    expect(wrapper.text()).toContain('审批日期：2026-09-01 至 2026-09-03')
    expect(wrapper.findComponent({ name: 'ElDatePicker' }).exists()).toBe(false)
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(8)
  })

  it('uses a full-width approval heading in the explicit mobile presentation', () => {
    const expense = useExpenseStore()
    const selected = [{
      processInstanceId: 'approval-long',
      title: '王广硕提交的境内出差申请',
      startDate: '2026-09-01',
      endDate: '2026-09-03',
    }]
    expense.setTripType('business')
    expense.syncSubsidyApprovals(selected)
    expense.setSubsidyIncluded(true)
    const wrapper = mount(TripSubsidyCard, {
      props: { mobile: true, approvals: selected, approvalTripType: 'business' },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.get('.subsidy-item__heading').classes()).toContain('subsidy-item__heading--mobile')
    expect(wrapper.get('.subsidy-approval-title').text()).toBe('王广硕提交的境内出差申请')
  })

  it('merges overlapping approvals into one subsidy editor and keeps gaps separate', () => {
    const selected = [
      { processInstanceId: 'approval-1', title: '合肥一期', startDate: '2026-09-01', endDate: '2026-09-03' },
      { processInstanceId: 'approval-2', title: '合肥延期', startDate: '2026-09-03', endDate: '2026-09-05' },
      { processInstanceId: 'approval-3', title: '合肥二期', startDate: '2026-09-05', endDate: '2026-09-07' },
      { processInstanceId: 'approval-4', title: '南京出差', startDate: '2026-09-09', endDate: '2026-09-10' },
    ]
    const wrapper = mountCard('business', selected)
    const items = wrapper.findAll('.subsidy-item')

    expect(items).toHaveLength(2)
    expect(items[0]?.text()).toContain('2026-09-01 至 2026-09-07')
    expect(items[0]?.text()).toContain('由 3 张审批合并')
    expect(items[0]?.text()).toContain('合肥一期')
    expect(items[0]?.text()).toContain('合肥延期')
    expect(items[0]?.text()).toContain('合肥二期')
    expect(items[1]?.text()).toContain('2026-09-09 至 2026-09-10')
    expect(items[1]?.text()).not.toContain('合并')
  })

  it('matches calculated amounts to approvals instead of relying on response order', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard()
    expense.totals = {
      expenseTotal: '0.00', subsidyTotal: '700.00', totalAmount: '700.00',
      receiptCount: 0, uppercaseAmount: '柒佰元整', subsidy: null,
      subsidies: [
        {
          relatedApprovalId: 'approval-2', tripType: 'business', calendarDays: 2,
          effectiveDays: '2.0', dailyRate: '100.00', total: '200.00',
        },
        {
          relatedApprovalId: 'approval-1', tripType: 'business', calendarDays: 3,
          effectiveDays: '2.5', dailyRate: '200.00', total: '500.00',
        },
      ],
    }
    await nextTick()

    const previews = wrapper.findAll('.subsidy-preview')
    expect(previews[0]?.text()).toContain('补助 ¥500.00')
    expect(previews[1]?.text()).toContain('补助 ¥200.00')
  })

  it('shows a subsidy calculation failure inside the subsidy card', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard()
    expense.calculationError = '出差补助金额计算失败，请重试'
    await nextTick()

    expect(wrapper.text()).toContain('出差补助金额计算失败，请重试')
  })

  it('edits morning and afternoon per approval without exact-time inputs', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard()
    expect(wrapper.findComponent({ name: 'ElTimePicker' }).exists()).toBe(false)
    const departure = wrapper.get('[aria-label="补助 1 出发时段"]')
    const returned = wrapper.get('[aria-label="补助 1 返回时段"]')

    await departure.get('input[value="afternoon"]').setValue()
    await returned.get('input[value="morning"]').setValue()
    expect(expense.subsidyTrips[0]?.startTime).toBe('18:00')
    expect(expense.subsidyTrips[0]?.endTime).toBe('09:00')
    expect(expense.subsidyTrips[1]?.startTime).toBe('09:00')
  })

  it('uses only a per-approval policy checkbox for same-city projects', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard('same_city_project')
    expect(wrapper.text()).not.toContain('确认有效天数')
    const confirmations = wrapper.findAllComponents({ name: 'ElCheckbox' })
    expect(confirmations).toHaveLength(2)
    expect(expense.policyInputError).toContain('逐项勾选')

    expense.subsidyTrips[0]!.policyConfirmed = true
    expense.subsidyTrips[1]!.policyConfirmed = true
    expect(expense.tripPayloads()).toHaveLength(2)
    expect(expense.tripPayloads().every((trip) => trip.policyConfirmed)).toBe(true)
  })

  it('shows that an internal trip over 30 days has no subsidy', () => {
    const longApproval = [{
      processInstanceId: 'approval-long',
      title: '长期内部出差',
      startDate: '2026-09-01',
      endDate: '2026-10-01',
    }]
    const wrapper = mountCard('internal', longApproval)
    expect(wrapper.text()).toContain('公司内部长期出差，不计算补助')
    expect(wrapper.text()).toContain('超过 30 天，本项补助为 0')
  })

  it('does not offer a subsidy for overseas approvals and permits clearing restored state', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard('overseas')
    expect(wrapper.text()).toContain('境外出差不计算出差补助')
    expect(wrapper.findAll('.subsidy-item')).toHaveLength(0)
    expect(expense.tripPayloads()).toEqual([])
    expect(wrapper.get('[role="switch"]').attributes('disabled')).toBeUndefined()
    wrapper.findComponent({ name: 'ElSwitch' }).vm.$emit('update:modelValue', false)
    await nextTick()
    expect(expense.includeSubsidy).toBe(false)
  })

  it('allows a restored subsidy to be turned off when its category can no longer be resolved', async () => {
    const expense = useExpenseStore()
    expense.syncSubsidyApprovals(approvals)
    expense.setSubsidyIncluded(true)
    const wrapper = mount(TripSubsidyCard, {
      props: { approvals, approvalTripType: null },
      global: { plugins: [ElementPlus] },
    })
    const toggle = wrapper.get('[role="switch"]')

    expect(toggle.attributes('disabled')).toBeUndefined()
    wrapper.findComponent({ name: 'ElSwitch' }).vm.$emit('update:modelValue', false)
    await nextTick()

    expect(expense.includeSubsidy).toBe(false)
  })

  it('locks all subsidy controls after submission', async () => {
    const expense = useExpenseStore()
    const wrapper = mountCard('business', approvals, true)
    expect(wrapper.findComponent({ name: 'ElForm' }).props('disabled')).toBe(true)
    await wrapper.get('[role="switch"]').trigger('click')
    expect(expense.includeSubsidy).toBe(true)
  })
})

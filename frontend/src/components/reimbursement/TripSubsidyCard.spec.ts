import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

import { calculateTotals } from '@/api/expenses'
import { useExpenseStore } from '@/stores/expense'
import type { ExpenseTotals, TripType } from '@/types/expenses'
import TripSubsidyCard from './TripSubsidyCard.vue'

enableAutoUnmount(afterEach)

vi.mock('@/api/expenses', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/expenses')>(),
  calculateTotals: vi.fn(),
}))

const approvals = [
  { processInstanceId: 'approval-1', title: '合肥出差', startDate: '2026-09-01', endDate: '2026-09-03' },
  { processInstanceId: 'approval-2', title: '南京出差', startDate: '2026-09-04', endDate: '2026-09-05' },
]

describe('TripSubsidyCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(calculateTotals).mockReset()
  })

  function mountCard(
    tripType: TripType = 'business',
    selected = approvals,
    readonly = false,
    mobile = false,
  ) {
    const expense = useExpenseStore()
    expense.setTripType(tripType)
    expense.syncSubsidyApprovals(selected)
    expense.setSubsidyIncluded(true)
    return mount(TripSubsidyCard, {
      props: { mobile, readonly, approvals: selected, approvalTripType: tripType },
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

  it.each([false, true])('allows an exclusion across merged approval boundaries, mobile=%s', async (mobile) => {
    const selected = [approvals[0]!, { ...approvals[1]!, startDate: '2026-09-03' }]
    const expense = useExpenseStore()
    const wrapper = mountCard('business', selected, false, mobile)
    await wrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
    Object.assign(expense.subsidyTrips[0]!.excludedPeriods![0]!, {
      startDate: '2026-09-02', endDate: '2026-09-04', endTime: '18:00',
    })
    expect(expense.policyInputError).toBe('')
    expect(expense.tripPayloads()).toHaveLength(2)
    expect(expense.tripPayloads()[0]?.excludedPeriods?.[0]?.endDate).toBe('2026-09-04')
    expense.totals = {
      expenseTotal: '0.00', subsidyTotal: '300.00', totalAmount: '300.00', receiptCount: 0,
      uppercaseAmount: '叁佰元整', subsidy: null, subsidies: [{
        relatedApprovalId: 'approval-1', tripType: 'business', calendarDays: 5,
        effectiveDays: '3.0', excludedDays: '2.0', dailyRate: '100.00', total: '300.00',
      }],
    }
    await nextTick()
    expect(wrapper.get('.subsidy-preview').text()).toContain('原补助 5.0 天')
    expect(wrapper.get('.subsidy-preview').text()).toContain('扣除 2.0 天')
  })

  it.each([
    { presentation: 'desktop', mobile: false },
    { presentation: 'mobile', mobile: true },
  ])('keeps the previous subsidy preview mounted while a changed period is recalculated on $presentation', async ({ mobile }) => {
    const selected = [approvals[0]!]
    const expense = useExpenseStore()
    const wrapper = mountCard('business', selected, false, mobile)
    expense.totals = {
      expenseTotal: '0.00', subsidyTotal: '300.00', totalAmount: '300.00',
      receiptCount: 0, uppercaseAmount: '叁佰元整', subsidy: null,
      subsidies: [{
        relatedApprovalId: 'approval-1', tripType: 'business', calendarDays: 3,
        effectiveDays: '3.0', dailyRate: '100.00', total: '300.00',
      }],
    }
    await nextTick()

    let finishCalculation!: (totals: ExpenseTotals) => void
    vi.mocked(calculateTotals).mockImplementationOnce(() => new Promise((resolve) => {
      finishCalculation = resolve
    }))
    await wrapper
      .get('[aria-label="补助 1 出发时段"] input[value="afternoon"]')
      .setValue()
    const refresh = expense.refreshCalculations()
    await nextTick()

    expect(expense.calculating).toBe(true)
    expect(wrapper.get('.subsidy-preview').text()).toContain('有效 3.0 天')
    expect(wrapper.get('.subsidy-preview').text()).toContain('补助 ¥300.00')

    finishCalculation({
      expenseTotal: '0.00', subsidyTotal: '250.00', totalAmount: '250.00',
      receiptCount: 0, uppercaseAmount: '贰佰伍拾元整', subsidy: null,
      subsidies: [{
        relatedApprovalId: 'approval-1', tripType: 'business', calendarDays: 3,
        effectiveDays: '2.5', dailyRate: '100.00', total: '250.00',
      }],
    })
    await refresh
    await nextTick()

    expect(wrapper.get('.subsidy-preview').text()).toContain('有效 2.5 天')
    expect(wrapper.get('.subsidy-preview').text()).toContain('补助 ¥250.00')
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

  it.each([false, true])('uses the approved internal category at any duration, mobile=%s', (mobile) => {
    const selected = [{ processInstanceId: 'long', title: '内部出差', startDate: '2026-09-01', endDate: '2026-10-01' }]
    const wrapper = mountCard('short_term_internal', selected, false, mobile)
    expect(wrapper.findComponent({ name: 'ElInput' }).props('modelValue')).toBe('公司内部出差（短期）')
    expect(wrapper.text()).not.toContain('超过 30 天')
    expect(wrapper.findAll('.subsidy-item')).toHaveLength(1)
  })

  it.each([false, true])('offers subsidies for configurable overseas categories, mobile=%s', (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('long_term_overseas', approvals, false, mobile)
    expect(wrapper.findComponent({ name: 'ElInput' }).props('modelValue')).toBe('境外商务出差（长期）')
    expect(wrapper.findAll('.subsidy-item')).toHaveLength(2)
    expect(expense.tripPayloads()[0]?.tripType).toBe('long_term_overseas')
  })

  it.each([false, true])('adds, edits, removes and validates multiple excluded periods, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('business', approvals, false, mobile)
    const add = wrapper.get('[aria-label="补助 1 添加非补助时段"]')
    await add.trigger('click')
    expect(expense.policyInputError).toContain('请补齐每个非补助时段')
    expect(expense.tripPayloads()).toEqual([])
    const first = expense.subsidyTrips[0]!.excludedPeriods![0]!
    Object.assign(first, { startDate: '2026-09-01', endDate: '2026-09-02' })
    await nextTick()
    await wrapper.get('[aria-label="补助 1 非补助时段 1 出发时段"] input[value="afternoon"]').setValue()
    await wrapper.get('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="afternoon"]').setValue()
    expect(first.startTime).toBe('18:00')
    expect(first.endTime).toBe('18:00')
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', null)
    await nextTick()
    expect(first.startDate).toBe('')
    dates[0]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    expect(first.startDate).toBe('2026-09-02')
    await add.trigger('click')
    Object.assign(expense.subsidyTrips[0]!.excludedPeriods![1]!, {
      startDate: '2026-09-03', endDate: '2026-09-03', endTime: '18:00',
    })
    expect(expense.tripPayloads()[0]?.excludedPeriods).toHaveLength(2)
    expect(expense.subsidyTrips[1]?.excludedPeriods).toBeUndefined()
    await wrapper.get('[aria-label="补助 1 删除非补助时段 1"]').trigger('click')
    expect(expense.tripPayloads()[0]?.excludedPeriods).toHaveLength(1)
    expense.syncSubsidyApprovals(approvals)
    expect(expense.subsidyTrips[0]?.excludedPeriods).toHaveLength(1)
    const readonlyWrapper = mountCard('business', approvals, true, mobile)
    expect(readonlyWrapper.findComponent({ name: 'ElForm' }).props('disabled')).toBe(true)
    await readonlyWrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
    expect(expense.subsidyTrips[0]?.excludedPeriods).toHaveLength(1)
  })

  it.each([false, true])('blocks overlapping absences across merged approvals until repaired, mobile=%s', async (mobile) => {
    const selected = [approvals[0]!, { ...approvals[1]!, startDate: '2026-09-02' }]
    const expense = useExpenseStore()
    const wrapper = mountCard('business', selected, false, mobile)
    expense.subsidyTrips[0]!.excludedPeriods = [{
      startDate: '2026-09-01', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00',
    }]
    expense.subsidyTrips[1]!.excludedPeriods = [{
      startDate: '2026-09-02', startTime: '18:00', endDate: '2026-09-03', endTime: '09:00',
    }]
    await nextTick()
    expect(wrapper.get('.field-error').text()).toContain('非补助时段 1 与时段 2 重叠')
    expect(expense.tripPayloads()).toEqual([])
    await expense.refreshCalculations()
    expect(calculateTotals).not.toHaveBeenCalled()
    expect(wrapper.find('.subsidy-preview').exists()).toBe(false)
    const second = expense.subsidyTrips[1]!.excludedPeriods![0]!
    second.startDate = '2026-09-03'
    second.endDate = '2026-09-05'
    await nextTick()
    expect(expense.policyInputError).toBe('')
    expect(expense.tripPayloads()).toHaveLength(2)
    expect(wrapper.find('.field-error').exists()).toBe(false)
    second.startTime = '09:00'
    await nextTick()
    expect(wrapper.get('.field-error').text()).toContain('非补助时段 1 与时段 2 重叠')
    second.startTime = '18:00'
    await nextTick()
    expect(expense.policyInputError).toBe('')
  })

  it.each([false, true])('preserves the frozen result for readonly historical overlapping absences, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('business', approvals, true, mobile)
    const excluded = { startDate: '2026-09-01', startTime: '09:00', endDate: '2026-09-03', endTime: '18:00' }
    expense.subsidyTrips[0]!.excludedPeriods = [{ ...excluded }, { ...excluded }]
    expense.totals = {
      expenseTotal: '0.00', subsidyTotal: '100.00', totalAmount: '100.00',
      receiptCount: 0, uppercaseAmount: '壹佰元整', subsidy: null,
      subsidies: [{
        relatedApprovalId: 'approval-1', tripType: 'business', calendarDays: 3,
        effectiveDays: '1.0', excludedDays: '2.0', dailyRate: '100.00', total: '100.00',
      }],
    }
    await nextTick()
    expect(wrapper.get('.subsidy-preview').text()).toContain('补助 ¥100.00')
    expect(wrapper.find('.field-error').exists()).toBe(false)
    expect(calculateTotals).not.toHaveBeenCalled()
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

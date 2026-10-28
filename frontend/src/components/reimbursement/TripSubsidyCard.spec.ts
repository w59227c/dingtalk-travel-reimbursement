import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
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
    expect(wrapper.findAllComponents({ name: 'ElDatePicker' })).toHaveLength(4)
    expect(wrapper.findAll('details.subsidy-help')).toHaveLength(1)
    expect(wrapper.find('.subsidy-help').attributes('open')).toBeUndefined()
    expect(wrapper.text()).not.toContain('中间完整日期不计补助')
    expect(wrapper.findAll('input[type="radio"]')).toHaveLength(8)
  })

  it('uses a full-width approval heading in the explicit mobile presentation', () => {
    const expense = useExpenseStore()
    const selected = [{
      processInstanceId: 'approval-long',
      title: '合成员工提交的境内出差申请',
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
    expect(wrapper.get('.subsidy-approval-title').text()).toBe('合成员工提交的境内出差申请')
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

  it.each([false, true])('does not require a policy acknowledgement for same-city projects, mobile=%s', (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('same_city_project', approvals, false, mobile)
    expect(wrapper.text()).not.toContain('确认有效天数')
    expect(wrapper.findComponent({ name: 'ElCheckbox' }).exists()).toBe(false)
    expect(wrapper.text()).not.toContain('我已按公司现行制度确认本项补助')
    expect(expense.policyInputError).toBe('')
    expect(expense.tripPayloads()).toHaveLength(2)
    expect(expense.tripPayloads().every((trip) => trip.policyConfirmed === false)).toBe(true)
  })

  it.each([false, true])('uses attendance dates for city travel and retains outside selections for manual correction, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, endDate: '2026-09-07' }]
    const wrapper = mountCard('same_city_project', selected, false, mobile)
    const expense = useExpenseStore()
    expect(wrapper.find('input[type=radio]').exists()).toBe(false)
    expect(wrapper.find('.excluded-periods').exists()).toBe(false)
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07'])
    await wrapper.get('[aria-label="补助 1 出勤日期 2026-09-05"]').trigger('click')
    expect(expense.tripPayloads()[0]!.attendanceDates).toContain('2026-09-05')
    await wrapper.get('[aria-label="补助 1 出勤日期 2026-09-01"]').trigger('click')
    expect(expense.tripPayloads()[0]!.attendanceDates).not.toContain('2026-09-01')
    wrapper.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-09-03')
    await nextTick()
    expect(expense.subsidyTrips[0]!.attendanceDates).toContain('2026-09-02')
    expect(expense.tripPayloads()).toEqual([])
    expect(wrapper.text()).toContain('以下已选日期需调整')
    await wrapper.get('[aria-label="补助 1 出勤日期 取消 2026-09-02"]').trigger('click')
    expect(expense.policyInputError).toBe('')
    expect(expense.tripPayloads()[0]!.attendanceDates).toContain('2026-09-05')
    const locked = mountCard('same_city_project', selected, true, mobile)
    const saved = JSON.stringify(expense.subsidyTrips)
    await locked.get('[aria-label="补助 1 出勤日期 2026-09-06"]').trigger('click')
    expect(JSON.stringify(expense.subsidyTrips)).toBe(saved)
  })

  it.each([false, true])('preserves the half-day presentation of locked legacy city snapshots, mobile=%s', async (mobile) => {
    const wrapper = mountCard('same_city_project', approvals, true, mobile)
    const expense = useExpenseStore()
    for (const trip of expense.subsidyTrips) trip.attendanceDates = undefined
    await nextTick()
    const saved = JSON.stringify(expense.subsidyTrips)
    expect(wrapper.find('.attendance-picker').exists()).toBe(false)
    expect(wrapper.find('input[type=radio]').exists()).toBe(true)
    expect(wrapper.find('.excluded-period__issue').exists()).toBe(false)
    wrapper.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    expect(JSON.stringify(expense.subsidyTrips)).toBe(saved)
  })

  it.each([false, true])('defaults from the uploaded calendar without replacing existing choices, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    expense.attendanceCalendar = { years: [2026], days: { '2026-09-20': { working: true, name: '补班' }, '2026-09-25': { working: false, name: '休息' } } }
    const selected = [{ ...approvals[0]!, startDate: '2026-09-20', endDate: '2026-09-27' }]
    const wrapper = mountCard('same_city_project', selected, false, mobile)
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'])
    await wrapper.get('[aria-label="补助 1 出勤日期 2026-09-25"]').trigger('click')
    expect(expense.tripPayloads()[0]!.attendanceDates).toContain('2026-09-25')
    const saved = [...expense.subsidyTrips[0]!.attendanceDates!]
    expense.attendanceCalendar.days['2026-09-21'] = { working: false, name: '调整' }
    expense.syncSubsidyApprovals(selected)
    await nextTick()
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(saved)
  })
  it.each([false, true])('shows and applies known partial-year arrangements in both modes, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    expense.attendanceCalendar = { years: [2027], days: {
      '2026-09-20': { working: true, name: '合成跨年补班' },
      '2026-09-25': { working: false, name: '合成跨年休息' },
    } }
    const wrapper = mountCard('same_city_project', [{ ...approvals[0]!, startDate: '2026-09-20', endDate: '2026-09-27' }], false, mobile)
    expect(expense.subsidyTrips[0]!.attendanceDates).toEqual(['2026-09-20', '2026-09-21', '2026-09-22', '2026-09-23', '2026-09-24'])
    expect(wrapper.text()).toContain('2026 年尚未完整维护节假日')
    expect(wrapper.get('[aria-label="补助 1 出勤日期 2026-09-20"]').text()).toContain('班')
    expect(wrapper.get('[aria-label="补助 1 出勤日期 2026-09-25"]').attributes('title')).toContain('合成跨年休息（放假）')
    await wrapper.get('[aria-label="补助 1 出勤日期 2026-09-25"]').trigger('click')
    expect(expense.tripPayloads()[0]!.attendanceDates).toContain('2026-09-25')
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
    const editor = wrapper.get('.excluded-period-editor')
    const leaving = editor.get('[aria-label="补助 1 非补助时段 1 离开"]')
    const returning = editor.get('[aria-label="补助 1 非补助时段 1 返回"]')
    expect(editor.findAll('.subsidy-endpoint-controls')).toHaveLength(2)
    expect(leaving.findAll('input[type="radio"]')).toHaveLength(2)
    expect(returning.findAll('input[type="radio"]')).toHaveLength(2)
    const dates = [leaving, returning].map((endpoint) => endpoint.findComponent({ name: 'ElDatePicker' }))
    for (const date of dates) {
      expect(date.props('popperClass')).toBe(mobile ? 'excluded-period-picker--mobile' : undefined)
    }
    dates[0]!.vm.$emit('update:modelValue', '2026-09-01')
    dates[1]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    expect(first.startDate).toBe('2026-09-01')
    expect(first.endDate).toBe('2026-09-02')
    await leaving.get('[aria-label="补助 1 非补助时段 1 离开时段"] input[value="afternoon"]').setValue()
    await returning.get('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="afternoon"]').setValue()
    expect(first.startTime).toBe('18:00')
    expect(first.endTime).toBe('18:00')
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
    const saved = JSON.stringify(expense.subsidyTrips)
    const locked = readonlyWrapper.get('.excluded-period-editor')
    locked.findComponent({ name: 'ElDatePicker' }).vm.$emit('update:modelValue', '2026-09-01')
    locked.findComponent({ name: 'ElRadioGroup' }).vm.$emit('update:modelValue', 'afternoon')
    await nextTick()
    expect(JSON.stringify(expense.subsidyTrips)).toBe(saved)
  })

  it.each([false, true])('limits absence calendars to their current subsidy window and updates after edits and reset, mobile=%s', async (mobile) => {
    const selected = [
      { ...approvals[0]!, endDate: '2026-09-10' },
      { ...approvals[1]!, startDate: '2026-09-12', endDate: '2026-09-14' },
    ]
    const wrapper = mountCard('business', selected, false, mobile)
    const first = wrapper.findAll('.subsidy-item')[0]!
    const boundaries = first.findAllComponents({ name: 'ElDatePicker' })
    boundaries[0]!.vm.$emit('update:modelValue', '2026-09-03')
    await nextTick()
    boundaries[1]!.vm.$emit('update:modelValue', '2026-09-08')
    await nextTick()
    await first.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
    function expectRange(start: number, end: number) {
      const dates = first.get('.excluded-period-editor').findAllComponents({ name: 'ElDatePicker' })
      for (const picker of dates) {
        const disabled = picker.props('disabledDate') as (date: Date) => boolean
        expect(picker.props('defaultValue')).toEqual(new Date(2026, 8, start))
        expect(disabled(new Date(2026, 8, start - 1))).toBe(true)
        expect(disabled(new Date(2026, 8, start))).toBe(false)
        expect(disabled(new Date(2026, 8, end, 23, 59))).toBe(false)
        expect(disabled(new Date(2026, 8, end + 1))).toBe(true)
      }
    }
    expectRange(3, 8)
    boundaries[0]!.vm.$emit('update:modelValue', '2026-09-04')
    await nextTick()
    boundaries[1]!.vm.$emit('update:modelValue', '2026-09-07')
    await nextTick()
    expectRange(4, 7)
    await wrapper.get('[aria-label="补助 2 添加非补助时段"]').trigger('click')
    const second = wrapper.findAll('.subsidy-item')[1]!
    for (const picker of second.get('.excluded-period-editor').findAllComponents({ name: 'ElDatePicker' })) {
      const disabled = picker.props('disabledDate') as (date: Date) => boolean
      expect(disabled(new Date(2026, 8, 7))).toBe(true)
      expect(disabled(new Date(2026, 8, 12))).toBe(false)
      expect(disabled(new Date(2026, 8, 14))).toBe(false)
      expect(disabled(new Date(2026, 8, 15))).toBe(true)
    }
    await first.get('[aria-label="补助 1 恢复审批日期"]').trigger('click')
    expectRange(1, 10)
  })

  it.each([false, true])('preserves invalid restored absences for correction and recovers calendars with malformed bounds, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, endDate: '2026-09-10' }]
    const wrapper = mountCard('business', selected, false, mobile)
    const expense = useExpenseStore()
    const trip = expense.subsidyTrips[0]!
    trip.excludedPeriods = [
      { type: 'late_departure', endDate: '2026-09-03', endTime: '09:00' },
      { type: 'early_end', startDate: '2026-09-08', startTime: '18:00' },
      { startDate: '2026-09-02', startTime: '09:00', endDate: '2026-09-05', endTime: '09:00' },
    ]
    await nextTick()
    const absence = trip.excludedPeriods[2]!
    const dates = wrapper.get('.excluded-period-editor').findAllComponents({ name: 'ElDatePicker' })
    expect(dates[0]!.props('modelValue')).toBe('2026-09-02')
    expect(expense.policyInputError).not.toBe('')
    dates[0]!.vm.$emit('update:modelValue', '2026-09-04')
    await nextTick()
    expect(expense.policyInputError).toBe('')
    expect(absence.startDate).toBe('2026-09-04')
    trip.excludedPeriods[0]!.endDate = '2026-02-30'
    await nextTick()
    for (const picker of dates) {
      expect(picker.props('defaultValue')).toBeUndefined()
      const disabled = picker.props('disabledDate') as (date: Date) => boolean
      expect(disabled(new Date(2026, 8, 4))).toBe(true)
    }
    await wrapper.get('[aria-label="补助 1 恢复审批日期"]').trigger('click')
    expect(trip.excludedPeriods).toEqual([absence])
    for (const picker of dates) {
      const disabled = picker.props('disabledDate') as (date: Date) => boolean
      expect(disabled(new Date(2026, 8, 4))).toBe(false)
    }
  })

  it.each([false, true])('limits return dates and half-days from departure without rewriting old values, mobile=%s', async (mobile) => {
    const wrapper = mountCard('business', [{ ...approvals[0]!, endDate: '2026-09-05' }], false, mobile)
    const expense = useExpenseStore()
    await wrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
    const editor = wrapper.get('.excluded-period-editor')
    const dates = editor.findAllComponents({ name: 'ElDatePicker' })
    const absence = expense.subsidyTrips[0]!.excludedPeriods![0]!
    const returnMorning = () => editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="morning"]')
    const returnAfternoon = () => editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="afternoon"]')
    dates[0]!.vm.$emit('update:modelValue', '2026-09-03')
    await nextTick()
    expect(dates[1]!.props('defaultValue')).toEqual(new Date(2026, 8, 3))
    let disabled = dates[1]!.props('disabledDate') as (date: Date) => boolean
    expect(disabled(new Date(2026, 8, 2))).toBe(true)
    expect(disabled(new Date(2026, 8, 3))).toBe(false)
    expect(disabled(new Date(2026, 8, 4))).toBe(false)
    dates[1]!.vm.$emit('update:modelValue', '2026-09-03')
    await nextTick()
    expect(returnMorning().element.disabled).toBe(false)
    await editor.get('[aria-label="补助 1 非补助时段 1 离开时段"] input[value="afternoon"]').setValue()
    expect(returnMorning().element.disabled).toBe(true)
    expect(returnAfternoon().element.disabled).toBe(false)
    expect(absence.endTime).toBe('09:00')
    expect(wrapper.get('.excluded-period__issue').text()).toContain('返回早于离开')
    expect(expense.tripPayloads()).toEqual([])
    await returnAfternoon().setValue()
    expect(absence.endTime).toBe('18:00')
    expect(expense.policyInputError).toBe('')
    await returnMorning().trigger('click')
    expect(absence.endTime).toBe('18:00')
    dates[0]!.vm.$emit('update:modelValue', '2026-09-04')
    await nextTick()
    expect(absence.endDate).toBe('2026-09-03')
    expect(returnMorning().element.disabled).toBe(true)
    expect(returnAfternoon().element.disabled).toBe(true)
    disabled = dates[1]!.props('disabledDate') as (date: Date) => boolean
    expect(disabled(new Date(2026, 8, 3))).toBe(true)
    expect(disabled(new Date(2026, 8, 4))).toBe(false)
    dates[1]!.vm.$emit('update:modelValue', '2026-09-05')
    await nextTick()
    expect(returnMorning().element.disabled).toBe(false)
    await returnMorning().setValue()
    expect(expense.policyInputError).toBe('')
  })

  it.each([false, true])('restricts absence half-days to the subsidy boundaries, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, startDate: '2026-09-02', endDate: '2026-09-04' }]
    const wrapper = mountCard('business', selected, false, mobile)
    await wrapper.get('[aria-label="补助 1 出发时段"] input[value="afternoon"]').setValue()
    await wrapper.get('[aria-label="补助 1 返回时段"] input[value="morning"]').setValue()
    await wrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
    const editor = wrapper.get('.excluded-period-editor')
    const dates = editor.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-09-02')
    dates[1]!.vm.$emit('update:modelValue', '2026-09-04')
    await nextTick()
    expect(editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 离开时段"] input[value="morning"]').element.disabled).toBe(true)
    expect(editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 离开时段"] input[value="afternoon"]').element.disabled).toBe(false)
    expect(editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="afternoon"]').element.disabled).toBe(true)
    expect(editor.get<HTMLInputElement>('[aria-label="补助 1 非补助时段 1 返回时段"] input[value="morning"]').element.disabled).toBe(false)
    await editor.get('[aria-label="补助 1 非补助时段 1 离开时段"] input[value="afternoon"]').setValue()
    expect(useExpenseStore().policyInputError).toBe('')
  })

  it.each([false, true])('marks only affected absences after changing the subsidy window and keeps manual repair available, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, endDate: '2026-09-10' }]
    const wrapper = mountCard('business', selected, false, mobile)
    const expense = useExpenseStore()
    const trip = expense.subsidyTrips[0]!
    const first = { startDate: '2026-09-02', startTime: '09:00', endDate: '2026-09-03', endTime: '09:00' }
    const second = { startDate: '2026-09-06', startTime: '09:00', endDate: '2026-09-07', endTime: '09:00' }
    trip.excludedPeriods = [first, second]
    await nextTick()
    wrapper.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-09-04')
    await nextTick()
    const periods = wrapper.findAll('.excluded-period')
    expect(wrapper.get('[aria-label="补助 1 开始日期"]').element).toHaveProperty('value', '2026-09-04')
    expect(periods[0]!.classes()).toContain('excluded-period--invalid')
    expect(periods[0]!.text()).toContain('超出补助时段，请修改')
    expect(periods[1]!.classes()).not.toContain('excluded-period--invalid')
    expect(first.startDate).toBe('2026-09-02')
    expect(second.endDate).toBe('2026-09-07')
    expect(expense.tripPayloads()).toEqual([])
    expect(wrapper.findAll('button').some((button) => /清除|一键/.test(button.text()))).toBe(false)
    const dates = periods[0]!.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-09-04')
    dates[1]!.vm.$emit('update:modelValue', '2026-09-05')
    await nextTick()
    expect(wrapper.find('.excluded-period--invalid').exists()).toBe(false)
    expect(expense.tripPayloads()[0]?.excludedPeriods?.filter((item) => !item.type)).toHaveLength(2)
    await wrapper.get('[aria-label="补助 1 出发时段"] input[value="afternoon"]').setValue()
    expect(wrapper.findAll('.excluded-period')[0]!.text()).toContain('超出补助时段')
    expect(trip.excludedPeriods?.filter((item) => !item.type)).toHaveLength(2)
    const saved = JSON.stringify(expense.subsidyTrips)
    const locked = mountCard('business', selected, true, mobile)
    expect(locked.find('.excluded-period__issue').exists()).toBe(false)
    locked.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-09-01')
    await nextTick()
    expect(JSON.stringify(expense.subsidyTrips)).toBe(saved)
  })

  describe.each([false, true])('calendar edge cases, mobile=%s', (mobile) => {
    it.each([
      { name: 'cross-year', approvalStart: '2026-12-28', approvalEnd: '2027-01-05', start: '2026-12-31', end: '2027-01-02', before: '2026-12-30', after: '2027-01-03' },
      { name: 'leap-day', approvalStart: '2028-02-27', approvalEnd: '2028-03-02', start: '2028-02-29', end: '2028-03-01', before: '2028-02-28', after: '2028-03-02' },
      { name: 'single-day', approvalStart: '2026-09-01', approvalEnd: '2026-09-03', start: '2026-09-02', end: '2026-09-02', before: '2026-09-01', after: '2026-09-03' },
    ])('keeps inclusive local calendar boundaries for $name', async ({ approvalStart, approvalEnd, start, end, before, after }) => {
      const wrapper = mountCard('business', [{ ...approvals[0]!, startDate: approvalStart, endDate: approvalEnd }], false, mobile)
      const boundaries = wrapper.findAllComponents({ name: 'ElDatePicker' })
      boundaries[0]!.vm.$emit('update:modelValue', start)
      await nextTick()
      boundaries[1]!.vm.$emit('update:modelValue', end)
      await nextTick()
      await wrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
      for (const picker of wrapper.get('.excluded-period-editor').findAllComponents({ name: 'ElDatePicker' })) {
        const disabled = picker.props('disabledDate') as (date: Date) => boolean
        expect(picker.props('defaultValue')).toEqual(new Date(`${start}T00:00:00`))
        expect(disabled(new Date(`${before}T23:59:59`))).toBe(true)
        expect(disabled(new Date(`${start}T00:00:00`))).toBe(false)
        expect(disabled(new Date(`${end}T23:59:59`))).toBe(false)
        expect(disabled(new Date(`${after}T00:00:00`))).toBe(true)
      }
    })

    it('preserves an open calendar month across unrelated parent renders', async () => {
      const wrapper = mountCard('business', [{ ...approvals[0]!, endDate: '2026-11-30' }], false, mobile)
      await wrapper.get('[aria-label="补助 1 添加非补助时段"]').trigger('click')
      const pickers = wrapper.get('.excluded-period-editor').findAllComponents({ name: 'ElDatePicker' })
      pickers[0]!.vm.$emit('update:modelValue', '2026-10-15')
      await nextTick()
      expect(pickers[1]!.props('defaultValue')).toEqual(new Date(2026, 9, 15))
      for (const picker of pickers) {
        const initial = picker.props('defaultValue')
        await picker.vm.handleOpen()
        await flushPromises()
        const panel = Array.from(document.body.querySelectorAll<HTMLElement>('.el-date-picker'))
          .find((element) => element.style.display !== 'none' && element.closest<HTMLElement>('.el-popper')?.style.display !== 'none')!
        const before = panel.querySelector('.el-date-picker__header')!.textContent
        const next = panel.querySelector<HTMLButtonElement>('button[aria-label="Next Month"]')!
        next.click()
        await flushPromises()
        const navigated = panel.querySelector('.el-date-picker__header')!.textContent
        expect(navigated).not.toBe(before)
        wrapper.vm.$forceUpdate()
        await flushPromises()
        expect(picker.props('defaultValue')).toBe(initial)
        expect(panel.querySelector('.el-date-picker__header')!.textContent).toBe(navigated)
        await picker.vm.handleClose()
        await flushPromises()
      }
    })
  })

  it.each([false, true])('edits one-way adjustments, retains data and locks the same controls, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, endDate: '2026-09-10' }]
    const expense = useExpenseStore()
    const wrapper = mountCard('long_term_project', selected, false, mobile)
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    dates[0]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    await wrapper.get('[aria-label="补助 1 出发时段"] input[value="afternoon"]').setValue()
    dates[1]!.vm.$emit('update:modelValue', '2026-09-09')
    await nextTick()
    await wrapper.get('[aria-label="补助 1 返回时段"] input[value="morning"]').setValue()
    expect(wrapper.findAll('.excluded-period')).toHaveLength(0)
    expect(wrapper.text()).toContain('审批日期：2026-09-01 至 2026-09-10')
    expect(expense.policyInputError).toBe('')
    expect(expense.tripPayloads()[0]?.excludedPeriods).toEqual([
      { type: 'late_departure', endDate: '2026-09-02', endTime: '18:00' },
      { type: 'early_end', startDate: '2026-09-09', startTime: '09:00' },
    ])
    const beforeInvalid = JSON.stringify(expense.subsidyTrips)
    dates[0]!.vm.$emit('update:modelValue', '2026-08-31')
    await nextTick()
    expect(wrapper.text()).toContain('补助日期必须在审批日期范围内')
    expect(JSON.stringify(expense.subsidyTrips)).toBe(beforeInvalid)
    dates[0]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    expect(wrapper.text()).not.toContain('补助日期必须在审批日期范围内')
    const saved = JSON.parse(JSON.stringify(expense.subsidyTrips))
    expense.setSubsidyIncluded(false)
    expense.setSubsidyIncluded(true)
    expect(expense.subsidyTrips).toEqual(saved)
    const locked = mountCard('long_term_project', selected, true, mobile)
    const before = JSON.parse(JSON.stringify(expense.subsidyTrips))
    locked.findAllComponents({ name: 'ElDatePicker' })[0]!.vm.$emit('update:modelValue', '2026-09-03')
    await nextTick()
    expect(expense.subsidyTrips).toEqual(before)
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

  it.each([false, true])('toggles from the card header without losing subsidy periods, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('business', approvals, false, mobile)
    expense.subsidyTrips[0]!.startTime = '18:00'
    expense.subsidyTrips[0]!.excludedPeriods = [{
      startDate: '2026-09-02', startTime: '09:00', endDate: '2026-09-03', endTime: '09:00',
    }]
    expense.subsidyTrips[1]!.endTime = '09:00'
    await nextTick()
    const saved = JSON.parse(JSON.stringify(expense.subsidyTrips))
    const header = wrapper.get('.el-card__header')
    const toggle = header.get('[role="switch"]')
    expect(header.text()).toBe('出差补助（可选）')
    expect(toggle.attributes('aria-label')).toBe('申请出差补助')
    expect(wrapper.find('.el-form [role="switch"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('是否申请出差补助')

    await toggle.trigger('click')
    expect(expense.includeSubsidy).toBe(false)
    expect(wrapper.find('.subsidy-item').exists()).toBe(false)
    expect(expense.subsidyTrips).toEqual(saved)
    expect(expense.tripPayloads()).toEqual([])

    await toggle.trigger('click')
    expect(expense.includeSubsidy).toBe(true)
    expect(wrapper.findAll('.subsidy-item')).toHaveLength(2)
    expect(expense.subsidyTrips).toEqual(saved)
    expect(wrapper.get('[aria-label="补助 1 非补助时段 1 离开日期"]').element)
      .toHaveProperty('value', '2026-09-02')
    expect(expense.tripPayloads()).toHaveLength(2)
  })

  it.each([false, true])('locks all subsidy controls after submission, mobile=%s', async (mobile) => {
    const expense = useExpenseStore()
    const wrapper = mountCard('business', approvals, true, mobile)
    expect(wrapper.findComponent({ name: 'ElForm' }).props('disabled')).toBe(true)
    const toggle = wrapper.get('.el-card__header [role="switch"]')
    expect(toggle.attributes('aria-disabled')).toBe('true')
    await toggle.trigger('click')
    expect(expense.includeSubsidy).toBe(true)
  })
  it.each([false, true])('repairs an invalid saved window and protects locked boundaries, mobile=%s', async (mobile) => {
    const selected = [{ ...approvals[0]!, endDate: '2026-09-06' }]
    const expense = useExpenseStore()
    const wrapper = mountCard('business', selected, false, mobile)
    expense.subsidyTrips[0]!.excludedPeriods = [
      { type: 'late_departure', endDate: '2026-08-31', endTime: '09:00' },
      { type: 'early_end', startDate: '2026-09-07', startTime: '18:00' },
    ]
    await nextTick()
    expect(expense.tripPayloads()).toEqual([])
    const dates = wrapper.findAllComponents({ name: 'ElDatePicker' })
    expect(dates[0]!.props('popperClass')).toBe(mobile ? 'excluded-period-picker--mobile' : undefined)
    expect(dates[1]!.props('popperClass')).toBe(mobile ? 'excluded-period-picker--mobile' : undefined)
    await wrapper.get('[aria-label="补助 1 恢复审批日期"]').trigger('click')
    expect(expense.tripPayloads()).toHaveLength(1)
    expect(expense.subsidyTrips[0]!.excludedPeriods).toEqual([])
    dates[0]!.vm.$emit('update:modelValue', '2026-09-02')
    await nextTick()
    expect(expense.tripPayloads()[0]!.excludedPeriods![0]!.endDate).toBe('2026-09-02')
    const locked = mountCard('business', selected, true, mobile)
    const saved = JSON.stringify(expense.subsidyTrips)
    locked.findAllComponents({ name: 'ElButton' }).find((button) => button.text() === '恢复审批日期')!.vm.$emit('click')
    expect(JSON.stringify(expense.subsidyTrips)).toBe(saved)
  })

  it.each([false, true])('groups dates and periods by boundary in both presentations, mobile=%s', (mobile) => {
    const wrapper = mountCard('business', approvals, false, mobile)
    const item = wrapper.findAll('.subsidy-item')[0]!
    const heading = item.get('.subsidy-item__heading')
    expect(item.findAllComponents({ name: 'ElDatePicker' })).toHaveLength(2)
    expect(item.findAll('input[type="radio"]')).toHaveLength(4)
    expect(heading.findComponent({ name: 'ElDatePicker' }).exists()).toBe(false)
    expect(heading.text()).toContain('审批日期：2026-09-01 至 2026-09-03')
    const start = item.get('[aria-label="补助 1 补助开始"]')
    const end = item.get('[aria-label="补助 1 补助结束"]')
    expect(start.findComponent({ name: 'ElDatePicker' }).props('modelValue')).toBe('2026-09-01')
    expect(start.find('[aria-label="补助 1 出发时段"]').exists()).toBe(true)
    expect(end.findComponent({ name: 'ElDatePicker' }).props('modelValue')).toBe('2026-09-03')
    expect(end.find('[aria-label="补助 1 返回时段"]').exists()).toBe(true)
  })

})

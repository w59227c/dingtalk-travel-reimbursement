import ElementPlus from 'element-plus'
import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { DefineComponent } from 'vue'
import type { AttendanceCalendar } from '@/types/attendance'

import AttendanceDatesPicker from './AttendanceDatesPicker.vue'

enableAutoUnmount(afterEach)
function picker(dates: string[] | undefined = ['2026-09-01', '2026-09-02']) {
  // The native TypeScript pass uses the generic *.vue shim; keep props typed there too.
  const component = AttendanceDatesPicker as DefineComponent<{
    modelValue?: readonly string[]
    startDate: string
    endDate: string
    label: string
    readonly?: boolean
    calendar?: AttendanceCalendar
  }>
  return mount(component, { props: { modelValue: dates, startDate: '2026-09-01', endDate: '2026-09-07', label: '合成出勤日期', calendar: { years: [2026], days: {} } }, global: { plugins: [ElementPlus] } })
}

describe('attendance calendar', () => {
  it('unblocks after refreshing a missing year without replacing existing selections', async () => {
    const wrapper = picker(['2026-09-05'])
    await wrapper.setProps({ calendar: { years: [], days: {} } })
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-05"]').attributes('disabled')).toBeDefined()
    expect(wrapper.findAll('button').find((button) => button.text() === '重新检查')).toBeDefined()
    await wrapper.setProps({ calendar: { years: [2026], days: {} } })
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-05"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-05"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
  it('allows selecting weekends and deselecting weekdays, with readonly protection', async () => {
    const wrapper = picker()
    await wrapper.get('[aria-label="合成出勤日期 2026-09-05"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual(['2026-09-01', '2026-09-02', '2026-09-05'])
    await wrapper.setProps({ modelValue: ['2026-09-01', '2026-09-02', '2026-09-05'] })
    await wrapper.get('[aria-label="合成出勤日期 2026-09-01"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual(['2026-09-02', '2026-09-05'])
    await wrapper.setProps({ readonly: true })
    const count = wrapper.emitted('update:modelValue')!.length
    await wrapper.get('[aria-label="合成出勤日期 2026-09-06"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toHaveLength(count)
  })

  it('keeps outside selections cancellable and does not automatically select expanded dates', async () => {
    const wrapper = picker()
    await wrapper.setProps({ startDate: '2026-09-03' })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-01"]').attributes('disabled')).toBeUndefined()
    await wrapper.get('[aria-label="合成出勤日期 取消 2026-09-01"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual(['2026-09-02'])
    await wrapper.setProps({ modelValue: ['2026-09-02'], startDate: '2026-09-01', endDate: '2026-09-10' })
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-08"]').attributes('aria-pressed')).toBe('false')
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-11"]').attributes('disabled')).toBeDefined()
  })

  it('marks uploaded exceptions, permits holiday attendance and explains missing years without changing selections', async () => {
    const wrapper = picker()
    await wrapper.setProps({ calendar: { years: [2026], days: { '2026-09-01': { working: false, name: '休息日' }, '2026-09-05': { working: true, name: '补班日' } } } })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-01"]').text()).toContain('休')
    expect(wrapper.get('[aria-label="合成出勤日期 2026-09-05"]').text()).toContain('班')
    await wrapper.setProps({ modelValue: [] })
    await wrapper.get('[aria-label="合成出勤日期 2026-09-01"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual(['2026-09-01'])
    await wrapper.setProps({ startDate: '2026-12-31', endDate: '2027-01-04' })
    expect(wrapper.text()).toContain('2027 年节假日与调休安排尚未完整维护')
  })

  it('only initializes a legacy selection on an explicit action', async () => {
    const wrapper = picker(undefined)
    await wrapper.setProps({ modelValue: undefined })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
    await wrapper.findAll('button').find((button) => button.text() === '带入默认工作日')!.trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual(['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07'])
  })
  it('marks known cross-year exceptions even when the adjacent year is not fully maintained', async () => {
    const wrapper = picker(undefined)
    await wrapper.setProps({ modelValue: undefined, startDate: '2025-12-26', endDate: '2025-12-31', calendar: { years: [2026], days: {
      '2025-12-27': { working: true, name: '合成跨年补班' },
      '2025-12-31': { working: false, name: '合成跨年放假' },
    } } })
    expect(wrapper.text()).toContain('2025 年节假日与调休安排尚未完整维护')
    expect(wrapper.get('[aria-label="合成出勤日期 2025-12-27"]').text()).toContain('班')
    expect(wrapper.get('[aria-label="合成出勤日期 2025-12-31"]').attributes('title')).toContain('合成跨年放假（放假）')
    expect(wrapper.findAll('button').find((button) => button.text() === '带入默认工作日')).toBeUndefined()
    await wrapper.findAll('button').find((button) => button.text() === '重新检查')!.trigger('click')
    expect(wrapper.emitted('refresh')).toHaveLength(1)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
})

import ElementPlus from 'element-plus'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { DefineComponent } from 'vue'
import type { AttendanceCalendarCorrection } from '@/types/attendance'
import CalendarCorrectionEditor from './CalendarCorrectionEditor.vue'

enableAutoUnmount(afterEach)
const value: AttendanceCalendarCorrection = { year: 2042, ranges: [{ name: '合成元旦', startDate: '2042-01-01', endDate: '2042-01-03', working: false }] }
const component = CalendarCorrectionEditor as DefineComponent<{ modelValue: AttendanceCalendarCorrection; disabled?: boolean; mobile?: boolean; yearLocked?: boolean }>
describe.each([false, true])('correction fields, mobile=%s', (mobile) => {
  it('locks only the source year when editing a saved annual calendar', () => {
    const wrapper = mount(component, { props: { modelValue: structuredClone(value), mobile, yearLocked: true }, global: { plugins: [ElementPlus] } })
    expect(wrapper.get('input[aria-label="修正安排的适用年份"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('input[aria-label="安排 1 节日名称"]').attributes('disabled')).toBeUndefined()
    expect(wrapper.findAllComponents({ name: 'ElDatePicker' }).every((picker) => !picker.props('disabled'))).toBe(true)
    expect(wrapper.get('[aria-label="删除修正安排 1"]').attributes('disabled')).toBeUndefined()
  })
  it('edits names, adds and removes ranges without mutating the supplied preview', async () => {
    const input = structuredClone(value)
    const wrapper = mount(component, { props: { modelValue: input, mobile }, global: { plugins: [ElementPlus] } })
    await wrapper.get('input[aria-label="安排 1 节日名称"]').setValue('春节')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual({ year: 2042, ranges: [{ ...value.ranges[0]!, name: '春节' }] })
    await wrapper.findAll('button').find((button) => button.text() === '添加日期安排')!.trigger('click')
    expect((wrapper.emitted('update:modelValue')!.at(-1)![0] as AttendanceCalendarCorrection).ranges).toHaveLength(2)
    await wrapper.get('[aria-label="删除修正安排 1"]').trigger('click')
    expect(wrapper.emitted('update:modelValue')!.at(-1)![0]).toEqual({ year: 2042, ranges: [] })
    expect(input).toEqual(value)
    expect(wrapper.get('.calendar-correction-editor').classes().includes('is-mobile')).toBe(mobile)
  })
  it('constrains adjacent years and return dates, supports clearing dates, and disables fields during requests', async () => {
    const wrapper = mount(component, { props: { modelValue: structuredClone(value), mobile }, global: { plugins: [ElementPlus] } })
    const pickers = wrapper.findAllComponents({ name: 'ElDatePicker' })
    const startDisabled = pickers[0]!.props('disabledDate')
    const endDisabled = pickers[1]!.props('disabledDate')
    expect(startDisabled(new Date(2040, 11, 31))).toBe(true)
    expect(startDisabled(new Date(2041, 11, 31))).toBe(false)
    expect(startDisabled(new Date(2044, 0, 1))).toBe(true)
    expect(endDisabled(new Date(2041, 11, 31))).toBe(true)
    expect(endDisabled(new Date(2042, 0, 1))).toBe(false)
    pickers[0]!.vm.$emit('update:modelValue', null); await flushPromises()
    expect((wrapper.emitted('update:modelValue')!.at(-1)![0] as AttendanceCalendarCorrection).ranges[0]!.startDate).toBe('')
    await wrapper.setProps({ disabled: true })
    expect(pickers.every((picker) => picker.props('disabled'))).toBe(true)
    expect(wrapper.get('[aria-label="删除修正安排 1"]').attributes('disabled')).toBeDefined()
    await wrapper.setProps({ disabled: false, modelValue: { ...value, year: 0 } })
    expect(pickers.every((picker) => picker.props('disabled'))).toBe(true)
  })
  it('preserves an open calendar month when unrelated fields or parent rendering change', async () => {
    const wrapper = mount(component, { props: { modelValue: structuredClone(value), mobile }, global: { plugins: [ElementPlus] } })
    const pickers = wrapper.findAllComponents({ name: 'ElDatePicker' })
    for (const picker of pickers) {
      const initial = picker.props('defaultValue')
      await picker.vm.handleOpen(); await flushPromises()
      const panel = Array.from(document.body.querySelectorAll<HTMLElement>('.el-date-picker'))
        .find((element) => element.style.display !== 'none' && element.closest<HTMLElement>('.el-popper')?.style.display !== 'none')!
      const header = panel.querySelector('.el-date-picker__header')!
      const before = header.textContent
      panel.querySelector<HTMLButtonElement>('button[aria-label="下个月"]')!.click(); await flushPromises()
      const navigated = header.textContent
      expect(navigated).not.toBe(before)
      await wrapper.setProps({ modelValue: { ...value, ranges: [{ ...value.ranges[0]!, name: '合成新名称' }] } })
      wrapper.vm.$forceUpdate(); await flushPromises()
      expect(picker.props('defaultValue')).toBe(initial)
      expect(header.textContent).toBe(navigated)
      await picker.vm.handleClose(); await flushPromises()
    }
  })
})

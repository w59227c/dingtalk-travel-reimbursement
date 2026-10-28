import { enableAutoUnmount, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'
import type { DefineComponent } from 'vue'
import type { AttendanceCalendarDay } from '@/types/attendance'
import CalendarArrangementPreview from './CalendarArrangementPreview.vue'

enableAutoUnmount(afterEach)
const days: AttendanceCalendarDay[] = [
  { date: '2042-01-01', working: false, name: '合成元旦' },
  { date: '2042-05-31', working: false, name: '合成假期' },
  { date: '2042-06-01', working: false, name: '合成假期' },
  { date: '2042-06-02', working: false, name: '合成假期' },
  { date: '2042-06-07', working: true, name: '合成补班' },
]
// Keep prop updates typed when the native TypeScript pass uses the generic *.vue shim.
const calendarComponent = CalendarArrangementPreview as DefineComponent<{ year: number; days: AttendanceCalendarDay[]; showDetails?: boolean; mobile?: boolean; month?: number }>
describe.each([false, true])('calendar navigation, mobile=%s', (mobile) => {
  it('offers all months directly and bounded previous/next navigation without changing saved dates', async () => {
    const input = structuredClone(days)
    const wrapper = mount(CalendarArrangementPreview, { props: { year: 2042, days: input, mobile } })
    const months = wrapper.get('[aria-label="选择查看月份"]')
    expect(months.findAll('button')).toHaveLength(12)
    expect(wrapper.find('select').exists()).toBe(false)
    expect(wrapper.get('[aria-label="上个月"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[aria-label="查看 2042 年 6 月"]').trigger('click')
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 6 月安排，只读')
    expect(wrapper.get('[aria-label="查看 2042 年 6 月"]').attributes('aria-pressed')).toBe('true')
    expect(wrapper.get('.arrangement-grid').findAll('.arrangement-day')).toHaveLength(42)
    expect(wrapper.get('[title="2042-06-07 合成补班 补班"]').classes()).toContain('work')
    expect(wrapper.get('[title="2042-06-01 合成假期 放假"]').classes()).toContain('rest')
    expect(wrapper.get('[title="2042-05-31（其他月份）"]').classes()).not.toContain('rest')
    await wrapper.get('[aria-label="上个月"]').trigger('click')
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 5 月安排，只读')
    await wrapper.get('[aria-label="下个月"]').trigger('click')
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 6 月安排，只读')
    await wrapper.get('[aria-label="查看 2042 年 12 月"]').trigger('click')
    expect(wrapper.get('[aria-label="下个月"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('本月无特殊安排')
    expect(input).toEqual(days)
  })
  it('uses the single annual list as month navigation and highlights ranges across months', async () => {
    const wrapper = mount(CalendarArrangementPreview, { props: { year: 2042, days, mobile } })
    const range = wrapper.get('[aria-label="查看 2042-05-31 至 2042-06-02 放假 合成假期"]')
    await range.trigger('click')
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 5 月安排，只读')
    expect(range.classes()).toContain('active')
    await wrapper.get('[aria-label="下个月"]').trigger('click')
    expect(range.classes()).toContain('active')
    await wrapper.get('[aria-label="查看 2042 年 9 月"]').trigger('click')
    expect(range.classes()).not.toContain('active')
    await range.trigger('click')
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 5 月安排，只读')
    expect(wrapper.classes().includes('is-mobile')).toBe(mobile)
  })
  it('can omit duplicate annual details while retaining month navigation and empty-month guidance', async () => {
    const wrapper = mount(calendarComponent, { props: { year: 2042, days: [], showDetails: false, mobile } })
    expect(wrapper.find('[aria-label="年度日期清单"]').exists()).toBe(false)
    await wrapper.get('[aria-label="查看 2042 年 6 月"]').trigger('click')
    await wrapper.setProps({ days: structuredClone(days) })
    expect(wrapper.get('.arrangement-grid').attributes('aria-label')).toBe('2042 年 6 月安排，只读')
    expect(wrapper.text()).not.toContain('本月无特殊安排')
  })
})

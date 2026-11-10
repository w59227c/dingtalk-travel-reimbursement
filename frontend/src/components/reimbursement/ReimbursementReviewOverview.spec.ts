import ElementPlus from 'element-plus'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReimbursementReviewOverview from './ReimbursementReviewOverview.vue'

describe('reimbursement review overview', () => {
  it.each([false, true])('keeps full project and period details and links to approvals (mobile=%s)', async mobile => {
    const budget = 'LOCAL-001 合成预算项目' + '长项目名称'.repeat(12)
    const wrapper = mount(ReimbursementReviewOverview, {
      props: { mobile, company: '合成公司', department: '合成部门', budget, approvalCount: 3,
        periods: [
          { startDate: '2026-12-30', endDate: '2027-01-02' },
          { startDate: '2027-01-06', endDate: '2027-01-06' },
        ] },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.text()).toContain(budget)
    expect(wrapper.text()).toContain('合成公司')
    expect(wrapper.text()).toContain('合成部门')
    const dates = wrapper.get('.review-overview__dates')
    expect(dates.get('dt').text()).toContain('已关联 3 张审批')
    expect(dates.get('dd').text()).not.toContain('已关联')
    expect(dates.findAll('.review-overview__period').map(period => period.text())).toEqual([
      '2026-12-30 至 2027-01-02', '2027-01-06',
    ])
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('approvals')).toHaveLength(1)
    wrapper.unmount()
  })

  it.each([false, true])('shows truthful empty approval information (mobile=%s)', mobile => {
    const wrapper = mount(ReimbursementReviewOverview, {
      props: { mobile, company: '', department: '合成部门', budget: '', periods: [], approvalCount: 0 },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.find('small').exists()).toBe(false)
    expect(wrapper.get('.review-overview__dates dd').text()).toBe('尚未关联审批')
    expect(wrapper.text()).not.toContain('张审批')
    wrapper.unmount()
  })
})

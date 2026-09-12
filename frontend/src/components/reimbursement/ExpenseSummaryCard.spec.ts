import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type { ReimbursementDraft } from '@/types/reimbursements'
import ExpenseSummaryCard from './ExpenseSummaryCard.vue'

function draft(): ReimbursementDraft {
  return {
    id: 'draft-1',
    status: 'DRAFT',
    revision: 2,
    department: { id: '100', name: '测试部门' },
    templateConfigVersion: 1,
    relatedApprovalCount: 0,
    expiresAt: '2026-10-01T00:00:00Z',
    createdAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:00Z',
    lockedAt: null,
    template: {
      processCode: 'PROC-1',
      configVersion: 1,
      schemaFingerprint: 'a'.repeat(64),
    },
    input: {
      ocrDispositionVersion: 1,
      companyValue: '北京',
      budgetCodeValue: '26007',
      project: { mode: 'manual', text: '测试项目' },
      trip: null,
      dismissedOcrFileIds: [],
      items: [],
    },
    totals: {
      expenseTotal: '0.00',
      subsidyTotal: '0.00',
      totalAmount: '0.00',
      receiptCount: 0,
      uppercaseAmount: '零元整',
      subsidy: null,
    },
    relatedApprovals: [],
    relatedApprovalSummary: null,
  }
}

describe('ExpenseSummaryCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it.each([false, true])('omits uppercase amount before and after calculation (calculated: %s)', (calculated) => {
    const expense = useExpenseStore()
    expense.totals = calculated ? draft().totals : null
    const wrapper = mount(ExpenseSummaryCard, {
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).not.toContain('人民币大写')
    expect(wrapper.text()).not.toContain('待服务端计算')
    expect(wrapper.text()).not.toContain('零元整')
    expect(wrapper.findAll('.totals-grid > div')).toHaveLength(4)
    expect(wrapper.text()).toContain('预览 Excel')
    wrapper.unmount()
  })

  it('downloads only the persisted draft preview and explains final server generation', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const preview = vi.spyOn(drafts, 'downloadExcelPreview').mockResolvedValue(undefined)
    const expense = useExpenseStore()
    expense.totals = draft().totals
    const wrapper = mount(ExpenseSummaryCard, {
      global: { plugins: [ElementPlus] },
    })

    const button = wrapper.find('button')
    expect(button.text()).toContain('预览 Excel')
    await button.trigger('click')

    expect(preview).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('正式提交时生成最终报销单和票据汇总 PDF')
    expect(wrapper.text()).not.toContain('生成并下载 Excel')
    wrapper.unmount()
  })

  it('uses the DingTalk native document flow on mobile instead of a browser link', async () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const preview = vi.spyOn(drafts, 'openExcelPreviewInDingTalk').mockResolvedValue(undefined)
    const wrapper = mount(ExpenseSummaryCard, {
      props: { mobile: true },
      global: { plugins: [ElementPlus] },
    })

    const button = wrapper.get('[data-testid="mobile-excel-preview-button"]')
    expect(button.element.tagName).toBe('BUTTON')
    await button.trigger('click')

    expect(preview).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('blocks a preview while the form differs from the saved draft', () => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const wrapper = mount(ExpenseSummaryCard, {
      props: { previewDisabledReason: '表单有未保存修改，请先保存草稿再预览' },
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.find('button').attributes()).toHaveProperty('disabled')
    expect(wrapper.text()).toContain('表单有未保存修改')
    wrapper.unmount()
  })

  it('leaves subsidy calculation errors in the subsidy card instead of repeating them here', () => {
    const expense = useExpenseStore()
    expense.includeSubsidy = true
    expense.calculationError = '出差补助金额计算失败，请重试'
    const wrapper = mount(ExpenseSummaryCard, {
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).not.toContain('出差补助金额计算失败，请重试')
    wrapper.unmount()
  })
})

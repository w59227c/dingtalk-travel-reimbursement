import ElementPlus, { ElMessage } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
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
    vi.restoreAllMocks()
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

  it('downloads the persisted draft preview', async () => {
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
    expect(wrapper.text()).not.toContain('生成并下载 Excel')
    wrapper.unmount()
  })

  it.each([false, true])('coalesces preview clicks while preparing saved content, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    let ready!: () => void
    const beforePreview = vi.fn(() => new Promise<void>((resolve) => { ready = resolve }))
    const download = vi.spyOn(drafts, mobile ? 'openExcelPreviewInDingTalk' : 'downloadExcelPreview').mockResolvedValue(undefined)
    const wrapper = mount(ExpenseSummaryCard, { props: { mobile, beforePreview }, global: { plugins: [ElementPlus] } })
    await wrapper.get('button').trigger('click')
    await wrapper.get('button').trigger('click')
    expect(beforePreview).toHaveBeenCalledOnce()
    ready()
    await flushPromises()
    expect(download).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it.each([false, true])('drops a prepared preview after changing drafts, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    let ready!: () => void
    const beforePreview = () => new Promise<void>((resolve) => { ready = resolve })
    const download = vi.spyOn(drafts, mobile ? 'openExcelPreviewInDingTalk' : 'downloadExcelPreview').mockResolvedValue(undefined)
    const wrapper = mount(ExpenseSummaryCard, { props: { mobile, beforePreview }, global: { plugins: [ElementPlus] } })
    await wrapper.get('button').trigger('click')
    drafts.currentDraft = { ...draft(), id: 'replacement-draft' }
    ready()
    await flushPromises()
    expect(download).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it.each([false, true])('does not announce an old download after switching drafts, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    let ready!: () => void
    vi.spyOn(drafts, mobile ? 'openExcelPreviewInDingTalk' : 'downloadExcelPreview')
      .mockImplementation(() => new Promise<void>((resolve) => { ready = resolve }))
    const success = vi.spyOn(ElMessage, 'success')
    const wrapper = mount(ExpenseSummaryCard, { props: { mobile }, global: { plugins: [ElementPlus] } })
    await wrapper.get('button').trigger('click')
    drafts.currentDraft = { ...draft(), id: 'replacement-draft' }
    ready()
    await flushPromises()
    expect(success).not.toHaveBeenCalled()
    success.mockRestore()
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

  it.each([false, true])('shows the subsidy error and blocks both preview paths, mobile=%s', async (mobile) => {
    const drafts = useReimbursementDraftStore()
    drafts.currentDraft = draft()
    const download = vi.spyOn(drafts, 'downloadExcelPreview').mockResolvedValue(undefined)
    const open = vi.spyOn(drafts, 'openExcelPreviewInDingTalk').mockResolvedValue(undefined)
    const beforePreview = vi.fn()
    const wrapper = mount(ExpenseSummaryCard, {
      props: { mobile, beforePreview, previewDisabledReason: '请补齐每个非补助时段的日期和出发时段' },
      global: { plugins: [ElementPlus] },
    })
    expect(wrapper.get('button').attributes()).toHaveProperty('disabled')
    expect(wrapper.get('.field-error').text()).toBe('请补齐每个非补助时段的日期和出发时段')
    await wrapper.get('button').trigger('click')
    expect(beforePreview).not.toHaveBeenCalled()
    expect(download).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers recalculation at the final totals when subsidy calculation fails', () => {
    const expense = useExpenseStore()
    expense.includeSubsidy = true
    expense.calculationError = '出差补助金额计算失败，请重试'
    const wrapper = mount(ExpenseSummaryCard, {
      global: { plugins: [ElementPlus] },
    })

    expect(wrapper.text()).toContain('出差补助金额计算失败，请重试')
    expect(wrapper.get('.grand-total strong').text()).toBe('待计算')
    expect(wrapper.findAll('button').some((button) => button.text().trim() === '重新计算')).toBe(true)
    wrapper.unmount()
  })
})

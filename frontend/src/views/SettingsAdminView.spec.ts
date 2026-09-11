import ElementPlus from 'element-plus'
import { ElMessageBox } from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getExpenseCategories } from '@/api/expenses'
import { getOaTemplateCatalog } from '@/api/oaTemplates'
import {
  createReceiptKeyword,
  listReceiptKeywords,
} from '@/api/receiptKeywords'
import { getAdminExpenseSettings } from '@/api/settings'
import SettingsAdminView from './SettingsAdminView.vue'

vi.mock('@/api/expenses', () => ({ getExpenseCategories: vi.fn() }))
vi.mock('@/api/oaTemplates', () => ({
  confirmOaTemplateCatalog: vi.fn(),
  getOaTemplateCatalog: vi.fn(),
  inspectOaTemplateCatalog: vi.fn(),
}))
vi.mock('@/api/receiptKeywords', () => ({
  createReceiptKeyword: vi.fn(),
  deleteReceiptKeyword: vi.fn(),
  listReceiptKeywords: vi.fn(),
  updateReceiptKeyword: vi.fn(),
}))
vi.mock('@/api/settings', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/settings')>()
  return {
    ...actual,
    getAdminExpenseSettings: vi.fn(),
    updateExpenseSettings: vi.fn(),
  }
})

describe('SettingsAdminView receipt keywords', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.mocked(getAdminExpenseSettings).mockResolvedValue({
      appTitle: '智能差旅费报销申请',
      adminUserIds: [],
      environmentAdminUserIds: ['admin-1'],
      subsidyRates: {
        business: '100.00',
        short_term_project: '100.00',
        long_term_project: '150.00',
        same_city_project: '50.00',
        internal: '100.00',
      },
      calculationMode: 'half_day_12',
    })
    vi.mocked(getOaTemplateCatalog).mockResolvedValue({
      configured: false,
      configVersion: null,
      compatibilityStatus: 'UNCONFIGURED',
      isSubmissionReady: false,
      requiresConfirmation: true,
      catalog: null,
    })
    vi.mocked(getExpenseCategories).mockResolvedValue([
      { id: 'office', name: '办公费', order: 1, manualSelectable: true },
      { id: 'other', name: '其他', order: 2, manualSelectable: true },
      { id: 'subsidy', name: '出差补助', order: 3, manualSelectable: false },
    ])
    const builtin = {
      id: 10,
      keyword: '酒店',
      categoryId: 'office' as const,
      categoryName: '办公费',
    }
    vi.mocked(listReceiptKeywords)
      .mockResolvedValueOnce([builtin])
      .mockResolvedValueOnce([
        builtin,
        {
          id: 1,
          keyword: '文具',
          categoryId: 'office',
          categoryName: '办公费',
        },
      ])
    vi.mocked(createReceiptKeyword).mockResolvedValue({
      id: 1,
      keyword: '文具',
      categoryId: 'office',
      categoryName: '办公费',
    })
  })

  it('returns mobile administrators to the mobile reimbursement entry', () => {
    const wrapper = mount(SettingsAdminView, {
      props: { mobile: true },
      global: {
        plugins: [createPinia(), ElementPlus],
        stubs: {
          RouterLink: {
            props: ['to'],
            template: '<a :data-to="to"><slot /></a>',
          },
        },
      },
    })

    const reimbursementLink = wrapper.findAll('a').find((link) => link.text().trim() === '报销单')!
    expect(reimbursementLink.attributes('data-to')).toBe('/m')
    wrapper.unmount()
  })

  it('lets an administrator add a fallback classification keyword', async () => {
    const wrapper = mount(SettingsAdminView, {
      global: {
        plugins: [createPinia(), ElementPlus],
        stubs: { RouterLink: { template: '<a><slot /></a>' } },
      },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('票据分类关键词')
    expect(wrapper.text()).toContain('按费用类别维护关键词')
    expect(wrapper.text()).toContain('未命中或冲突时自动归入')
    expect(wrapper.text()).toContain('钉钉 OA 模板目录')
    expect(wrapper.text().indexOf('票据分类关键词'))
      .toBeLessThan(wrapper.text().indexOf('每日补助标准'))
    const officeGroup = wrapper.find('[aria-label="办公费分类关键词"]')
    const add = officeGroup.findAll('button').find(
      (button) => button.text().trim() === '添加',
    )
    expect(add).toBeTruthy()
    await add!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.el-dialog__title').text()).toBe('为办公费添加关键词')
    expect(wrapper.find('.el-dialog .el-select').exists()).toBe(false)
    await wrapper.find('.el-dialog input').setValue('文具')
    const save = wrapper.findAll('.el-dialog button').find(
      (button) => button.text().trim() === '保存',
    )
    await save!.trigger('click')
    await flushPromises()

    expect(createReceiptKeyword).toHaveBeenCalledWith({
      keyword: '文具',
      categoryId: 'office',
    })
    expect(wrapper.text()).toContain('办公费')
    wrapper.unmount()
  })

  it('shows an inline explanation when a one-character keyword is rejected', async () => {
    const wrapper = mount(SettingsAdminView, {
      global: {
        plugins: [createPinia(), ElementPlus],
        stubs: { RouterLink: { template: '<a><slot /></a>' } },
      },
    })
    await flushPromises()

    const officeGroup = wrapper.find('[aria-label="办公费分类关键词"]')
    const add = officeGroup.findAll('button').find(
      (button) => button.text().trim() === '添加',
    )
    await add!.trigger('click')
    await wrapper.find('.el-dialog input').setValue('1')
    const save = wrapper.findAll('.el-dialog button').find(
      (button) => button.text().trim() === '保存',
    )
    await save!.trigger('click')
    await flushPromises()

    expect(wrapper.find('.el-dialog [role="alert"]').text())
      .toContain('单个数字容易误匹配日期、金额和票据号码')
    expect(createReceiptKeyword).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('shows a stable Chinese confirmation without opening the edit dialog', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    const wrapper = mount(SettingsAdminView, {
      global: {
        plugins: [createPinia(), ElementPlus],
        stubs: { RouterLink: { template: '<a><slot /></a>' } },
      },
    })
    await flushPromises()

    await wrapper.find('.settings-keyword-tag .el-tag__close').trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledWith(
      '确定删除关键词“酒店”吗？',
      '删除分类关键词',
      {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        showClose: false,
        closeOnClickModal: false,
      },
    )
    expect(wrapper.find('.el-dialog__title').exists()).toBe(false)
    confirm.mockRestore()
    wrapper.unmount()
  })
})

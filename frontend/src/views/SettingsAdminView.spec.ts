import ElementPlus from 'element-plus'
import { ElMessageBox } from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listAttendanceCalendars } from '@/api/attendanceCalendars'
import { getExpenseCategories } from '@/api/expenses'
import { getOaTemplateCatalog } from '@/api/oaTemplates'
import {
  createReceiptKeyword,
  deleteReceiptKeyword,
  listReceiptKeywords,
  updateReceiptKeyword,
} from '@/api/receiptKeywords'
import { getAdminExpenseSettings, updateExpenseSettings } from '@/api/settings'
import SettingsAdminView from './SettingsAdminView.vue'
import ReceiptDiagnosticsCard from '@/components/settings/ReceiptDiagnosticsCard.vue'

vi.mock('@/api/attendanceCalendars', () => ({ listAttendanceCalendars: vi.fn(), parseAttendanceNotice: vi.fn(), applyAttendanceNotice: vi.fn(), deleteAttendanceCalendar: vi.fn() }))
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
    vi.mocked(listAttendanceCalendars).mockResolvedValue([])
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
        short_term_internal: '100.00',
        long_term_internal: '100.00',
        short_term_overseas: '0.00',
        long_term_overseas: '0.00',
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
      { id: 'local_transport', name: '市内交通费', order: 0, manualSelectable: true },
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

  it.each([false, true])('includes approval diagnostics in both settings modes, mobile=%s', (mobile) => {
    const wrapper = mount(SettingsAdminView, {
      props: { mobile },
      global: { plugins: [createPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    expect(wrapper.get('#approval-diagnostics').text()).toContain('出差审批诊断')
    expect(wrapper.get('a[href="#approval-diagnostics"]').text()).toBe('审批诊断')
    wrapper.unmount()
  })

  it.each([false, true])('edits and saves all eight independent subsidy rates, mobile=%s', async (mobile) => {
    vi.mocked(updateExpenseSettings).mockImplementation(async (input) => input)
    const wrapper = mount(SettingsAdminView, {
      props: { mobile },
      global: { plugins: [createPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    await flushPromises()
    const labels = [
      '境内商务出差', '境内市外项目出差（长期）', '境内市外项目出差（短期）',
      '境内市内项目出差（长/短期）', '公司内部出差（长期）', '公司内部出差（短期）',
      '境外商务出差（长期）', '境外商务出差（短期）',
    ]
    for (const label of labels) {
      expect(wrapper.find(`[aria-label="${label}每日补助标准"]`).exists()).toBe(true)
    }
    await wrapper.get('[aria-label="境内商务出差每日补助标准"]').setValue('0')
    await wrapper.get('[aria-label="公司内部出差（长期）每日补助标准"]').setValue('125.5')
    await wrapper.get('[aria-label="境外商务出差（长期）每日补助标准"]').setValue('80')
    await wrapper.findAll('button').find((button) => button.text() === '保存设置')!.trigger('click')
    await flushPromises()
    expect(updateExpenseSettings).toHaveBeenCalledWith(expect.objectContaining({
      subsidyRates: {
        business: '0.00', short_term_project: '100.00', long_term_project: '150.00',
        same_city_project: '50.00', short_term_internal: '100.00', long_term_internal: '125.50',
        short_term_overseas: '0.00', long_term_overseas: '80.00',
      },
    }))
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

  it.each([false, true])('groups, adds, moves and deletes transport keywords, mobile=%s', async (mobile) => {
    const mappings = [
      { id: 21, keyword: '客运服务费', categoryId: 'local_transport' as const, categoryName: '市内交通费', transportType: 'other' as const },
      { id: 22, keyword: '滴滴畅行', categoryId: 'local_transport' as const, categoryName: '市内交通费', transportType: 'ride_hailing' as const },
      { id: 23, keyword: '出租车', categoryId: 'local_transport' as const, categoryName: '市内交通费', transportType: 'taxi' as const },
    ]
    vi.mocked(listReceiptKeywords).mockReset().mockResolvedValue(mappings)
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue('confirm' as Awaited<ReturnType<typeof ElMessageBox.confirm>>)
    const wrapper = mount(SettingsAdminView, {
      props: { mobile },
      global: { plugins: [createPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('出差审批诊断')
    expect(wrapper.text()).not.toContain('运行记录')
    expect(wrapper.getComponent(ReceiptDiagnosticsCard).props()).toMatchObject({ mobile })
    expect(wrapper.get('[aria-label="市内交通费通用关键词"]').text()).toContain('客运服务费')
    expect(wrapper.get('[aria-label="市内交通费出租车关键词"]').text()).toContain('出租车')
    const rideGroup = wrapper.get('[aria-label="市内交通费网约车关键词"]')
    expect(rideGroup.text()).toContain('滴滴畅行')
    await rideGroup.findAll('button').find((button) => button.text().trim() === '添加')!.trigger('click')
    await wrapper.get('.el-dialog input').setValue('示例出行')
    await wrapper.findAll('.el-dialog button').find((button) => button.text().trim() === '保存')!.trigger('click')
    await flushPromises()
    expect(createReceiptKeyword).toHaveBeenCalledWith({ keyword: '示例出行', categoryId: 'local_transport', transportType: 'ride_hailing' })
    await wrapper.get('[aria-label="市内交通费网约车关键词"] .settings-keyword-tag').trigger('click')
    const typeSelect = wrapper.findAllComponents({ name: 'ElSelect' }).find((select) => select.props('ariaLabel') === '关键词交通类型')!
    typeSelect.vm.$emit('update:modelValue', 'taxi')
    await wrapper.findAll('.el-dialog button').find((button) => button.text().trim() === '保存')!.trigger('click')
    await flushPromises()
    expect(updateReceiptKeyword).toHaveBeenCalledWith(22, { keyword: '滴滴畅行', categoryId: 'local_transport', transportType: 'taxi' })
    await wrapper.get('[aria-label="市内交通费网约车关键词"] .el-tag__close').trigger('click')
    await flushPromises()
    expect(deleteReceiptKeyword).toHaveBeenCalledWith(22)
    confirm.mockRestore()
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

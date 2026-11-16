import ElementPlus from 'element-plus'
import { ElMessageBox } from 'element-plus'
import { createPinia } from 'pinia'
import { flushPromises, mount, type DOMWrapper } from '@vue/test-utils'
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
import {
  getAdminExpenseSettings, getReimbursementDelegationSettings, listAdminDepartments,
  lookupAdminDirectoryLabels, searchAdminEmployees, updateExpenseSettings, updateReimbursementDelegationSettings,
  type AdminExpenseSettings,
} from '@/api/settings'
import SettingsAdminView from './SettingsAdminView.vue'
import ReceiptDiagnosticsCard from '@/components/settings/ReceiptDiagnosticsCard.vue'
import ReceiptKeywordSettingsCard from '@/components/settings/ReceiptKeywordSettingsCard.vue'
import { useAuthStore } from '@/stores/auth'

function adminPinia() {
  const pinia = createPinia()
  useAuthStore(pinia).session = { user: { userId: 'synthetic-admin', name: '合成管理员' }, departments: [], selectedDepartment: null, csrfToken: 'synthetic-csrf', isAdmin: true }
  return pinia
}

async function chooseEmployee(field: Pick<DOMWrapper<Element>, 'findAll' | 'get'>): Promise<void> {
  await field.findAll('button').find(button => button.text() === '选择员工')!.trigger('click')
  await field.get('input[aria-label="员工姓名或 userId"]').setValue('合成员工')
  await field.findAll('button').find(button => button.text() === '搜索')!.trigger('click')
  await flushPromises()
  const results = field.findAll('.directory-choices label')
  expect(results).toHaveLength(2)
  expect(results[0]!.text()).toContain('合成采购部')
  expect(results[0]!.text()).toContain('synthetic-same-name')
  expect(results[1]!.text()).toContain('合成人事部、合成项目部')
  expect(results[1]!.text()).toContain('synthetic-user')
  await results[1]!.get('input[type="checkbox"]').setValue(true)
  await field.findAll('button').find(button => button.text() === '确定')!.trigger('click')
}

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
    getReimbursementDelegationSettings: vi.fn(),
    listAdminDepartments: vi.fn(),
    lookupAdminDirectoryLabels: vi.fn(),
    searchAdminEmployees: vi.fn(),
    updateExpenseSettings: vi.fn(),
    updateReimbursementDelegationSettings: vi.fn(),
  }
})

beforeEach(() => {
  vi.mocked(getReimbursementDelegationSettings).mockReset().mockResolvedValue({
    employeeUserIds: [], departmentIds: [], revision: 'synthetic-delegation-v1',
  })
  vi.mocked(updateReimbursementDelegationSettings).mockReset().mockImplementation(async (value) => ({
    ...value, revision: 'synthetic-delegation-v2',
  }))
  vi.mocked(listAdminDepartments).mockReset().mockImplementation(async parentId => parentId === '1'
    ? [{ id: '201', name: '合成人事部' }] : [{ id: '302', name: '合成行政组' }])
  vi.mocked(searchAdminEmployees).mockReset().mockResolvedValue([
    { id: 'synthetic-same-name', name: '合成员工', departmentNames: ['合成采购部'] },
    { id: 'synthetic-user', name: '合成员工', departmentNames: ['合成人事部', '合成项目部'] },
  ])
  vi.mocked(lookupAdminDirectoryLabels).mockReset().mockImplementation(async (_kind, ids) =>
    ids.map(id => ({ id, name: `已授权 ${id}` })))
})

describe('SettingsAdminView receipt keywords', () => {
  it.each([false, true])('configures independent employee and department grants, mobile=%s', async (mobile) => {
    const pinia = adminPinia()
    const refresh = vi.spyOn(useAuthStore(pinia), 'refreshMe').mockResolvedValue(undefined)
    const wrapper = mount(SettingsAdminView, {
      props: { mobile }, global: { plugins: [pinia, ElementPlus], stubs: { RouterLink: true } },
    })
    try {
      await flushPromises()
      expect(wrapper.get('a[href="#reimbursement-delegation"]').text()).toBe('代报权限')
      const card = wrapper.get('#reimbursement-delegation')
      const departments = card.get('[aria-label="允许代报的部门"]')
      await departments.findAll('button').find(button => button.text() === '选择部门')!.trigger('click')
      await flushPromises()
      await departments.get('input[type="checkbox"]').setValue(true)
      await departments.get('button[aria-label="查看合成人事部的下级部门"]').trigger('click')
      await flushPromises()
      await departments.get('input[type="checkbox"]').setValue(true)
      await departments.findAll('button').find(button => button.text() === '确定')!.trigger('click')
      const employees = card.get('[aria-label="允许代报的员工"]')
      await chooseEmployee(employees)
      expect(employees.get('.directory-employee-tag').text()).toMatch(/合成员工\s+synthetic-user/)
      await card.findAll('button').find(button => button.text() === '保存代报权限')!.trigger('click')
      await flushPromises()
      expect(updateReimbursementDelegationSettings).toHaveBeenCalledWith({
        employeeUserIds: ['synthetic-user'], departmentIds: ['201', '302'], revision: 'synthetic-delegation-v1',
      }, expect.any(AbortSignal))
      expect(card.text()).toContain('代报权限已保存')
      expect(card.text()).toContain('合成行政组')
      expect(card.text()).toContain('合成员工')
      expect(refresh).toHaveBeenCalledOnce()
    } finally { wrapper.unmount(); refresh.mockRestore() }
  })

  it.each([false, true])('chooses additional admin employees and departments, mobile=%s', async (mobile) => {
    const settings: AdminExpenseSettings = {
      revision: 'synthetic-settings-v1', appTitle: '报销系统', adminUserIds: ['synthetic-same-name'],
      adminDepartmentIds: [], environmentAdminUserIds: ['bootstrap-admin'], calculationMode: 'half_day_12',
      subsidyRates: {
        business: '100', short_term_project: '100', long_term_project: '100', same_city_project: '100',
        short_term_internal: '100', long_term_internal: '100', short_term_overseas: '0', long_term_overseas: '0',
      },
    }
    vi.mocked(lookupAdminDirectoryLabels).mockImplementation(async (_kind, ids) =>
      ids.map(id => ({ id, name: id === 'synthetic-same-name' ? '合成员工' : `已授权 ${id}` })))
    vi.mocked(getAdminExpenseSettings).mockResolvedValue(settings)
    vi.mocked(updateExpenseSettings).mockImplementation(async value => value)
    const pinia = adminPinia()
    const refresh = vi.spyOn(useAuthStore(pinia), 'refreshMe').mockResolvedValue(undefined)
    const wrapper = mount(SettingsAdminView, {
      props: { mobile }, global: { plugins: [pinia, ElementPlus], stubs: { RouterLink: true } },
    })
    try {
      await flushPromises()
      const employees = wrapper.get('[aria-label="额外管理员员工"]')
      expect(employees.get('.directory-employee-tag').text()).toMatch(/合成员工\s+synthetic-same-name/)
      expect(wrapper.get('[aria-label="配置文件管理员"]').find('button').exists()).toBe(false)
      const field = wrapper.get('[aria-label="额外管理员部门"]')
      await field.findAll('button').find(button => button.text() === '选择部门')!.trigger('click')
      await flushPromises()
      await field.get('input[type="checkbox"]').setValue(true)
      await field.findAll('button').find(button => button.text() === '确定')!.trigger('click')
      await chooseEmployee(employees)
      const tags = employees.findAll('.directory-employee-tag')
      expect(tags.map(tag => tag.get('.directory-employee-id').text())).toEqual([
        'synthetic-same-name', 'synthetic-user',
      ])
      await wrapper.get('.settings-subsidy-card').findAll('button')
        .find(button => button.text() === '保存设置')!.trigger('click')
      await flushPromises()
      expect(updateExpenseSettings).toHaveBeenCalledWith(expect.objectContaining({
        adminUserIds: ['synthetic-same-name', 'synthetic-user'], adminDepartmentIds: ['201'], revision: settings.revision,
      }))
      expect(refresh).toHaveBeenCalledOnce()
    } finally { wrapper.unmount(); refresh.mockRestore() }
  })

  it.each([false, true])('locks the keyword editor and preserves queued edits during save, mobile=%s', async (mobile) => {
    vi.mocked(listReceiptKeywords).mockResolvedValue([])
    vi.mocked(updateReceiptKeyword).mockResolvedValue({ id: 1, keyword: '合成较新关键词', categoryId: 'office', categoryName: '办公费' })
    let finish!: (value: never) => void
    vi.mocked(createReceiptKeyword).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = mount(SettingsAdminView, { props: { mobile }, global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } } })
    try {
      await flushPromises()
      const card = wrapper.getComponent(ReceiptKeywordSettingsCard)
      const add = card.get('[aria-label="办公费分类关键词"]').findAll('button').find(b => b.text().trim() === '添加')!
      await add.trigger('click')
      await card.get('.el-dialog input').setValue('合成原关键词')
      await card.findAll('button').find(b => b.text().trim() === '保存')!.trigger('click')
      await flushPromises()
      expect(card.get<HTMLInputElement>('.el-dialog input').element.disabled).toBe(true)
      expect(card.findAll('button').find(b => b.text().trim() === '取消')!.element.disabled).toBe(true)
      expect(add.element.disabled).toBe(true)
      const dialog = card.getComponent({ name: 'ElDialog' })
      expect(dialog.props('closeOnClickModal')).toBe(false)
      expect(dialog.props('closeOnPressEscape')).toBe(false)
      // A model event queued just before disabling must survive the response.
      card.getComponent({ name: 'ElInput' }).vm.$emit('update:modelValue', '合成较新关键词')
      await flushPromises()
      finish({ id: 1, keyword: '合成原关键词', categoryId: 'office', categoryName: '办公费' } as never)
      await flushPromises()
      expect(card.get('.el-dialog').isVisible()).toBe(true)
      expect(card.get<HTMLInputElement>('.el-dialog input').element.value).toBe('合成较新关键词')
      expect(card.get<HTMLInputElement>('.el-dialog input').element.disabled).toBe(false)
      expect(createReceiptKeyword).toHaveBeenCalledOnce()
      await card.findAll('button').find(b => b.text().trim() === '保存')!.trigger('click')
      await flushPromises()
      expect(createReceiptKeyword).toHaveBeenCalledOnce()
      expect(updateReceiptKeyword).toHaveBeenLastCalledWith(1, { keyword: '合成较新关键词', categoryId: 'office' })
      expect(card.get('.el-dialog').isVisible()).toBe(false)
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('keeps keyword input after save failure and allows retry, mobile=%s', async (mobile) => {
    vi.mocked(createReceiptKeyword).mockRejectedValueOnce(new Error('合成网络失败'))
    const wrapper = mount(SettingsAdminView, { props: { mobile }, global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } } })
    try {
      await flushPromises()
      const card = wrapper.getComponent(ReceiptKeywordSettingsCard)
      await card.get('[aria-label="办公费分类关键词"]').findAll('button').find(b => b.text().trim() === '添加')!.trigger('click')
      await card.get('.el-dialog input').setValue('合成待重试关键词')
      await card.findAll('button').find(b => b.text().trim() === '保存')!.trigger('click')
      await flushPromises()
      expect(card.get('.el-dialog').isVisible()).toBe(true)
      expect(card.get<HTMLInputElement>('.el-dialog input').element.value).toBe('合成待重试关键词')
      expect(card.get<HTMLInputElement>('.el-dialog input').element.disabled).toBe(false)
      await card.findAll('button').find(b => b.text().trim() === '保存')!.trigger('click')
      await flushPromises()
      expect(createReceiptKeyword).toHaveBeenCalledTimes(2)
      expect(card.get('.el-dialog').isVisible()).toBe(false)
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('links to the template, calendar and submission recovery controls, mobile=%s', async (mobile) => {
    const wrapper = mount(SettingsAdminView, {
      props: { mobile }, global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    await flushPromises()
    for (const id of ['attendance-calendar', 'submission-recovery', 'oa-template-catalog']) {
      expect(wrapper.find(`a[href="#${id}"]`).exists()).toBe(true)
      expect(wrapper.find(`#${id}`).exists()).toBe(true)
    }
    wrapper.unmount()
  })

  it.each([false, true])('does not mount any settings tool for an employee, mobile=%s', async (mobile) => {
    const pinia = adminPinia(); useAuthStore(pinia).session!.isAdmin = false
    const wrapper = mount(SettingsAdminView, { props: { mobile }, global: { plugins: [pinia, ElementPlus], stubs: { RouterLink: true } } })
    await flushPromises()
    expect(wrapper.find('main').exists()).toBe(false)
    expect(getAdminExpenseSettings).not.toHaveBeenCalled()
    expect(listReceiptKeywords).not.toHaveBeenCalled()
    expect(listAttendanceCalendars).not.toHaveBeenCalled()
    expect(getReimbursementDelegationSettings).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it.each([false, true])('locks settings during save and preserves any queued newer input, mobile=%s', async (mobile) => {
    let finish!: (value: AdminExpenseSettings) => void
    vi.mocked(updateExpenseSettings).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const wrapper = mount(SettingsAdminView, { props: { mobile }, global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } } })
    try {
      await flushPromises()
      const rate = wrapper.get<HTMLInputElement>('input[aria-label="境内商务出差每日补助标准"]')
      await rate.setValue('110.00')
      await wrapper.findAll('button').find(x => x.text() === '保存设置')!.trigger('click')
      const sent = JSON.parse(JSON.stringify(vi.mocked(updateExpenseSettings).mock.calls[0]![0])) as AdminExpenseSettings
      expect(rate.element.disabled).toBe(true)
      // A model event already queued before disabling must also be preserved.
      wrapper.findAllComponents({ name: 'ElInput' }).find(input =>
        input.find('input').attributes('aria-label') === '境内商务出差每日补助标准',
      )!.vm.$emit('update:modelValue', '120.00')
      await flushPromises()
      finish({ ...sent, revision: 'b'.repeat(64) })
      await flushPromises()
      expect(rate.element.value).toBe('120.00')
      expect(wrapper.text()).toContain('还有修改未保存')
    } finally { wrapper.unmount() }
  })

  it.each([false, true])('keeps a stale settings form until the administrator confirms reloading, mobile=%s', async (mobile) => {
    const wrapper = mount(SettingsAdminView, { props: { mobile }, global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } } })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockRejectedValue('cancel')
    try {
      await flushPromises()
      const rate = wrapper.get<HTMLInputElement>('input[aria-label="境内商务出差每日补助标准"]')
      await rate.setValue('115.00')
      vi.mocked(updateExpenseSettings).mockRejectedValueOnce({ isAxiosError: true, response: { status: 409,
        data: { error: { code: 'SETTINGS_REVISION_CONFLICT', message: '其他页面已更新设置' } } } })
      await wrapper.findAll('button').find(x => x.text() === '保存设置')!.trigger('click')
      await flushPromises()
      expect(wrapper.get('[data-testid="settings-save-error"]').text()).toContain('其他页面已更新设置')
      const reload = wrapper.findAll('button').find(x => x.text() === '重新加载设置')!
      await reload.trigger('click'); await flushPromises()
      expect(rate.element.value).toBe('115.00')
      expect(getAdminExpenseSettings).toHaveBeenCalledTimes(1)
      confirm.mockResolvedValueOnce('confirm' as never)
      const current = await vi.mocked(getAdminExpenseSettings).mock.results[0]!.value
      vi.mocked(getAdminExpenseSettings).mockResolvedValueOnce({ ...current, revision: 'b'.repeat(64),
        subsidyRates: { ...current.subsidyRates, business: '130.00' } })
      await reload.trigger('click'); await flushPromises()
      expect(rate.element.value).toBe('130.00')
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(false)
    } finally { confirm.mockRestore(); wrapper.unmount() }
  })

  it.each([false, true])('confirms its saved settings after losing the save response, mobile=%s', async (mobile) => {
    const wrapper = mount(SettingsAdminView, {
      props: { mobile },
      global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    try {
      await flushPromises()
      const rate = wrapper.get<HTMLInputElement>('input[aria-label="境内商务出差每日补助标准"]')
      await rate.setValue('115')
      vi.mocked(updateExpenseSettings).mockImplementationOnce(async (sent) => {
        const readback = structuredClone(sent)
        readback.revision = 'b'.repeat(64)
        readback.subsidyRates.business = '115.00'
        // Server serialization need not preserve property insertion order.
        readback.subsidyRates = Object.fromEntries(Object.entries(readback.subsidyRates).reverse()) as AdminExpenseSettings['subsidyRates']
        vi.mocked(getAdminExpenseSettings).mockResolvedValueOnce(readback)
        throw new Error('Synthetic settings response lost')
      })
      await wrapper.findAll('button').find((button) => button.text() === '保存设置')!.trigger('click')
      await flushPromises()
      expect(rate.element.value).toBe('115.00')
      expect(wrapper.find('[data-testid="settings-save-error"]').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('还有修改未保存')
      expect(getAdminExpenseSettings).toHaveBeenCalledTimes(2)
    } finally {
      wrapper.unmount()
    }
  })

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listAttendanceCalendars).mockResolvedValue([])
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.mocked(getAdminExpenseSettings).mockResolvedValue({
      revision: 'a'.repeat(64),
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
        plugins: [adminPinia(), ElementPlus],
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
      global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } },
    })
    expect(wrapper.get('#approval-diagnostics').text()).toContain('出差审批诊断')
    expect(wrapper.get('a[href="#approval-diagnostics"]').text()).toBe('审批诊断')
    wrapper.unmount()
  })

  it.each([false, true])('edits and saves all eight independent subsidy rates, mobile=%s', async (mobile) => {
    vi.mocked(updateExpenseSettings).mockImplementation(async (input) => input)
    const wrapper = mount(SettingsAdminView, {
      props: { mobile },
      global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } },
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
        plugins: [adminPinia(), ElementPlus],
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
      global: { plugins: [adminPinia(), ElementPlus], stubs: { RouterLink: true } },
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
        plugins: [adminPinia(), ElementPlus],
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
        plugins: [adminPinia(), ElementPlus],
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

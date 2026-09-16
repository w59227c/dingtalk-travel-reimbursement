import ElementPlus, { ElSelect } from 'element-plus'
import { ElMessageBox } from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  confirmOaTemplateCatalog,
  getOaTemplateCatalog,
  inspectOaTemplateCatalog,
} from '@/api/oaTemplates'
import type {
  OaFormComponent,
  OaFormSchema,
  OaLogicalField,
  OaTemplateCatalog,
  OaTemplateCatalogInspection,
} from '@/types/oa'
import OaTemplateSettingsCard from './OaTemplateSettingsCard.vue'

vi.mock('@/api/oaTemplates', () => ({
  confirmOaTemplateCatalog: vi.fn(),
  getOaTemplateCatalog: vi.fn(),
  inspectOaTemplateCatalog: vi.fn(),
}))

const reimbursementFields: OaLogicalField[] = [
  ['company', '所属公司', 'DDSelectField'],
  ['budgetCode', '预算代码', 'DDSelectField'],
  ['travelType', '出差类别', 'DDSelectField'],
  ['startDate', '开始日期', 'DDDateField'],
  ['endDate', '结束日期', 'DDDateField'],
  ['durationDays', '时长（天）', 'NumberField'],
  ['description', '明细说明', 'TextareaField'],
  ['reimbursementAmount', '报销金额', 'MoneyField'],
  ['relatedApprovals', '关联审批单', 'RelateField'],
  ['attachments', '附件', 'DDAttachment'],
].map(([key, label, type]) => ({
  key: key!,
  label: label!,
  supportedComponentTypes: [type!],
}))
const travelFields: OaLogicalField[] = [
  { key: 'startDate', label: '出差开始日期', supportedComponentTypes: ['DDDateField'] },
  { key: 'endDate', label: '出差结束日期', supportedComponentTypes: ['DDDateField'] },
]
const reimbursementMappings = Object.fromEntries(
  reimbursementFields.map((field) => [field.key, `${field.key}-id`]),
)
const travelMappings = { startDate: 'travel-startDate-id', endDate: 'travel-endDate-id' }
const businessOption = { value: 'BUSINESS', label: '商务出差', key: 'option_1' }
const internalOption = { value: 'INTERNAL', label: '公司内部出差', key: 'option_2' }

function component(
  componentId: string,
  componentType: string,
  label: string,
  logicalKey: string,
  options = [] as typeof businessOption[],
  required = true,
): OaFormComponent {
  return {
    componentId,
    componentType,
    label,
    bizAlias: null,
    required,
    disabled: false,
    hidden: false,
    ancestorDisabled: false,
    ancestorHidden: false,
    nested: false,
    inSubtable: false,
    unsupportedContainerAncestor: false,
    format: componentType === 'DDDateField' ? 'yyyy-MM-dd' : null,
    unit: null,
    options,
    parentComponentId: null,
    relatedTemplatePolicy: componentType === 'RelateField'
      ? { mode: 'RESTRICTED', processCodes: ['PROC-TRAVEL'] }
      : null,
    compatibleLogicalFields: [logicalKey],
  }
}

function reimbursementSchema(fingerprint = 'a'.repeat(64)): OaFormSchema {
  return {
    processCode: 'PROC-REIMBURSEMENT',
    formCode: 'FORM-REIMBURSEMENT',
    formUuid: 'form-uuid',
    modifiedAt: '2026-09-04T09:00:00+08:00',
    status: 'PUBLISHED',
    templateName: '差旅费报销申请',
    title: '差旅费报销申请',
    schemaFingerprint: fingerprint,
    components: [
      ...reimbursementFields.map((field) => component(
        `${field.key}-id`,
        field.supportedComponentTypes[0]!,
        field.label,
        field.key,
        field.key === 'travelType' ? [businessOption, internalOption] : [],
      )),
      component(
        'relatedApprovals-alternative-id',
        'RelateField',
        '关联审批单（备用）',
        'relatedApprovals',
        [],
        false,
      ),
    ],
  }
}

function travelSchema(fingerprint = 'b'.repeat(64)): OaFormSchema {
  return {
    processCode: 'PROC-TRAVEL',
    formCode: 'FORM-TRAVEL',
    formUuid: 'travel-uuid',
    modifiedAt: '2026-09-04T09:00:00+08:00',
    status: 'PUBLISHED',
    templateName: '境内出差申请',
    title: '境内出差申请',
    schemaFingerprint: fingerprint,
    components: [
      ...travelFields.map((field) => component(
        `travel-${field.key}-id`,
        'DDDateField',
        field.label,
        field.key,
      )),
      component(
        'travel-startDate-alternative-id',
        'DDDateField',
        '出差开始日期（备用）',
        'startDate',
        [],
        false,
      ),
    ],
  }
}

function catalog(version = 3): OaTemplateCatalog {
  const reimbursement = reimbursementSchema()
  const travel = travelSchema()
  return {
    configVersion: version,
    processCode: reimbursement.processCode,
    templateName: reimbursement.templateName,
    schemaFingerprint: reimbursement.schemaFingerprint,
    confirmedSchemaFingerprint: reimbursement.schemaFingerprint,
    compatibilityStatus: 'COMPATIBLE',
    isSubmissionReady: true,
    requiresConfirmation: false,
    reimbursement: {
      processCode: reimbursement.processCode,
      schema: reimbursement,
      logicalFields: reimbursementFields,
      mappings: reimbursementMappings,
    },
    travelProfiles: [{
      profileKey: 'business',
      displayName: '商务出差',
      processCode: travel.processCode,
      schema: travel,
      logicalFields: travelFields,
      mappings: travelMappings,
      travelTypeOption: businessOption,
      confirmedSchemaFingerprint: travel.schemaFingerprint,
      schemaFingerprint: travel.schemaFingerprint,
    }],
    allowedTravelProcessCodes: [travel.processCode],
    lastCheckedAt: '2026-09-04T01:00:00Z',
    confirmedAt: '2026-09-04T01:00:00Z',
    updatedAt: '2026-09-04T01:00:00Z',
  }
}

function inspection(
  compatibilityStatus: OaTemplateCatalogInspection['compatibilityStatus'] = 'COMPATIBLE',
  configuredConfigVersion = 3,
): OaTemplateCatalogInspection {
  const stored = catalog()
  return {
    configured: true,
    compatibilityStatus,
    requiresConfirmation: compatibilityStatus !== 'COMPATIBLE',
    isSubmissionReady: compatibilityStatus === 'COMPATIBLE',
    configuredConfigVersion,
    reimbursement: {
      ...stored.reimbursement,
      confirmedSchemaFingerprint: stored.confirmedSchemaFingerprint,
    },
    travelProfiles: stored.travelProfiles,
  }
}

function findButton(wrapper: ReturnType<typeof mount>, text: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === text)
}

async function changeSelect(
  wrapper: ReturnType<typeof mount>,
  selectIndex: number,
  value: string,
): Promise<void> {
  const select = wrapper.findAllComponents(ElSelect)[selectIndex]
  expect(select, `missing select at index ${selectIndex}`).toBeTruthy()
  select!.vm.$emit('update:modelValue', value)
  select!.vm.$emit('change', value)
  await flushPromises()
}

describe('OaTemplateSettingsCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.ResizeObserver = class ResizeObserver {
      observe(): void {}
      unobserve(): void {}
      disconnect(): void {}
    }
    vi.mocked(getOaTemplateCatalog).mockResolvedValue({
      configured: true,
      configVersion: 3,
      compatibilityStatus: 'COMPATIBLE',
      isSubmissionReady: true,
      requiresConfirmation: false,
      catalog: catalog(),
    })
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue(inspection())
    vi.mocked(confirmOaTemplateCatalog).mockResolvedValue(catalog(4))
  })

  it('explains compatible full-fingerprint changes without requesting remapping', async () => {
    const saved = catalog()
    saved.schemaFingerprint = 'c'.repeat(64)
    saved.reimbursement.schema.schemaFingerprint = saved.schemaFingerprint
    saved.travelProfiles[0]!.schemaFingerprint = 'd'.repeat(64)
    saved.travelProfiles[0]!.schema.schemaFingerprint = 'd'.repeat(64)
    vi.mocked(getOaTemplateCatalog).mockResolvedValue({
      configured: true,
      configVersion: 3,
      compatibilityStatus: 'COMPATIBLE',
      isSubmissionReady: true,
      requiresConfirmation: false,
      catalog: saved,
    })

    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('仅发布时间或选项发生兼容更新')
    expect(wrapper.text()).toContain('兼容更新已自动同步')
    wrapper.unmount()
  })

  it('inspects all template headers and saves one version-checked catalog', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('目录版本 3')
    expect(wrapper.text()).toContain('差旅费报销申请')
    expect(wrapper.text()).toContain('商务出差')
    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()

    expect(inspectOaTemplateCatalog).toHaveBeenCalledWith({
      reimbursementProcessCode: 'PROC-REIMBURSEMENT',
      travelProfiles: [{
        profileKey: 'business',
        displayName: '商务出差',
        processCode: 'PROC-TRAVEL',
      }],
    })
    await findButton(wrapper, '保存整个目录')!.trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledOnce()
    expect(confirmOaTemplateCatalog).toHaveBeenCalledOnce()
    expect(confirmOaTemplateCatalog).toHaveBeenCalledWith({
      expectedConfigVersion: 3,
      reimbursement: {
        processCode: 'PROC-REIMBURSEMENT',
        schemaFingerprint: 'a'.repeat(64),
        mappings: reimbursementMappings,
      },
      travelProfiles: [{
        profileKey: 'business',
        displayName: '商务出差',
        processCode: 'PROC-TRAVEL',
        schemaFingerprint: 'b'.repeat(64),
        mappings: travelMappings,
        travelTypeOption: businessOption,
      }],
    })
    expect(wrapper.text()).toContain('目录版本 4')
    confirm.mockRestore()
    wrapper.unmount()
  })

  it('preserves per-source category mappings through inspection and save', async () => {
    const saved = catalog()
    const profile = saved.travelProfiles[0]!
    profile.logicalFields = [...profile.logicalFields, { key: 'travelType', label: '出差类别', supportedComponentTypes: ['DDSelectField'] }]
    profile.schema.components.push(component('source-type', 'DDSelectField', '出差类别', 'travelType', [businessOption, internalOption]))
    profile.mappings.travelType = 'source-type'
    profile.travelTypeMappings = { BUSINESS: businessOption, INTERNAL: internalOption }
    vi.mocked(getOaTemplateCatalog).mockResolvedValue({ configured: true, configVersion: 3, compatibilityStatus: 'COMPATIBLE', isSubmissionReady: true, requiresConfirmation: false, catalog: saved })
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue({ ...inspection(), travelProfiles: [profile] })
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(OaTemplateSettingsCard, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain('商务出差 → 报销类别')
    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()
    await findButton(wrapper, '保存整个目录')!.trigger('click')
    await flushPromises()
    expect(vi.mocked(confirmOaTemplateCatalog).mock.calls[0]![0].travelProfiles[0]).toMatchObject({ mappings: { travelType: 'source-type' }, travelTypeMappings: profile.travelTypeMappings })
    confirm.mockRestore()
    wrapper.unmount()
  })

  it('uses refreshed category mappings when a changed template invalidates stored option identities', async () => {
    const saved = catalog()
    const savedProfile = saved.travelProfiles[0]!
    savedProfile.logicalFields = [
      ...savedProfile.logicalFields,
      { key: 'travelType', label: '出差类别', supportedComponentTypes: ['DDSelectField'] },
    ]
    savedProfile.schema.components.push(component(
      'source-type',
      'DDSelectField',
      '出差类别',
      'travelType',
      [businessOption],
    ))
    savedProfile.mappings.travelType = 'source-type'
    savedProfile.travelTypeMappings = { BUSINESS: businessOption }

    const refreshedTarget = { ...businessOption, key: 'option_1_refreshed' }
    const refreshed = inspection('DRIFTED')
    refreshed.reimbursement.schema = reimbursementSchema('c'.repeat(64))
    refreshed.reimbursement.schema.components = refreshed.reimbursement.schema.components.map(
      (item) => item.componentId === 'travelType-id'
        ? { ...item, options: [refreshedTarget, internalOption] }
        : item,
    )
    const refreshedProfile = {
      ...savedProfile,
      schema: travelSchema('d'.repeat(64)),
      confirmedSchemaFingerprint: savedProfile.schemaFingerprint,
      travelTypeMappings: { BUSINESS: refreshedTarget },
    }
    refreshedProfile.schema.components.push(component(
      'source-type',
      'DDSelectField',
      '出差类别',
      'travelType',
      [businessOption],
    ))
    refreshed.travelProfiles = [refreshedProfile]

    vi.mocked(getOaTemplateCatalog).mockResolvedValue({
      configured: true,
      configVersion: 3,
      compatibilityStatus: 'COMPATIBLE',
      isSubmissionReady: true,
      requiresConfirmation: false,
      catalog: saved,
    })
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue(refreshed)
    const wrapper = mount(OaTemplateSettingsCard, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()

    expect((findButton(wrapper, '保存整个目录')!.element as HTMLButtonElement).disabled)
      .toBe(false)
    wrapper.unmount()
  })

  it('surfaces schema drift and requires a fresh confirmation', async () => {
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue(inspection('DRIFTED'))
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('结构已变化')
    expect(wrapper.text()).toContain('请检查映射和选项后重新确认')
    wrapper.unmount()
  })

  it('surfaces a changed template catalog with the aggregate status name', async () => {
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue(inspection('CATALOG_CHANGED'))
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('模板目录已变化')
    expect(wrapper.text()).toContain('请重新检查全部字段并确认')
    wrapper.unmount()
  })

  it.each([
    {
      name: '关联审批字段',
      selectIndex: 8,
      value: 'relatedApprovals-alternative-id',
    },
    {
      name: '出差日期字段',
      selectIndex: 10,
      value: 'travel-startDate-alternative-id',
    },
    {
      name: '出差类别精确选项',
      selectIndex: 12,
      value: JSON.stringify([
        internalOption.value,
        internalOption.label,
        internalOption.key,
      ]),
    },
  ])('requires a fresh inspection after changing $name', async ({ selectIndex, value }) => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()

    await changeSelect(wrapper, selectIndex, value)

    expect((findButton(wrapper, '保存整个目录')!.element as HTMLButtonElement).disabled)
      .toBe(true)
    expect(confirmOaTemplateCatalog).not.toHaveBeenCalled()

    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()
    expect((findButton(wrapper, '保存整个目录')!.element as HTMLButtonElement).disabled)
      .toBe(false)

    await findButton(wrapper, '保存整个目录')!.trigger('click')
    await flushPromises()
    expect(confirmOaTemplateCatalog).toHaveBeenCalledOnce()
    const payload = vi.mocked(confirmOaTemplateCatalog).mock.calls[0]![0]
    if (selectIndex === 8) {
      expect(payload.reimbursement.mappings.relatedApprovals).toBe(value)
    } else if (selectIndex === 10) {
      expect(payload.travelProfiles[0]!.mappings.startDate).toBe(value)
    } else {
      expect(payload.travelProfiles[0]!.travelTypeOption).toEqual(internalOption)
    }
    confirm.mockRestore()
    wrapper.unmount()
  })

  it('repairs an unreadable configured catalog with the top-level CAS version', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    vi.mocked(getOaTemplateCatalog).mockResolvedValue({
      configured: true,
      configVersion: 7,
      compatibilityStatus: 'DRIFTED',
      isSubmissionReady: false,
      requiresConfirmation: true,
      catalog: null,
    })
    vi.mocked(inspectOaTemplateCatalog).mockResolvedValue(inspection('DRIFTED', 7))
    vi.mocked(confirmOaTemplateCatalog).mockResolvedValue(catalog(8))
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('目录版本 7')
    expect(wrapper.text()).toContain('已保存的目录当前无法读取')
    await wrapper.find('input[aria-label="报销 processCode"]')
      .setValue('PROC-REIMBURSEMENT')
    await wrapper.find('input[aria-label="出差模板 1 profileKey"]')
      .setValue('business')
    await wrapper.find('input[aria-label="出差模板 1 显示名称"]')
      .setValue('商务出差')
    await wrapper.find('input[aria-label="出差模板 1 processCode"]')
      .setValue('PROC-TRAVEL')
    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()
    await findButton(wrapper, '保存整个目录')!.trigger('click')
    await flushPromises()

    expect(confirmOaTemplateCatalog).toHaveBeenCalledWith(
      expect.objectContaining({ expectedConfigVersion: 7 }),
    )
    expect(wrapper.text()).toContain('目录版本 8')
    confirm.mockRestore()
    wrapper.unmount()
  })

  it('does not retry a non-idempotent save after a CAS conflict', async () => {
    const confirm = vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    vi.mocked(confirmOaTemplateCatalog).mockRejectedValue({
      isAxiosError: true,
      response: {
        status: 409,
        data: {
          error: {
            code: 'OA_TEMPLATE_CONFIGURATION_CHANGED',
            message: '审批模板目录刚刚被其他管理员更新，请重试',
          },
        },
      },
    })
    vi.mocked(getOaTemplateCatalog)
      .mockResolvedValueOnce({
        configured: true,
        configVersion: 3,
        compatibilityStatus: 'COMPATIBLE',
        isSubmissionReady: true,
        requiresConfirmation: false,
        catalog: catalog(),
      })
      .mockResolvedValueOnce({
        configured: true,
        configVersion: 4,
        compatibilityStatus: 'COMPATIBLE',
        isSubmissionReady: true,
        requiresConfirmation: false,
        catalog: catalog(4),
      })
    const wrapper = mount(OaTemplateSettingsCard, {
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await findButton(wrapper, '读取并检查模板')!.trigger('click')
    await flushPromises()
    await findButton(wrapper, '保存整个目录')!.trigger('click')
    await flushPromises()

    expect(confirmOaTemplateCatalog).toHaveBeenCalledOnce()
    expect(getOaTemplateCatalog).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('目录版本 4')
    confirm.mockRestore()
    wrapper.unmount()
  })
})

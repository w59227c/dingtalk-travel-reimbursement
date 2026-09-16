<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import {
  confirmOaTemplateCatalog,
  getOaTemplateCatalog,
  inspectOaTemplateCatalog,
} from '@/api/oaTemplates'
import type {
  ConfirmOaTemplateCatalogInput,
  OaFieldMappings,
  OaFormComponent,
  OaFormOption,
  OaFormSchema,
  OaLogicalField,
  OaTemplateCatalog,
  OaTemplateCatalogInspection,
  OaTemplateCompatibilityStatus,
  OaTravelTemplateCatalog,
  OaTravelTemplateInspection,
} from '@/types/oa'

interface EditableTravelProfile {
  localId: number
  profileKey: string
  displayName: string
  processCode: string
  schema: OaFormSchema | null
  logicalFields: OaLogicalField[]
  mappings: OaFieldMappings
  travelTypeOptionIdentity: string
  dynamicTravelType: boolean
  travelTypeMappings: Record<string, string>
  confirmedSchemaFingerprint: string | null
}

const PROCESS_CODE_PATTERN = /^[A-Za-z0-9_-]{1,128}$/
const PROFILE_KEY_PATTERN = /^[A-Za-z0-9_-]{1,64}$/
const MAX_TRAVEL_PROFILES = 20

let nextProfileId = 1
let inspectionRequestId = 0
let loadRequestId = 0

const loading = ref(false)
const inspecting = ref(false)
const saving = ref(false)
const loadError = ref('')
const inspectError = ref('')
const catalogWarning = ref('')
const inspected = ref(false)
const expectedConfigVersion = ref<number | null>(null)
const compatibilityStatus = ref<OaTemplateCompatibilityStatus>('UNCONFIGURED')
const submissionReady = ref(false)
const currentCatalogVersion = ref<number | null>(null)

const reimbursement = reactive({
  processCode: '',
  schema: null as OaFormSchema | null,
  logicalFields: [] as OaLogicalField[],
  mappings: {} as OaFieldMappings,
  confirmedSchemaFingerprint: null as string | null,
})
const travelProfiles = ref<EditableTravelProfile[]>([])

const statusView = computed(() => {
  const values: Record<OaTemplateCompatibilityStatus, {
    label: string
    tagType: 'success' | 'warning' | 'info' | 'danger'
    alertType: 'success' | 'warning' | 'info' | 'error'
    detail: string
  }> = {
    UNCONFIGURED: {
      label: '未配置',
      tagType: 'info',
      alertType: 'info',
      detail: '填写模板并完成检查后，才能保存目录。',
    },
    COMPATIBLE: {
      label: '兼容',
      tagType: 'success',
      alertType: 'success',
      detail: submissionReady.value
        ? '当前目录可以用于创建 OA 报销审批。'
        : '模板结构兼容，仍需确认并保存。',
    },
    CATALOG_CHANGED: {
      label: '模板目录已变化',
      tagType: 'warning',
      alertType: 'warning',
      detail: '模板目录与已保存配置不同，请重新检查全部字段并确认。',
    },
    DRIFTED: {
      label: '结构已变化',
      tagType: 'danger',
      alertType: 'error',
      detail: '钉钉模板结构已变化，请检查映射和选项后重新确认。',
    },
  }
  return values[compatibilityStatus.value]
})

const travelTypeOptions = computed<OaFormOption[]>(() => {
  const componentId = reimbursement.mappings.travelType
  if (!componentId || reimbursement.schema === null) return []
  return reimbursement.schema.components.find(
    (component) => component.componentId === componentId,
  )?.options ?? []
})

const relatedApprovalPolicyError = computed(() => {
  if (reimbursement.schema === null) return ''
  const componentId = reimbursement.mappings.relatedApprovals
  const component = reimbursement.schema.components.find(
    (item) => item.componentId === componentId,
  )
  if (!component || component.relatedTemplatePolicy === null) {
    return '关联审批单控件没有可验证的模板范围，请检查该控件配置'
  }
  if (component.relatedTemplatePolicy.mode !== 'RESTRICTED') return ''
  const allowed = new Set(component.relatedTemplatePolicy.processCodes)
  const outside = travelProfiles.value
    .filter((profile) => profile.processCode && !allowed.has(profile.processCode))
    .map((profile) => profile.displayName || profile.processCode)
  return outside.length
    ? `以下出差模板不在关联审批控件允许范围内：${outside.join('、')}`
    : ''
})

const saveDisabledReason = computed(() => {
  if (loadError.value) return '请先重新加载 OA 模板目录'
  if (!inspected.value) return '请先读取并检查当前钉钉模板'
  if (!mappingComplete(
    reimbursement.schema,
    reimbursement.logicalFields,
    reimbursement.mappings,
  )) return '请完成报销模板的全部字段映射'
  if (relatedApprovalPolicyError.value) return relatedApprovalPolicyError.value
  for (const profile of travelProfiles.value) {
    if (!mappingComplete(profile.schema, profile.logicalFields, profile.mappings, true)) {
      return `请完成“${profile.displayName || profile.profileKey || '出差模板'}”的日期字段映射`
    }
    if (profile.dynamicTravelType ? !sourceTravelOptions(profile).length
      || sourceTravelOptions(profile).some((option) => !resolveTravelTypeOption(profile.travelTypeMappings[option.value] ?? ''))
      : resolveTravelTypeOption(profile.travelTypeOptionIdentity) === null) {
      return `请选择“${profile.displayName || profile.profileKey || '出差模板'}”对应的出差类别`
    }
  }
  return ''
})

onMounted(load)

function blankTravelProfile(): EditableTravelProfile {
  return {
    localId: nextProfileId++,
    profileKey: '',
    displayName: '',
    processCode: '',
    schema: null,
    logicalFields: [],
    mappings: {},
    travelTypeOptionIdentity: '',
    dynamicTravelType: false,
    travelTypeMappings: {},
    confirmedSchemaFingerprint: null,
  }
}

function resetForm(): void {
  Object.assign(reimbursement, {
    processCode: '',
    schema: null,
    logicalFields: [],
    mappings: {},
    confirmedSchemaFingerprint: null,
  })
  travelProfiles.value = [blankTravelProfile()]
}

async function load(): Promise<void> {
  const requestId = ++loadRequestId
  inspectionRequestId += 1
  inspecting.value = false
  loading.value = true
  loadError.value = ''
  inspectError.value = ''
  catalogWarning.value = ''
  inspected.value = false
  try {
    const state = await getOaTemplateCatalog()
    if (requestId !== loadRequestId) return
    compatibilityStatus.value = state.compatibilityStatus
    submissionReady.value = state.isSubmissionReady
    currentCatalogVersion.value = state.configVersion
    expectedConfigVersion.value = state.configVersion
    if (state.catalog !== null) {
      applyCatalog(state.catalog)
    } else {
      resetForm()
      if (state.configured) {
        catalogWarning.value = '已保存的目录当前无法读取，请按钉钉现有模板重新配置并确认。'
      }
    }
  } catch (error) {
    if (requestId !== loadRequestId) return
    loadError.value = apiErrorMessage(error, 'OA 模板目录加载失败，请重试')
  } finally {
    if (requestId === loadRequestId) loading.value = false
  }
}

function applyCatalog(catalog: OaTemplateCatalog): void {
  Object.assign(reimbursement, {
    processCode: catalog.reimbursement.processCode,
    schema: catalog.reimbursement.schema,
    logicalFields: catalog.reimbursement.logicalFields,
    mappings: { ...catalog.reimbursement.mappings },
    confirmedSchemaFingerprint: catalog.confirmedSchemaFingerprint,
  })
  travelProfiles.value = catalog.travelProfiles.map(profileFromCatalog)
  compatibilityStatus.value = catalog.compatibilityStatus
  submissionReady.value = catalog.isSubmissionReady
  currentCatalogVersion.value = catalog.configVersion
  expectedConfigVersion.value = catalog.configVersion
}

function profileFromCatalog(profile: OaTravelTemplateCatalog): EditableTravelProfile {
  return {
    localId: nextProfileId++,
    profileKey: profile.profileKey,
    displayName: profile.displayName,
    processCode: profile.processCode,
    schema: profile.schema,
    logicalFields: profile.logicalFields,
    mappings: { ...profile.mappings },
    travelTypeOptionIdentity: profile.travelTypeOption ? optionIdentity(profile.travelTypeOption) : '',
    dynamicTravelType: Boolean(Object.keys(profile.travelTypeMappings ?? {}).length),
    travelTypeMappings: Object.fromEntries(Object.entries(profile.travelTypeMappings ?? {}).map(([key, option]) => [key, optionIdentity(option)])),
    confirmedSchemaFingerprint: profile.confirmedSchemaFingerprint,
  }
}

function addTravelProfile(): void {
  if (travelProfiles.value.length >= MAX_TRAVEL_PROFILES) {
    ElMessage.warning(`最多配置 ${MAX_TRAVEL_PROFILES} 个出差审批模板`)
    return
  }
  travelProfiles.value.push(blankTravelProfile())
  invalidateContractProof()
}

function removeTravelProfile(index: number): void {
  if (travelProfiles.value.length === 1) {
    ElMessage.warning('至少保留一个出差审批模板')
    return
  }
  travelProfiles.value.splice(index, 1)
  invalidateContractProof()
}

function invalidateInspection(): void {
  inspectionRequestId += 1
  inspecting.value = false
  inspected.value = false
  inspectError.value = ''
  submissionReady.value = false
}

function invalidateContractProof(): void {
  invalidateInspection()
}

function reimbursementProcessCodeChanged(): void {
  invalidateContractProof()
  reimbursement.schema = null
  reimbursement.logicalFields = []
  reimbursement.mappings = {}
  reimbursement.confirmedSchemaFingerprint = null
  for (const profile of travelProfiles.value) profile.travelTypeOptionIdentity = ''
}

function travelProcessCodeChanged(profile: EditableTravelProfile): void {
  invalidateContractProof()
  profile.schema = null
  profile.logicalFields = []
  profile.mappings = {}
  profile.confirmedSchemaFingerprint = null
}

function profileIdentityChanged(): void {
  invalidateContractProof()
}

function profileDisplayNameChanged(): void {
  invalidateInspection()
}

function reimbursementMappingChanged(logicalKey: string): void {
  if (logicalKey === 'travelType') travelTypeMappingChanged()
  invalidateContractProof()
}

function travelContractChanged(): void {
  invalidateContractProof()
}

function validateHeaders(): string | null {
  reimbursement.processCode = reimbursement.processCode.trim()
  if (!PROCESS_CODE_PATTERN.test(reimbursement.processCode)) {
    return '报销 processCode 只能包含字母、数字、下划线和连字符，长度不超过 128 位'
  }
  if (travelProfiles.value.length === 0) return '至少配置一个出差审批模板'
  const profileKeys = new Set<string>()
  const processCodes = new Set<string>()
  for (const profile of travelProfiles.value) {
    profile.profileKey = profile.profileKey.trim()
    profile.displayName = profile.displayName.trim()
    profile.processCode = profile.processCode.trim()
    if (!PROFILE_KEY_PATTERN.test(profile.profileKey)) {
      return 'profileKey 只能包含字母、数字、下划线和连字符，长度不超过 64 位'
    }
    if (!profile.displayName || profile.displayName.length > 128) {
      return '每个出差模板都要填写不超过 128 个字符的显示名称'
    }
    if (!PROCESS_CODE_PATTERN.test(profile.processCode)) {
      return `“${profile.displayName}”的 processCode 格式不正确`
    }
    if (profile.processCode === reimbursement.processCode) {
      return '报销模板不能同时作为出差模板'
    }
    if (profileKeys.has(profile.profileKey)) return '出差模板的 profileKey 不能重复'
    if (processCodes.has(profile.processCode)) return '出差模板的 processCode 不能重复'
    profileKeys.add(profile.profileKey)
    processCodes.add(profile.processCode)
  }
  return null
}

async function inspectCatalog(): Promise<void> {
  const validationError = validateHeaders()
  if (validationError !== null) {
    ElMessage.warning(validationError)
    return
  }
  const requestId = ++inspectionRequestId
  inspected.value = false
  inspecting.value = true
  inspectError.value = ''
  try {
    const result = await inspectOaTemplateCatalog({
      reimbursementProcessCode: reimbursement.processCode,
      travelProfiles: travelProfiles.value.map((profile) => ({
        profileKey: profile.profileKey,
        displayName: profile.displayName,
        processCode: profile.processCode,
      })),
    })
    if (requestId !== inspectionRequestId) return
    applyInspection(result)
    ElMessage.success('已读取并检查当前钉钉模板')
  } catch (error) {
    if (requestId !== inspectionRequestId) return
    inspectError.value = apiErrorMessage(error, '钉钉模板读取失败，请检查 processCode 和应用权限')
  } finally {
    if (requestId === inspectionRequestId) inspecting.value = false
  }
}

function applyInspection(result: OaTemplateCatalogInspection): void {
  const oldReimbursementMappings = reimbursement.mappings
  Object.assign(reimbursement, {
    processCode: result.reimbursement.processCode,
    schema: result.reimbursement.schema,
    logicalFields: result.reimbursement.logicalFields,
    mappings: usableMappings(
      result.reimbursement.schema,
      result.reimbursement.logicalFields,
      oldReimbursementMappings,
      result.reimbursement.mappings,
    ),
    confirmedSchemaFingerprint: result.reimbursement.confirmedSchemaFingerprint,
  })

  travelProfiles.value = result.travelProfiles.map((item, index) => {
    const existing = travelProfiles.value[index] ?? blankTravelProfile()
    const optionCandidates = travelTypeOptions.value.map(optionIdentity)
    const storedOption = item.travelTypeOption === null
      ? ''
      : optionIdentity(item.travelTypeOption)
    const selectedOption = optionCandidates.includes(existing.travelTypeOptionIdentity)
      ? existing.travelTypeOptionIdentity
      : optionCandidates.includes(storedOption)
        ? storedOption
        : ''
    return profileFromInspection(item, existing, selectedOption)
  })
  expectedConfigVersion.value = result.configuredConfigVersion
  currentCatalogVersion.value = result.configuredConfigVersion
  compatibilityStatus.value = result.compatibilityStatus
  submissionReady.value = result.isSubmissionReady
  inspected.value = true
}

function profileFromInspection(
  item: OaTravelTemplateInspection,
  existing: EditableTravelProfile,
  selectedOption: string,
): EditableTravelProfile {
  const mappings = usableMappings(
    item.schema,
    item.logicalFields,
    existing.mappings,
    item.mappings,
  )
  const inspectedTypeMappings = Object.fromEntries(
    Object.entries(item.travelTypeMappings ?? {}).map(([key, option]) => [key, optionIdentity(option)]),
  )
  const sourceOptions = item.schema.components.find(
    (component) => component.componentId === mappings.travelType,
  )?.options ?? []
  const travelTypeMappings = Object.fromEntries(sourceOptions.map((source) => {
    const existingIdentity = existing.travelTypeMappings[source.value] ?? ''
    return [
      source.value,
      resolveTravelTypeOption(existingIdentity) === null
        ? inspectedTypeMappings[source.value] ?? ''
        : existingIdentity,
    ]
  }))
  return {
    localId: existing.localId,
    profileKey: item.profileKey,
    displayName: item.displayName,
    processCode: item.processCode,
    schema: item.schema,
    logicalFields: item.logicalFields,
    mappings,
    travelTypeOptionIdentity: selectedOption,
    dynamicTravelType: existing.dynamicTravelType || Boolean(Object.keys(item.travelTypeMappings ?? {}).length),
    travelTypeMappings,
    confirmedSchemaFingerprint: item.confirmedSchemaFingerprint,
  }
}

function compatibleComponents(
  schema: OaFormSchema | null,
  logicalKey: string,
): OaFormComponent[] {
  if (schema === null) return []
  return schema.components.filter(
    (component) => component.compatibleLogicalFields.includes(logicalKey),
  )
}

function usableMappings(
  schema: OaFormSchema,
  logicalFields: OaLogicalField[],
  ...candidates: Array<OaFieldMappings | null>
): OaFieldMappings {
  return Object.fromEntries(logicalFields.map((field) => {
    const components = compatibleComponents(schema, field.key)
    const candidate = candidates
      .map((mapping) => mapping?.[field.key])
      .find((componentId) => components.some(
        (component) => component.componentId === componentId,
      ))
    const exactLabels = components.filter((component) => component.label === field.label)
    return [field.key, candidate ?? (exactLabels.length === 1 ? exactLabels[0]!.componentId
      : components.length === 1 && !['company', 'budgetCode'].includes(field.key) ? components[0]!.componentId : '')]
  }))
}

function mappingComplete(
  schema: OaFormSchema | null,
  logicalFields: OaLogicalField[],
  mappings: OaFieldMappings,
  optionalTravelType = false,
): boolean {
  return schema !== null
    && logicalFields.length > 0
    && logicalFields.every((field) => optionalTravelType && field.key === 'travelType' && !mappings[field.key] || compatibleComponents(schema, field.key).some(
      (component) => component.componentId === mappings[field.key],
    ))
}

function componentDisplay(component: OaFormComponent): string {
  return `${component.label}（${component.componentType} · ${component.componentId}）`
}

function sourceTravelOptions(profile: EditableTravelProfile): OaFormOption[] {
  return profile.schema?.components.find((component) => component.componentId === profile.mappings.travelType)?.options ?? []
}

function optionIdentity(option: OaFormOption): string {
  return JSON.stringify([option.value, option.label, option.key])
}

function resolveTravelTypeOption(identity: string): OaFormOption | null {
  return travelTypeOptions.value.find(
    (option) => optionIdentity(option) === identity,
  ) ?? null
}

function travelTypeMappingChanged(): void {
  const available = new Set(travelTypeOptions.value.map(optionIdentity))
  for (const profile of travelProfiles.value) {
    if (!available.has(profile.travelTypeOptionIdentity)) {
      profile.travelTypeOptionIdentity = ''
    }
  }
}

function unmappedRequiredComponents(
  schema: OaFormSchema | null,
  mappings: OaFieldMappings,
): OaFormComponent[] {
  if (schema === null) return []
  const mapped = new Set(Object.values(mappings))
  return schema.components.filter((component) => component.required
    && !component.disabled
    && !component.hidden
    && !component.ancestorDisabled
    && !component.ancestorHidden
    && !component.inSubtable
    && !mapped.has(component.componentId))
}

function displayFingerprint(value: string | null): string {
  if (!value) return '尚未确认'
  return value
}

async function saveCatalog(): Promise<void> {
  if (saveDisabledReason.value) {
    ElMessage.warning(saveDisabledReason.value)
    return
  }
  const reimbursementSchema = reimbursement.schema
  if (reimbursementSchema === null) return
  const profiles = travelProfiles.value.map((profile) => {
    const option = resolveTravelTypeOption(profile.travelTypeOptionIdentity)
    if (profile.schema === null || !profile.dynamicTravelType && option === null) return null
    return {
      profileKey: profile.profileKey,
      displayName: profile.displayName,
      processCode: profile.processCode,
      schemaFingerprint: profile.schema.schemaFingerprint,
      mappings: Object.fromEntries(Object.entries(profile.mappings).filter(([key, value]) => value && (key !== 'travelType' || profile.dynamicTravelType))),
      travelTypeOption: profile.dynamicTravelType ? travelTypeOptions.value[0]! : option!,
      ...(profile.dynamicTravelType ? { travelTypeMappings: Object.fromEntries(sourceTravelOptions(profile).map((source) => [source.value, resolveTravelTypeOption(profile.travelTypeMappings[source.value] ?? '')!])) } : {}),
    }
  })
  if (profiles.some((profile) => profile === null)) return
  const payload: ConfirmOaTemplateCatalogInput = {
    expectedConfigVersion: expectedConfigVersion.value,
    reimbursement: {
      processCode: reimbursement.processCode,
      schemaFingerprint: reimbursementSchema.schemaFingerprint,
      mappings: { ...reimbursement.mappings },
    },
    travelProfiles: profiles as ConfirmOaTemplateCatalogInput['travelProfiles'],
  }

  try {
    await ElMessageBox.confirm(
      '将一次性保存报销模板、全部出差模板、字段映射和出差类别。保存后会作为员工创建 OA 审批的正式目录。',
      '确认保存 OA 模板目录',
      {
        type: 'warning',
        confirmButtonText: '确认保存',
        cancelButtonText: '取消',
        showClose: false,
        closeOnClickModal: false,
      },
    )
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    throw error
  }

  saving.value = true
  try {
    const saved = await confirmOaTemplateCatalog(payload)
    applyCatalog(saved)
    applyInspection(inspectionFromCatalog(saved))
    ElMessage.success('OA 模板目录已保存')
  } catch (error) {
    const code = apiErrorCode(error)
    if (code === 'OA_TEMPLATE_CONFIGURATION_CHANGED') {
      ElMessage.warning('目录已被其他管理员更新，正在加载最新版本；本次保存没有覆盖对方的修改')
      await load()
    } else if (code === 'OA_TEMPLATE_SCHEMA_CHANGED') {
      ElMessage.warning('钉钉模板在保存前发生变化，请重新检查字段后再保存')
      invalidateContractProof()
      await inspectCatalog()
    } else {
      ElMessage.error(apiErrorMessage(error, 'OA 模板目录保存失败，请重试'))
    }
  } finally {
    saving.value = false
  }
}

function inspectionFromCatalog(catalog: OaTemplateCatalog): OaTemplateCatalogInspection {
  return {
    configured: true,
    compatibilityStatus: catalog.compatibilityStatus,
    requiresConfirmation: catalog.requiresConfirmation,
    isSubmissionReady: catalog.isSubmissionReady,
    configuredConfigVersion: catalog.configVersion,
    reimbursement: {
      ...catalog.reimbursement,
      confirmedSchemaFingerprint: catalog.confirmedSchemaFingerprint,
    },
    travelProfiles: catalog.travelProfiles,
  }
}
</script>

<template>
  <el-card
    v-loading="loading || inspecting || saving"
    shadow="never"
    class="oa-template-card"
  >
    <template #header>
      <div class="oa-template-heading">
        <div>
          <h2>钉钉 OA 模板目录</h2>
          <p>配置报销模板和可关联的出差模板。先读取钉钉当前结构，再一次性保存整个目录。</p>
        </div>
        <div class="oa-template-status">
          <el-tag :type="statusView.tagType">
            {{ statusView.label }}
          </el-tag>
          <span v-if="currentCatalogVersion !== null">目录版本 {{ currentCatalogVersion }}</span>
        </div>
      </div>
    </template>

    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      show-icon
      :closable="false"
      class="admin-load-error"
    >
      <template #default>
        <el-button
          link
          type="primary"
          @click="load"
        >
          重新加载
        </el-button>
      </template>
    </el-alert>

    <template v-else>
      <el-alert
        :title="statusView.detail"
        :type="statusView.alertType"
        show-icon
        :closable="false"
        class="oa-template-alert"
      />
      <el-alert
        v-if="catalogWarning"
        :title="catalogWarning"
        type="warning"
        show-icon
        :closable="false"
        class="oa-template-alert"
      />
      <el-alert
        v-if="inspectError"
        :title="inspectError"
        type="error"
        show-icon
        :closable="false"
        class="oa-template-alert"
      />

      <el-form label-position="top">
        <section
          class="oa-template-section"
          aria-labelledby="reimbursement-template-heading"
        >
          <h3 id="reimbursement-template-heading">
            报销审批模板
          </h3>
          <el-form-item label="报销 processCode">
            <el-input
              v-model="reimbursement.processCode"
              maxlength="128"
              placeholder="例如 PROC-REIMBURSEMENT"
              class="full-width"
              aria-label="报销 processCode"
              @input="reimbursementProcessCodeChanged"
            />
          </el-form-item>

          <div
            v-if="reimbursement.schema"
            class="oa-template-schema-summary"
          >
            <strong>{{ reimbursement.schema.templateName || reimbursement.schema.title }}</strong>
            <span>当前指纹：<code>{{ displayFingerprint(reimbursement.schema.schemaFingerprint) }}</code></span>
            <span>已确认指纹：<code>{{ displayFingerprint(reimbursement.confirmedSchemaFingerprint) }}</code></span>
            <small
              v-if="compatibilityStatus === 'COMPATIBLE' && reimbursement.confirmedSchemaFingerprint !== reimbursement.schema.schemaFingerprint"
            >
              仅发布时间或选项发生兼容更新，已自动同步，无需重新确认字段映射。
            </small>
          </div>

          <div
            v-if="reimbursement.logicalFields.length"
            class="oa-template-mapping-grid"
          >
            <el-form-item
              v-for="field in reimbursement.logicalFields"
              :key="field.key"
              :label="field.label"
            >
              <el-select
                v-model="reimbursement.mappings[field.key]"
                filterable
                class="full-width"
                :aria-label="`${field.label}字段映射`"
                placeholder="选择 OA 控件"
                @change="reimbursementMappingChanged(field.key)"
              >
                <el-option
                  v-for="component in compatibleComponents(reimbursement.schema, field.key)"
                  :key="component.componentId"
                  :label="componentDisplay(component)"
                  :value="component.componentId"
                />
              </el-select>
              <div class="field-help">
                支持 {{ field.supportedComponentTypes.join(' / ') }}
              </div>
            </el-form-item>
          </div>
          <p
            v-else
            class="oa-template-placeholder"
          >
            填写 processCode 并点击“读取并检查模板”后，在这里映射 10 个报销字段。
          </p>
          <el-alert
            v-if="reimbursement.schema && !inspected"
            title="保存任何修改前，都要重新读取钉钉当前模板；这样可以避免用过期字段覆盖目录。"
            type="info"
            :closable="false"
            show-icon
            class="oa-template-alert"
          />
          <el-alert
            v-if="unmappedRequiredComponents(reimbursement.schema, reimbursement.mappings).length"
            :title="`还有未映射的必填控件：${unmappedRequiredComponents(reimbursement.schema, reimbursement.mappings).map((item) => item.label).join('、')}`"
            type="warning"
            :closable="false"
            show-icon
            class="oa-template-alert"
          />
          <el-alert
            v-if="relatedApprovalPolicyError"
            :title="relatedApprovalPolicyError"
            type="error"
            :closable="false"
            show-icon
            class="oa-template-alert"
          />
        </section>

        <section
          class="oa-template-section"
          aria-labelledby="travel-template-heading"
        >
          <div class="oa-template-section-heading">
            <div>
              <h3 id="travel-template-heading">
                可关联的出差审批模板
              </h3>
              <p>可为整个模板设置固定类别，也可按出差申请中的类别逐项对应。</p>
            </div>
            <el-button
              plain
              :disabled="travelProfiles.length >= MAX_TRAVEL_PROFILES"
              @click="addTravelProfile"
            >
              添加出差模板
            </el-button>
          </div>

          <article
            v-for="(profile, index) in travelProfiles"
            :key="profile.localId"
            class="oa-travel-profile"
            :aria-label="`出差模板 ${index + 1}`"
          >
            <div class="oa-travel-profile-heading">
              <strong>{{ profile.displayName || `出差模板 ${index + 1}` }}</strong>
              <el-button
                link
                type="danger"
                @click="removeTravelProfile(index)"
              >
                删除
              </el-button>
            </div>
            <div class="oa-template-header-grid">
              <el-form-item label="profileKey">
                <el-input
                  v-model="profile.profileKey"
                  maxlength="64"
                  placeholder="例如 business"
                  :aria-label="`出差模板 ${index + 1} profileKey`"
                  @input="profileIdentityChanged"
                />
              </el-form-item>
              <el-form-item label="显示名称">
                <el-input
                  v-model="profile.displayName"
                  maxlength="128"
                  placeholder="例如 商务出差"
                  :aria-label="`出差模板 ${index + 1} 显示名称`"
                  @input="profileDisplayNameChanged"
                />
              </el-form-item>
              <el-form-item label="processCode">
                <el-input
                  v-model="profile.processCode"
                  maxlength="128"
                  placeholder="例如 PROC-TRAVEL"
                  :aria-label="`出差模板 ${index + 1} processCode`"
                  @input="travelProcessCodeChanged(profile)"
                />
              </el-form-item>
            </div>

            <div
              v-if="profile.schema"
              class="oa-template-schema-summary"
            >
              <strong>{{ profile.schema.templateName || profile.schema.title }}</strong>
              <span>当前指纹：<code>{{ displayFingerprint(profile.schema.schemaFingerprint) }}</code></span>
              <span>已确认指纹：<code>{{ displayFingerprint(profile.confirmedSchemaFingerprint) }}</code></span>
              <small
                v-if="compatibilityStatus === 'COMPATIBLE' && profile.confirmedSchemaFingerprint !== profile.schema.schemaFingerprint"
              >
                兼容更新已自动同步。
              </small>
            </div>

            <div
              v-if="profile.logicalFields.length"
              class="oa-template-header-grid"
            >
              <el-form-item
                v-for="field in profile.logicalFields.filter((item) => item.key !== 'travelType' || profile.dynamicTravelType)"
                :key="field.key"
                :label="field.label"
              >
                <el-select
                  v-model="profile.mappings[field.key]"
                  filterable
                  class="full-width"
                  :aria-label="`${profile.displayName} ${field.label}字段映射`"
                  placeholder="选择 OA 控件"
                  @change="travelContractChanged"
                >
                  <el-option
                    v-for="component in compatibleComponents(profile.schema, field.key)"
                    :key="component.componentId"
                    :label="componentDisplay(component)"
                    :value="component.componentId"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="出差类别对应方式">
                <el-switch
                  v-model="profile.dynamicTravelType"
                  active-text="按申请类别对应"
                  inactive-text="固定类别"
                  @change="travelContractChanged"
                />
              </el-form-item>
              <el-form-item
                v-if="!profile.dynamicTravelType"
                label="对应的报销出差类别"
              >
                <el-select
                  v-model="profile.travelTypeOptionIdentity"
                  class="full-width"
                  :aria-label="`${profile.displayName} 对应的报销出差类别`"
                  placeholder="精确选择一个选项"
                  @change="travelContractChanged"
                >
                  <el-option
                    v-for="option in travelTypeOptions"
                    :key="optionIdentity(option)"
                    :label="option.label"
                    :value="optionIdentity(option)"
                  >
                    <span>{{ option.label }}</span>
                    <small class="oa-option-value">{{ option.value }}</small>
                  </el-option>
                </el-select>
              </el-form-item>
              <template v-else>
                <el-form-item
                  v-for="source in sourceTravelOptions(profile)"
                  :key="source.value"
                  :label="`${source.label} → 报销类别`"
                >
                  <el-select
                    v-model="profile.travelTypeMappings[source.value]"
                    class="full-width"
                    :aria-label="`${profile.displayName} ${source.label}对应的报销类别`"
                    placeholder="请选择对应类别"
                    @change="travelContractChanged"
                  >
                    <el-option
                      v-for="option in travelTypeOptions"
                      :key="optionIdentity(option)"
                      :label="option.label"
                      :value="optionIdentity(option)"
                    />
                  </el-select>
                </el-form-item>
              </template>
            </div>
            <p
              v-else
              class="oa-template-placeholder"
            >
              检查模板后映射开始日期、结束日期，并选择对应的报销出差类别。
            </p>
          </article>
        </section>

        <div class="oa-template-actions">
          <span
            v-if="saveDisabledReason"
            class="field-help oa-template-save-reason"
            role="status"
          >{{ saveDisabledReason }}</span>
          <el-button
            :loading="inspecting"
            :disabled="loading || saving || inspecting"
            @click="inspectCatalog"
          >
            读取并检查模板
          </el-button>
          <el-button
            type="primary"
            :loading="saving"
            :disabled="Boolean(saveDisabledReason) || saving || inspecting"
            :title="saveDisabledReason"
            @click="saveCatalog"
          >
            保存整个目录
          </el-button>
        </div>
      </el-form>
    </template>
  </el-card>
</template>

<style scoped>
.oa-template-card {
  margin-top: 18px;
}

.oa-template-heading,
.oa-template-section-heading,
.oa-travel-profile-heading,
.oa-template-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.oa-template-heading h2,
.oa-template-section h3 {
  margin: 0;
}

.oa-template-heading p,
.oa-template-section-heading p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.oa-template-status {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.oa-template-alert {
  margin-bottom: 16px;
}

.oa-template-section + .oa-template-section {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.oa-template-section > h3,
.oa-template-section-heading {
  margin-bottom: 16px;
}

.oa-template-mapping-grid,
.oa-template-header-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 16px;
}

.oa-template-schema-summary {
  display: grid;
  gap: 5px;
  margin: 0 0 16px;
  padding: 12px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-regular);
  font-size: 13px;
}

.oa-template-schema-summary code {
  overflow-wrap: anywhere;
}

.oa-template-placeholder {
  margin: 0;
  padding: 14px;
  border: 1px dashed var(--el-border-color);
  border-radius: 8px;
  color: var(--el-text-color-secondary);
  line-height: 1.6;
}

.oa-travel-profile {
  padding: 16px;
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
  background: var(--el-bg-color-page);
}

.oa-travel-profile + .oa-travel-profile {
  margin-top: 14px;
}

.oa-travel-profile-heading {
  margin-bottom: 12px;
}

.oa-option-value {
  float: right;
  margin-left: 18px;
  color: var(--el-text-color-secondary);
}

.oa-template-actions {
  justify-content: flex-end;
  margin-top: 24px;
}

.oa-template-save-reason {
  align-self: center;
  margin-right: auto;
  color: var(--el-color-warning-dark-2);
}

@media (max-width: 720px) {
  .oa-template-heading,
  .oa-template-section-heading {
    flex-direction: column;
  }

  .oa-template-mapping-grid,
  .oa-template-header-grid {
    grid-template-columns: 1fr;
  }

  .oa-template-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .oa-template-actions :deep(.el-button + .el-button) {
    margin-left: 0;
  }
}
</style>

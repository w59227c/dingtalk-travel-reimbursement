<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type {
  OaTravelApproval,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
} from '@/types/reimbursements'
import { mappedTravelTypeOption } from '@/utils/travelTypes'

const props = withDefaults(defineProps<{
  modelValue: ReimbursementRelatedApprovalSelection[]
  linkedApprovals?: ReimbursementRelatedApproval[]
  mobile?: boolean
  readonly?: boolean
  busy?: boolean
  single?: boolean
  requiredStartDate?: string
  requiredEndDate?: string
}>(), {
  linkedApprovals: () => [],
  mobile: false,
  readonly: false,
  busy: false,
  single: false,
  requiredStartDate: '',
  requiredEndDate: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: ReimbursementRelatedApprovalSelection[]]
}>()

interface ApprovalRow {
  processInstanceId: string
  profileKey: string
  title: string
  businessId: string
  startDate: string
  endDate: string
  travelTypeLabel: string
  linkedOnly: boolean
}

const drafts = useReimbursementDraftStore()
const fromDate = ref('')
const toDate = ref('')
const keyword = ref('')
const localError = ref('')
const candidateCache = new Map<string, OaTravelApproval>()

const selectedIds = computed(() => new Set(
  props.modelValue.map((selection) => selection.processInstanceId),
))
const rows = computed<ApprovalRow[]>(() => {
  const result: ApprovalRow[] = drafts.travelApprovals.map((approval) => ({
    processInstanceId: approval.processInstanceId,
    profileKey: approval.profileKey,
    title: approval.title,
    businessId: approval.businessId,
    startDate: approval.startDate,
    endDate: approval.endDate,
    travelTypeLabel: approval.travelTypeOption.label,
    linkedOnly: false,
  }))
  const present = new Set(result.map((row) => row.processInstanceId))
  for (const approval of props.linkedApprovals) {
    if (present.has(approval.processInstanceId)) continue
    result.push({
      processInstanceId: approval.processInstanceId,
      profileKey: approval.profileKey,
      title: approval.title,
      businessId: approval.businessId,
      startDate: approval.startDate,
      endDate: approval.endDate,
      travelTypeLabel: linkedTravelTypeLabel(approval),
      linkedOnly: true,
    })
    present.add(approval.processInstanceId)
  }
  for (const selection of props.modelValue) {
    if (present.has(selection.processInstanceId)) continue
    const approval = candidateCache.get(selection.processInstanceId)
    if (!approval) continue
    result.push({
      processInstanceId: approval.processInstanceId,
      profileKey: approval.profileKey,
      title: approval.title,
      businessId: approval.businessId,
      startDate: approval.startDate,
      endDate: approval.endDate,
      travelTypeLabel: approval.travelTypeOption.label,
      linkedOnly: false,
    })
  }
  return result
})

function linkedTravelTypeLabel(approval: ReimbursementRelatedApproval): string {
  const profile = drafts.reimbursementOptions?.travelProfiles.find(
    (item) => item.profileKey === approval.profileKey,
  )
  return mappedTravelTypeOption(profile, approval.sourceTravelTypeValue)?.label
    ?? '出差类别待重新核验'
}

watch(
  () => drafts.travelApprovals,
  (approvals) => {
    for (const approval of approvals) candidateCache.set(approval.processInstanceId, approval)
  },
  { immediate: true },
)

onMounted(() => {
  if (
    drafts.travelApprovals.length === 0
    && !drafts.loadingTravelApprovals
    && !drafts.travelApprovalsError
  ) void search()
})

async function search(): Promise<void> {
  if (props.readonly) return
  localError.value = ''
  const hasFrom = Boolean(fromDate.value)
  const hasTo = Boolean(toDate.value)
  if (hasFrom !== hasTo) {
    localError.value = '自定义日期范围需要同时填写开始和结束日期'
    return
  }
  if (hasFrom && fromDate.value > toDate.value) {
    localError.value = '开始日期不能晚于结束日期'
    return
  }
  await drafts.loadTravelApprovals({
    ...(hasFrom ? { from: fromDate.value, to: toDate.value } : {}),
    ...(keyword.value.trim() ? { query: keyword.value.trim() } : {}),
  })
}

function selectionForRow(row: ApprovalRow): ReimbursementRelatedApprovalSelection | null {
  const existing = props.modelValue.find(
    (selection) => selection.processInstanceId === row.processInstanceId,
  )
  if (existing) return existing
  const candidate = candidateCache.get(row.processInstanceId)
  const queryWindow = drafts.travelApprovalQueryWindow
  if (!candidate || !queryWindow) return null
  return {
    processInstanceId: candidate.processInstanceId,
    profileKey: candidate.profileKey,
    queryWindow: { ...queryWindow },
  }
}

function toggle(row: ApprovalRow, checked: boolean): void {
  if (props.readonly || props.busy) return
  localError.value = ''
  if (!checked) {
    emit(
      'update:modelValue',
      props.modelValue.filter(
        (selection) => selection.processInstanceId !== row.processInstanceId,
      ),
    )
    return
  }
  const reason = unavailableReason(row)
  if (reason) { localError.value = reason; return }
  const selection = selectionForRow(row)
  if (!selection) {
    localError.value = '该审批的查询凭据已失效，请重新加载后再选择'
    return
  }
  emit('update:modelValue', props.single ? [selection] : [...props.modelValue, selection])
}

function unavailableReason(row: ApprovalRow): string {
  // A selected row must always be removable, including a stale restored row.
  if (selectedIds.value.has(row.processInstanceId)) return ''
  const candidate = candidateCache.get(row.processInstanceId)
  if (candidate?.unavailableReason) return candidate.unavailableReason
  if (!candidate?.companyOption || !candidate.budgetCodeOption) return '所属公司或预算代码不可用，请联系管理员'
  if (props.requiredStartDate && props.requiredEndDate
    && (row.endDate < props.requiredStartDate || row.startDate > props.requiredEndDate)) {
    return '审批日期与出差补助日期不重合'
  }
  if (props.single) return ''
  const first = props.modelValue[0]
  if (!first) return ''
  const baseline = candidateCache.get(first.processInstanceId)
  const restoredApproval = props.linkedApprovals?.find(
    (item) => item.processInstanceId === first.processInstanceId,
  )
  const sourceDepartmentId = baseline?.originatorDepartmentId
    ?? restoredApproval?.originatorDepartmentId
  if (sourceDepartmentId
    && candidate.originatorDepartmentId !== sourceDepartmentId) {
    return '发起部门与已选审批不同，请按部门分别报销'
  }
  const company = baseline?.companyOption?.value ?? drafts.currentDraft?.input.companyValue
  const budget = baseline?.budgetCodeOption?.value ?? drafts.currentDraft?.input.budgetCodeValue
  const profile = drafts.reimbursementOptions?.travelProfiles
    .find((item) => item.profileKey === first.profileKey)
  const restoredSource = restoredApproval?.sourceTravelTypeValue
  const restoredType = profile?.travelTypeMappings
    ? (restoredSource ? profile.travelTypeMappings[restoredSource]?.value : undefined)
    : profile?.travelTypeOption.value
  const travelType = baseline?.travelTypeOption.value ?? restoredType
  const sourceTravelType = baseline?.sourceTravelTypeValue ?? restoredSource
  if (!baseline && profile?.travelTypeMappings && !restoredType) return '已选审批的出差类别来源失效，请重新选择'
  if (!company || !budget || !travelType) return '请等待已选审批核验完成，或重新查询该审批'
  if (candidate.companyOption.value !== company) return '所属公司与已选审批不同'
  if (candidate.budgetCodeOption.value !== budget) return '预算代码与已选审批不同'
  if (candidate.travelTypeOption.value !== travelType) return '出差类别与已选审批不同'
  if ((sourceTravelType || candidate.sourceTravelTypeValue)
    && candidate.sourceTravelTypeValue !== sourceTravelType) {
    return '出差类别与已选审批不同'
  }
  return ''
}

function accountingLabel(row: ApprovalRow): string {
  const approval = candidateCache.get(row.processInstanceId)
  return [approval?.companyOption?.label, approval?.budgetCodeOption?.label].filter(Boolean).join(' · ')
}
</script>

<template>
  <section
    class="travel-approval-section"
    data-testid="travel-approval-section"
    aria-labelledby="travel-approval-heading"
    :aria-busy="busy"
  >
    <div class="card-header travel-approval-heading">
      <div>
        <h2 id="travel-approval-heading">
          {{ props.single ? '选择本次出差申请' : '关联已通过的出差审批' }}
        </h2>
        <p v-if="props.single">
          系统会优先匹配审批的发起部门；无法匹配当前部门时再请你确认。
        </p>
        <p v-else>
          先选本人已通过的审批；可继续关联同一发起部门、公司、预算和出差类别的多张审批，日期不连续也可以。
        </p>
      </div>
      <el-tag
        :type="modelValue.length ? 'success' : 'warning'"
        effect="light"
      >
        已选 {{ modelValue.length }} 张
      </el-tag>
    </div>

    <fieldset
      class="plain-fieldset"
      :disabled="readonly"
    >
      <div
        class="travel-query-grid"
        :class="{ 'travel-query-grid--mobile': props.mobile }"
      >
        <el-date-picker
          v-model="fromDate"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="props.mobile ? '开始日期' : '查询开始日期'"
          aria-label="出差审批查询开始日期"
          class="full-width"
        />
        <el-date-picker
          v-model="toDate"
          type="date"
          value-format="YYYY-MM-DD"
          :placeholder="props.mobile ? '结束日期' : '查询结束日期'"
          aria-label="出差审批查询结束日期"
          class="full-width"
        />
        <el-input
          v-model="keyword"
          clearable
          maxlength="100"
          placeholder="标题或审批编号"
          aria-label="搜索出差审批"
          @keyup.enter="search"
        />
        <el-button
          class="travel-query-button"
          :loading="drafts.loadingTravelApprovals"
          @click="search"
        >
          查询审批
        </el-button>
      </div>
    </fieldset>

    <el-alert
      v-if="!readonly && (localError || drafts.travelApprovalsError)"
      :title="localError || drafts.travelApprovalsError"
      type="error"
      :closable="false"
      show-icon
      class="travel-query-alert"
    >
      <template #default>
        <el-button
          v-if="drafts.travelApprovalsError"
          link
          type="primary"
          @click="search"
        >
          重新加载
        </el-button>
      </template>
    </el-alert>

    <el-skeleton
      v-if="drafts.loadingTravelApprovals && rows.length === 0"
      :rows="3"
      animated
      class="travel-result-loading"
    />
    <el-empty
      v-else-if="rows.length === 0"
      description="当前查询范围内没有已通过的出差审批"
      :image-size="72"
    >
      <el-button
        v-if="!readonly"
        :loading="drafts.loadingTravelApprovals"
        @click="search"
      >
        重新加载
      </el-button>
    </el-empty>
    <div
      v-else
      class="travel-approval-list"
      aria-label="可关联的出差审批"
      aria-live="polite"
    >
      <article
        v-for="row in rows"
        :key="row.processInstanceId"
        class="travel-approval-row"
        :class="{ 'is-selected': selectedIds.has(row.processInstanceId) }"
      >
        <el-checkbox
          :model-value="selectedIds.has(row.processInstanceId)"
          :disabled="readonly || busy || Boolean(unavailableReason(row))"
          :aria-label="`选择出差审批 ${row.title}`"
          @change="(checked: boolean) => toggle(row, checked)"
        />
        <span class="travel-approval-main">
          <span class="travel-approval-title">
            <strong>{{ row.title }}</strong>
            <el-tag
              v-if="row.linkedOnly"
              size="small"
              type="info"
            >
              已关联
            </el-tag>
          </span>
          <span>{{ row.startDate }} 至 {{ row.endDate }} · 出差类别：{{ row.travelTypeLabel }}</span>
          <small>审批编号：{{ row.businessId }}</small>
          <small v-if="accountingLabel(row)">{{ accountingLabel(row) }}</small>
          <small
            v-if="unavailableReason(row)"
            role="status"
          >不可选择：{{ unavailableReason(row) }}</small>
        </span>
      </article>
    </div>

    <p class="field-help">
      默认查询近期审批；需要更早记录时设置日期范围。选中的审批会自动保存并由服务器重新核验。
    </p>
  </section>
</template>

<style scoped>
.plain-fieldset {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
}

.travel-approval-section {
  margin-top: 22px;
  padding-top: 22px;
  border-top: 1px solid var(--el-border-color-lighter);
}

.travel-approval-heading {
  align-items: flex-start;
  margin-bottom: 16px;
}

.travel-approval-heading h2 {
  margin: 0;
  color: var(--el-text-color-primary);
  font-size: 16px;
}

.travel-approval-heading p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.55;
}

.travel-query-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)) minmax(180px, 1.2fr) auto;
  gap: 10px;
}

.travel-query-button {
  width: 112px;
}

.travel-query-grid--mobile {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.travel-query-grid--mobile > * {
  min-width: 0;
  width: 100%;
}

.travel-query-grid--mobile > :nth-child(3),
.travel-query-grid--mobile > :nth-child(4) {
  grid-column: 1 / -1;
}

.travel-query-grid--mobile :deep(.el-date-editor) {
  min-width: 0;
  width: 100%;
}

.travel-query-grid--mobile :deep(.el-button) {
  margin-left: 0;
  width: 100%;
}

.travel-query-alert,
.travel-result-loading {
  margin-top: 14px;
}

.travel-approval-list {
  display: grid;
  gap: 10px;
  margin-top: 16px;
}

.travel-approval-row {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 13px 14px;
  border: 1px solid var(--el-border-color);
  border-radius: 10px;
}

.travel-approval-row.is-selected {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}

.travel-approval-main {
  display: grid;
  min-width: 0;
  gap: 5px;
  color: var(--el-text-color-regular);
  line-height: 1.45;
}

.travel-approval-main small {
  color: var(--el-text-color-secondary);
  overflow-wrap: anywhere;
}

.travel-approval-title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-primary);
}

@media (max-width: 720px) {
  .travel-query-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .travel-query-button { width: 100%; }
}

@media (max-width: 480px) {
  .travel-query-grid {
    grid-template-columns: 1fr;
  }
}
</style>

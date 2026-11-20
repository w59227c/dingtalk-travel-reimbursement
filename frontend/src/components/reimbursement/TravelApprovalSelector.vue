<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import type {
  OaTravelApproval,
  ReimbursementRelatedApproval,
  ReimbursementRelatedApprovalSelection,
} from '@/types/reimbursements'
import { mappedTravelTypeOption } from '@/utils/travelTypes'
import { approvalQueryDateError } from '@/utils/approvalQueryDates'
import ApprovalAccountingLine from './ApprovalAccountingLine.vue'
import ApprovalSelectorPanel from './ApprovalSelectorPanel.vue'

const props = withDefaults(defineProps<{
  modelValue: ReimbursementRelatedApprovalSelection[]
  linkedApprovals?: ReimbursementRelatedApproval[]
  mobile?: boolean
  readonly?: boolean
  busy?: boolean
  delegated?: boolean
  single?: boolean
  departmentChoices?: { id: string; name: string }[]
  requiredStartDate?: string
  requiredEndDate?: string
}>(), {
  linkedApprovals: () => [],
  mobile: false,
  readonly: false,
  busy: false,
  delegated: false,
  single: false,
  departmentChoices: undefined,
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
const queryNeedsAdjustment = computed(() => !!approvalQueryDateError(fromDate.value, toDate.value) || [
  'TRAVEL_APPROVAL_QUERY_HISTORY_UNAVAILABLE', 'TRAVEL_APPROVAL_QUERY_FUTURE',
  'TRAVEL_APPROVAL_QUERY_WINDOW_INVALID', 'DINGTALK_QUERY_TIME_INVALID',
].includes(drafts.travelApprovalsErrorCode))
const optionsNeedRefresh = computed(() => [
  'OA_TEMPLATE_CONFIGURATION_CHANGED', 'OA_TEMPLATE_SCHEMA_CHANGED', 'OA_TEMPLATE_CONFIRMATION_REQUIRED',
].includes(drafts.travelApprovalsErrorCode))
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
  const dateError = approvalQueryDateError(fromDate.value, toDate.value)
  if (dateError) { localError.value = dateError; return }
  await drafts.loadTravelApprovals({
    ...(hasFrom ? { from: fromDate.value, to: toDate.value } : {}),
    ...(keyword.value.trim() ? { query: keyword.value.trim() } : {}),
  })
}

async function retrySearch(): Promise<void> {
  if (drafts.loadingReimbursementOptions || drafts.loadingTravelApprovals) return
  if (optionsNeedRefresh.value) {
    await drafts.loadReimbursementOptions()
    await nextTick()
    if (drafts.reimbursementOptionsError) return
  }
  await search()
}

defineExpose({ search })

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

function accountingFor(row: ApprovalRow): { company?: string; budget?: string } {
  const approval = candidateCache.get(row.processInstanceId)
  if (approval) return {
    company: approval.companyOption?.label || approval.companyOption?.value,
    budget: approval.budgetCodeOption?.label || approval.budgetCodeOption?.value,
  }
  const draft = drafts.currentDraft
  if (!row.linkedOnly || !draft?.relatedApprovals.some((linked) => (
    linked.processInstanceId === row.processInstanceId && linked.profileKey === row.profileKey
  ))) return {}
  if (draft.status === 'LOCKED') return {
    company: draft.submittedDetails?.companyLabel,
    budget: draft.submittedDetails?.budgetLabel,
  }
  if (!draft.input.accountingSourceVerified) return {}
  return {
    company: drafts.reimbursementOptions?.companyOptions.find(
      (option) => option.value === draft.input.companyValue,
    )?.label || draft.input.companyValue,
    budget: drafts.reimbursementOptions?.budgetCodeOptions.find(
      (option) => option.value === draft.input.budgetCodeValue,
    )?.label || draft.input.project?.text || draft.input.budgetCodeValue,
  }
}

function departmentLabelFor(row: ApprovalRow): string {
  const candidate = candidateCache.get(row.processInstanceId)
  const department = props.departmentChoices?.find((item) => item.id === candidate?.originatorDepartmentId)
  return department ? `发起部门：${department.name}` : '需确认办理部门'
}
</script>

<template>
  <ApprovalSelectorPanel
    v-model:from-date="fromDate"
    v-model:to-date="toDate"
    v-model:keyword="keyword"
    kind="travel"
    :title="props.single ? '选择本次出差申请' : '关联已通过的出差审批'"
    :mobile="mobile"
    :readonly="readonly"
    :busy="busy"
    :loading="drafts.loadingTravelApprovals"
    :query-needs-adjustment="queryNeedsAdjustment"
    :empty-description="localError || drafts.travelApprovalsError
      ? queryNeedsAdjustment ? '请调整查询日期后重新查询' : '审批查询未完成，请重试'
      : props.delegated ? '当前查询范围内没有抄送给你的可关联审批' : '当前查询范围内没有已通过的出差审批'"
    :rows="rows"
    :selected-ids="selectedIds"
    :unavailable-reason="unavailableReason"
    :selection-help="departmentChoices ? '选择审批并确认后更换部门。找不到时可筛选日期或标题。' : undefined"
    @search="retrySearch"
    @toggle="toggle"
  >
    <template #intro>
      <p
        v-if="props.single"
        class="field-help"
      >
        系统会优先匹配审批的发起部门；无法匹配当前部门时再请你确认。
      </p>
      <p
        v-else
        class="field-help"
      >
        可关联公司、预算和出差类别相同的多张审批，发起部门可不同，日期可不连续。
      </p>
      <p
        v-if="props.delegated"
        class="field-help"
      >
        代报时只显示该员工已通过且抄送给你的出差审批。
      </p>
    </template>
    <template #notifications>
      <el-alert
        v-if="!readonly && (localError || drafts.travelApprovalsError)"
        :title="localError || drafts.travelApprovalsError"
        type="error"
        :closable="false"
        show-icon
        class="travel-query-alert"
      >
        <el-button
          v-if="!queryNeedsAdjustment"
          link
          type="primary"
          :loading="drafts.loadingReimbursementOptions || drafts.loadingTravelApprovals"
          @click="retrySearch"
        >
          {{ optionsNeedRefresh ? '刷新表单与审批' : '重新加载' }}
        </el-button>
      </el-alert>
    </template>
    <template #empty-actions>
      <el-button
        v-if="!readonly && !queryNeedsAdjustment"
        :loading="drafts.loadingReimbursementOptions || drafts.loadingTravelApprovals"
        @click="retrySearch"
      >
        重新加载
      </el-button>
    </template>
    <template #details="{ row }">
      <span><span class="travel-approval-dates">{{ row.startDate }} 至 {{ row.endDate }}</span> · 出差类别：{{ row.travelTypeLabel }}</span>
      <small v-if="departmentChoices">{{ departmentLabelFor(row) }}</small>
    </template>
    <template #accounting="{ row }">
      <ApprovalAccountingLine v-bind="accountingFor(row)" />
    </template>
  </ApprovalSelectorPanel>
</template>

<style scoped>
.travel-approval-dates { white-space: nowrap; }
</style>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { getSavedGeneralApprovals, listGeneralApprovals } from '@/api/reimbursements'
import type { GeneralApproval, GeneralApprovalIncompleteReason } from '@/types/reimbursements'
import { approvalQueryDateError } from '@/utils/approvalQueryDates'
import ApprovalSelectorPanel from './ApprovalSelectorPanel.vue'

const props = defineProps<{
  modelValue: string[]
  draftId: string
  savedApprovals?: GeneralApproval[]
  mobile: boolean
  active: boolean
  readonly: boolean
  busy: boolean
  saveError: string
}>()
const emit = defineEmits<{ 'update:modelValue': [instanceIds: string[]] }>()
const expanded = defineModel<boolean>('expanded', { required: true })
const fromDate = ref('')
const toDate = ref('')
const keyword = ref('')
const visibleFilters = computed(() => ({
  from: fromDate.value, to: toDate.value, query: keyword.value.trim(),
}))
const activeFilters = ref<{ from: string; to: string; query: string } | null>(null)
const filtersChanged = computed(() => {
  const applied = activeFilters.value
  const visible = visibleFilters.value
  return Boolean(applied && (applied.from !== visible.from || applied.to !== visible.to
    || applied.query !== visible.query))
})
const dateError = computed(() => approvalQueryDateError(fromDate.value, toDate.value, undefined, 365))
const loading = ref(false)
const complete = ref(false)
const error = ref('')
const incomplete = ref(false)
const incompleteReasons = ref<GeneralApprovalIncompleteReason[]>([])
const incompleteDescription = computed(() => {
  const descriptions: Record<GeneralApprovalIncompleteReason, string> = {
    directory: '部分审批模板目录未能读取。',
    list: '部分审批列表未能读取。',
    detail: '部分审批详情未能读取。',
    pagination: '部分审批的分页进度异常。',
  }
  return incompleteReasons.value.map(reason => descriptions[reason]).join('')
    + '当前仅展示已查到的已通过审批，可重新加载。'
})
const nextCursor = ref('')
const items = ref<GeneralApproval[]>([])
const emptyDescription = computed(() => {
  if (props.readonly) return '未关联审批'
  if (error.value) return '审批查询未完成，请重试'
  if (filtersChanged.value) return '筛选条件已修改，请查询审批'
  if (loading.value || nextCursor.value) return '正在查询符合条件的审批'
  if (incomplete.value) return '审批查询未完成，请重新加载'
  return '当前查询范围内没有符合条件的审批'
})
const known = reactive(new Map<string, GeneralApproval>())
const saved = computed(() => new Map(props.savedApprovals?.map(entry => [entry.processInstanceId, entry])))
const pendingIds = ref<string[] | null>(null)
const selectedIds = computed(() => new Set(pendingIds.value ?? props.modelValue))
function approvalRow(processInstanceId: string, entry: GeneralApproval | undefined, linkedOnly: boolean) {
  return {
    processInstanceId,
    title: entry?.title ?? '已关联审批',
    businessId: entry?.businessId ?? '',
    entry,
    linkedOnly,
  }
}
const rows = computed(() => [
  ...items.value.map(entry => approvalRow(entry.processInstanceId, entry, false)),
  ...props.modelValue.filter(id => !items.value.some(entry => entry.processInstanceId === id))
    .map(id => approvalRow(id, known.get(id) ?? saved.value.get(id), true)),
])
const limit = 20
let request: AbortController | undefined
const queryAvailable = computed(() => expanded.value && props.active && !props.readonly)
function cancelSearch(): void {
  const previous = request
  request = undefined
  previous?.abort()
  loading.value = false
}
async function search(cursor?: string): Promise<void> {
  if (!queryAvailable.value || props.busy || (cursor && filtersChanged.value)) return
  if (!cursor && dateError.value) { error.value = dateError.value; return }
  cancelSearch()
  if (!cursor) {
    activeFilters.value = visibleFilters.value
    nextCursor.value = ''
    items.value = []
    incompleteReasons.value = []
  }
  const filters = activeFilters.value
  if (!filters) return
  const controller = new AbortController()
  const draftId = props.draftId
  request = controller
  const acceptsResponse = () => request === controller && !controller.signal.aborted
    && queryAvailable.value && props.draftId === draftId && !filtersChanged.value
  loading.value = true
  complete.value = false
  error.value = ''
  incomplete.value = false
  try {
    let pageCursor = cursor
    const seenCursors = new Set<string>()
    while (acceptsResponse()) {
      if (pageCursor) {
        if (seenCursors.has(pageCursor)) {
          nextCursor.value = ''
          throw new Error('审批查询进度异常，请重新查询')
        }
        seenCursors.add(pageCursor)
      }
      const response = await listGeneralApprovals({
        ...(filters.from ? { from: filters.from, to: filters.to } : {}),
        ...(filters.query ? { query: filters.query } : {}),
        ...(pageCursor ? { cursor: pageCursor } : {}),
      }, controller.signal)
      if (!acceptsResponse()) return
      const approved = response.items.filter(isApproved)
      for (const entry of approved) known.set(entry.processInstanceId, entry)
      const byId = new Map([...items.value, ...approved].map(entry => [entry.processInstanceId, entry]))
      items.value = [...byId.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)
        || b.processInstanceId.localeCompare(a.processInstanceId))
      nextCursor.value = response.nextCursor ?? ''
      incompleteReasons.value = [...new Set([...incompleteReasons.value, ...(response.incompleteReasons ?? [])])]
      complete.value = response.complete && !nextCursor.value && !incompleteReasons.value.length
      incomplete.value = !nextCursor.value && !complete.value
      if (!nextCursor.value) break
      pageCursor = nextCursor.value
    }
  } catch (err) {
    if (!acceptsResponse()) return
    if (apiErrorCode(err) === 'GENERAL_APPROVAL_SEARCH_CURSOR_INVALID') nextCursor.value = ''
    error.value = apiErrorMessage(err, '暂时无法查询审批，请重试')
  } finally {
    if (request === controller) {
      request = undefined
      loading.value = false
    }
  }
}

function toggle(id: string, checked: boolean): void {
  if (props.readonly || props.busy || pendingIds.value || (checked && props.modelValue.length >= limit)) return
  if (checked && !isApproved(known.get(id) ?? saved.value.get(id))) return
  pendingIds.value = checked ? [...props.modelValue, id] : props.modelValue.filter(value => value !== id)
  emit('update:modelValue', pendingIds.value)
}

function isApproved(entry: GeneralApproval | undefined): boolean {
  return entry?.status === 'COMPLETED' && entry.result === 'agree'
}

function statusLabel(entry: GeneralApproval): string {
  if (entry.status === 'RUNNING') return '审批中'
  if (entry.status === 'TERMINATED') return '已终止'
  if (entry.result === 'agree') return '已通过'
  if (entry.result === 'refuse') return '已拒绝'
  return entry.status === 'COMPLETED' ? '已完成' : entry.status
}

watch(() => props.busy, (busy) => { if (!busy) pendingIds.value = null })
watch([queryAvailable, () => props.busy, filtersChanged], ([available, busy, changed]) => {
  if (!available || changed) { cancelSearch(); return }
  if (!busy && !request && !complete.value && !error.value && !incomplete.value) {
    void search(nextCursor.value || undefined)
  }
}, { immediate: true })
watch(() => props.draftId, () => {
  cancelSearch()
  activeFilters.value = null
  fromDate.value = ''; toDate.value = ''; keyword.value = ''
  complete.value = false
  error.value = ''; incomplete.value = false; incompleteReasons.value = []
  nextCursor.value = ''; items.value = []; known.clear(); pendingIds.value = null
  if (queryAvailable.value && !props.busy) void search()
})
watch([expanded, () => props.active, () => props.modelValue, saved, () => props.draftId], ([open, active, ids], _previous, onCleanup) => {
  if (!open || !active || ids.every(id => saved.value.has(id) || known.has(id))) return
  const controller = new AbortController()
  const draftId = props.draftId
  onCleanup(() => controller.abort())
  void getSavedGeneralApprovals(draftId, controller.signal).then(entries => {
    if (!controller.signal.aborted && props.draftId === draftId) {
      for (const entry of entries) known.set(entry.processInstanceId, entry)
    }
  }).catch(() => { /* Keep unavailable saved rows visible and removable. */ })
}, { immediate: true })
onBeforeUnmount(cancelSearch)
</script>

<template>
  <ApprovalSelectorPanel
    v-model:from-date="fromDate"
    v-model:to-date="toDate"
    v-model:keyword="keyword"
    v-model:expanded="expanded"
    kind="general"
    title="关联审批单（选填）"
    :mobile="mobile"
    :readonly="readonly"
    :busy="busy"
    :loading="loading"
    :query-needs-adjustment="Boolean(dateError)"
    :empty-description="emptyDescription"
    :rows="rows"
    :selected-ids="selectedIds"
    :max-selections="limit"
    selection-help="仅可关联本人发起且已通过的审批，选中或取消后自动保存。"
    @search="search()"
    @toggle="(row, checked) => toggle(row.processInstanceId, checked)"
  >
    <template #notifications>
      <p
        v-if="loading"
        role="status"
        class="field-help"
        data-testid="general-approval-query-progress"
      >
        正在查询审批，已找到 {{ items.length }} 张已通过审批，将自动查完当前范围。
      </p>
      <p
        v-else-if="complete && !filtersChanged && !error"
        role="status"
        class="field-help"
        data-testid="general-approval-query-complete"
      >
        当前范围已查完，共 {{ items.length }} 张已通过审批。
      </p>
      <el-alert
        v-if="filtersChanged"
        title="筛选条件已修改，请点击「查询审批」。"
        type="info"
        :closable="false"
        class="travel-query-alert"
      />
      <el-alert
        v-if="(error || incomplete) && !filtersChanged"
        :title="error || '审批查询未完成，已保留当前结果。'"
        :type="error ? 'error' : 'warning'"
        :closable="false"
        class="travel-query-alert"
        show-icon
      >
        <p v-if="!error">
          {{ incompleteDescription }}
        </p>
        <el-button
          v-if="!readonly && !dateError"
          link
          type="primary"
          :disabled="busy"
          :loading="loading"
          @click="search(filtersChanged ? undefined : nextCursor || undefined)"
        >
          重新加载
        </el-button>
      </el-alert>
      <el-alert
        v-if="saveError"
        :title="saveError"
        type="error"
        :closable="false"
        class="travel-query-alert"
        show-icon
      />
    </template>
    <template #status="{ row }">
      <span
        v-if="row.entry"
        class="approval-row-status"
        :class="{ 'is-approved': isApproved(row.entry) }"
      >{{ statusLabel(row.entry) }}</span>
    </template>
    <template #details="{ row }">
      <div
        v-if="row.entry?.summaryFields?.length"
        class="approval-summary-fields"
        data-testid="approval-summary-fields"
      >
        <small
          v-for="(field, index) in row.entry.summaryFields.slice(0, 3)"
          :key="index"
          :title="`${field.label}：${field.value}`"
        >{{ field.label }}：{{ field.value }}</small>
      </div>
      <small
        v-if="row.entry"
        class="approval-created-at"
      >发起日期：{{ row.entry.createdAt.slice(0, 10) }}</small>
      <small v-else>审批详情暂不可用</small>
      <small v-if="row.entry && !isApproved(row.entry) && !readonly">该关联审批未通过，请取消关联后再办理。</small>
    </template>
  </ApprovalSelectorPanel>
</template>

<style scoped>
.approval-row-status { margin-left: auto; flex: none; color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.approval-row-status.is-approved { color: var(--el-color-success); }
.approval-summary-fields { display: grid; gap: 4px; min-width: 0; }
.approval-summary-fields small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--el-text-color-secondary); }
.approval-created-at { margin-top: 3px; }
</style>

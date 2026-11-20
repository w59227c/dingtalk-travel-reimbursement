<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { getSavedGeneralApprovals, listGeneralApprovals } from '@/api/reimbursements'
import type { GeneralApproval } from '@/types/reimbursements'
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
const error = ref('')
const incomplete = ref(false)
const nextCursor = ref('')
const items = ref<GeneralApproval[]>([])
const emptyDescription = computed(() => {
  if (props.readonly) return '未关联审批'
  if (error.value) return '审批查询未完成，请重试'
  if (nextCursor.value) return '本批未找到符合条件的审批'
  if (incomplete.value) return '暂未查到符合条件的审批'
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
let started = false
async function search(cursor?: string): Promise<void> {
  if (props.readonly || props.busy || (cursor && filtersChanged.value)) return
  if (!cursor) {
    if (dateError.value) { error.value = dateError.value; return }
    activeFilters.value = visibleFilters.value
    nextCursor.value = ''
  }
  const filters = activeFilters.value
  if (!filters) return
  request?.abort()
  const controller = new AbortController()
  request = controller
  started = true
  loading.value = true
  error.value = ''
  incomplete.value = false
  try {
    const response = await listGeneralApprovals({
      ...(filters.from ? { from: filters.from, to: filters.to } : {}),
      ...(filters.query ? { query: filters.query } : {}),
      ...(cursor ? { cursor } : {}),
    }, controller.signal)
    if (request !== controller) return
    for (const entry of response.items) known.set(entry.processInstanceId, entry)
    const byId = new Map((cursor ? [...items.value, ...response.items] : response.items)
      .map(entry => [entry.processInstanceId, entry]))
    items.value = [...byId.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)
      || b.processInstanceId.localeCompare(a.processInstanceId))
    nextCursor.value = response.nextCursor ?? ''
    incomplete.value = !response.complete && !response.nextCursor
  } catch (err) {
    if (request !== controller) return
    if (!cursor) items.value = []
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
  pendingIds.value = checked ? [...props.modelValue, id] : props.modelValue.filter(value => value !== id)
  emit('update:modelValue', pendingIds.value)
}

function statusLabel(entry: GeneralApproval): string {
  if (entry.status === 'RUNNING') return '审批中'
  if (entry.status === 'TERMINATED') return '已终止'
  if (entry.result === 'agree') return '已通过'
  if (entry.result === 'refuse') return '已拒绝'
  return entry.status === 'COMPLETED' ? '已完成' : entry.status
}

watch(() => props.busy, (busy) => { if (!busy) pendingIds.value = null })
watch(() => expanded.value && props.active && !props.readonly && !props.busy, (ready) => {
  if (ready && !started) void search()
}, { immediate: true })
watch([expanded, () => props.active, () => props.modelValue, saved], ([open, active, ids], _previous, onCleanup) => {
  if (!open || !active || ids.every(id => saved.value.has(id) || known.has(id))) return
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  void getSavedGeneralApprovals(props.draftId, controller.signal).then(entries => {
    if (!controller.signal.aborted) for (const entry of entries) known.set(entry.processInstanceId, entry)
  }).catch(() => { /* Keep unavailable saved rows visible and removable. */ })
}, { immediate: true })
onBeforeUnmount(() => { request?.abort(); request = undefined })
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
    @search="search()"
    @toggle="(row, checked) => toggle(row.processInstanceId, checked)"
  >
    <template #notifications>
      <el-alert
        v-if="nextCursor && !error"
        :title="filtersChanged ? '筛选条件已修改，请点击「查询审批」。' : '还有审批可查，继续查看当前筛选条件下的结果。'"
        type="info"
        :closable="false"
        class="travel-query-alert"
      >
        <el-button
          link
          type="primary"
          :disabled="busy || filtersChanged"
          :loading="loading"
          @click="search(nextCursor)"
        >
          继续查找
        </el-button>
      </el-alert>
      <el-alert
        v-if="error || incomplete"
        :title="error || '部分审批暂未查到，可以重新加载。'"
        :type="error ? 'error' : 'warning'"
        :closable="false"
        class="travel-query-alert"
        show-icon
      >
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
    <template #empty-actions>
      <el-button
        v-if="!readonly && !dateError && !nextCursor"
        :disabled="busy"
        :loading="loading"
        @click="search()"
      >
        重新加载
      </el-button>
    </template>
    <template #details="{ row }">
      <span v-if="row.entry">{{ row.entry.createdAt.slice(0, 10) }} · {{ statusLabel(row.entry) }}</span>
      <small v-else>审批详情暂不可用</small>
    </template>
  </ApprovalSelectorPanel>
</template>

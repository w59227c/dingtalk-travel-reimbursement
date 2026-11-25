<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { getSavedGeneralApprovals, listGeneralApprovals } from '@/api/reimbursements'
import type { GeneralApproval, GeneralApprovalIncompleteReason, GeneralApprovalTemplate } from '@/types/reimbursements'
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
const processCode = ref('')
const templates = ref<GeneralApprovalTemplate[]>([])
const queryTemplates = ref<GeneralApprovalTemplate[]>([])
const visibleRange = computed(() => ({
  from: fromDate.value, to: toDate.value,
}))
const activeRange = ref<{ from: string; to: string } | null>(null)
const rangeChanged = computed(() => {
  const applied = activeRange.value
  const visible = visibleRange.value
  return Boolean(applied && (applied.from !== visible.from || applied.to !== visible.to))
})
const querySequence = ref(0)
const normalizedKeyword = computed(() => keyword.value.trim().toLowerCase())
const resultScope = computed(() => JSON.stringify([
  props.draftId, querySequence.value, processCode.value, normalizedKeyword.value,
]))
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
    + '当前仅展示已查到的审批，可重新加载。'
})
const nextCursor = ref('')
const items = ref<GeneralApproval[]>([])
const emptyDescription = computed(() => {
  if (props.readonly) return '未关联审批'
  if (error.value) return '审批查询未完成，请重试'
  if (rangeChanged.value) return '日期范围已修改，请查询审批'
  if (loading.value || nextCursor.value) return '已加载结果中暂无匹配审批，正在继续查询'
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
    linkedOnlyLabel: !linkedOnly ? undefined
      : items.value.some(item => item.processInstanceId === processInstanceId)
        ? '已关联 · 不符合当前筛选' : '已关联 · 不在当前查询结果中',
  }
}
const filteredItems = computed(() => items.value.filter(entry => (
  !processCode.value || entry.processCode === processCode.value
) && (!normalizedKeyword.value || [entry.title, entry.businessId,
  ...(entry.summaryFields?.slice(0, 3).map(field => `${field.label} ${field.value}`) ?? []),
].join(' ').toLowerCase().includes(normalizedKeyword.value))))
const rows = computed(() => [
  ...filteredItems.value.map(entry => approvalRow(entry.processInstanceId, entry, false)),
  ...props.modelValue.filter(id => !filteredItems.value.some(entry => entry.processInstanceId === id))
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
  if (!queryAvailable.value || props.busy || (cursor && rangeChanged.value)) return
  if (!cursor && dateError.value) { error.value = dateError.value; return }
  cancelSearch()
  if (!cursor) {
    activeRange.value = visibleRange.value
    querySequence.value += 1
    nextCursor.value = ''
    items.value = []
    queryTemplates.value = []
    incompleteReasons.value = []
  }
  const range = activeRange.value
  if (!range) return
  const controller = new AbortController()
  const draftId = props.draftId
  request = controller
  const acceptsResponse = () => request === controller && !controller.signal.aborted
    && queryAvailable.value && props.draftId === draftId && !rangeChanged.value
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
        ...(range.from ? { from: range.from, to: range.to } : {}),
        ...(pageCursor ? { cursor: pageCursor } : {}),
      }, controller.signal)
      if (!acceptsResponse()) return
      if (response.templates) {
        queryTemplates.value = [...new Map([...queryTemplates.value, ...response.templates]
          .map(template => [template.processCode, template])).values()]
      }
      for (const entry of response.items) known.set(entry.processInstanceId, entry)
      const byId = new Map([...items.value, ...response.items].map(entry => [entry.processInstanceId, entry]))
      items.value = [...byId.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)
        || b.processInstanceId.localeCompare(a.processInstanceId))
      nextCursor.value = response.nextCursor ?? ''
      incompleteReasons.value = [...new Set([...incompleteReasons.value, ...(response.incompleteReasons ?? [])])]
      complete.value = response.complete && !nextCursor.value && !incompleteReasons.value.length
      incomplete.value = !nextCursor.value && !complete.value
      // Only a complete range disproves previously discovered types. Failed
      // searches and cursor resumes keep both the options and selection usable.
      templates.value = complete.value ? [...queryTemplates.value]
        : [...new Map([...templates.value, ...queryTemplates.value]
          .map(template => [template.processCode, template])).values()]
      if (complete.value && !templates.value.some(template => template.processCode === processCode.value)) {
        processCode.value = ''
      }
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
  if (checked && !(known.get(id) ?? saved.value.get(id))) return
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
watch([queryAvailable, () => props.busy, rangeChanged], ([available, busy, changed]) => {
  if (!available || changed) { cancelSearch(); return }
  if (!busy && !request && !complete.value && !error.value && !incomplete.value) {
    void search(nextCursor.value || undefined)
  }
}, { immediate: true })
watch(() => props.draftId, () => {
  cancelSearch()
  activeRange.value = null
  fromDate.value = ''; toDate.value = ''; keyword.value = ''; processCode.value = ''; templates.value = []
  queryTemplates.value = []
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
    :result-scope="resultScope"
    :search-label="rangeChanged ? '查询审批' : '刷新审批'"
    :max-selections="limit"
    selection-help="可关联本人发起的各状态审批，选中或取消后自动保存。"
    @search="search()"
    @toggle="(row, checked) => toggle(row.processInstanceId, checked)"
  >
    <template #category-filter>
      <div
        v-if="!readonly"
        class="approval-category-filter"
      >
        <el-select
          v-model="processCode"
          aria-label="审批类别"
          placeholder="全部审批单"
          :disabled="busy"
          filterable
        >
          <el-option
            label="全部审批单"
            value=""
          />
          <el-option
            v-for="item in templates"
            :key="item.processCode"
            :label="item.name"
            :value="item.processCode"
          />
        </el-select>
      </div>
    </template>
    <template #notifications>
      <p
        v-if="loading"
        role="status"
        class="field-help"
        data-testid="general-approval-query-progress"
      >
        正在查询审批，已找到 {{ items.length }} 张，当前筛选 {{ filteredItems.length }} 张；结果仍在补充。
      </p>
      <p
        v-else-if="complete && !rangeChanged && !error"
        role="status"
        class="field-help"
        data-testid="general-approval-query-complete"
      >
        当前范围已查完，共查到 {{ items.length }} 张审批，当前筛选 {{ filteredItems.length }} 张。
      </p>
      <p
        v-else-if="activeRange && !rangeChanged"
        class="field-help"
        data-testid="general-approval-query-count"
      >
        已查到 {{ items.length }} 张审批，当前筛选 {{ filteredItems.length }} 张。
      </p>
      <el-alert
        v-if="rangeChanged"
        title="日期范围已修改，请点击「查询审批」。当前仍展示上次查询结果。"
        type="info"
        :closable="false"
        class="travel-query-alert"
      />
      <el-alert
        v-if="dateError || ((error || incomplete) && !rangeChanged)"
        :title="dateError || error || '审批查询未完成，已保留当前结果。'"
        :type="dateError || error ? 'error' : 'warning'"
        :closable="false"
        class="travel-query-alert"
        show-icon
      >
        <p v-if="!dateError && !error">
          {{ incompleteDescription }}
        </p>
        <el-button
          v-if="!readonly && !dateError"
          link
          type="primary"
          :disabled="busy"
          :loading="loading"
          @click="search(rangeChanged ? undefined : nextCursor || undefined)"
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
    </template>
  </ApprovalSelectorPanel>
</template>

<style scoped>
.approval-row-status { margin-left: auto; flex: none; color: var(--el-text-color-secondary); font-size: 12px; font-weight: normal; }
.approval-row-status.is-approved { color: var(--el-color-success); }
.approval-summary-fields { display: grid; gap: 4px; min-width: 0; }
.approval-summary-fields small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--el-text-color-secondary); }
.approval-created-at { margin-top: 3px; }
.approval-category-filter .el-select {
  width: 100%;
}
</style>

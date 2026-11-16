<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

import { listReimbursementEmployees } from '@/api/auth'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import type { ReimbursementEmployee } from '@/types/auth'

const open = defineModel<boolean>({ required: true })
const props = defineProps<{
  switching: boolean
  switchError: string
}>()
const emit = defineEmits<{ choose: [employee: ReimbursementEmployee] }>()

const query = ref('')
const loading = ref(false)
const searched = ref(false)
const error = ref('')
const matches = ref<ReimbursementEmployee[]>([])
let pendingSearch: AbortController | undefined

watch(open, () => {
  pendingSearch?.abort()
  query.value = ''
  error.value = ''
  matches.value = []
  loading.value = false
  searched.value = false
})
onBeforeUnmount(() => pendingSearch?.abort())

async function search(): Promise<void> {
  const text = query.value.trim()
  if (!open.value || !text || props.switching) return
  pendingSearch?.abort()
  const request = new AbortController()
  pendingSearch = request
  loading.value = true
  searched.value = false
  error.value = ''
  matches.value = []
  try {
    const result = await listReimbursementEmployees(text, request.signal)
    if (!request.signal.aborted && open.value) {
      matches.value = result
      searched.value = true
    }
  } catch (cause) {
    if (!request.signal.aborted && open.value) {
      error.value = ['REIMBURSEMENT_EMPLOYEE_SEARCH_UNAVAILABLE', 'DINGTALK_PERMISSION_MISSING'].includes(apiErrorCode(cause) ?? '')
        ? '钉钉姓名搜索不可用，请输入准确的员工 userId 后重试'
        : apiErrorMessage(cause, '员工查询失败，请重试')
    }
  } finally {
    if (pendingSearch === request) {
      pendingSearch = undefined
      loading.value = false
    }
  }
}

function invalidateResults(): void {
  pendingSearch?.abort()
  matches.value = []
  searched.value = false
  error.value = ''
}
</script>

<template>
  <el-dialog
    v-model="open"
    title="为谁报销"
    width="min(480px, calc(100vw - 32px))"
    :close-on-click-modal="!props.switching"
    :close-on-press-escape="!props.switching"
    :show-close="!props.switching"
  >
    <form
      class="employee-search"
      @submit.prevent="search"
    >
      <el-input
        v-model="query"
        aria-label="员工姓名或 userId"
        placeholder="输入员工姓名或 userId"
        maxlength="128"
        :disabled="props.switching"
        @input="invalidateResults"
      />
      <el-button
        native-type="submit"
        type="primary"
        :disabled="!query.trim() || props.switching"
        :loading="loading"
      >
        搜索
      </el-button>
    </form>
    <p class="employee-search-help">
      姓名搜索不可用时，可以用准确的员工 userId 搜索。
    </p>
    <p
      v-if="error || props.switchError"
      class="employee-picker-error"
      role="alert"
    >
      {{ error || props.switchError }}
    </p>
    <p
      v-if="searched && !matches.length"
      role="status"
    >
      未找到可选择的员工
    </p>
    <ul
      v-if="matches.length"
      class="employee-matches"
      aria-label="匹配的员工"
    >
      <li
        v-for="employee in matches"
        :key="employee.userId"
      >
        <button
          type="button"
          class="employee-match"
          :disabled="props.switching"
          @click="emit('choose', employee)"
        >
          <strong>{{ employee.name }}</strong>
          <span v-if="employee.departmentNames?.length">{{ employee.departmentNames.join('、') }}</span>
          <span>{{ employee.userId }}</span>
        </button>
      </li>
    </ul>
    <template #footer>
      <el-button
        :disabled="props.switching"
        @click="open = false"
      >
        取消
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.employee-search {
  display: flex;
  gap: 8px;
}

.employee-matches {
  padding: 0;
  margin: 12px 0 0;
  list-style: none;
  max-height: min(48vh, 320px);
  overflow-y: auto;
}

.employee-search-help { color: var(--el-text-color-secondary); margin: 8px 0 0; }

.employee-match {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 10px;
  padding: 10px 4px;
  width: 100%;
  border: 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: transparent;
  color: var(--el-text-color-regular);
  text-align: left;
  cursor: pointer;
}

.employee-match:hover:not(:disabled) {
  background: var(--el-fill-color-light);
}

.employee-match span {
  font-size: 12px;
  overflow-wrap: anywhere;
}

.employee-picker-error {
  color: var(--el-color-danger);
}
</style>

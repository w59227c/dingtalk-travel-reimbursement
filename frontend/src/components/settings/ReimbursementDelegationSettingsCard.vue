<script setup lang="ts">
import { ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import { getReimbursementDelegationSettings, updateReimbursementDelegationSettings } from '@/api/settings'
import type { ReimbursementDelegationSettings } from '@/api/settings'
import DirectoryGrantField from '@/components/settings/DirectoryGrantField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const snapshot = ref<ReimbursementDelegationSettings | null>(null)
const employeeUserIds = ref<string[]>([])
const departmentIds = ref<string[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const notice = ref('')
let activeRequest: AbortController | undefined

const formValue = computed(() => ({
  employeeUserIds: [...employeeUserIds.value].sort(),
  departmentIds: [...departmentIds.value].sort(),
}))
const dirty = computed(() => snapshot.value !== null
  && (JSON.stringify(formValue.value.employeeUserIds) !== JSON.stringify([...snapshot.value.employeeUserIds].sort())
    || JSON.stringify(formValue.value.departmentIds) !== JSON.stringify([...snapshot.value.departmentIds].sort())))

function adopt(value: ReimbursementDelegationSettings): void {
  snapshot.value = value
  employeeUserIds.value = [...value.employeeUserIds]
  departmentIds.value = [...value.departmentIds]
}

async function load(): Promise<void> {
  if (loading.value || saving.value || !auth.isAdmin) return
  const request = new AbortController()
  activeRequest = request
  loading.value = true
  error.value = ''
  notice.value = ''
  try {
    const next = await getReimbursementDelegationSettings(request.signal)
    if (!request.signal.aborted) adopt(next)
  } catch (cause) {
    if (!request.signal.aborted) error.value = apiErrorMessage(cause, '代报权限加载失败，请重试')
  } finally {
    if (activeRequest === request) {
      activeRequest = undefined
      loading.value = false
    }
  }
}

async function reload(): Promise<void> {
  if (loading.value || saving.value) return
  const original = snapshot.value
  if (dirty.value) {
    try {
      await ElMessageBox.confirm('重新加载会舍弃当前未保存的授权名单。', '重新加载代报权限', {
        type: 'warning', confirmButtonText: '重新加载', cancelButtonText: '保留当前输入',
      })
    } catch { return }
  }
  if (snapshot.value === original) await load()
}

async function save(): Promise<void> {
  if (!snapshot.value || !dirty.value || loading.value || saving.value || !auth.isAdmin) return
  const request = new AbortController()
  activeRequest = request
  saving.value = true
  error.value = ''
  notice.value = ''
  const value = { ...formValue.value, revision: snapshot.value.revision }
  const inputs = JSON.stringify(formValue.value)
  try {
    const saved = await updateReimbursementDelegationSettings(value, request.signal)
    if (request.signal.aborted) return
    if (JSON.stringify(formValue.value) === inputs) {
      adopt(saved)
      notice.value = '代报权限已保存'
    } else {
      snapshot.value = saved
      notice.value = '已保存，当前名单还有修改未保存'
    }
    void auth.refreshMe().catch(() => { /* The next authenticated request checks the saved grant. */ })
  } catch (cause) {
    if (!request.signal.aborted) error.value = apiErrorMessage(cause, '代报权限保存失败，请重试')
  } finally {
    if (activeRequest === request) {
      activeRequest = undefined
      saving.value = false
    }
  }
}

watch(
  () => [auth.session?.operator?.userId ?? auth.session?.user.userId, auth.isAdmin] as const,
  ([userId, admin], previous) => {
    if (previous?.[0] === userId && previous?.[1] === admin) return
    activeRequest?.abort()
    activeRequest = undefined
    snapshot.value = null
    employeeUserIds.value = []
    departmentIds.value = []
    loading.value = false
    saving.value = false
    error.value = ''
    notice.value = ''
    if (admin) void load()
  },
  { immediate: true, flush: 'sync' },
)
onBeforeUnmount(() => activeRequest?.abort())
</script>

<template>
  <el-card
    id="reimbursement-delegation"
    shadow="never"
  >
    <template #header>
      <strong>代报权限</strong>
    </template>
    <p class="delegation-help">
      名单中的员工及部门成员可以帮同事报销。代报时只显示抄送给经办人的已通过出差审批。
    </p>
    <div class="delegation-inputs">
      <div>
        <strong>允许代报的部门</strong>
        <DirectoryGrantField
          v-model="departmentIds"
          kind="department"
          label="允许代报的部门"
          :disabled="loading || saving || !snapshot"
        />
      </div>
      <div>
        <strong>允许代报的员工</strong>
        <DirectoryGrantField
          v-model="employeeUserIds"
          kind="employee"
          label="允许代报的员工"
          :disabled="loading || saving || !snapshot"
        />
      </div>
    </div>
    <p
      v-if="error || notice"
      :role="error ? 'alert' : 'status'"
      :class="{ 'delegation-error': error }"
    >
      {{ error || notice }}
    </p>
    <div class="delegation-actions">
      <el-button
        type="primary"
        :disabled="!dirty || loading"
        :loading="saving"
        @click="save"
      >
        保存代报权限
      </el-button>
      <el-button
        :disabled="saving"
        :loading="loading"
        @click="reload"
      >
        重新加载代报权限
      </el-button>
    </div>
  </el-card>
</template>

<style scoped>
.delegation-help { margin: 0 0 12px; }
.delegation-inputs { display: grid; gap: 10px; }
.delegation-inputs strong { display: inline-block; margin-bottom: 4px; }
.delegation-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.delegation-actions :deep(.el-button) { margin: 0; }
.delegation-error { color: var(--el-color-danger); }
</style>

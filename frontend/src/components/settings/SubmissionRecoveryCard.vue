<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { recoverReimbursementSubmission } from '@/api/submissionRecovery'
import type { ReimbursementSubmission } from '@/types/reimbursements'

const submissionId = ref('')
const processInstanceId = ref('')
const recovering = ref(false)
const result = ref<ReimbursementSubmission | null>(null)
const resultStatusLabel = computed(() => result.value?.status === 'VERIFYING'
  ? '正在重新核对审批'
  : result.value?.status === 'ORPHAN_CLEANUP'
    ? '正在清理未关联附件'
    : '处理状态已更新')

async function attachInstance(): Promise<void> {
  const submission = submissionId.value.trim()
  const instance = processInstanceId.value.trim()
  if (!submission || !instance) {
    ElMessage.error('请填写提交记录 ID 和钉钉审批实例 ID')
    return
  }
  recovering.value = true
  try {
    result.value = await recoverReimbursementSubmission(submission, {
      action: 'ATTACH_INSTANCE',
      processInstanceId: instance,
    })
    ElMessage.success('已恢复审批回读，系统将继续核对')
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '恢复审批核对失败'))
  } finally {
    recovering.value = false
  }
}

async function confirmNotCreated(): Promise<void> {
  const submission = submissionId.value.trim()
  if (!submission) {
    ElMessage.error('请填写提交记录 ID')
    return
  }
  try {
    await ElMessageBox.confirm(
      '仅在钉钉中确认这次 OA 确实没有创建时继续。系统会清理已上传但未关联的附件。',
      '确认 OA 未创建',
      {
        type: 'warning',
        confirmButtonText: '确认未创建',
        cancelButtonText: '取消',
        showClose: false,
        closeOnClickModal: false,
      },
    )
  } catch {
    return
  }
  await recoverNotCreated(submission, false)
}

async function recoverNotCreated(
  submission: string,
  confirmUncertainUploadsAbsent: boolean,
): Promise<void> {
  recovering.value = true
  try {
    result.value = await recoverReimbursementSubmission(submission, {
      action: 'CONFIRM_NOT_CREATED',
      confirmUncertainUploadsAbsent,
    })
    ElMessage.success('已进入自动清理流程')
  } catch (error) {
    if (
      !confirmUncertainUploadsAbsent
      && apiErrorCode(error) === 'OA_REMOTE_FILE_CONFIRMATION_REQUIRED'
    ) {
      recovering.value = false
      try {
        await ElMessageBox.confirm(
          '有附件的提交结果未知。请先在钉钉文件中确认这些附件也不存在；确认后系统才会放弃未知记录并继续清理。',
          '再次确认附件不存在',
          {
            type: 'error',
            confirmButtonText: '已核实，不存在',
            cancelButtonText: '取消',
            showClose: false,
            closeOnClickModal: false,
          },
        )
      } catch {
        return
      }
      await recoverNotCreated(submission, true)
      return
    }
    ElMessage.error(apiErrorMessage(error, '异常提交恢复失败'))
  } finally {
    recovering.value = false
  }
}
</script>

<template>
  <el-card
    shadow="never"
    class="submission-recovery-card"
  >
    <template #header>
      <div class="submission-recovery-heading">
        <strong>异常提交恢复</strong>
        <el-tag type="warning">
          管理员工具
        </el-tag>
      </div>
    </template>
    <el-alert
      title="仅处理停在“需要人工核对”的报销提交；普通失败会由系统自动重试。"
      type="info"
      :closable="false"
      show-icon
    />
    <el-form label-position="top">
      <el-form-item label="提交记录 ID">
        <el-input
          v-model="submissionId"
          maxlength="36"
          placeholder="由报销人页面上的异常信息提供"
        />
      </el-form-item>
      <el-form-item label="已找到的钉钉审批实例 ID（可选）">
        <el-input
          v-model="processInstanceId"
          maxlength="128"
          placeholder="确认 OA 已创建时填写"
        />
      </el-form-item>
      <div class="submission-recovery-actions">
        <el-button
          type="primary"
          :loading="recovering"
          @click="attachInstance"
        >
          绑定审批并继续核对
        </el-button>
        <el-button
          type="danger"
          plain
          :disabled="recovering"
          @click="confirmNotCreated"
        >
          确认未创建并清理
        </el-button>
      </div>
    </el-form>
    <el-alert
      v-if="result"
      class="submission-recovery-result"
      type="success"
      :closable="false"
      :title="resultStatusLabel"
      :description="result.processInstanceId ? `审批实例：${result.processInstanceId}` : '系统正在清理未关联附件'"
      show-icon
    />
  </el-card>
</template>

<style scoped>
.submission-recovery-card {
  margin-bottom: 20px;
}

.submission-recovery-heading,
.submission-recovery-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.submission-recovery-card :deep(.el-alert) {
  margin-bottom: 18px;
}

.submission-recovery-result {
  margin-top: 18px;
  margin-bottom: 0 !important;
}
</style>

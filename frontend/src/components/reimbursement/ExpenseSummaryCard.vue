<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed } from 'vue'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'

const props = withDefaults(defineProps<{
  mobile?: boolean
  previewDisabledReason?: string
  beforePreview?: () => Promise<void>
}>(), {
  mobile: false,
  previewDisabledReason: '',
  beforePreview: undefined,
})

const expense = useExpenseStore()
const drafts = useReimbursementDraftStore()
const summaryCalculationError = computed(() => {
  if (!expense.calculationError) return ''
  if (expense.includeSubsidy && !expense.itemReadinessError) return ''
  return expense.calculationError
})

const disabledReason = computed(() => {
  if (!drafts.currentDraft) return '正在准备报销表单'
  if (props.previewDisabledReason) return props.previewDisabledReason
  if (drafts.pendingMutations > 0 && !props.beforePreview) return '请等待内容保存完成'
  return ''
})
async function downloadExcel(): Promise<void> {
  if (disabledReason.value) {
    ElMessage.warning(disabledReason.value)
    return
  }
  try {
    await props.beforePreview?.()
    if (props.mobile) {
      await drafts.openExcelPreviewInDingTalk()
      ElMessage.success('已在钉钉中打开 Excel 预览')
    } else {
      await drafts.downloadExcelPreview()
      ElMessage.success('已生成报销单 Excel 预览')
    }
  } catch (error) {
    ElMessage.error(
      drafts.mutationError
      || (error instanceof Error && error.message ? error.message : 'Excel 预览生成失败，请重试'),
    )
  }
}
</script>

<template>
  <el-card
    v-loading="expense.calculating"
    shadow="never"
    class="content-card summary-card"
  >
    <div class="totals-grid">
      <div><span>票据金额</span><strong>¥{{ expense.displayExpenseTotal }}</strong></div>
      <div><span>出差补助</span><strong>¥{{ expense.displaySubsidyTotal }}</strong></div>
      <div><span>票据张数</span><strong>{{ expense.displayReceiptCount }} 张</strong></div>
      <div class="grand-total">
        <span>合计</span><strong>¥{{ expense.displayTotal }}</strong>
      </div>
    </div>
    <el-alert
      v-if="summaryCalculationError"
      :title="summaryCalculationError"
      type="error"
      :closable="false"
    />
    <div class="excel-download-action">
      <el-button
        :loading="drafts.downloadingPreview"
        :disabled="Boolean(disabledReason)"
        :data-testid="props.mobile ? 'mobile-excel-preview-button' : undefined"
        @click="downloadExcel"
      >
        {{ props.mobile ? '打开 Excel' : '预览 Excel' }}
      </el-button>
      <p
        v-if="disabledReason"
        class="field-error"
      >
        {{ disabledReason }}
      </p>
      <p
        v-else
        class="field-help excel-preview-help"
      >
        {{ props.mobile
          ? '点击后由钉钉下载并打开报销单；正式提交时仍会重新生成最终文件。'
          : '预览前会自动保存当前内容；正式提交时生成最终报销单和票据汇总 PDF。' }}
      </p>
    </div>
  </el-card>
</template>

<style scoped>
.excel-preview-help {
  max-width: 520px;
  text-align: right;
}

@media (max-width: 600px) {
  .excel-preview-help {
    text-align: left;
  }
}
</style>

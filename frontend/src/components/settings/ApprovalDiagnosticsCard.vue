<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { diagnoseTravelApproval } from '@/api/approvalDiagnostics'
import { apiErrorMessage } from '@/api/errors'
import { useAuthStore } from '@/stores/auth'
import type {
  ApprovalDiagnosticCheckStatus,
  ApprovalDiagnosticCounts,
  ApprovalDiagnosticReport,
  ApprovalIdentifierKind,
} from '@/types/approvalDiagnostics'

const props = withDefaults(defineProps<{ mobile?: boolean }>(), { mobile: false })
const auth = useAuthStore()
const userId = ref('')
const identifier = ref('')
const identifierKind = ref<ApprovalIdentifierKind>('auto')
const fromDate = ref('')
const toDate = ref('')
const keyword = ref('')
const loading = ref(false)
const errorMessage = ref('')
const report = ref<ApprovalDiagnosticReport | null>(null)
const summaryOpen = ref(false)
const copyMessage = ref('')
let generation = 0
let controller: AbortController | null = null

const resultType = computed(() => {
  if (report.value?.outcome === 'visible') return 'success'
  if (report.value?.outcome === 'blocked') return 'error'
  return 'warning'
})
const checkedAt = computed(() => report.value
  ? new Date(report.value.checkedAt).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '')
const safeSummary = computed(() => report.value ? JSON.stringify({
  requestId: report.value.requestId,
  checkedAt: report.value.checkedAt,
  outcome: report.value.outcome,
  summary: report.value.summary,
  configVersion: report.value.configVersion,
  queryWindow: report.value.queryWindow,
  counts: report.value.counts,
  checks: report.value.checks.map(({ key, status }) => ({ key, status })),
  error: report.value.error,
}, null, 2) : '')
const metrics: { key: Exclude<keyof ApprovalDiagnosticCounts, 'exclusions'>, label: string }[] = [
  { key: 'templates', label: '查询模板数' },
  { key: 'listed', label: '钉钉返回实例数' },
  { key: 'detailsRead', label: '成功读取详情数' },
  { key: 'excluded', label: '规则或关键词排除数' },
  { key: 'returned', label: '最终返回数' },
  { key: 'unselectable', label: '返回但不可选择数' },
]
const checkLabels: Record<ApprovalDiagnosticCheckStatus, string> = {
  passed: '通过', failed: '未通过', unknown: '未确认', skipped: '未检查',
}
const checkTypes = {
  passed: 'success', failed: 'danger', unknown: 'warning', skipped: 'info',
} as const

function invalidate(): void {
  generation += 1
  controller?.abort()
  controller = null
  loading.value = false
  report.value = null
  errorMessage.value = ''
  copyMessage.value = ''
  summaryOpen.value = false
}

watch([userId, identifier, identifierKind, fromDate, toDate, keyword], invalidate)
watch(() => [auth.session?.user.userId, auth.isAdmin], invalidate)
onBeforeUnmount(invalidate)

function reset(): void {
  invalidate()
  userId.value = ''
  identifier.value = ''
  identifierKind.value = 'auto'
  fromDate.value = ''
  toDate.value = ''
  keyword.value = ''
}

async function run(): Promise<void> {
  if (loading.value) return
  invalidate()
  if (!userId.value.trim() || !identifier.value.trim()) {
    errorMessage.value = '请填写员工 userId 和审批编号或实例 ID'
    return
  }
  if (Boolean(fromDate.value) !== Boolean(toDate.value)) {
    errorMessage.value = '自定义查询范围需要同时填写开始和结束日期'
    return
  }
  if (fromDate.value && (fromDate.value > toDate.value
    || (Date.parse(toDate.value) - Date.parse(fromDate.value)) / 86400000 >= 180)) {
    errorMessage.value = '查询范围必须连续且不超过 180 天'
    return
  }
  controller = new AbortController()
  const activeController = controller
  const version = generation
  loading.value = true
  try {
    const result = await diagnoseTravelApproval({
      userId: userId.value.trim(), approvalIdentifier: identifier.value.trim(),
      identifierKind: identifierKind.value,
      ...(fromDate.value ? { from: fromDate.value, to: toDate.value } : {}),
      ...(keyword.value.trim() ? { keyword: keyword.value.trim() } : {}),
    }, activeController.signal)
    if (version === generation && !activeController.signal.aborted) report.value = result
  } catch (error) {
    if (version === generation && !activeController.signal.aborted) {
      errorMessage.value = apiErrorMessage(error, '审批诊断请求失败，请重试')
    }
  } finally {
    if (version === generation) {
      loading.value = false
      controller = null
    }
  }
}

async function copySummary(): Promise<void> {
  try {
    await navigator.clipboard.writeText(safeSummary.value)
    copyMessage.value = '已复制脱敏摘要'
  } catch {
    summaryOpen.value = true
    copyMessage.value = '请选中下方摘要手动复制'
  }
}
</script>

<template>
  <el-card
    id="approval-diagnostics"
    shadow="never"
    class="content-card approval-diagnostics-card"
    data-testid="approval-diagnostics-card"
  >
    <template #header>
      <strong>出差审批诊断</strong>
    </template>
    <p class="field-help">
      管理员可按员工 userId 复现审批查询，查看查不到或不可选择的原因。诊断只读取数据，不修改审批、草稿或模板配置。
    </p>
    <el-form
      label-position="top"
      @submit.prevent="run"
    >
      <div
        class="diagnostic-form-grid"
        :class="{ 'diagnostic-form-grid--mobile': props.mobile }"
      >
        <el-form-item label="员工 userId">
          <el-input
            v-model="userId"
            aria-label="诊断员工 userId"
            autocomplete="off"
            :maxlength="128"
            :disabled="loading"
            placeholder="钉钉员工 userId"
          />
        </el-form-item>
        <el-form-item label="审批编号 / 实例 ID">
          <el-input
            v-model="identifier"
            aria-label="诊断审批编号或实例 ID"
            autocomplete="off"
            :maxlength="512"
            :disabled="loading"
            placeholder="支持审批页面的长数字编号或实例 ID"
          />
        </el-form-item>
      </div>
      <details class="diagnostic-advanced">
        <summary>更多查询条件</summary>
        <p class="field-help">
          默认按最近 180 天查询。可还原员工页面的日期范围和关键词；日期范围对应审批发起时间。
        </p>
        <div
          class="diagnostic-form-grid"
          :class="{ 'diagnostic-form-grid--mobile': props.mobile }"
        >
          <el-form-item label="查询开始日期">
            <el-date-picker
              v-model="fromDate"
              type="date"
              value-format="YYYY-MM-DD"
              format="YYYY-MM-DD"
              aria-label="诊断查询开始日期"
              :disabled="loading"
              placeholder="默认最近 180 天"
            />
          </el-form-item>
          <el-form-item label="查询结束日期">
            <el-date-picker
              v-model="toDate"
              type="date"
              value-format="YYYY-MM-DD"
              format="YYYY-MM-DD"
              aria-label="诊断查询结束日期"
              :disabled="loading"
              placeholder="默认今天"
            />
          </el-form-item>
          <el-form-item label="原查询关键词">
            <el-input
              v-model="keyword"
              aria-label="诊断查询关键词"
              :maxlength="100"
              :disabled="loading"
              placeholder="可留空，匹配标题或审批编号"
            />
          </el-form-item>
          <el-form-item label="审批标识类型">
            <el-select
              v-model="identifierKind"
              aria-label="审批标识类型"
              :disabled="loading"
            >
              <el-option
                label="自动识别"
                value="auto"
              />
              <el-option
                label="审批编号"
                value="business_id"
              />
              <el-option
                label="实例 ID"
                value="instance_id"
              />
            </el-select>
          </el-form-item>
        </div>
      </details>
      <div class="diagnostic-actions">
        <el-button
          type="primary"
          native-type="submit"
          :loading="loading"
        >
          开始诊断
        </el-button>
        <el-button @click="reset">
          {{ loading ? '取消并清空' : '清空' }}
        </el-button>
      </div>
    </el-form>
    <el-alert
      v-if="loading"
      title="正在复现员工查询并核对审批，尚未得出结论"
      type="info"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="errorMessage"
      :title="errorMessage"
      type="error"
      :closable="false"
      show-icon
      role="alert"
    />
    <section
      v-if="report"
      class="diagnostic-report"
      aria-label="审批诊断结果"
      aria-live="polite"
    >
      <el-alert
        :title="report.summary"
        :type="resultType"
        :closable="false"
        show-icon
      />
      <p class="field-help">
        本次范围：{{ report.queryWindow.from }} 至 {{ report.queryWindow.to }} · {{ checkedAt }}
      </p>
      <p
        v-if="report.counts"
        class="field-help"
      >
        以下数量统计员工查询阶段；额外的目标回查不计入这些数量。
      </p>
      <dl
        v-if="report.counts"
        class="diagnostic-metrics"
        :class="{ 'diagnostic-metrics--mobile': props.mobile }"
      >
        <div
          v-for="metric in metrics"
          :key="metric.key"
        >
          <dt>{{ metric.label }}</dt><dd>{{ report.counts[metric.key] ?? '未完成' }}</dd>
        </div>
      </dl>
      <el-descriptions
        v-if="report.record"
        :column="props.mobile ? 1 : 2"
        border
      >
        <el-descriptions-item label="发起人与输入 userId">
          {{ report.record.ownerMatches ? '一致' : '不一致' }}
        </el-descriptions-item>
        <el-descriptions-item label="审批状态">
          {{ report.record.status === 'COMPLETED' ? '已完成' : report.record.status === 'RUNNING' ? '进行中' : report.record.status === 'TERMINATED' ? '已撤销' : '其他或未知状态' }}
        </el-descriptions-item>
        <el-descriptions-item label="审批结果">
          {{ report.record.result === 'agree' ? '同意' : report.record.result === 'refuse' ? '拒绝' : '其他或未给出' }}
        </el-descriptions-item>
        <el-descriptions-item label="行程类型">
          {{ report.record.sameDay === null ? '未确认' : report.record.sameDay ? '同日行程' : '跨日行程' }}
        </el-descriptions-item>
        <el-descriptions-item
          v-if="report.record.createdDate"
          label="审批发起日期"
        >
          {{ report.record.createdDate }}
        </el-descriptions-item>
        <el-descriptions-item
          v-if="report.record.startDate"
          label="行程日期"
        >
          {{ report.record.startDate }} 至 {{ report.record.endDate }}
        </el-descriptions-item>
      </el-descriptions>
      <ol class="diagnostic-checks">
        <li
          v-for="item in report.checks"
          :key="item.key"
        >
          <div>
            <el-tag
              :type="checkTypes[item.status]"
              size="small"
            >
              {{ checkLabels[item.status] }}
            </el-tag><strong>{{ item.label }}</strong>
          </div>
          <p>{{ item.message }}</p>
        </li>
      </ol>
      <ul class="diagnostic-hints">
        <li
          v-for="hint in report.hints"
          :key="hint"
        >
          {{ hint }}
        </li>
      </ul>
      <p
        v-if="report.error"
        class="field-help"
      >
        错误代码：{{ report.error.code }}<span v-if="report.error.upstreamErrorCode"> · 钉钉错误：{{ report.error.upstreamErrorCode }}</span>
      </p>
      <p class="field-help diagnostic-request-id">
        诊断编号：{{ report.requestId }}
      </p>
      <p class="field-help">
        结果仅反映本次输入和当前配置，不代表员工之前那次请求。未完成的检查不会视为通过。
      </p>
      <el-button @click="copySummary">
        复制脱敏摘要
      </el-button>
      <p
        v-if="copyMessage"
        class="field-help"
        role="status"
      >
        {{ copyMessage }}
      </p>
      <details
        :open="summaryOpen"
        class="diagnostic-summary"
      >
        <summary>查看脱敏摘要</summary>
        <el-input
          type="textarea"
          :model-value="safeSummary"
          readonly
          :rows="8"
          aria-label="脱敏诊断摘要"
        />
      </details>
    </section>
  </el-card>
</template>

<style scoped>
.approval-diagnostics-card { margin-bottom: 20px; }
.diagnostic-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 18px; }
.diagnostic-form-grid--mobile { grid-template-columns: minmax(0, 1fr); }
.diagnostic-form-grid :deep(.el-date-editor), .diagnostic-form-grid :deep(.el-select) { width: 100%; }
.diagnostic-advanced { margin-bottom: 18px; }
summary { cursor: pointer; font-weight: 600; margin-bottom: 12px; }
.diagnostic-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }
.diagnostic-actions :deep(.el-button + .el-button) { margin-left: 0; }
.approval-diagnostics-card :deep(.el-alert) { margin-top: 12px; }
.diagnostic-report { margin-top: 20px; overflow-wrap: anywhere; }
.diagnostic-metrics { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
.diagnostic-metrics--mobile { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.diagnostic-metrics div { padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 6px; }
.diagnostic-metrics dt { font-size: 12px; color: var(--el-text-color-secondary); }
.diagnostic-metrics dd { margin: 4px 0 0; font-size: 20px; font-weight: 600; }
.diagnostic-checks { padding-left: 22px; margin: 20px 0; }
.diagnostic-checks li { padding: 5px 0; }
.diagnostic-checks li > div { display: flex; align-items: center; gap: 10px; }
.diagnostic-checks p { margin: 6px 0; color: var(--el-text-color-regular); overflow-wrap: anywhere; }
.diagnostic-hints { padding-left: 22px; line-height: 1.7; }
.diagnostic-request-id { overflow-wrap: anywhere; }
.diagnostic-summary { margin-top: 14px; }
</style>

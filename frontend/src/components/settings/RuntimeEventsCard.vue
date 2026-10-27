<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import { listRuntimeEvents } from '@/api/runtimeEvents'
import { useAuthStore } from '@/stores/auth'
import type { RuntimeEvent, RuntimeEventPage, RuntimeModule, RuntimeResult } from '@/types/runtimeEvents'

const props = withDefaults(defineProps<{ mobile?: boolean }>(), { mobile: false })
const auth = useAuthStore()
const from = ref('')
const to = ref('')
const module = ref<RuntimeModule | ''>('')
const result = ref<RuntimeResult | ''>('')
const requestId = ref('')
const page = ref<RuntimeEventPage | null>(null)
const loading = ref(false)
const error = ref('')
const copyMessage = ref('')
const manualCopy = ref('')
let controller: AbortController | null = null
let generation = 0
const modules: Record<RuntimeModule, string> = { approvals: '审批查询', materials: '材料与 OCR', submission: 'OA 提交', auth: '登录与身份', system: '系统' }
const results: Record<RuntimeResult, string> = { success: '成功', empty: '结果为空', failed: '失败', incomplete: '未完成', info: '信息' }
const events: Record<RuntimeEvent['event'], string> = { request: '接口请求', approval_query: '员工审批查询过程', upstream_failure: '钉钉调用失败', ocr_failure: 'OCR 处理失败', internal_failure: '内部处理失败', collection_gap: '记录采集缺口' }
const operations: Record<string, string> = { travel_approvals: '查询出差审批', approval_diagnosis: '管理员审批诊断', template_options: '读取模板选项', materials: '处理报销材料', submission: '报销提交与状态查询', login: '登录', logout: '退出登录', session: '读取或更新登录状态', settings: '系统设置', other: '其他业务请求' }
const stages: Record<string, string> = { catalog: '核对模板', list: '读取钉钉列表', details: '读取及校验详情', complete: '查询完成' }
const failureKinds = { timeout: '请求超时', transport: '网络通信失败' }
const reasons: Record<string, string> = { INSTANCE_ID_MISMATCH: '实例不一致', ORIGINATOR_MISMATCH: '发起人不一致', NOT_COMPLETED: '流程未完成', NOT_AGREED: '审批结果不是同意', DATE_FIELDS_INVALID: '日期无法解析', KEYWORD_MISMATCH: '关键词不匹配' }
const metrics = [
  ['templates', '查询模板数'], ['listRequests', '列表请求数'], ['listed', '钉钉返回实例数'],
  ['detailsRead', '成功读取详情数'], ['evaluated', '已校验数'], ['excluded', '规则或关键词排除数'],
  ['returned', '最终返回数'], ['unselectable', '返回但不可选择数'],
] as const
const collectionWarning = computed(() => page.value && (page.value.collector.writeError
  || page.value.collector.droppedSinceStart > 0 || page.value.collector.pending > 0))

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
}
function invalidate(): void {
  generation += 1
  controller?.abort()
  controller = null
  loading.value = false
  page.value = null
  error.value = ''
  copyMessage.value = ''
  manualCopy.value = ''
}
watch([from, to, module, result, requestId], invalidate)
watch(() => [auth.session?.user.userId, auth.isAdmin], invalidate)
onBeforeUnmount(invalidate)
onMounted(() => { void load() })

async function load(append = false): Promise<void> {
  if (loading.value) return
  const cursor = append ? page.value?.nextCursor : null
  if (append && !cursor) return
  if (!append) invalidate()
  if (Boolean(from.value) !== Boolean(to.value) || (from.value && (from.value > to.value
    || (Date.parse(to.value) - Date.parse(from.value)) / 86400000 >= 7))) {
    error.value = '请同时填写起止日期，范围不超过 7 天'
    return
  }
  const version = generation
  const active = new AbortController()
  controller = active
  loading.value = true
  error.value = ''
  try {
    const data = await listRuntimeEvents({
      ...(from.value ? { from: from.value, to: to.value } : {}),
      ...(module.value ? { module: module.value } : {}), ...(result.value ? { result: result.value } : {}),
      ...(requestId.value.trim() ? { requestId: requestId.value.trim() } : {}),
      ...(cursor ? { before: cursor } : {}),
    }, active.signal)
    if (version !== generation || active.signal.aborted) return
    page.value = { ...data, items: append ? [...(page.value?.items ?? []), ...data.items] : data.items }
  } catch (failure) {
    if (version === generation && !active.signal.aborted) error.value = apiErrorMessage(failure, '运行记录加载失败，请重试')
  } finally {
    if (version === generation) { loading.value = false; controller = null }
  }
}
async function sameRequest(id: string): Promise<void> {
  from.value = ''; to.value = ''; module.value = ''; result.value = ''; requestId.value = id
  await nextTick()
  await load()
}
async function clear(): Promise<void> {
  invalidate()
  from.value = ''; to.value = ''; module.value = ''; result.value = ''; requestId.value = ''
  await nextTick()
  await load()
}
async function copy(value: string): Promise<void> {
  const version = generation
  manualCopy.value = ''
  try {
    await navigator.clipboard.writeText(value)
    if (version === generation) copyMessage.value = '已复制'
  } catch {
    if (version === generation) { manualCopy.value = value; copyMessage.value = '请选中下方内容手动复制' }
  }
}
function summary(row: RuntimeEvent): string {
  return JSON.stringify({ ...row, coverage: page.value?.coverage, collector: page.value?.collector }, null, 2)
}
function hint(row: RuntimeEvent): string {
  if (row.event === 'collection_gap') return '此处存在采集缺口，缺少记录不能证明没有发生业务操作。'
  if (row.details.failureKind === 'timeout') return '钉钉调用超时；请结合调用步骤和业务状态核对结果。'
  if (row.details.failureKind === 'transport') return '与钉钉的网络通信失败；请检查网络连通性，并核对本次业务状态。'
  if (row.details.errorCode === 'DINGTALK_RATE_LIMITED') return '钉钉限流，本次查询尚未完成；稍后重试。'
  if (row.details.errorCode === 'DINGTALK_PERMISSION_MISSING') return '核对应用权限及对应员工、模板的数据范围。'
  if (row.event !== 'approval_query') return ''
  if (row.result === 'incomplete' || row.result === 'failed') return '本次查询没有完成，不能把未完成的数量当作零条结果。'
  if (row.details.listed === 0) return '钉钉列表没有返回实例。请核对查询日期、模板和应用数据范围，再使用审批诊断定位目标。'
  if (row.result === 'empty') return '钉钉返回了实例，但没有通过全部筛选条件；请查看排除原因。'
  return ''
}
</script>

<template>
  <el-card
    id="runtime-events"
    shadow="never"
    class="content-card runtime-events-card"
    data-testid="runtime-events-card"
  >
    <template #header>
      <strong>运行记录</strong>
    </template>
    <p class="field-help">
      查看已采集的脱敏运行事件。默认查询最近 7 天，按北京时间筛选日期；排查编号可从员工审批查询页面复制。
    </p>
    <form @submit.prevent="load()">
      <div
        class="runtime-filters"
        :class="{ 'runtime-filters--mobile': props.mobile }"
      >
        <label>开始日期<input
          v-model="from"
          type="date"
          aria-label="运行记录开始日期"
        ></label>
        <label>结束日期<input
          v-model="to"
          type="date"
          aria-label="运行记录结束日期"
        ></label>
        <label>功能模块<select
          v-model="module"
          aria-label="运行记录功能模块"
        ><option value="">全部模块</option><option
          v-for="(label, key) in modules"
          :key="key"
          :value="key"
        >{{ label }}</option></select></label>
        <label>结果<select
          v-model="result"
          aria-label="运行记录结果"
        ><option value="">全部结果</option><option
          v-for="(label, key) in results"
          :key="key"
          :value="key"
        >{{ label }}</option></select></label>
      </div>
      <label class="runtime-request-filter">排查编号<input
        v-model="requestId"
        maxlength="128"
        autocomplete="off"
        aria-label="运行记录排查编号"
        placeholder="粘贴员工提供的排查编号"
      ></label>
      <div class="runtime-actions">
        <el-button
          type="primary"
          native-type="submit"
          :loading="loading"
        >
          查询 / 刷新
        </el-button>
        <el-button
          :disabled="loading"
          @click="clear"
        >
          重置条件
        </el-button>
        <el-button
          v-if="loading"
          @click="invalidate"
        >
          取消加载
        </el-button>
      </div>
    </form>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
    />
    <template v-if="page">
      <p
        class="field-help"
        data-testid="runtime-coverage"
      >
        当前保留 {{ page.coverage.records }} 条；最早 {{ formatTime(page.coverage.oldestAt) }}，最新 {{ formatTime(page.coverage.newestAt) }}。最多保留 {{ page.limits.retentionDays }} 天、{{ page.limits.maxRecords }} 条、{{ Math.round(page.limits.maxBytes / 1048576) }} MiB 记录数据，达到任一上限即淘汰旧记录。
      </p>
      <p class="field-help">
        时间范围仅说明现存记录的最早和最新时间，不代表期间记录完整。功能上线前、已过期或未成功采集的事件无法补回。
      </p>
      <el-alert
        v-if="collectionWarning"
        type="warning"
        :closable="false"
        show-icon
        title="记录采集尚未完整"
      >
        <p v-if="page.collector.writeError">
          最近一次写入失败，业务操作仍可继续；请检查数据库可用性。
        </p>
        <p>本进程启动以来遗漏 {{ page.collector.droppedSinceStart }} 条，待写入 {{ page.collector.pending }} 条。可稍后刷新。</p>
      </el-alert>
      <el-empty
        v-if="page.items.length === 0"
        description="当前条件下没有可展示的已采集记录"
        :image-size="64"
      />
      <div
        v-else
        aria-label="运行记录列表"
        aria-live="polite"
      >
        <article
          v-for="row in page.items"
          :key="row.id"
          class="runtime-event"
          :data-event-id="row.id"
        >
          <div class="runtime-event-heading">
            <strong>{{ events[row.event] }}</strong><el-tag :type="row.result === 'failed' ? 'danger' : row.result === 'success' ? 'success' : 'warning'">
              {{ results[row.result] }}
            </el-tag>
          </div>
          <p>{{ formatTime(row.occurredAt) }} · {{ modules[row.module] }}<span v-if="row.details.operation"> · {{ operations[row.details.operation] ?? row.details.operation }}</span></p>
          <p
            v-if="row.requestId"
            class="runtime-request-id"
          >
            排查编号：{{ row.requestId }}
          </p>
          <p v-if="row.details.httpStatus !== undefined">
            HTTP {{ row.details.httpStatus }}<span v-if="row.details.durationMs !== undefined"> · {{ row.details.durationMs }} ms</span>
          </p>
          <p v-if="row.details.errorCode">
            错误码：{{ row.details.errorCode }}
          </p>
          <p v-if="row.details.failureKind">
            失败类别：{{ failureKinds[row.details.failureKind] }}
          </p>
          <p v-if="row.details.upstreamOperation">
            钉钉操作：{{ row.details.upstreamOperation }} · HTTP {{ row.details.upstreamHttpStatus ?? '—' }} · {{ row.details.upstreamErrorCode ?? '无上游错误码' }}
          </p>
          <p v-if="row.details.exceptionType">
            异常类型：{{ row.details.exceptionType }}
          </p>
          <p v-if="row.details.stage">
            执行到：{{ stages[row.details.stage] ?? row.details.stage }}
          </p>
          <p v-if="row.details.queryWindow">
            查询日期：{{ row.details.queryWindow.from }} 至 {{ row.details.queryWindow.to }}；模板版本 {{ row.details.configVersion ?? '未确认' }}；{{ row.details.keywordPresent ? '使用了关键词（原文不记录）' : '未使用关键词' }}
          </p>
          <dl
            v-if="row.event === 'approval_query'"
            class="runtime-metrics"
          >
            <div
              v-for="[key, label] in metrics"
              :key="key"
            >
              <dt>{{ label }}</dt><dd>{{ row.details[key] ?? '未完成' }}</dd>
            </div>
          </dl>
          <p
            v-for="reason in row.details.exclusions ?? []"
            :key="reason.code"
          >
            排除：{{ reasons[reason.code] ?? reason.code }} {{ reason.count }} 条
          </p>
          <p v-if="row.details.dropped">
            未成功采集 {{ row.details.dropped }} 条
          </p>
          <p
            v-if="hint(row)"
            class="field-help"
          >
            {{ hint(row) }}
          </p>
          <div class="runtime-actions">
            <el-button
              v-if="row.requestId"
              link
              type="primary"
              @click="sameRequest(row.requestId)"
            >
              查看同次请求
            </el-button><el-button
              link
              type="primary"
              @click="copy(summary(row))"
            >
              复制脱敏摘要
            </el-button>
          </div>
        </article>
      </div>
      <el-button
        v-if="page.nextCursor"
        :loading="loading"
        @click="load(true)"
      >
        加载更早记录
      </el-button>
    </template>
    <p
      v-if="copyMessage"
      role="status"
    >
      {{ copyMessage }}
    </p>
    <textarea
      v-if="manualCopy"
      :value="manualCopy"
      readonly
      aria-label="手动复制运行记录摘要"
      rows="8"
      class="runtime-copy"
    />
  </el-card>
</template>

<style scoped>
.runtime-filters { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.runtime-filters--mobile { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.runtime-filters label, .runtime-request-filter { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.runtime-filters input, .runtime-filters select, .runtime-request-filter input { box-sizing: border-box; width: 100%; min-width: 0; height: 38px; padding: 6px; border: 1px solid var(--el-border-color); border-radius: 4px; background: white; font: inherit; }
.runtime-request-filter { margin-top: 12px; }
.runtime-actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }
.runtime-actions :deep(.el-button + .el-button) { margin-left: 0; }
.runtime-event { padding: 16px 0; border-bottom: 1px solid var(--el-border-color); overflow-wrap: anywhere; }
.runtime-event p { margin: 8px 0; }
.runtime-event-heading { display: flex; gap: 10px; align-items: center; }
.runtime-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.runtime-metrics div { display: flex; justify-content: space-between; gap: 8px; }
.runtime-metrics dt { color: var(--el-text-color-secondary); }
.runtime-metrics dd { margin: 0; }
.runtime-copy { width: 100%; box-sizing: border-box; }
</style>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import { diagnoseReceipt, type ReceiptDiagnostic } from '@/api/receiptDiagnostics'
import { useAuthStore } from '@/stores/auth'

withDefaults(defineProps<{ mobile?: boolean }>(), { mobile: false })
const auth = useAuthStore()
const file = ref<File | null>(null)
const input = ref<HTMLInputElement | null>(null)
const loading = ref(false)
const report = ref<ReceiptDiagnostic | null>(null)
const error = ref('')
const preview = ref('')
let controller: AbortController | null = null
let generation = 0
const trace = computed(() => report.value?.recognition.diagnostic)
const sourceNames: Record<string, string> = { pdf_text: 'PDF 原生文字', paddle: '图片 OCR' }
const parserNames: Record<string, string> = {
  TrainTicketParser: '铁路票据专用规则',
  PhysicalTaxiReceiptParser: '纸质出租车票专用规则',
  ForeignReceiptParser: '境外票据规则',
  GenericInvoiceParser: '通用发票规则',
  FallbackParser: '保守兜底规则',
}
const policies: Record<string, string> = {
  specialized_parser: '专用票据规则确定类别，普通关键词不覆盖该分类。',
  keyword_match: '通用发票匹配到一个费用类别，按命中的关键词分类。',
  keyword_fallback: '原解析结果为“其他”，唯一类别关键词触发分类兜底。',
  keyword_conflict: '命中了多个费用类别，无法唯一判断，保留为“其他”。',
  passenger_guard: '旅客运输票据缺少可靠的交通类型依据，未用普通关键词强行分类。',
  no_keyword_match: '没有命中可用的分类关键词，保留为“其他”，需要人工确认。',
}
const transportNames: Record<string, string> = { ride_hailing: '网约车', taxi: '出租车', rail: '铁路', hotel: '住宿', other: '其他／未明确' }
const materialNames: Record<string, string> = {
  expense: '费用票据',
  itinerary: '行程单',
  payment_proof: '付款凭证',
  hotel_bill: '住宿明细',
  unknown: '用途待确认',
}

function invalidate(): void {
  generation += 1
  controller?.abort()
  controller = null
  loading.value = false
  report.value = null
  error.value = ''
}
function clear(): void {
  invalidate()
  file.value = null
  if (preview.value) URL.revokeObjectURL(preview.value)
  preview.value = ''
  if (input.value) input.value.value = ''
}
function choose(event: Event): void {
  const selected = (event.target as HTMLInputElement).files?.[0]
  clear()
  if (!selected) return
  if (!/\.(jpe?g|png|pdf)$/i.test(selected.name)) {
    error.value = '请选择 JPG、PNG 或单页 PDF'
    return
  }
  file.value = selected
  if (/\.(jpe?g|png)$/i.test(selected.name)) preview.value = URL.createObjectURL(selected)
}
watch(() => [auth.session?.user.userId, auth.isAdmin], clear, { flush: 'sync' })
onBeforeUnmount(clear)

async function run(): Promise<void> {
  if (loading.value) return
  invalidate()
  const selected = file.value
  if (!selected) {
    error.value = '请先选择一张票据'
    return
  }
  const active = new AbortController()
  controller = active
  const version = generation
  loading.value = true
  try {
    const data = await diagnoseReceipt(selected, active.signal)
    if (version === generation && !active.signal.aborted) report.value = data
  } catch (failure) {
    if (version === generation && !active.signal.aborted) {
      error.value = apiErrorMessage(failure, '票据诊断失败，请重试')
    }
  } finally {
    if (version === generation) {
      loading.value = false
      controller = null
    }
  }
}
</script>

<template>
  <el-card
    id="receipt-diagnostics"
    shadow="never"
    class="content-card receipt-diagnostics-card"
    data-testid="receipt-diagnostics-card"
  >
    <template #header>
      <strong>票据识别诊断</strong>
    </template>
    <p class="field-help">
      上传一张识别不准确的票据，核对识别文字与分类依据。使用当前关键词和正式识别流程；识别原文仅在本页临时显示，不写入报销单或日志；诊断文件在本次处理结束后清理。
    </p>
    <form @submit.prevent="run">
      <div class="receipt-diagnostic-upload">
        <input
          ref="input"
          type="file"
          accept=".jpg,.jpeg,.png,.pdf"
          aria-label="诊断票据文件"
          hidden
          @change="choose"
        >
        <el-button
          type="primary"
          plain
          native-type="button"
          aria-label="选择诊断票据"
          @click="input?.click()"
        >
          {{ file ? '更换票据' : '选择票据' }}
        </el-button>
        <span
          class="receipt-diagnostic-filename"
          role="status"
        >{{ file?.name || '尚未选择票据' }}</span>
      </div>
      <p class="receipt-diagnostic-upload-help">
        支持 JPG、PNG 或单页 PDF，每次选择一张。
      </p>
      <img
        v-if="preview"
        :src="preview"
        alt="本次诊断票据预览"
        class="receipt-diagnostic-preview"
      >
      <div class="receipt-diagnostic-actions">
        <el-button
          type="primary"
          native-type="submit"
          :loading="loading"
        >
          开始诊断
        </el-button><el-button @click="clear">
          {{ loading ? '取消并清空' : '清空' }}
        </el-button>
      </div>
    </form>
    <p
      v-if="loading"
      role="status"
    >
      正在排队或识别票据，请稍候；尚未得出诊断结论。
    </p>
    <el-alert
      v-if="error"
      :title="error"
      type="error"
      :closable="false"
    />
    <div
      v-if="report && trace"
      aria-label="票据诊断结果"
    >
      <p><strong>识别用途：{{ materialNames[report.recognition._materialClassification.kind] ?? '待确认' }}<span v-if="report.recognition.categoryName"> · 分类：{{ report.recognition.categoryName }}</span></strong></p>
      <p v-if="report.recognition._materialClassification.reason">
        {{ report.recognition._materialClassification.reason }}
      </p>
      <p>提取来源：{{ trace.sources.map((source) => sourceNames[source] ?? source).join('、') || '未取得文字' }}；当前关键词 {{ report.keywordRuleCount }} 条。</p>
      <el-alert
        v-if="!trace.complete || trace.recognizedTextTruncated || trace.classificationTextTruncated || trace.derivedFieldsTruncated"
        type="warning"
        :closable="false"
        title="识别未完整或展示文本已截断，请结合原件核对；分类使用本次流程取得的完整可用文字。"
      />
      <p v-if="trace.parser">
        选中解析器：{{ parserNames[trace.parser] ?? trace.parser }}
      </p>
      <p v-if="trace.policy">
        {{ policies[trace.policy] ?? trace.policy }}
      </p>
      <p v-if="report.recognition.amount !== undefined">
        金额：{{ report.recognition.amount ?? '未识别' }}；日期：{{ report.recognition.date ?? '未识别' }}；置信度：{{ report.recognition.confidence ?? '—' }}
      </p>
      <p v-if="report.recognition.description">
        说明：{{ report.recognition.description }}
      </p>
      <p v-if="report.recognition.transportType">
        交通类型：{{ transportNames[report.recognition.transportType] ?? report.recognition.transportType }}<span v-if="report.recognition.requiresItinerary">；需关联行程单</span>
      </p>
      <p v-if="report.recognition.originalCurrency || report.recognition.originalAmount">
        原币金额：{{ report.recognition.originalAmount ?? '未识别' }} {{ report.recognition.originalCurrency ?? '币种未识别' }}
      </p>
      <p
        v-for="warning in report.recognition.warnings ?? []"
        :key="warning"
      >
        识别警告：{{ warning }}
      </p>
      <strong>命中的分类关键词</strong>
      <p v-if="!trace.parser">
        该材料未进入费用票据分类，未执行费用分类关键词规则。
      </p><p v-else-if="!trace.matchedRules?.length">
        没有命中费用分类关键词；专用票据分类仍可能有效。
      </p>
      <p
        v-for="(rule, index) in trace.matchedRules ?? []"
        :key="index"
      >
        “{{ rule.keyword }}” → {{ rule.categoryName }}<span v-if="rule.transportType">（{{ rule.transportType === 'ride_hailing' ? '网约车' : rule.transportType === 'taxi' ? '出租车' : rule.transportType }}）</span>
      </p>
      <details v-if="trace.parserScores">
        <summary>解析器匹配得分</summary><p
          v-for="score in trace.parserScores"
          :key="score.parser"
        >
          {{ parserNames[score.parser] ?? score.parser }}：{{ score.score }}<span v-if="trace.selectionThreshold !== undefined">（选择阈值 {{ trace.selectionThreshold }}）</span>
        </p>
      </details>
      <h4>识别原文</h4>
      <section aria-label="诊断识别原文">
        <template v-if="trace.extractions?.length">
          <div
            v-for="(extraction, index) in trace.extractions"
            :key="index"
          >
            <p>第 {{ extraction.page }} 页 · {{ sourceNames[extraction.source] }}<span v-if="!extraction.usedForParsing">（未用于本次解析）</span></p>
            <pre>{{ extraction.text || (extraction.truncated ? '文字展示已达上限' : '未取得可用文字') }}</pre>
          </div>
        </template>
        <pre v-else>{{ trace.recognizedText || '未取得可用文字' }}</pre>
      </section>
      <details v-if="trace.derivedFields?.length">
        <summary>版面推导字段</summary>
        <p class="field-help">
          以下字段由版面或规则补充，属于解析依据，不是 OCR 原始输出。
        </p>
        <pre
          v-for="(field, index) in trace.derivedFields"
          :key="index"
        >第 {{ field.page }} 页：{{ field.text || '字段展示已达上限' }}</pre>
      </details>
      <details v-if="trace.classificationText !== undefined">
        <summary>参与关键词分类的文字</summary><p class="field-help">
          已排除买方等无关字段；可能包含从版面补充的交通字段。可与识别原文对照。
        </p><pre aria-label="诊断分类输入文字">{{ trace.classificationText }}</pre>
      </details>
      <p class="field-help">
        原文缺字、错字时先检查图片或 OCR；文字正确但关键词未命中或冲突时，检查“票据分类关键词”。专用规则决定的类别不由普通关键词覆盖。
      </p>
      <small>诊断编号：{{ report.requestId }}</small>
    </div>
  </el-card>
</template>

<style scoped>
.receipt-diagnostics-card { overflow-wrap: anywhere; }
.receipt-diagnostic-upload { display: flex; align-items: center; flex-wrap: wrap; gap: 10px 12px; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-fill-color-extra-light); }
.receipt-diagnostic-upload :deep(.el-button) { flex: 0 0 auto; margin: 0; }
.receipt-diagnostic-filename { flex: 1 1 140px; min-width: 0; font-size: 13px; color: var(--el-text-color-regular); }
.receipt-diagnostic-upload-help { margin: 6px 0 0; font-size: 12px; color: var(--el-text-color-secondary); }
.receipt-diagnostic-preview { display: block; max-width: 100%; max-height: 240px; object-fit: contain; margin: 12px 0; }
.receipt-diagnostic-actions { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 360px; overflow: auto; background: var(--el-fill-color-light); padding: 12px; }
</style>

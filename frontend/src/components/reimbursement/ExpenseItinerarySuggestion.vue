<script setup lang="ts">
import { computed } from 'vue'
import type { ItinerarySuggestion } from '@/utils/itineraryMatching'

const props = defineProps<{
  suggestion: ItinerarySuggestion
  fileName: string
  disabled: boolean
  previewLoading: boolean
}>()
const emit = defineEmits<{ confirm: []; choose: []; preview: [] }>()
const fillLabels = computed(() => [
  props.suggestion.fill?.date ? '乘车日期' : '',
  props.suggestion.fill?.description ? '路线说明' : '',
].filter(Boolean).join('、'))
</script>

<template>
  <div class="itinerary-suggestion">
    <template v-if="suggestion.documentSummary">
      <strong>疑似对应整份行程单 · {{ suggestion.documentSummary.tripCount }} 笔 · ¥{{ suggestion.documentSummary.amount }}</strong>
      <span>{{ suggestion.documentSummary.startDate }} 至 {{ suggestion.documentSummary.endDate }}</span>
    </template>
    <template v-else-if="suggestion.trip">
      <strong>疑似对应打车行程 · ¥{{ suggestion.trip.amount }} · {{ suggestion.trip.date }}</strong>
      <span>{{ suggestion.trip.origin }} → {{ suggestion.trip.destination }}</span>
    </template>
    <button
      type="button"
      class="suggestion-preview"
      :disabled="previewLoading"
      @click="emit('preview')"
    >
      {{ fileName }} · 预览
    </button>
    <span
      v-if="suggestion.basis === 'amount_only'"
      class="suggestion-hint"
    >{{ suggestion.documentSummary ? '行程合计与发票金额相同，请核对是否为这张汇总发票；确认后仅关联文件，不改日期和说明。' : '仅金额相同，请核对是否为同一笔。' }}</span>
    <span
      v-if="fillLabels"
      class="suggestion-hint"
    >确认后补全：{{ fillLabels }}；保留已修改内容。</span>
    <div class="suggestion-actions">
      <el-button
        type="primary"
        size="small"
        :disabled="disabled"
        @click="emit('confirm')"
      >
        确认关联
      </el-button>
      <el-button
        type="primary"
        plain
        size="small"
        :disabled="disabled"
        @click="emit('choose')"
      >
        选择其他
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.itinerary-suggestion { display: grid; gap: 6px; padding: 12px; margin-top: 8px; border: 1px solid #d9e9ff; border-radius: 8px; background: #f0f7ff; color: #475467; font-size: 12px; line-height: 1.6; }
.itinerary-suggestion strong { font-weight: 600; }
.suggestion-preview { width: fit-content; max-width: 100%; padding: 0; border: 0; background: none; color: #337ecc; text-align: left; overflow-wrap: anywhere; font: inherit; cursor: pointer; }
.suggestion-preview:disabled { cursor: wait; opacity: 0.6; }
.suggestion-hint { color: #667085; }
.suggestion-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 2px; }
.suggestion-actions .el-button + .el-button { margin-left: 0; }
</style>

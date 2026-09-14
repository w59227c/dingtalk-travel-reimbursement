<script setup lang="ts">
import { computed } from 'vue'
import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { isActiveProof, isRecordedProof, missingExpenseMaterials } from '@/utils/expenseProofs'

const props = defineProps<{
  item: ExpenseItem
  files: ReimbursementDraftFile[]
  disabled: boolean
  previewLoading: boolean
  uploading?: boolean
  error?: string
  hasItinerarySuggestion?: boolean
  allowPurged?: boolean
}>()
const emit = defineEmits<{
  preview: [file: ReimbursementDraftFile]
  purpose: [file: ReimbursementDraftFile]
  upload: [replaceId?: string]
  reuse: []
  unlink: [fileId: string]
  itinerary: []
  hotelUpload: [replaceId?: string]
  hotelReuse: []
  hotelUnlink: [fileId: string]
}>()
const missing = computed(() => missingExpenseMaterials(props.item, props.files, { allowPurged: props.allowPurged }))
const paymentFiles = computed(() => props.files.filter((file) => isRecordedProof(file, 'payment_proof', props.allowPurged)
  && props.item.paymentProofFileIds?.includes(file.id)))
const hasReusableProof = computed(() => props.files.some((file) => isActiveProof(file, 'payment_proof')
  && !props.item.paymentProofFileIds?.includes(file.id)))
const hotelFiles = computed(() => props.files.filter((file) => isRecordedProof(file, 'hotel_bill', props.allowPurged)
  && props.item.hotelBillFileIds?.includes(file.id)))
const hasReusableHotel = computed(() => props.files.some((file) => isActiveProof(file, 'hotel_bill')
  && !props.item.hotelBillFileIds?.includes(file.id)))

function hotelSummary(file: ReimbursementDraftFile): string {
  const details = file.hotelBillDetails
  if (!details) return '未提取住宿信息，请预览原件核对；已确认的材料用途不受影响。'
  return [details.guest, details.checkIn && `${details.checkIn} 入住`, details.checkOut && `${details.checkOut} 离店`,
    details.nights && `${details.nights} 晚`, details.nightlyRate && `单价 ${details.nightlyRate}`,
    details.total && `合计 ${details.total}`, details.currency].filter(Boolean).join(' · ')
}
</script>

<template>
  <div
    class="expense-materials"
    :data-expense-material-id="item.id"
    tabindex="-1"
  >
    <div
      v-if="item.category === 'lodging'"
      class="material-needed"
      :class="{ 'material-complete': !missing.includes('住宿明细') }"
      data-testid="hotel-bill-needed"
    >
      <span>{{ missing.includes('住宿明细') ? '待补住宿明细· 每笔住宿必需' : '住宿明细· 可关联多份' }}</span>
      <div class="material-actions">
        <el-button
          type="primary"
          plain
          size="small"
          :loading="uploading"
          :disabled="disabled"
          @click="emit('hotelUpload')"
        >
          ＋ 添加住宿明细
        </el-button>
        <el-button
          v-if="hasReusableHotel"
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('hotelReuse')"
        >
          从已上传材料选择
        </el-button>
      </div>
    </div>
    <div
      v-for="file in hotelFiles"
      :key="file.id"
      data-testid="hotel-bill-attached"
    >
      <div class="attached-material">
        <span class="attached-label">已附住宿明细</span>
        <button
          v-if="file.status !== 'PURGED'"
          class="material-preview"
          type="button"
          :disabled="previewLoading"
          @click="emit('preview', file)"
        >
          {{ file.name }} · 预览
        </button>
        <span
          v-else
          class="attached-label"
        >{{ file.name }}</span>
        <div class="material-actions">
          <el-button
            type="primary"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('hotelUpload', file.id)"
          >
            更换
          </el-button>
          <el-button
            type="danger"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('hotelUnlink', file.id)"
          >
            移除关联
          </el-button>
          <el-button
            type="primary"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('purpose', file)"
          >
            修改用途
          </el-button>
        </div>
      </div>
      <p class="attached-label">
        {{ hotelSummary(file) }}
      </p>
    </div>
    <div
      v-if="missing.includes('行程单') && !hasItinerarySuggestion"
      class="material-needed"
    >
      <span>待补行程单 · 网约车费用</span>
      <el-button
        type="primary"
        plain
        size="small"
        :disabled="disabled"
        @click="emit('itinerary')"
      >
        选择行程单
      </el-button>
    </div>
    <div
      v-if="missing.includes('付款凭证')"
      class="material-needed"
      data-testid="payment-proof-needed"
    >
      <span>待补付款凭证 · 本笔超过 500 元</span>
      <div class="material-actions">
        <el-button
          type="primary"
          plain
          size="small"
          :loading="uploading"
          :disabled="disabled"
          @click="emit('upload')"
        >
          ＋ 添加付款凭证
        </el-button>
        <el-button
          v-if="hasReusableProof"
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('reuse')"
        >
          从已上传材料选择
        </el-button>
      </div>
    </div>
    <div
      v-for="file in paymentFiles"
      :key="file.id"
      class="attached-material"
      data-testid="payment-proof-attached"
    >
      <span class="attached-label">已附凭证</span>
      <button
        v-if="file.status !== 'PURGED'"
        class="material-preview"
        type="button"
        :disabled="previewLoading"
        @click="emit('preview', file)"
      >
        {{ file.name }} · 预览
      </button>
      <span
        v-else
        class="attached-label"
      >{{ file.name }}</span>
      <div class="material-actions">
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('upload', file.id)"
        >
          更换
        </el-button>
        <el-button
          type="danger"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('unlink', file.id)"
        >
          移除关联
        </el-button>
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('purpose', file)"
        >
          修改用途
        </el-button>
      </div>
    </div>
    <p
      v-if="error"
      class="material-error"
      role="alert"
    >
      {{ error }}
    </p>
  </div>
</template>

<style scoped>
.expense-materials { margin-top: 8px; outline-offset: 4px; }
.expense-materials:empty { display: none; }
.material-needed { display: grid; gap: 8px; padding: 10px 12px; margin-top: 6px; border-radius: 8px; background: #fff8ed; color: #8b5a16; font-size: 12px; line-height: 1.6; }
.material-complete { background: transparent; color: #667085; }
.material-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 10px; }
.material-actions .el-button + .el-button { margin-left: 0; }
.attached-material { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 12px; line-height: 1.6; }
.attached-label { color: #667085; }
.material-preview { padding: 0; border: 0; background: none; color: var(--el-color-primary); cursor: pointer; overflow-wrap: anywhere; text-align: left; font: inherit; }
.material-error { color: var(--el-color-danger); font-size: 12px; }
</style>

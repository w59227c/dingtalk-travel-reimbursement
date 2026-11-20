<script setup lang="ts">
import { computed } from 'vue'
import ExpenseMaterialGroup from './ExpenseMaterialGroup.vue'
import ExpenseMaterialFile from './ExpenseMaterialFile.vue'
import type { ExpenseItem } from '@/types/expenses'
import type { ReimbursementDraftFile } from '@/types/reimbursements'
import { isCourierExpense, isRecordedProof, missingExpenseMaterials } from '@/utils/expenseProofs'

const props = withDefaults(defineProps<{
  item: ExpenseItem
  files: ReimbursementDraftFile[]
  disabled: boolean
  previewLoading: boolean
  hasItinerarySuggestion?: boolean
  allowPurged?: boolean
  allowPreview?: boolean
}>(), { allowPreview: true })
const emit = defineEmits<{
  preview: [file: ReimbursementDraftFile]
  purpose: [file: ReimbursementDraftFile]
  manage: [kind: 'payment_proof' | 'hotel_bill' | 'waybill']
  itinerary: []
}>()
const missing = computed(() => missingExpenseMaterials(props.item, props.files, { allowPurged: props.allowPurged }))
const paymentFiles = computed(() => props.files.filter((file) => isRecordedProof(file, 'payment_proof', props.allowPurged)
  && props.item.paymentProofFileIds?.includes(file.id)))
const hotelFiles = computed(() => props.files.filter((file) => isRecordedProof(file, 'hotel_bill', props.allowPurged)
  && props.item.hotelBillFileIds?.includes(file.id)))
const receiptFiles = computed(() => props.files.filter((file) => isRecordedProof(file, 'other', props.allowPurged)
  && props.item.receiptAttachmentFileIds?.includes(file.id)))
const waybills = computed(() => props.files.filter((file) => isRecordedProof(file, 'waybill', props.allowPurged)
  && props.item.waybillFileIds?.includes(file.id)))
const canLinkWaybill = computed(() => props.allowPreview && (isCourierExpense(props.item) || waybills.value.length > 0))
function hotelSummary(file: ReimbursementDraftFile): string {
  const details = file.hotelBillDetails
  if (!details) return props.allowPreview ? '请预览核对住宿信息。' : ''
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
    <ExpenseMaterialGroup
      v-if="waybills.length || canLinkWaybill"
      label="运单明细"
      :count="waybills.length || undefined"
    >
      <template
        v-if="canLinkWaybill"
        #actions
      >
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('manage', 'waybill')"
        >
          关联运单明细
        </el-button>
      </template>
      <p
        v-if="canLinkWaybill && !waybills.length"
        class="field-help"
      >
        运单明细可选。
      </p>
      <ExpenseMaterialFile
        v-for="file in waybills"
        :key="file.id"
        :name="file.name"
        :preview-available="allowPreview && file.status !== 'PURGED'"
        :preview-loading="previewLoading"
        @preview="emit('preview', file)"
      >
        <template
          v-if="canLinkWaybill"
          #actions
        >
          <el-button
            type="primary"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('purpose', file)"
          >
            修改用途
          </el-button>
        </template>
      </ExpenseMaterialFile>
    </ExpenseMaterialGroup>
    <ExpenseMaterialGroup
      v-if="receiptFiles.length"
      label="票据材料"
      :count="receiptFiles.length"
    >
      <ExpenseMaterialFile
        v-for="file in receiptFiles"
        :key="file.id"
        :name="file.name"
        :preview-available="allowPreview && file.status !== 'PURGED'"
        :preview-loading="previewLoading"
        data-testid="manual-receipt-attached"
        @preview="emit('preview', file)"
      />
    </ExpenseMaterialGroup>
    <ExpenseMaterialGroup
      v-if="item.category === 'lodging' || hotelFiles.length"
      label="住宿明细"
      :count="hotelFiles.length || undefined"
      data-testid="hotel-bill-group"
    >
      <template
        v-if="allowPreview"
        #actions
      >
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('manage', 'hotel_bill')"
        >
          关联住宿明细
        </el-button>
      </template>
      <p
        v-if="missing.includes('住宿明细')"
        class="material-needed"
        data-testid="hotel-bill-needed"
      >
        待补住宿明细 · 每笔住宿必需
      </p>
      <ExpenseMaterialFile
        v-for="file in hotelFiles"
        :key="file.id"
        :name="file.name"
        :preview-available="allowPreview && file.status !== 'PURGED'"
        :preview-loading="previewLoading"
        :summary="hotelSummary(file)"
        class="attached-material"
        data-testid="hotel-bill-attached"
        @preview="emit('preview', file)"
      >
        <template
          v-if="allowPreview"
          #actions
        >
          <el-button
            type="primary"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('purpose', file)"
          >
            修改用途
          </el-button>
        </template>
      </ExpenseMaterialFile>
    </ExpenseMaterialGroup>
    <ExpenseMaterialGroup
      v-if="missing.includes('行程单') && !hasItinerarySuggestion"
      label="行程单"
    >
      <template
        v-if="allowPreview"
        #actions
      >
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('itinerary')"
        >
          关联行程单
        </el-button>
      </template>
      <p class="material-needed">
        待补行程单 · 网约车费用
      </p>
    </ExpenseMaterialGroup>
    <ExpenseMaterialGroup
      v-if="missing.includes('付款凭证') || paymentFiles.length"
      label="付款凭证"
      :count="paymentFiles.length || undefined"
      data-testid="payment-proof-group"
    >
      <template
        v-if="allowPreview"
        #actions
      >
        <el-button
          type="primary"
          plain
          size="small"
          :disabled="disabled"
          @click="emit('manage', 'payment_proof')"
        >
          关联付款凭证
        </el-button>
      </template>
      <p
        v-if="missing.includes('付款凭证')"
        class="material-needed"
        data-testid="payment-proof-needed"
      >
        待补付款凭证 · 本笔超过 500 元
      </p>
      <ExpenseMaterialFile
        v-for="file in paymentFiles"
        :key="file.id"
        :name="file.name"
        :preview-available="allowPreview && file.status !== 'PURGED'"
        :preview-loading="previewLoading"
        class="attached-material"
        data-testid="payment-proof-attached"
        @preview="emit('preview', file)"
      >
        <template
          v-if="allowPreview"
          #actions
        >
          <el-button
            type="primary"
            plain
            size="small"
            :disabled="disabled"
            @click="emit('purpose', file)"
          >
            修改用途
          </el-button>
        </template>
      </ExpenseMaterialFile>
    </ExpenseMaterialGroup>
  </div>
</template>

<style scoped>
.expense-materials { display: grid; gap: 12px; margin-top: 4px; padding-top: 12px; border-top: 1px solid #e4e7ed; outline-offset: 4px; }
.expense-materials:empty { display: none; }
.material-needed { margin: 0; padding: 8px 10px; border-radius: 8px; background: #fff8ed; color: #8b5a16; font-size: 12px; line-height: 1.6; }
</style>

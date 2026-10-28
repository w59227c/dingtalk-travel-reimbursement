<script setup lang="ts">
import { computed } from 'vue'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import type { AttendanceCalendarCorrection, AttendanceCorrectionRange } from '@/types/attendance'

const props = defineProps<{ modelValue: AttendanceCalendarCorrection; disabled?: boolean; mobile?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: AttendanceCalendarCorrection] }>()
const year = computed(() => props.modelValue.year)
function updateYear(value: number | undefined): void { emit('update:modelValue', { ...props.modelValue, year: value ?? 0 }) }
function update(index: number, value: Partial<AttendanceCorrectionRange>): void {
  emit('update:modelValue', { ...props.modelValue, ranges: props.modelValue.ranges.map((item, position) => position === index ? { ...item, ...value } : item) })
}
function add(): void {
  emit('update:modelValue', { ...props.modelValue, ranges: [...props.modelValue.ranges, { name: '', startDate: '', endDate: '', working: false }] })
}
function remove(index: number): void { emit('update:modelValue', { ...props.modelValue, ranges: props.modelValue.ranges.filter((_, position) => position !== index) }) }
function outside(date: Date): boolean { return !year.value || Math.abs(date.getFullYear() - year.value) > 1 }
function endDisabled(date: Date, start: string): boolean {
  const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  return outside(date) || !!start && value < start
}
const initialMonth = computed(() => {
  const date = new Date(0)
  date.setHours(0, 0, 0, 0)
  date.setFullYear(year.value || new Date().getFullYear(), 0, 1)
  return date
})
// Stable Date references keep Element Plus from resetting a navigated calendar.
const endMonths = computed<Date[]>((previous) => props.modelValue.ranges.map((item, index) => {
  const candidate = item.startDate ? new Date(`${item.startDate}T00:00:00`) : initialMonth.value
  const existing = previous?.[index]
  return existing?.getTime() === candidate.getTime() ? existing : candidate
}))
</script>

<template>
  <el-config-provider :locale="zhCn">
    <section
      class="calendar-correction-editor"
      :class="{ 'is-mobile': mobile }"
      aria-label="手动修正年度安排"
    >
      <p class="correction-intro">
        只需填写放假和补班日期，其余按周一至周五。请覆盖元旦、春节、清明、劳动、端午、中秋、国庆的全年安排；跨年日期需写实际年份，每段最多 32 天。
      </p>
      <label class="correction-year">适用年份<el-input-number
        :model-value="year || undefined"
        :min="1"
        :max="9999"
        :precision="0"
        :disabled="disabled"
        aria-label="修正安排的适用年份"
        @update:model-value="updateYear"
      /></label>
      <div class="correction-rows">
        <section
          v-for="(item, index) in modelValue.ranges"
          :key="index"
          class="correction-row"
          :aria-label="`修正安排 ${index + 1}`"
        >
          <div class="correction-row-heading">
            <strong>安排 {{ index + 1 }}</strong><el-button
              link
              type="danger"
              :disabled="disabled"
              :aria-label="`删除修正安排 ${index + 1}`"
              @click="remove(index)"
            >
              删除
            </el-button>
          </div>
          <div class="correction-fields">
            <label>节日名称<el-input
              :model-value="item.name"
              maxlength="80"
              :disabled="disabled"
              placeholder="如：春节；合并可填国庆节、中秋节"
              :aria-label="`安排 ${index + 1} 节日名称`"
              @update:model-value="update(index, { name: $event })"
            /></label>
            <div class="correction-type">
              安排<el-radio-group
                :model-value="item.working"
                :disabled="disabled"
                :aria-label="`安排 ${index + 1} 类型`"
                @update:model-value="update(index, { working: $event === true })"
              >
                <el-radio-button
                  :value="false"
                >
                  放假
                </el-radio-button><el-radio-button
                  :value="true"
                >
                  补班
                </el-radio-button>
              </el-radio-group>
            </div>
            <label class="correction-date">开始日期<el-date-picker
              :model-value="item.startDate || null"
              type="date"
              value-format="YYYY-MM-DD"
              :disabled="disabled || !year"
              :disabled-date="outside"
              :default-value="initialMonth"
              :editable="false"
              :aria-label="`安排 ${index + 1} 开始日期`"
              popper-class="calendar-correction-picker"
              @update:model-value="update(index, { startDate: $event || '' })"
            /></label>
            <label class="correction-date">结束日期<el-date-picker
              :model-value="item.endDate || null"
              type="date"
              value-format="YYYY-MM-DD"
              :disabled="disabled || !year"
              :disabled-date="(date: Date) => endDisabled(date, item.startDate)"
              :default-value="endMonths[index]"
              :editable="false"
              :aria-label="`安排 ${index + 1} 结束日期`"
              popper-class="calendar-correction-picker"
              @update:model-value="update(index, { endDate: $event || '' })"
            /></label>
          </div>
        </section>
      </div>
      <el-button
        :disabled="disabled || modelValue.ranges.length >= 80"
        @click="add"
      >
        添加日期安排
      </el-button>
    </section>
  </el-config-provider>
</template>

<style scoped>
.correction-intro { color: var(--el-text-color-secondary); line-height: 1.7; margin: 0 0 16px; }
.correction-year { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; font-weight: 600; }
.correction-rows { display: grid; gap: 12px; margin-bottom: 16px; }
.correction-row { padding: 12px; border: 1px solid var(--el-border-color-lighter); border-radius: 6px; min-width: 0; }
.correction-row-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.correction-fields { display: grid; grid-template-columns: minmax(0,1.2fr) minmax(0,.6fr) minmax(0,1fr) minmax(0,1fr); gap: 12px; }
.correction-fields label, .correction-type { display: flex; flex-direction: column; gap: 6px; min-width: 0; font-size: 12px; color: var(--el-text-color-regular); }
.correction-fields :deep(.el-input), .correction-fields :deep(.el-select) { width: 100%; min-width: 0; }
.is-mobile .correction-fields { grid-template-columns: repeat(2,minmax(0,1fr)); }
@media (max-width: 360px) { .is-mobile .correction-date { grid-column: 1 / -1; } }
</style>
<style>
.calendar-correction-picker { max-width: calc(100vw - 24px); }
.calendar-correction-picker .el-date-picker { width: min(322px,calc(100vw - 24px)); }
.calendar-correction-picker .el-picker-panel__content { width: auto; margin: 10px; }
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { attendanceDate, defaultAttendanceDates, missingAttendanceYears, MAX_ATTENDANCE_DATES } from '@/utils/attendanceDates'
import type { AttendanceCalendar } from '@/types/attendance'

const props = withDefaults(defineProps<{
  modelValue?: readonly string[]
  startDate: string
  endDate: string
  label: string
  readonly?: boolean
  calendar?: AttendanceCalendar
  refreshing?: boolean
}>(), { modelValue: undefined, readonly: false, calendar: undefined })
const emit = defineEmits<{ 'update:modelValue': [dates: string[]]; refresh: [] }>()
const month = ref(props.startDate.slice(0, 7))
watch(() => [props.startDate, props.endDate], () => {
  if (month.value < props.startDate.slice(0, 7) || month.value > props.endDate.slice(0, 7)) month.value = props.startDate.slice(0, 7)
})
const selected = computed(() => new Set(props.modelValue ?? []))
const missingYears = computed(() => missingAttendanceYears(props.startDate, props.endDate, props.calendar))
const outside = computed(() => [...selected.value].filter((day) => !attendanceDate(day) || day < props.startDate || day > props.endDate).sort())
const cells = computed(() => {
  const first = attendanceDate(`${month.value}-01`)
  if (!first) return []
  const mondayOffset = (first.getUTCDay() + 6) % 7
  return Array.from({ length: 42 }, (_, index) => {
    const date = new Date(first.getTime() + (index - mondayOffset) * 86_400_000)
    const day = date.toISOString().slice(0, 10)
    const schedule = props.calendar?.days[day]
    return { day, number: date.getUTCDate(), otherMonth: day.slice(0, 7) !== month.value, weekend: date.getUTCDay() === 0 || date.getUTCDay() === 6, schedule }
  })
})
const canDefault = computed(() => {
  const start = attendanceDate(props.startDate), end = attendanceDate(props.endDate)
  return start && end && start <= end && (end.getTime() - start.getTime()) / 86_400_000 < MAX_ATTENDANCE_DATES
})
function move(delta: number): void {
  const date = attendanceDate(`${month.value}-01`)
  if (!date) return
  date.setUTCMonth(date.getUTCMonth() + delta)
  month.value = date.toISOString().slice(0, 7)
}
function toggle(day: string): void {
  if (props.readonly || missingYears.value.length) return
  const next = new Set(selected.value)
  if (next.has(day)) next.delete(day)
  else if (attendanceDate(day) && day >= props.startDate && day <= props.endDate && next.size < MAX_ATTENDANCE_DATES) next.add(day)
  emit('update:modelValue', [...next].sort())
}
</script>

<template>
  <section
    class="attendance-picker"
    :aria-label="props.label"
  >
    <div class="attendance-heading">
      <strong>出勤日期</strong>
      <span role="status">已选 {{ selected.size }} 天</span>
    </div>
    <p class="attendance-help">
      默认排除节假日、选中调休补班；加班可补选，未出勤日可取消。
    </p>
    <div
      v-if="missingYears.length && !props.readonly"
      class="attendance-legacy"
      role="alert"
    >
      <p>{{ missingYears.join('、') }} 年节假日与调休安排尚未完整维护，请联系管理员维护后重新检查。已填写内容保留，补齐前暂不能确认报销。</p>
      <el-button
        :loading="props.refreshing"
        @click="emit('refresh')"
      >
        重新检查
      </el-button>
    </div>
    <p
      v-if="props.modelValue === undefined && !props.readonly && !missingYears.length"
      class="attendance-legacy"
      role="status"
    >
      尚未填写出勤日期，请带入默认工作日后核对。
      <el-button
        link
        type="primary"
        :disabled="!canDefault"
        @click="emit('update:modelValue', defaultAttendanceDates(props.startDate, props.endDate, props.calendar))"
      >
        带入默认工作日
      </el-button>
    </p>
    <div class="attendance-calendar">
      <div class="attendance-navigation">
        <el-button
          :aria-label="`${props.label} 上个月`"
          :disabled="month <= props.startDate.slice(0, 7)"
          @click="move(-1)"
        >
          ‹
        </el-button>
        <strong>{{ month.replace('-', '年') }}月</strong>
        <el-button
          :aria-label="`${props.label} 下个月`"
          :disabled="month >= props.endDate.slice(0, 7)"
          @click="move(1)"
        >
          ›
        </el-button>
      </div>
      <div class="attendance-grid">
        <span
          v-for="day in ['一', '二', '三', '四', '五', '六', '日']"
          :key="day"
          class="attendance-weekday"
        >{{ day }}</span>
        <button
          v-for="cell in cells"
          :key="cell.day"
          type="button"
          class="attendance-day"
          :class="{ 'attendance-day--selected': selected.has(cell.day), 'attendance-day--weekend': cell.weekend, 'attendance-day--muted': cell.otherMonth, 'attendance-day--outside': selected.has(cell.day) && (cell.day < props.startDate || cell.day > props.endDate) }"
          :aria-label="`${props.label} ${cell.day}`"
          :title="cell.schedule ? `${cell.day} ${cell.schedule.name}（${cell.schedule.working ? '补班' : '放假'}）` : cell.day"
          :aria-pressed="selected.has(cell.day)"
          :disabled="props.readonly || !!missingYears.length || (!selected.has(cell.day) && (cell.day < props.startDate || cell.day > props.endDate))"
          @click="toggle(cell.day)"
        >
          {{ cell.number }}
          <small
            v-if="cell.schedule"
            class="attendance-day__schedule"
            :class="{ 'attendance-day__schedule--rest': !cell.schedule.working }"
            aria-hidden="true"
          >{{ cell.schedule.working ? '班' : '休' }}</small>
        </button>
      </div>
    </div>
    <div
      v-if="outside.length"
      class="attendance-outside"
      role="status"
    >
      <span>以下已选日期需调整：</span>
      <el-button
        v-for="day in outside"
        :key="day"
        size="small"
        type="danger"
        plain
        :disabled="props.readonly || !!missingYears.length"
        :aria-label="`${props.label} 取消 ${day}`"
        @click="toggle(day)"
      >
        {{ day || '无效日期' }} ×
      </el-button>
    </div>
  </section>
</template>

<style scoped>
.attendance-picker { max-width: 446px; min-width: 0; margin-top: 12px; }
.attendance-heading, .attendance-navigation { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.attendance-heading > span, .attendance-help { color: var(--el-text-color-secondary); font-size: 13px; }
.attendance-help { margin: 6px 0 12px; line-height: 1.5; }
.attendance-calendar { max-width: 420px; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; background: var(--el-bg-color); }
.attendance-navigation { margin-bottom: 10px; }
.attendance-navigation .el-button { margin: 0; padding-inline: 10px; }
.attendance-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
.attendance-weekday { text-align: center; font-size: 12px; color: var(--el-text-color-secondary); padding-bottom: 5px; }
.attendance-day { position: relative; min-width: 0; min-height: 36px; padding: 8px 0 4px; border: 1px solid transparent; border-radius: 6px; background: transparent; color: var(--el-text-color-primary); cursor: pointer; font-size: 13px; }
.attendance-day--weekend, .attendance-day--muted { color: var(--el-text-color-secondary); }
.attendance-day--selected { background: var(--el-color-primary); color: white; }
.attendance-day--outside { background: var(--el-color-danger-light-9); border-color: var(--el-color-danger); color: var(--el-color-danger); }
.attendance-day__schedule { position: absolute; top: 0; right: 2px; font-size: 9px; line-height: 12px; color: var(--el-color-primary); }
.attendance-day__schedule--rest { color: var(--el-color-danger); }
.attendance-day--selected .attendance-day__schedule { color: white; }
.attendance-day:disabled { cursor: default; opacity: .45; }
.attendance-outside { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; color: var(--el-color-danger); font-size: 13px; }
.attendance-outside .el-button { margin: 0; }
.attendance-legacy { font-size: 13px; color: var(--el-color-warning); }
</style>

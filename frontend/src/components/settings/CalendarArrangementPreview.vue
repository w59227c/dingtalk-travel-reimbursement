<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import type { AttendanceCalendarDay } from '@/types/attendance'
import { calendarMonthDays, calendarRanges, type CalendarRange } from '@/utils/calendarMaintenance'

const props = withDefaults(defineProps<{ year: number; days: AttendanceCalendarDay[]; showDetails?: boolean; mobile?: boolean }>(), { showDetails: true, mobile: false })
const month = defineModel<number>('month', { default: 1 })
const calendar = ref<HTMLElement | null>(null)
const cells = computed(() => calendarMonthDays(props.year, month.value))
const byDate = computed(() => new Map(props.days.map((day) => [day.date, day])))
const ranges = computed(() => calendarRanges(props.days))
const markedMonths = computed(() => new Set(props.days.map((day) => Number(day.date.slice(5, 7)))))
function selectMonth(value: number): void { if (Number.isInteger(value) && value >= 1 && value <= 12) month.value = value }
function activeRange(range: CalendarRange): boolean {
  const current = `${String(props.year).padStart(4, '0')}-${String(month.value).padStart(2, '0')}`
  return range.start.slice(0, 7) <= current && range.end.slice(0, 7) >= current
}
async function locate(range: CalendarRange): Promise<void> {
  selectMonth(Number(range.start.slice(5, 7)))
  await nextTick()
  calendar.value?.scrollIntoView?.({ block: 'nearest' })
}
function dayTitle(cell: { date: string; otherMonth: boolean }): string {
  if (cell.otherMonth) return `${cell.date}（其他月份）`
  const value = byDate.value.get(cell.date)
  return `${cell.date} ${value ? `${value.name} ${value.working ? '补班' : '放假'}` : '按周一至周五默认'}`
}
</script>
<template>
  <div
    class="arrangement-preview"
    :class="{ 'arrangement-preview--details': showDetails, 'is-mobile': mobile }"
  >
    <div class="arrangement-layout">
      <section
        ref="calendar"
        class="arrangement-calendar"
        aria-label="月历"
      >
        <div class="arrangement-month-heading">
          <button
            type="button"
            class="arrangement-step"
            aria-label="上个月"
            :disabled="month === 1"
            @click="selectMonth(month - 1)"
          >
            ‹
          </button>
          <strong aria-live="polite">{{ year }} 年 {{ month }} 月</strong>
          <button
            type="button"
            class="arrangement-step"
            aria-label="下个月"
            :disabled="month === 12"
            @click="selectMonth(month + 1)"
          >
            ›
          </button>
        </div>
        <nav
          class="arrangement-month-nav"
          aria-label="选择查看月份"
        >
          <button
            v-for="value in 12"
            :key="value"
            type="button"
            :aria-label="`查看 ${year} 年 ${value} 月`"
            :aria-pressed="month === value"
            :title="markedMonths.has(value) ? `${value} 月有特殊安排` : `${value} 月按默认工作周`"
            @click="selectMonth(value)"
          >
            {{ value }} 月<span
              v-if="markedMonths.has(value)"
              class="month-mark"
              aria-hidden="true"
            />
          </button>
        </nav>
        <div class="arrangement-legend">
          <span><i class="rest" />放假</span><span><i class="work" />补班</span><span><i class="month-mark-example" />有特殊安排的月份</span>
        </div>
        <div
          class="arrangement-grid"
          :aria-label="`${year} 年 ${month} 月安排，只读`"
        >
          <span
            v-for="label in ['一', '二', '三', '四', '五', '六', '日']"
            :key="label"
            class="weekday"
          >{{ label }}</span>
          <span
            v-for="cell in cells"
            :key="cell.date"
            class="arrangement-day"
            :class="{ muted: cell.otherMonth || cell.weekend, rest: !cell.otherMonth && byDate.get(cell.date)?.working === false, work: !cell.otherMonth && byDate.get(cell.date)?.working === true }"
            :title="dayTitle(cell)"
          >
            {{ cell.number }}<small v-if="!cell.otherMonth && byDate.has(cell.date)">{{ byDate.get(cell.date)!.working ? '班' : '休' }}</small>
          </span>
        </div>
        <p
          v-if="!markedMonths.has(month)"
          class="arrangement-empty"
        >
          本月无特殊安排，按周一至周五默认出勤。
        </p>
      </section>
      <section
        v-if="showDetails"
        class="arrangement-ranges"
        aria-label="年度日期清单"
      >
        <div class="arrangement-ranges-heading">
          <strong>年度安排</strong><span>点击日期定位月份</span>
        </div>
        <ul class="arrangement-range-list">
          <li
            v-for="range in ranges"
            :key="range.start"
          >
            <button
              type="button"
              :class="{ active: activeRange(range) }"
              :aria-label="`查看 ${range.start}${range.end !== range.start ? ` 至 ${range.end}` : ''} ${range.working ? '补班' : '放假'} ${range.name}`"
              @click="locate(range)"
            >
              <span>{{ range.start }}{{ range.end !== range.start ? ` 至 ${range.end}` : '' }}</span><span :class="range.working ? 'work-text' : 'rest-text'">{{ range.working ? '补班' : '放假' }} · {{ range.name }}</span>
            </button>
          </li>
        </ul>
        <p
          v-if="!days.length"
          class="arrangement-empty"
        >
          该年没有特殊安排。
        </p>
      </section>
    </div>
  </div>
</template>
<style scoped>
.arrangement-preview, .arrangement-calendar { min-width: 0; }
.arrangement-preview--details .arrangement-layout { display: grid; grid-template-columns: repeat(auto-fit,minmax(min(100%,320px),1fr)); align-items: start; gap: 24px; }
.arrangement-preview.is-mobile .arrangement-layout { grid-template-columns: minmax(0,1fr); }
.arrangement-month-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.arrangement-month-heading strong { font-size: 15px; }
.arrangement-step { width: 34px; height: 34px; background: var(--el-bg-color); color: var(--el-text-color-regular); border: 1px solid var(--el-border-color); border-radius: 4px; font-size: 24px; line-height: 1; }
.arrangement-step:disabled { opacity: .4; cursor: default; }
.arrangement-month-nav { display: grid; grid-template-columns: repeat(6,minmax(0,1fr)); gap: 6px; margin-bottom: 12px; }
.arrangement-month-nav button { position: relative; min-width: 0; height: 38px; padding: 0 2px; border: 1px solid transparent; border-radius: 4px; background: var(--el-fill-color-light); color: var(--el-text-color-regular); font: inherit; font-size: 12px; }
.arrangement-month-nav button[aria-pressed="true"] { background: var(--el-color-primary); color: white; }
.arrangement-month-nav button:hover:not([aria-pressed="true"]) { border-color: var(--el-color-primary-light-5); color: var(--el-color-primary); }
.month-mark { position: absolute; right: 5px; top: 5px; width: 4px; height: 4px; border-radius: 50%; background: var(--el-color-primary); }
.arrangement-month-nav button[aria-pressed="true"] .month-mark { background: white; }
.arrangement-legend { color: var(--el-text-color-secondary); font-size: 11px; display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.arrangement-legend > span { display: inline-flex; align-items: center; gap: 4px; }
.arrangement-legend i { width: 7px; height: 7px; border-radius: 2px; }
.arrangement-legend .rest { background: #e77a74; }.arrangement-legend .work { background: #5297ed; }
.arrangement-legend .month-mark-example { width: 4px; height: 4px; border-radius: 50%; background: var(--el-color-primary); }
.arrangement-grid { display: grid; grid-template-columns: repeat(7,minmax(0,1fr)); gap: 4px; }
.weekday { text-align: center; padding: 4px 0; font-size: 12px; color: var(--el-text-color-secondary); }
.arrangement-day { min-height: 40px; display: flex; align-items: center; justify-content: center; position: relative; border-radius: 4px; font-size: 14px; }
.arrangement-day small { position: absolute; font-size: 9px; top: 1px; right: 3px; }
.muted { color: var(--el-text-color-placeholder); }
.rest { background: #fff0ef; color: #b44942; }.work { background: #eaf3ff; color: #2469bc; }
.arrangement-ranges { min-width: 0; color: var(--el-text-color-regular); }
.arrangement-ranges-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin: 8px 0 12px; }.arrangement-ranges-heading > span { font-size: 12px; color: var(--el-text-color-secondary); }
.arrangement-range-list { padding: 0; margin: 0; list-style: none; }
.arrangement-preview:not(.is-mobile) .arrangement-range-list { max-height: 430px; overflow-y: auto; scrollbar-gutter: stable; }
.arrangement-range-list button { width: 100%; display: flex; flex-direction: column; gap: 4px; text-align: left; padding: 10px 12px; border: 1px solid transparent; border-bottom-color: var(--el-border-color-lighter); border-radius: 4px; background: var(--el-bg-color); color: var(--el-text-color-regular); font: inherit; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
.arrangement-range-list button.active { background: var(--el-color-primary-light-9); border-color: var(--el-color-primary-light-8); }
.arrangement-range-list button:hover { border-color: var(--el-color-primary-light-5); }
button:not(:disabled) { cursor: pointer; }button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; }
.arrangement-empty { color: var(--el-text-color-secondary); font-size: 12px; line-height: 1.6; margin: 10px 0 0; }
.work-text { color: #2469bc; }.rest-text { color: #b44942; }
</style>

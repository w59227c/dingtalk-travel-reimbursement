<script setup lang="ts">
import { computed, ref } from 'vue'

import { useExpenseStore } from '@/stores/expense'
import type { ExcludedPeriodInput, SubsidyResult, TripInput, TripType } from '@/types/expenses'
import { adjustmentType, subsidyHalfDayIndex } from '@/utils/subsidyAdjustments'
import { resetSubsidyWindow, subsidyAbsenceIssue, subsidyWindow, updateSubsidyWindow } from '@/utils/subsidyWindow'
import { groupOverlappingSubsidyTrips, type SubsidyTripGroup } from '@/utils/subsidyTripGroups'
import { TRIP_PERIOD_TIME, tripPeriodFromTime, type TripDayPeriod } from '@/utils/tripPeriod'
import { groupAttendanceDates, setGroupAttendanceDates } from '@/utils/attendanceDates'
import AttendanceDatesPicker from './AttendanceDatesPicker.vue'

interface SubsidyApproval {
  processInstanceId: string
  title: string
  startDate: string
  endDate: string
}

const props = withDefaults(defineProps<{
  mobile?: boolean
  readonly?: boolean
  approvals?: readonly SubsidyApproval[]
  approvalTripType?: TripType | null
}>(), {
  mobile: false,
  readonly: false,
  approvals: () => [],
  approvalTripType: null,
})

const subsidyEndpoints = [
  { key: 'start', label: '补助开始', dateField: 'startDate', timeField: 'startTime', dateLabel: '开始日期', periodLabel: '出发时段' },
  { key: 'end', label: '补助结束', dateField: 'endDate', timeField: 'endTime', dateLabel: '结束日期', periodLabel: '返回时段' },
] as const

const excludedEndpoints = [
  { key: 'start', label: '离开', dateField: 'startDate', timeField: 'startTime' },
  { key: 'end', label: '返回', dateField: 'endDate', timeField: 'endTime' },
] as const

const expense = useExpenseStore()
const subsidyGroups = computed(() => groupOverlappingSubsidyTrips(expense.subsidyTrips))
// Element Plus resets an open calendar when its default Date reference changes.
const subsidyCalendarStarts = computed<Map<string, Date | undefined>>((previous) => {
  const starts = new Map<string, Date | undefined>()
  for (const group of subsidyGroups.value) {
    const startDate = subsidyWindow(group).startDate
    const existing = previous?.get(group.key)
    starts.set(group.key, existing && calendarDay(existing) === startDate ? existing : parseCalendarDay(startDate))
  }
  return starts
})
const returnCalendarStarts = computed<Map<ExcludedPeriodInput, Date | undefined>>((previous) => {
  const starts = new Map<ExcludedPeriodInput, Date | undefined>()
  for (const group of subsidyGroups.value) {
    const window = subsidyWindow(group)
    for (const absence of group.excludedPeriods.filter((item) => adjustmentType(item) === 'temporary_absence')) {
      const departureDay = absence.startDate ?? ''
      const day = parseCalendarDay(window.startDate) && parseCalendarDay(window.endDate)
        && parseCalendarDay(departureDay) && departureDay >= window.startDate && departureDay <= window.endDate
        ? departureDay : window.startDate
      const existing = previous?.get(absence)
      starts.set(absence, existing && calendarDay(existing) === day ? existing : parseCalendarDay(day))
    }
  }
  return starts
})
const absenceIssues = computed(() => {
  const issues = new Map<ExcludedPeriodInput, string>()
  if (props.readonly) return issues
  for (const group of subsidyGroups.value) {
    for (const absence of group.excludedPeriods.filter((item) => adjustmentType(item) === 'temporary_absence')) {
      issues.set(absence, attendanceMode(group) ? '旧时段，请核对后删除' : subsidyAbsenceIssue(group, absence))
    }
  }
  return issues
})

function attendanceMode(group: SubsidyTripGroup): boolean {
  return props.approvalTripType === 'same_city_project'
    && (!props.readonly || group.trips.every((trip) => trip.attendanceDates !== undefined))
}

function updateAttendance(group: SubsidyTripGroup, dates: string[]): void {
  if (!props.readonly) setGroupAttendanceDates(group, dates)
}
const subsidyCalculationError = computed(() => {
  if (!expense.includeSubsidy || !expense.calculationError || expense.itemReadinessError) return ''
  if (expense.calculationError === expense.policyInputError) return ''
  return expense.calculationError
})

const tripTypeNames: Record<TripType, string> = {
  business: '境内商务出差',
  project: '境内市外项目',
  same_city_project: '境内同市项目',
  internal: '公司内部出差',
  overseas: '境外出差',
  short_term_project: '境内市外项目出差（短期）',
  long_term_project: '境内市外项目出差（长期）',
  short_term_internal: '公司内部出差（短期）',
  long_term_internal: '公司内部出差（长期）',
  short_term_overseas: '境外商务出差（短期）',
  long_term_overseas: '境外商务出差（长期）',
}

function tripTypeName(type: TripType | null | undefined): string {
  return type ? tripTypeNames[type] : '未识别出差类别'
}

function approvalFor(item: TripInput): SubsidyApproval | undefined {
  return props.approvals.find((approval) => approval.processInstanceId === item.relatedApprovalId)
}

function period(time: string | undefined): TripDayPeriod | undefined {
  return tripPeriodFromTime(time)
}

const windowErrors = ref<Record<string, string>>({})

function restoreApprovalDates(group: SubsidyTripGroup): void {
  if (props.readonly) return
  resetSubsidyWindow(group)
  windowErrors.value[group.key] = ''
}

function setWindow(group: SubsidyTripGroup, field: 'startDate' | 'endDate' | 'startTime' | 'endTime', value: unknown): void {
  if (props.readonly) return
  let normalized = typeof value === 'string' ? value : ''
  if (field === 'startTime' || field === 'endTime') {
    if (value !== 'morning' && value !== 'afternoon') return
    normalized = TRIP_PERIOD_TIME[value]
  }
  windowErrors.value[group.key] = updateSubsidyWindow(group, field, normalized)
}

function calendarDay(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function parseCalendarDay(value: string): Date | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined
  const date = new Date(`${value}T00:00:00`)
  return Number.isFinite(date.getTime()) && calendarDay(date) === value ? date : undefined
}

function disabledDate(bounds: Pick<TripInput, 'startDate' | 'endDate'>, date: Date): boolean {
  const day = calendarDay(date)
  return !parseCalendarDay(bounds.startDate) || !parseCalendarDay(bounds.endDate)
    || day < bounds.startDate || day > bounds.endDate
}

function disabledExcludedPeriod(
  group: SubsidyTripGroup,
  absence: ExcludedPeriodInput,
  field: 'startTime' | 'endTime',
  day: string | undefined,
  part: TripDayPeriod,
): boolean {
  const candidate = subsidyHalfDayIndex(day, TRIP_PERIOD_TIME[part])
  if (candidate === null) return false
  const window = subsidyWindow(group)
  const bounds = [
    subsidyHalfDayIndex(group.startDate, group.startTime),
    subsidyHalfDayIndex(group.endDate, group.endTime),
    subsidyHalfDayIndex(window.startDate, window.startTime),
    subsidyHalfDayIndex(window.endDate, window.endTime),
  ]
  if (bounds.some((bound) => bound === null)) return true
  if (candidate < Math.max(bounds[0]!, bounds[2]!) || candidate > Math.min(bounds[1]!, bounds[3]!)) return true
  if (field === 'endTime') {
    const departureDay = absence.startDate ?? ''
    if (day && parseCalendarDay(departureDay) && day < departureDay) return true
    const departure = subsidyHalfDayIndex(absence.startDate, absence.startTime)
    if (departure !== null && candidate < departure) return true
  }
  return false
}

function disabledExcludedDate(
  group: SubsidyTripGroup,
  absence: ExcludedPeriodInput,
  field: 'startDate' | 'endDate',
  date: Date,
): boolean {
  if (disabledDate(group, date) || disabledDate(subsidyWindow(group), date)) return true
  const timeField = field === 'startDate' ? 'startTime' : 'endTime'
  const day = calendarDay(date)
  return disabledExcludedPeriod(group, absence, timeField, day, 'morning')
    && disabledExcludedPeriod(group, absence, timeField, day, 'afternoon')
}

function addExcludedPeriod(group: SubsidyTripGroup): void {
  if (props.readonly || group.excludedPeriods.length >= 100) return
  const first = group.trips[0]!
  first.excludedPeriods ??= []
  first.excludedPeriods.push({ startDate: '', startTime: '09:00', endDate: '', endTime: '09:00' })
}

function removeExcludedPeriod(group: SubsidyTripGroup, excluded: ExcludedPeriodInput): void {
  if (props.readonly) return
  for (const trip of group.trips) {
    trip.excludedPeriods = trip.excludedPeriods?.filter((item) => item !== excluded)
  }
}

function setExcludedPeriod(
  excluded: ExcludedPeriodInput,
  field: 'startTime' | 'endTime',
  value: string | number | boolean | undefined,
): void {
  if (!props.readonly && (value === 'morning' || value === 'afternoon')) {
    excluded[field] = TRIP_PERIOD_TIME[value]
  }
}

function setExcludedDate(excluded: ExcludedPeriodInput, field: 'startDate' | 'endDate', value: unknown): void {
  if (!props.readonly) excluded[field] = typeof value === 'string' ? value : ''
}

function subsidyFor(group: SubsidyTripGroup, index: number): SubsidyResult | null {
  const subsidies = expense.totals?.subsidies
  return subsidies?.find((result) => result.relatedApprovalId === group.primaryRelatedApprovalId)
    ?? subsidies?.[index]
    ?? (expense.subsidyTrips.length === 1 ? expense.totals?.subsidy ?? null : null)
}
</script>

<template>
  <el-card
    shadow="never"
    class="content-card reimbursement-card"
  >
    <template #header>
      <div class="subsidy-card-header">
        <strong>出差补助（可选）</strong>
        <el-switch
          :model-value="expense.includeSubsidy"
          :disabled="props.readonly || (!expense.includeSubsidy
            && (!props.approvals.length || !props.approvalTripType))"
          aria-label="申请出差补助"
          @update:model-value="expense.setSubsidyIncluded"
        />
      </div>
    </template>
    <details
      v-if="expense.includeSubsidy"
      class="subsidy-help"
    >
      <summary>补助计算说明</summary>
      <p v-if="subsidyGroups.some(attendanceMode)">
        市内出差按实际出勤日期计，每个选中日期计 1 天，不区分上午下午。首次按管理员维护的放假调休日历默认选择工作日，可补选加班日期或取消未出勤日期。
      </p>
      <p v-else>
        补助日期可在审批范围内调整；出发日上午计 1 天、下午计 0.5 天，返回日上午计 0.5 天、下午计 1 天，同日最多计 1 天。中途离开可添加非补助时段，各段不能重叠。
      </p>
    </details>
    <el-form
      label-position="top"
      :disabled="props.readonly"
    >
      <el-alert
        v-if="!props.approvals.length"
        title="请先关联已通过的出差审批"
        description="系统按审批日期生成补助时段；不连续时分别计算，日期重叠时自动合并。"
        type="info"
        :closable="false"
      />
      <el-alert
        v-else-if="!props.approvalTripType"
        title="未能识别关联审批的出差类别"
        description="请让管理员检查出差审批模板的类别映射后再申请补助。"
        type="warning"
        :closable="false"
      />
      <el-alert
        v-else-if="!expense.includeSubsidy"
        title="本报销单不申请出差补助"
        description="费用明细仍可正常填写和提交。"
        type="info"
        :closable="false"
      />

      <template v-else>
        <el-form-item label="补助规则">
          <el-input
            :model-value="tripTypeName(props.approvalTripType)"
            disabled
          />
        </el-form-item>

        <article
          v-for="(group, index) in subsidyGroups"
          :key="group.key"
          class="subsidy-item"
          :class="{ 'subsidy-item--mobile': props.mobile, 'subsidy-item--attendance': attendanceMode(group) }"
        >
          <div
            class="subsidy-item__heading"
            :class="{ 'subsidy-item__heading--mobile': props.mobile }"
          >
            <div>
              <strong>补助 {{ index + 1 }}</strong>
              <p class="subsidy-approval-title">
                {{ group.trips.map((item) => approvalFor(item)?.title || '出差审批').join('、') }}
              </p>
              <small v-if="group.trips.length > 1">由 {{ group.trips.length }} 张审批合并</small>
            </div>
            <span class="subsidy-approval-dates">{{ group.trips.length > 1 ? '合并审批日期' : '审批日期' }}：<time>{{ group.startDate }}</time> 至 <time>{{ group.endDate }}</time></span>
          </div>

          <div class="subsidy-date-editor">
            <div
              v-for="endpoint in subsidyEndpoints"
              :key="endpoint.key"
              class="subsidy-endpoint"
              role="group"
              :aria-label="`补助 ${index + 1} ${endpoint.label}`"
            >
              <el-form-item :label="endpoint.label">
                <div class="subsidy-endpoint-controls">
                  <el-date-picker
                    :model-value="subsidyWindow(group)[endpoint.dateField]"
                    type="date"
                    :popper-class="props.mobile ? 'excluded-period-picker--mobile' : undefined"
                    value-format="YYYY-MM-DD"
                    :clearable="false"
                    :disabled-date="(date: Date) => disabledDate(group, date)"
                    :aria-label="`补助 ${index + 1} ${endpoint.dateLabel}`"
                    @update:model-value="setWindow(group, endpoint.dateField, $event)"
                  />
                  <el-radio-group
                    v-if="!attendanceMode(group)"
                    :model-value="period(subsidyWindow(group)[endpoint.timeField])"
                    :aria-label="`补助 ${index + 1} ${endpoint.periodLabel}`"
                    @update:model-value="setWindow(group, endpoint.timeField, $event)"
                  >
                    <el-radio-button value="morning">
                      上午
                    </el-radio-button>
                    <el-radio-button value="afternoon">
                      下午
                    </el-radio-button>
                  </el-radio-group>
                </div>
              </el-form-item>
            </div>
            <div
              v-if="group.excludedPeriods.some((item) => adjustmentType(item) !== 'temporary_absence')"
              class="subsidy-window-actions"
            >
              <el-button
                link
                type="primary"
                :aria-label="`补助 ${index + 1} 恢复审批日期`"
                @click="restoreApprovalDates(group)"
              >
                恢复审批日期
              </el-button>
            </div>
            <small
              v-if="windowErrors[group.key]"
              class="subsidy-window-error"
              role="alert"
            >{{ windowErrors[group.key] }}</small>
          </div>

          <AttendanceDatesPicker
            v-if="attendanceMode(group)"
            :model-value="groupAttendanceDates(group)"
            :calendar="expense.attendanceCalendar"
            :start-date="subsidyWindow(group).startDate"
            :end-date="subsidyWindow(group).endDate"
            :readonly="props.readonly"
            :label="`补助 ${index + 1} 出勤日期`"
            @update:model-value="updateAttendance(group, $event)"
          />

          <section
            v-if="!attendanceMode(group) || group.excludedPeriods.some((item) => adjustmentType(item) === 'temporary_absence')"
            class="excluded-periods"
            :class="{ 'excluded-periods--mobile': props.mobile }"
            :aria-label="`补助 ${index + 1} 非补助时段`"
          >
            <div class="excluded-periods__heading">
              <strong>{{ attendanceMode(group) ? '原非补助时段（核对后逐项删除）' : '非补助时段' }}</strong>
              <el-button
                v-if="!attendanceMode(group)"
                :aria-label="`补助 ${index + 1} 添加非补助时段`"
                :disabled="group.excludedPeriods.length >= 100"
                @click="addExcludedPeriod(group)"
              >
                添加非补助时段
              </el-button>
            </div>

            <div
              v-for="(excluded, excludedIndex) in group.excludedPeriods.filter((item) => adjustmentType(item) === 'temporary_absence')"
              :key="excludedIndex"
              class="excluded-period"
              :class="{ 'excluded-period--invalid': absenceIssues.get(excluded) }"
            >
              <div class="excluded-periods__heading">
                <div class="excluded-period__title">
                  <span>时段 {{ excludedIndex + 1 }}</span>
                  <small
                    v-if="absenceIssues.get(excluded)"
                    class="excluded-period__issue"
                    role="status"
                  >{{ absenceIssues.get(excluded) }}{{ attendanceMode(group) ? '' : '，请修改' }}</small>
                </div>
                <el-button
                  type="danger"
                  link
                  :aria-label="`补助 ${index + 1} 删除非补助时段 ${excludedIndex + 1}`"
                  @click="removeExcludedPeriod(group, excluded)"
                >
                  删除
                </el-button>
              </div>
              <div class="subsidy-date-editor excluded-period-editor">
                <div
                  v-for="endpoint in excludedEndpoints"
                  :key="endpoint.key"
                  class="subsidy-endpoint"
                  role="group"
                  :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} ${endpoint.label}`"
                >
                  <el-form-item :label="endpoint.label">
                    <div class="subsidy-endpoint-controls">
                      <el-date-picker
                        :disabled="attendanceMode(group)"
                        :model-value="excluded[endpoint.dateField]"
                        type="date"
                        :popper-class="props.mobile ? 'excluded-period-picker--mobile' : undefined"
                        value-format="YYYY-MM-DD"
                        format="YYYY-MM-DD"
                        :default-value="endpoint.key === 'end' ? returnCalendarStarts.get(excluded) : subsidyCalendarStarts.get(group.key)"
                        :disabled-date="(date: Date) => disabledExcludedDate(group, excluded, endpoint.dateField, date)"
                        :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} ${endpoint.label}日期`"
                        @update:model-value="setExcludedDate(excluded, endpoint.dateField, $event)"
                      />
                      <el-radio-group
                        :disabled="attendanceMode(group)"
                        :model-value="period(excluded[endpoint.timeField])"
                        :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} ${endpoint.label}时段`"
                        @update:model-value="setExcludedPeriod(excluded, endpoint.timeField, $event)"
                      >
                        <el-radio-button
                          value="morning"
                          :disabled="disabledExcludedPeriod(group, excluded, endpoint.timeField, excluded[endpoint.dateField], 'morning')"
                        >
                          上午
                        </el-radio-button>
                        <el-radio-button
                          value="afternoon"
                          :disabled="disabledExcludedPeriod(group, excluded, endpoint.timeField, excluded[endpoint.dateField], 'afternoon')"
                        >
                          下午
                        </el-radio-button>
                      </el-radio-group>
                    </div>
                  </el-form-item>
                </div>
              </div>
            </div>
          </section>

          <div
            v-if="subsidyFor(group, index) && (props.readonly || !expense.policyInputError)"
            class="subsidy-preview"
            role="status"
            aria-live="polite"
          >
            <span v-if="attendanceMode(group)">出勤 {{ Number(subsidyFor(group, index)?.effectiveDays) }} 天</span>
            <span v-else>审批自然日 {{ subsidyFor(group, index)?.calendarDays }} 天</span>
            <span v-if="!attendanceMode(group) && subsidyFor(group, index)?.excludedDays !== undefined">
              原补助 {{ (Number(subsidyFor(group, index)?.effectiveDays) + Number(subsidyFor(group, index)?.excludedDays)).toFixed(1) }} 天
            </span>
            <span v-if="!attendanceMode(group) && subsidyFor(group, index)?.excludedDays !== undefined">扣除 {{ subsidyFor(group, index)?.excludedDays }} 天</span>
            <span v-if="!attendanceMode(group)">有效 {{ subsidyFor(group, index)?.effectiveDays }} 天</span>
            <span>¥{{ subsidyFor(group, index)?.dailyRate }} / 天</span>
            <strong>补助 ¥{{ subsidyFor(group, index)?.total }}</strong>
          </div>
        </article>

        <p
          v-if="!props.readonly && expense.policyInputError"
          class="field-error"
        >
          {{ expense.policyInputError }}
        </p>
        <el-alert
          v-if="subsidyCalculationError"
          :title="subsidyCalculationError"
          type="error"
          :closable="false"
          class="subsidy-calculation-error"
        />
      </template>
    </el-form>
  </el-card>
</template>

<style scoped>
.subsidy-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.subsidy-card-header :deep(.el-switch) {
  flex-shrink: 0;
}

.subsidy-item {
  margin-top: 16px;
  padding: 18px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 10px;
  background: var(--el-fill-color-lighter);
}

.subsidy-item__heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.subsidy-item__heading > div {
  min-width: 0;
}

.subsidy-item__heading.subsidy-item__heading--mobile {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 6px;
}

.subsidy-item__heading.subsidy-item__heading--mobile .subsidy-approval-title {
  display: -webkit-box;
  overflow: hidden;
  line-height: 1.55;
  overflow-wrap: break-word;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.subsidy-item__heading.subsidy-item__heading--mobile > span {
  margin-top: 0;
  text-align: left;
  white-space: normal;
}

.subsidy-item__heading p {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
}

.subsidy-item__heading small {
  display: block;
  margin-top: 5px;
  color: var(--el-color-primary);
}

.subsidy-item__heading > span {
  min-width: 0;
  color: var(--el-text-color-secondary);
  text-align: right;
  white-space: normal;
}

.subsidy-date-editor {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  column-gap: 24px;
}

.subsidy-endpoint,
.subsidy-endpoint :deep(.el-form-item__content) {
  min-width: 0;
}

.subsidy-endpoint-controls {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  width: 100%;
}

.subsidy-endpoint-controls :deep(.el-date-editor) {
  width: 168px;
  max-width: 100%;
}

.subsidy-item--mobile .subsidy-date-editor {
  grid-template-columns: minmax(0, 1fr);
}

.subsidy-item--mobile .subsidy-endpoint-controls {
  flex-wrap: nowrap;
  gap: 8px;
}

.subsidy-item--mobile .subsidy-endpoint-controls :deep(.el-date-editor) {
  flex: 1 1 0;
  width: 0;
  min-width: 0;
}

.subsidy-item--mobile .subsidy-endpoint-controls :deep(.el-input__wrapper) {
  padding-inline: 8px;
}

.subsidy-item--mobile .subsidy-endpoint-controls :deep(.el-radio-group) {
  display: flex;
  flex-wrap: nowrap;
  flex: 0 0 auto;
}

.subsidy-item--mobile .subsidy-endpoint-controls :deep(.el-radio-button__inner) {
  padding-inline: 8px;
}

.subsidy-window-actions,
.subsidy-window-error {
  grid-column: 1 / -1;
}

.subsidy-window-actions {
  margin-top: -8px;
}

.subsidy-window-error {
  color: var(--el-color-danger);
}

.excluded-periods { margin-top: 16px; }
.excluded-periods__heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.excluded-period { margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--el-border-color-light); }
.excluded-period--invalid { border-top-color: var(--el-color-danger-light-5); }
.excluded-period__title { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; min-width: 0; }
.excluded-period__issue { color: var(--el-color-danger); font-size: 12px; }
.excluded-period-editor { margin-top: 12px; row-gap: 16px; }
.excluded-period-editor :deep(.el-form-item) { margin-bottom: 0; }
.excluded-periods--mobile .excluded-periods__heading { flex-wrap: wrap; }

:global(.excluded-period-picker--mobile .el-date-picker) {
  width: min(322px, calc(100vw - 24px));
}

:global(.excluded-period-picker--mobile .el-date-picker .el-picker-panel__content) {
  width: calc(100% - 24px);
  margin: 12px;
}

.subsidy-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
  margin-top: 14px;
  padding: 12px 14px;
  border-radius: 8px;
  background: var(--el-color-primary-light-9);
}

.subsidy-preview strong {
  margin-left: auto;
  color: var(--el-color-primary);
}

.field-error {
  margin: 12px 0 0;
  color: var(--el-color-danger);
}

@media (min-width: 900px) {
  .subsidy-item--attendance:not(.subsidy-item--mobile) {
    display: grid;
    grid-template-columns: minmax(180px, .65fr) minmax(0, 1fr);
    column-gap: 32px;
  }

  .subsidy-item--attendance:not(.subsidy-item--mobile) > .subsidy-item__heading,
  .subsidy-item--attendance:not(.subsidy-item--mobile) > .excluded-periods,
  .subsidy-item--attendance:not(.subsidy-item--mobile) > .subsidy-preview {
    grid-column: 1 / -1;
  }

  .subsidy-item--attendance:not(.subsidy-item--mobile) > .subsidy-date-editor {
    display: block;
    align-self: start;
  }

  .subsidy-item--attendance:not(.subsidy-item--mobile) > .attendance-picker {
    margin-top: 0;
  }
}

@media (max-width: 720px) {
  .subsidy-date-editor {
    grid-template-columns: minmax(0, 1fr);
  }

  .subsidy-item__heading {
    display: block;
  }

  .subsidy-item__heading > span {
    display: block;
    margin-top: 6px;
    text-align: left;
  }

  .subsidy-preview strong {
    width: 100%;
    margin-left: 0;
  }
}

</style>

<style scoped>
.subsidy-item--mobile .subsidy-preview { font-size: 13px; gap: 6px 10px; padding: 10px; }
.subsidy-item--mobile .subsidy-preview span { white-space: nowrap; }
.subsidy-item--mobile .subsidy-preview strong { width: auto; margin-left: auto; font-size: 16px; }
.subsidy-help { margin-bottom: 12px; color: var(--el-text-color-secondary); font-size: 13px; }
.subsidy-approval-dates time { white-space: nowrap; }
.subsidy-approval-dates { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.5; }
</style>

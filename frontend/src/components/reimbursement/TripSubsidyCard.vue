<script setup lang="ts">
import { computed } from 'vue'

import { useExpenseStore } from '@/stores/expense'
import type { ExcludedPeriodInput, SubsidyResult, TripInput, TripType } from '@/types/expenses'
import { groupOverlappingSubsidyTrips, type SubsidyTripGroup } from '@/utils/subsidyTripGroups'
import { TRIP_PERIOD_TIME, tripPeriodFromTime, type TripDayPeriod } from '@/utils/tripPeriod'

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

const expense = useExpenseStore()
const subsidyGroups = computed(() => groupOverlappingSubsidyTrips(expense.subsidyTrips))
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

function period(time: string): TripDayPeriod | undefined {
  return tripPeriodFromTime(time)
}

function setPeriod(group: SubsidyTripGroup, field: 'startTime' | 'endTime', value: string | number | boolean | undefined): void {
  if (value !== 'morning' && value !== 'afternoon') return
  const boundary = field === 'startTime' ? group.startDate : group.endDate
  const dateField = field === 'startTime' ? 'startDate' : 'endDate'
  for (const trip of group.trips) {
    if (trip[dateField] === boundary) trip[field] = TRIP_PERIOD_TIME[value]
  }
}

function setPolicyConfirmed(group: SubsidyTripGroup, value: boolean): void {
  for (const trip of group.trips) trip.policyConfirmed = value
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
      <strong>出差补助（可选）</strong>
    </template>
    <el-form
      label-position="top"
      :disabled="props.readonly"
    >
      <el-form-item label="是否申请出差补助">
        <el-switch
          :model-value="expense.includeSubsidy"
          :disabled="props.readonly || (!expense.includeSubsidy
            && (!props.approvals.length || !props.approvalTripType))"
          active-text="申请出差补助"
          inactive-text="不申请"
          @update:model-value="expense.setSubsidyIncluded"
        />
      </el-form-item>

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
            <span>{{ group.trips.length > 1 ? '合并时段' : '审批日期' }}：{{ group.startDate }} 至 {{ group.endDate }}</span>
          </div>

          <div class="trip-grid">
            <el-form-item label="出发时段">
              <el-radio-group
                :model-value="period(group.startTime)"
                :aria-label="`补助 ${index + 1} 出发时段`"
                @update:model-value="setPeriod(group, 'startTime', $event)"
              >
                <el-radio-button value="morning">
                  上午
                </el-radio-button>
                <el-radio-button value="afternoon">
                  下午
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="返回时段">
              <el-radio-group
                :model-value="period(group.endTime)"
                :aria-label="`补助 ${index + 1} 返回时段`"
                @update:model-value="setPeriod(group, 'endTime', $event)"
              >
                <el-radio-button value="morning">
                  上午
                </el-radio-button>
                <el-radio-button value="afternoon">
                  下午
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
          </div>

          <el-alert
            title="自动计算规则"
            description="出发日上午计 1 天、下午计 0.5 天；返回日上午计 0.5 天、下午计 1 天。同一天最多计 1 天。"
            type="info"
            :closable="false"
          />

          <section
            class="excluded-periods"
            :class="{ 'excluded-periods--mobile': props.mobile }"
            :aria-label="`补助 ${index + 1} 非补助时段`"
          >
            <div class="excluded-periods__heading">
              <strong>非补助时段</strong>
              <el-button
                :aria-label="`补助 ${index + 1} 添加非补助时段`"
                :disabled="group.excludedPeriods.length >= 100"
                @click="addExcludedPeriod(group)"
              >
                添加非补助时段
              </el-button>
            </div>
            <p class="excluded-periods__hint">
              出发日：上午出发给半天补助，下午出发给全天；返回日：上午出发给全天，下午出发给半天。中间完整日期不补助，各段不能占用同一个上午或下午。
            </p>
            <div
              v-for="(excluded, excludedIndex) in group.excludedPeriods"
              :key="excludedIndex"
              class="excluded-period"
            >
              <div class="excluded-periods__heading">
                <span>时段 {{ excludedIndex + 1 }}</span>
                <el-button
                  type="danger"
                  link
                  :aria-label="`补助 ${index + 1} 删除非补助时段 ${excludedIndex + 1}`"
                  @click="removeExcludedPeriod(group, excluded)"
                >
                  删除
                </el-button>
              </div>
              <div class="trip-grid">
                <el-form-item label="出发日期">
                  <el-date-picker
                    :model-value="excluded.startDate"
                    type="date"
                    value-format="YYYY-MM-DD"
                    format="YYYY-MM-DD"
                    :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} 出发日期`"
                    @update:model-value="setExcludedDate(excluded, 'startDate', $event)"
                  />
                </el-form-item>
                <el-form-item label="出发时段">
                  <el-radio-group
                    :model-value="period(excluded.startTime)"
                    :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} 出发时段`"
                    @update:model-value="setExcludedPeriod(excluded, 'startTime', $event)"
                  >
                    <el-radio-button value="morning">
                      上午
                    </el-radio-button>
                    <el-radio-button value="afternoon">
                      下午
                    </el-radio-button>
                  </el-radio-group>
                </el-form-item>
                <el-form-item label="返回日期">
                  <el-date-picker
                    :model-value="excluded.endDate"
                    type="date"
                    value-format="YYYY-MM-DD"
                    format="YYYY-MM-DD"
                    :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} 返回日期`"
                    @update:model-value="setExcludedDate(excluded, 'endDate', $event)"
                  />
                </el-form-item>
                <el-form-item label="返回时段">
                  <el-radio-group
                    :model-value="period(excluded.endTime)"
                    :aria-label="`补助 ${index + 1} 非补助时段 ${excludedIndex + 1} 返回时段`"
                    @update:model-value="setExcludedPeriod(excluded, 'endTime', $event)"
                  >
                    <el-radio-button value="morning">
                      上午
                    </el-radio-button>
                    <el-radio-button value="afternoon">
                      下午
                    </el-radio-button>
                  </el-radio-group>
                </el-form-item>
              </div>
            </div>
          </section>

          <div
            v-if="props.approvalTripType === 'same_city_project'"
            class="policy-confirmation"
          >
            <el-checkbox
              :model-value="group.trips.every((item) => item.policyConfirmed)"
              @update:model-value="setPolicyConfirmed(group, $event)"
            >
              我已按公司现行制度确认本项补助
            </el-checkbox>
          </div>

          <div
            v-if="subsidyFor(group, index) && (props.readonly || !expense.policyInputError)"
            class="subsidy-preview"
            role="status"
            aria-live="polite"
          >
            <span>自然日 {{ subsidyFor(group, index)?.calendarDays }} 天</span>
            <span v-if="subsidyFor(group, index)?.excludedDays !== undefined">
              原补助 {{ (Number(subsidyFor(group, index)?.effectiveDays) + Number(subsidyFor(group, index)?.excludedDays)).toFixed(1) }} 天
            </span>
            <span v-if="subsidyFor(group, index)?.excludedDays !== undefined">扣除 {{ subsidyFor(group, index)?.excludedDays }} 天</span>
            <span>有效 {{ subsidyFor(group, index)?.effectiveDays }} 天</span>
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
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.trip-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 24px;
}

.excluded-periods { margin-top: 16px; }
.excluded-periods__heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.excluded-periods__hint { color: var(--el-text-color-secondary); font-size: 13px; line-height: 1.6; }
.excluded-period { margin-top: 12px; padding: 12px; border: 1px solid var(--el-border-color-light); border-radius: 8px; }
.excluded-period .trip-grid { margin-top: 12px; }
.excluded-period :deep(.el-date-editor.el-input) { width: 100%; min-width: 0; }
.excluded-periods--mobile .trip-grid { grid-template-columns: minmax(0, 1fr); }
.excluded-periods--mobile .excluded-periods__heading { flex-wrap: wrap; }

.policy-confirmation {
  margin-top: 14px;
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

@media (max-width: 720px) {
  .trip-grid {
    grid-template-columns: 1fr;
  }

  .subsidy-item__heading {
    display: block;
  }

  .subsidy-item__heading > span {
    display: block;
    margin-top: 6px;
  }

  .subsidy-preview strong {
    width: 100%;
    margin-left: 0;
  }
}
</style>

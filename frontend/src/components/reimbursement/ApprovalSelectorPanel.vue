<script setup lang="ts" generic="Row extends { processInstanceId: string; title: string; businessId: string; linkedOnly: boolean }">
import { computed, ref, watch } from 'vue'
import {
  APPROVAL_QUERY_HINT, GENERAL_APPROVAL_QUERY_HINT, approvalQueryEndDisabled, approvalQueryStartDisabled,
} from '@/utils/approvalQueryDates'

const props = defineProps<{
  kind: 'travel' | 'general'
  title: string
  mobile?: boolean
  readonly?: boolean
  busy?: boolean
  loading: boolean
  queryNeedsAdjustment: boolean
  emptyDescription: string
  rows: Row[]
  selectedIds: ReadonlySet<string>
  maxSelections?: number
  unavailableReason?: (row: Row) => string
  selectionHelp?: string
}>()
const fromDate = defineModel<string>('fromDate', { required: true })
const toDate = defineModel<string>('toDate', { required: true })
const keyword = defineModel<string>('keyword', { required: true })
const expanded = defineModel<boolean>('expanded', { default: true })
const emit = defineEmits<{ search: []; toggle: [row: Row, checked: boolean] }>()
const pageSize = 10
const page = ref(1)
const pageCount = computed(() => Math.max(1, Math.ceil(props.rows.length / pageSize)))
const visibleRows = computed(() => props.rows.slice((page.value - 1) * pageSize, page.value * pageSize))
const filtersOpen = ref(false)
const hasFilters = computed(() => Boolean(fromDate.value || toDate.value || keyword.value.trim()))
const startCalendarMonth = computed(() => toDate.value ? new Date(`${toDate.value}T00:00:00`) : undefined)
const endCalendarMonth = computed(() => fromDate.value ? new Date(`${fromDate.value}T00:00:00`) : undefined)
const general = computed(() => props.kind === 'general')
const dateLimit = computed(() => general.value ? 365 : 180)
const headingId = computed(() => `${props.kind}-approval-heading`)
const filtersId = computed(() => general.value ? 'general-approval-filters' : 'approval-query-filters')
const contentId = computed(() => `${props.kind}-approval-content`)
const maxReached = computed(() => props.maxSelections !== undefined && props.selectedIds.size >= props.maxSelections)
watch(() => props.queryNeedsAdjustment, (value) => { if (value) filtersOpen.value = true }, { immediate: true })
watch(pageCount, (count) => { page.value = Math.min(page.value, count) })

function search() {
  page.value = 1
  emit('search')
}
</script>

<template>
  <section
    class="travel-approval-section"
    :data-testid="`${kind}-approval-section`"
    :aria-labelledby="headingId"
    :aria-busy="busy || loading"
  >
    <div
      class="travel-approval-heading"
      :class="{ 'travel-approval-heading--mobile': mobile, 'travel-approval-heading--collapsed': !expanded }"
    >
      <div class="travel-approval-heading-row">
        <h2 :id="headingId">
          {{ title }}
        </h2>
        <div class="travel-approval-heading-actions">
          <el-tag
            v-if="expanded || selectedIds.size"
            class="travel-approval-count"
            :type="selectedIds.size ? 'success' : 'warning'"
            effect="light"
          >
            已选 {{ selectedIds.size }} 张
          </el-tag>
          <el-switch
            v-if="general"
            v-model="expanded"
            aria-label="展开关联审批单"
            :aria-controls="contentId"
          />
        </div>
      </div>
      <slot name="intro" />
    </div>

    <div
      v-show="expanded"
      :id="contentId"
    >
      <div
        v-if="mobile && !readonly"
        class="approval-filter-heading"
      >
        <span>近期已通过的审批</span>
        <el-button
          link
          type="primary"
          aria-label="筛选审批"
          :aria-expanded="filtersOpen"
          :aria-controls="filtersId"
          @click="filtersOpen = !filtersOpen"
        >
          {{ filtersOpen ? '收起筛选' : hasFilters ? '筛选审批 · 已设置' : '筛选审批' }}
        </el-button>
      </div>
      <fieldset
        v-show="!mobile || filtersOpen"
        :id="filtersId"
        class="plain-fieldset"
        :disabled="readonly"
      >
        <div
          class="travel-query-grid"
          :class="{ 'travel-query-grid--mobile': mobile }"
        >
          <el-date-picker
            v-model="fromDate"
            type="date"
            value-format="YYYY-MM-DD"
            :placeholder="mobile ? '开始日期' : '查询开始日期'"
            :editable="false"
            :disabled-date="(date: Date) => approvalQueryStartDisabled(date, toDate, undefined, dateLimit)"
            :default-value="startCalendarMonth"
            :aria-label="general ? '审批查询开始日期' : '出差审批查询开始日期'"
            popper-class="approval-query-picker"
            class="full-width"
          />
          <el-date-picker
            v-model="toDate"
            type="date"
            value-format="YYYY-MM-DD"
            :placeholder="mobile ? '结束日期' : '查询结束日期'"
            :editable="false"
            :disabled-date="(date: Date) => approvalQueryEndDisabled(date, fromDate, undefined, dateLimit)"
            :default-value="endCalendarMonth"
            :aria-label="general ? '审批查询结束日期' : '出差审批查询结束日期'"
            popper-class="approval-query-picker"
            class="full-width"
          />
          <el-input
            v-model="keyword"
            clearable
            maxlength="100"
            :placeholder="general ? '标题、审批编号或说明' : '标题或审批编号'"
            :aria-label="general ? '搜索本人审批' : '搜索出差审批'"
            @keyup.enter="search"
          />
          <el-button
            class="travel-query-button"
            :loading="loading"
            @click="search"
          >
            查询审批
          </el-button>
        </div>
        <p class="travel-query-help">
          {{ general ? GENERAL_APPROVAL_QUERY_HINT : APPROVAL_QUERY_HINT }}
        </p>
      </fieldset>

      <slot name="notifications" />

      <el-skeleton
        v-if="loading && rows.length === 0"
        :rows="3"
        animated
        class="travel-result-loading"
      />
      <el-empty
        v-else-if="rows.length === 0"
        :description="emptyDescription"
        :image-size="72"
      >
        <slot name="empty-actions" />
      </el-empty>
      <div
        v-else
        class="travel-approval-list"
        :aria-label="general ? '可关联的本人审批' : '可关联的出差审批'"
        aria-live="polite"
      >
        <article
          v-for="row in visibleRows"
          :key="row.processInstanceId"
          class="travel-approval-row"
          :class="{ 'is-selected': selectedIds.has(row.processInstanceId) }"
        >
          <el-checkbox
            :model-value="selectedIds.has(row.processInstanceId)"
            :disabled="readonly || busy || Boolean(unavailableReason?.(row)) || (maxReached && !selectedIds.has(row.processInstanceId))"
            :aria-label="`选择${general ? '' : '出差'}审批 ${row.title}`"
            @change="(checked: boolean) => emit('toggle', row, checked)"
          />
          <span class="travel-approval-main">
            <span class="travel-approval-title">
              <strong>{{ row.title }}</strong>
              <el-tag
                v-if="row.linkedOnly"
                size="small"
                type="info"
              >
                已关联
              </el-tag>
              <slot
                name="status"
                :row="row"
              />
            </span>
            <slot
              name="details"
              :row="row"
            />
            <small v-if="row.businessId">审批编号：{{ row.businessId }}</small>
            <slot
              name="accounting"
              :row="row"
            />
            <small
              v-if="unavailableReason?.(row)"
              role="status"
            >不可选择：{{ unavailableReason?.(row) }}</small>
          </span>
        </article>
      </div>

      <el-pagination
        v-if="pageCount > 1"
        v-model:current-page="page"
        :page-count="pageCount"
        :pager-count="5"
        :disabled="loading || busy"
        layout="prev, pager, next"
        size="small"
        class="approval-pagination"
        aria-label="审批结果分页"
      />

      <p class="field-help">
        {{ maxReached ? `已选满 ${maxSelections} 张。取消已选审批后可继续选择。`
          : selectionHelp || '选中的审批会自动保存。找不到时可筛选日期或标题。' }}
      </p>
    </div>
  </section>
</template>

<style scoped src="./approvalSelector.css"></style>

<script setup lang="ts">
withDefaults(defineProps<{
  mobile?: boolean
  company: string
  budget: string
  department: string
  periods: Array<{ startDate: string; endDate: string }>
  approvalCount: number
}>(), { mobile: false })
const emit = defineEmits<{ approvals: [] }>()
</script>

<template>
  <section
    class="review-overview"
    :class="{ 'review-overview--mobile': mobile }"
    aria-label="本次报销信息"
    data-testid="review-overview"
  >
    <header>
      <h3>本次报销</h3>
      <el-button
        link
        type="primary"
        @click="emit('approvals')"
      >
        查看关联审批
      </el-button>
    </header>
    <dl>
      <div><dt>所属公司</dt><dd>{{ company || '尚未关联审批' }}</dd></div>
      <div><dt>报销部门</dt><dd>{{ department }}</dd></div>
      <div>
        <dt>预算项目</dt><dd>{{ budget || '尚未关联审批' }}</dd>
      </div>
      <div class="review-overview__dates">
        <dt>
          出差日期
          <small v-if="approvalCount">已关联 {{ approvalCount }} 张审批</small>
        </dt>
        <dd>
          <span
            v-for="period in periods"
            :key="`${period.startDate}-${period.endDate}`"
            class="review-overview__period"
          >
            {{ period.startDate }}<template v-if="period.startDate !== period.endDate"> 至 {{ period.endDate }}</template>
          </span>
          <span v-if="!periods.length">尚未关联审批</span>
        </dd>
      </div>
    </dl>
  </section>
</template>

<style scoped>
.review-overview { margin-bottom: 16px; padding: 14px 16px; border: 1px solid var(--el-border-color-light); border-radius: 12px; background: #fff; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3 { margin: 0; font-size: 16px; }
dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 32px; margin: 12px 0 0; }
dl > div { min-width: 0; }
dt { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px; margin-bottom: 4px; color: var(--el-text-color-secondary); }
dd { margin: 0; min-width: 0; overflow-wrap: anywhere; line-height: 1.6; }
.review-overview__period { display: block; }
small { font-size: 12px; }
.review-overview--mobile dl { grid-template-columns: minmax(0, 1fr); gap: 10px; }
.review-overview--mobile dl > div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px; }
.review-overview--mobile dt { margin-bottom: 0; }
.review-overview--mobile dl > .review-overview__dates { grid-template-columns: minmax(0, 1fr); gap: 4px; }
@media (max-width: 600px) { .review-overview:not(.review-overview--mobile) dl { grid-template-columns: minmax(0, 1fr); } }
</style>

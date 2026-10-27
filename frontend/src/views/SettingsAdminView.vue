<script setup lang="ts">
import ApprovalDiagnosticsCard from '@/components/settings/ApprovalDiagnosticsCard.vue'
import RuntimeEventsCard from '@/components/settings/RuntimeEventsCard.vue'
import OaTemplateSettingsCard from '@/components/settings/OaTemplateSettingsCard.vue'
import ReceiptKeywordSettingsCard from '@/components/settings/ReceiptKeywordSettingsCard.vue'
import SubmissionRecoveryCard from '@/components/settings/SubmissionRecoveryCard.vue'
import SubsidyRateSettingsCard from '@/components/settings/SubsidyRateSettingsCard.vue'

const props = withDefaults(defineProps<{
  mobile?: boolean
}>(), {
  mobile: false,
})
</script>

<template>
  <main
    class="page-shell narrow-page"
    :class="{ 'page-shell--mobile': props.mobile }"
  >
    <div class="admin-page-header">
      <div>
        <nav
          class="admin-links admin-page-nav"
          aria-label="管理员功能"
        >
          <RouterLink :to="props.mobile ? '/m' : '/'">
            报销单
          </RouterLink>
          <RouterLink
            :to="props.mobile ? '/m/settings' : '/admin/settings'"
            aria-current="page"
          >
            系统设置
          </RouterLink>
          <a href="#approval-diagnostics">审批诊断</a>
          <a href="#runtime-events">运行记录</a>
        </nav>
        <h1>系统设置</h1>
      </div>
    </div>

    <ReceiptKeywordSettingsCard />
    <SubsidyRateSettingsCard />
    <SubmissionRecoveryCard />
    <ApprovalDiagnosticsCard :mobile="props.mobile" />
    <RuntimeEventsCard :mobile="props.mobile" />
    <OaTemplateSettingsCard />
  </main>
</template>

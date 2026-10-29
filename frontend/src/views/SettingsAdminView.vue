<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import AttendanceCalendarSettingsCard from '@/components/settings/AttendanceCalendarSettingsCard.vue'
import ApprovalDiagnosticsCard from '@/components/settings/ApprovalDiagnosticsCard.vue'
import ReceiptDiagnosticsCard from '@/components/settings/ReceiptDiagnosticsCard.vue'
import OaTemplateSettingsCard from '@/components/settings/OaTemplateSettingsCard.vue'
import ReceiptKeywordSettingsCard from '@/components/settings/ReceiptKeywordSettingsCard.vue'
import SubmissionRecoveryCard from '@/components/settings/SubmissionRecoveryCard.vue'
import SubsidyRateSettingsCard from '@/components/settings/SubsidyRateSettingsCard.vue'

const auth = useAuthStore()

const props = withDefaults(defineProps<{
  mobile?: boolean
}>(), {
  mobile: false,
})
</script>

<template>
  <main
    v-if="auth.isAdmin"
    class="page-shell narrow-page settings-page"
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
          <a href="#receipt-diagnostics">票据诊断</a>
        </nav>
        <h1>系统设置</h1>
      </div>
    </div>

    <ReceiptKeywordSettingsCard />
    <SubsidyRateSettingsCard />
    <AttendanceCalendarSettingsCard :mobile="props.mobile" />
    <SubmissionRecoveryCard :mobile="props.mobile" />
    <ApprovalDiagnosticsCard :mobile="props.mobile" />
    <ReceiptDiagnosticsCard :mobile="props.mobile" />
    <OaTemplateSettingsCard />
  </main>
</template>

<style scoped>
.settings-page {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.settings-page > .admin-page-header {
  margin-bottom: 4px;
}

.settings-page > :deep(.el-card) {
  margin: 0;
  border-radius: 8px;
}
</style>

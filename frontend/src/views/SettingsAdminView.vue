<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import AttendanceCalendarSettingsCard from '@/components/settings/AttendanceCalendarSettingsCard.vue'
import ApprovalDiagnosticsCard from '@/components/settings/ApprovalDiagnosticsCard.vue'
import ReceiptDiagnosticsCard from '@/components/settings/ReceiptDiagnosticsCard.vue'
import OaTemplateSettingsCard from '@/components/settings/OaTemplateSettingsCard.vue'
import GeneralOaTemplateSettingsCard from '@/components/settings/GeneralOaTemplateSettingsCard.vue'
import ReceiptKeywordSettingsCard from '@/components/settings/ReceiptKeywordSettingsCard.vue'
import ReimbursementDelegationSettingsCard from '@/components/settings/ReimbursementDelegationSettingsCard.vue'
import SubmissionRecoveryCard from '@/components/settings/SubmissionRecoveryCard.vue'
import SubsidyRateSettingsCard from '@/components/settings/SubsidyRateSettingsCard.vue'

const auth = useAuthStore()

const props = withDefaults(defineProps<{
  mobile?: boolean
  initialTemplateTab?: 'travel' | 'general'
}>(), {
  mobile: false,
  initialTemplateTab: 'travel',
})
const templateTab = ref(props.initialTemplateTab)
watch(() => props.initialTemplateTab, (tab) => { templateTab.value = tab })
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
          <a href="#reimbursement-delegation">代报权限</a>
          <a href="#approval-diagnostics">审批诊断</a>
          <a href="#receipt-diagnostics">票据诊断</a>
          <a href="#attendance-calendar">节假日与调休</a>
          <a href="#submission-recovery">异常提交恢复</a>
          <a href="#oa-template-catalog">OA 模板目录</a>
        </nav>
        <h1>系统设置</h1>
      </div>
    </div>

    <ReceiptKeywordSettingsCard />
    <SubsidyRateSettingsCard />
    <ReimbursementDelegationSettingsCard />
    <AttendanceCalendarSettingsCard :mobile="props.mobile" />
    <SubmissionRecoveryCard :mobile="props.mobile" />
    <ApprovalDiagnosticsCard :mobile="props.mobile" />
    <ReceiptDiagnosticsCard :mobile="props.mobile" />
    <section
      id="oa-template-catalog"
      class="oa-template-catalog"
      aria-labelledby="oa-template-catalog-title"
    >
      <h2 id="oa-template-catalog-title">
        钉钉 OA 模板目录
      </h2>
      <el-tabs v-model="templateTab">
        <el-tab-pane
          label="差旅报销"
          name="travel"
        >
          <OaTemplateSettingsCard />
        </el-tab-pane>
        <el-tab-pane
          label="普通费用报销"
          name="general"
          lazy
        >
          <GeneralOaTemplateSettingsCard />
        </el-tab-pane>
      </el-tabs>
    </section>
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

.oa-template-catalog > h2 { margin: 0 0 8px; }
</style>

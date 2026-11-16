<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import {
  getAdminExpenseSettings,
  normalizeSubsidyRate,
  updateExpenseSettings,
  type AdminExpenseSettings,
} from '@/api/settings'
import DirectoryGrantField from '@/components/settings/DirectoryGrantField.vue'
import { useAuthStore } from '@/stores/auth'
import type { SubsidyRateType } from '@/types/expenses'

const auth = useAuthStore()
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const saveError = ref('')
const conflict = ref(false)
const unsavedAfterSave = ref(false)
const form = reactive({
  revision: '',
  appTitle: '智能差旅费报销申请',
  adminUserIds: [] as string[],
  adminDepartmentIds: [] as string[],
  environmentAdminUserIds: [] as string[],
  subsidyRates: {
    business: '100.00',
    short_term_project: '100.00',
    long_term_project: '150.00',
    same_city_project: '50.00',
    short_term_internal: '100.00',
    long_term_internal: '100.00',
    short_term_overseas: '0.00',
    long_term_overseas: '0.00',
  },
  calculationMode: 'half_day_12' as const,
})
const formDisabled = computed(() => loading.value || saving.value || !!loadError.value || !form.revision)
const rateFields: Array<{ id: SubsidyRateType; label: string }> = [
  { id: 'business', label: '境内商务出差' },
  { id: 'long_term_project', label: '境内市外项目出差（长期）' },
  { id: 'short_term_project', label: '境内市外项目出差（短期）' },
  { id: 'same_city_project', label: '境内市内项目出差（长/短期）' },
  { id: 'long_term_internal', label: '公司内部出差（长期）' },
  { id: 'short_term_internal', label: '公司内部出差（短期）' },
  { id: 'long_term_overseas', label: '境外商务出差（长期）' },
  { id: 'short_term_overseas', label: '境外商务出差（短期）' },
]

onMounted(load)

async function load(): Promise<void> {
  if (loading.value || saving.value) return
  loading.value = true
  loadError.value = ''
  try {
    const settings = await getAdminExpenseSettings()
    Object.assign(form, settings, { adminDepartmentIds: settings.adminDepartmentIds ?? [] })
    saveError.value = ''
    conflict.value = false
    unsavedAfterSave.value = false
  } catch (error) {
    loadError.value = apiErrorMessage(error, '系统设置加载失败，请重试')
  } finally {
    loading.value = false
  }
}

async function reloadSettings(): Promise<void> {
  if (loading.value || saving.value) return
  try {
    await ElMessageBox.confirm('重新加载会替换本页尚未保存的设置，是否继续？', '重新加载设置', {
      confirmButtonText: '重新加载',
      cancelButtonText: '取消',
      type: 'warning',
      closeOnClickModal: false,
      showClose: false,
    })
  } catch {
    return
  }
  await load()
}

function writableSignature(value: AdminExpenseSettings): string {
  return JSON.stringify({
    appTitle: value.appTitle,
    adminUserIds: [...value.adminUserIds].sort(),
    adminDepartmentIds: [...(value.adminDepartmentIds ?? [])].sort(),
    subsidyRates: rateFields.map(({ id }) => normalizeSubsidyRate(value.subsidyRates[id])
      ?? value.subsidyRates[id]),
    calculationMode: value.calculationMode,
  })
}

function acceptSaved(updated: AdminExpenseSettings, sent: AdminExpenseSettings): void {
  const newerInput = writableSignature(form) !== writableSignature(sent)
  if (newerInput) {
    form.revision = updated.revision
    form.environmentAdminUserIds = updated.environmentAdminUserIds
  } else {
    Object.assign(form, updated, { adminDepartmentIds: updated.adminDepartmentIds ?? [] })
  }
  unsavedAfterSave.value = newerInput
  saveError.value = ''
  conflict.value = false
  auth.appTitle = updated.appTitle
  document.title = updated.appTitle
  void auth.refreshMe().catch(() => { /* Admin access is rechecked on the next request. */ })
  if (!newerInput) ElMessage.success('系统设置已保存')
}

async function save(): Promise<void> {
  if (formDisabled.value || conflict.value) return
  form.appTitle = form.appTitle.trim()
  if (!form.appTitle || form.appTitle.length > 40) {
    ElMessage.error('页面标题需要填写，最多 40 个字符')
    return
  }
  const normalizedAdmins = form.adminUserIds.map((value) => value.trim())
  if (
    normalizedAdmins.some((value) => !value || value.includes(',') || /\s/.test(value))
    || new Set(normalizedAdmins).size !== normalizedAdmins.length
  ) {
    ElMessage.error('额外管理员 userId 不能重复、包含空格或逗号')
    return
  }
  form.adminUserIds = normalizedAdmins
  for (const field of rateFields) {
    const normalized = normalizeSubsidyRate(form.subsidyRates[field.id])
    if (normalized === null) {
      ElMessage.error(`${field.label}不能小于 0 或超过 10000 元，最多两位小数`)
      return
    }
    form.subsidyRates[field.id] = normalized
  }
  saving.value = true
  const sent = JSON.parse(JSON.stringify(form)) as AdminExpenseSettings
  try {
    acceptSaved(await updateExpenseSettings(sent), sent)
  } catch (error) {
    conflict.value = apiErrorCode(error) === 'SETTINGS_REVISION_CONFLICT'
    if (!conflict.value && apiErrorCode(error) === null) {
      try {
        const readback = await getAdminExpenseSettings()
        if (writableSignature(readback) === writableSignature(sent)) {
          acceptSaved(readback, sent)
          return
        }
      } catch {
        // Keep local input when the server cannot be checked.
      }
    }
    saveError.value = apiErrorMessage(error, '系统设置保存失败，请重试')
    ElMessage.error(saveError.value)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-card
    v-loading="loading"
    shadow="never"
    class="settings-subsidy-card"
  >
    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      show-icon
      :closable="false"
      class="admin-load-error"
    >
      <template #default>
        <el-button
          link
          type="primary"
          @click="load"
        >
          重新加载
        </el-button>
      </template>
    </el-alert>
    <el-alert
      v-if="saveError"
      :title="saveError"
      type="error"
      :closable="false"
      data-testid="settings-save-error"
      class="admin-load-error"
    >
      <el-button
        link
        type="primary"
        :disabled="loading || saving"
        @click="reloadSettings"
      >
        重新加载设置
      </el-button>
    </el-alert>
    <p
      v-if="unsavedAfterSave"
      role="status"
    >
      本页还有修改未保存，请再次保存。
    </p>
    <el-form
      label-position="top"
      :disabled="formDisabled"
    >
      <div class="settings-form-heading">
        <strong>应用显示</strong>
      </div>
      <el-form-item label="页面标题">
        <el-input
          v-model="form.appTitle"
          maxlength="40"
          show-word-limit
        />
      </el-form-item>
      <el-form-item label="配置文件管理员（始终保留）">
        <DirectoryGrantField
          v-model="form.environmentAdminUserIds"
          kind="employee"
          label="配置文件管理员"
          read-only
        />
      </el-form-item>
      <el-form-item label="额外管理员员工">
        <DirectoryGrantField
          v-model="form.adminUserIds"
          kind="employee"
          label="额外管理员员工"
          :limit="50"
          :disabled="formDisabled"
        />
      </el-form-item>
      <el-form-item label="额外管理员部门">
        <DirectoryGrantField
          v-model="form.adminDepartmentIds"
          kind="department"
          label="额外管理员部门"
          :limit="50"
          :disabled="formDisabled"
        />
        <p class="settings-admin-note">
          所选部门的直接成员拥有系统管理员权限。
        </p>
      </el-form-item>
      <div class="settings-form-heading">
        <strong>每日补助标准</strong>
        <span>单位：元/天</span>
      </div>
      <div class="settings-rate-list">
        <el-form-item
          v-for="field in rateFields"
          :key="field.id"
          :label="field.label"
        >
          <el-input
            v-model="form.subsidyRates[field.id]"
            inputmode="decimal"
            maxlength="8"
            placeholder="0.00 - 10000.00"
            class="full-width"
            :aria-label="`${field.label}每日补助标准`"
          >
            <template #prepend>
              ¥
            </template>
          </el-input>
        </el-form-item>
      </div>
      <section
        class="settings-rule-summary"
        aria-labelledby="subsidy-rule-heading"
      >
        <h2 id="subsidy-rule-heading">
          补助计算规则
        </h2>
        <div class="settings-rule-row">
          <span>计算方式</span>
          <strong>市内按出勤日期计整天；其他类别按 12:00 半天边界计算</strong>
        </div>
        <div class="settings-rule-row">
          <span>补助类别</span>
          <strong>采用已通过出差审批中的类别和长短期，按对应每日标准计算</strong>
        </div>
        <div class="settings-rule-row">
          <span>有效天数</span>
          <strong>市内按所选出勤日期计算；其他类别按补助时段扣除非补助时段</strong>
        </div>
        <p class="settings-rule-exception">
          每类均可独立配置，0 元表示不发补助。非补助时段按出发和返回时段扣除，各段不能重叠或重复。
        </p>
      </section>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="formDisabled || conflict"
        :title="loadError ? '请先重新加载系统设置' : ''"
        @click="save"
      >
        保存设置
      </el-button>
    </el-form>
  </el-card>
</template>

<style scoped>
.settings-admin-note { color: var(--el-text-color-secondary); margin: 2px 0 0; }
</style>

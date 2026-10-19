<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import {
  getAdminExpenseSettings,
  normalizeSubsidyRate,
  updateExpenseSettings,
} from '@/api/settings'
import { useAuthStore } from '@/stores/auth'
import type { SubsidyRateType } from '@/types/expenses'

const auth = useAuthStore()
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const form = reactive({
  appTitle: '智能差旅费报销申请',
  adminUserIds: [] as string[],
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
  loading.value = true
  loadError.value = ''
  try {
    Object.assign(form, await getAdminExpenseSettings())
  } catch (error) {
    loadError.value = apiErrorMessage(error, '系统设置加载失败，请重试')
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
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
  try {
    const updated = await updateExpenseSettings(form)
    Object.assign(form, updated)
    auth.appTitle = updated.appTitle
    document.title = updated.appTitle
    ElMessage.success('系统设置已保存')
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '系统设置保存失败，请重试'))
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
    <el-form label-position="top">
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
        <div class="admin-id-list">
          <el-tag
            v-for="userId in form.environmentAdminUserIds"
            :key="userId"
          >
            {{ userId }}
          </el-tag>
          <span v-if="!form.environmentAdminUserIds.length">
            未配置
          </span>
        </div>
      </el-form-item>
      <el-form-item label="额外管理员 userId">
        <div class="admin-id-editor">
          <div
            v-for="(_userId, index) in form.adminUserIds"
            :key="index"
            class="admin-id-row"
          >
            <el-input
              v-model="form.adminUserIds[index]"
              maxlength="128"
              placeholder="钉钉 userId"
            />
            <el-button
              type="danger"
              plain
              @click="form.adminUserIds.splice(index, 1)"
            >
              移除
            </el-button>
          </div>
          <el-button
            :disabled="form.adminUserIds.length >= 50"
            @click="form.adminUserIds.push('')"
          >
            添加管理员
          </el-button>
        </div>
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
          <strong>按 12:00 半天边界计算</strong>
        </div>
        <div class="settings-rule-row">
          <span>补助类别</span>
          <strong>采用已通过出差审批中的类别和长短期，按对应每日标准计算</strong>
        </div>
        <div class="settings-rule-row">
          <span>有效天数</span>
          <strong>所有类型均按日期和时段自动计算；同市项目另需员工按制度确认</strong>
        </div>
        <p class="settings-rule-exception">
          每类均可独立配置，0 元表示不发补助。非补助时段按出发和返回时段扣除，各段不能重叠或重复。
        </p>
      </section>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="Boolean(loadError) || saving"
        :title="loadError ? '请先重新加载系统设置' : ''"
        @click="save"
      >
        保存设置
      </el-button>
    </el-form>
  </el-card>
</template>

<style scoped>
.admin-id-editor, .admin-id-list { display: flex; width: 100%; flex-direction: column; gap: 10px; }
.admin-id-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
</style>

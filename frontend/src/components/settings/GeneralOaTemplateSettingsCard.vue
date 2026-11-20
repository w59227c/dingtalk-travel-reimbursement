<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { confirmGeneralOaTemplate, getGeneralOaTemplate, inspectGeneralOaTemplate } from '@/api/oaTemplates'
import type { GeneralOaTemplate, GeneralOaTemplateInspection } from '@/types/oa'

const processCode = ref('')
const confirmed = ref<GeneralOaTemplate | null>(null)
const inspected = ref<GeneralOaTemplateInspection | null>(null)
const loading = ref(false)
const inspecting = ref(false)
const saving = ref(false)
const confirming = ref(false)
const loadError = ref('')
const actionError = ref('')
const needsReload = ref(false)
const busy = computed(() => loading.value || inspecting.value || saving.value || confirming.value)
const status = computed(() => confirmed.value?.status ?? 'UNCONFIGURED')
const statuses = {
  UNCONFIGURED: { label: '未配置', type: 'info', detail: '填写模板并完成检查后，即可确认保存。' },
  COMPATIBLE: { label: '兼容', type: 'success', detail: '已保存的模板可以用于创建普通费用 OA 审批。' },
  DRIFTED: { label: '结构已变化', type: 'error', detail: '请重新读取和检查普通费用模板，再确认保存。' },
} as const
const statusView = computed(() => statuses[status.value])
const saveDisabledReason = computed(() => {
  if (needsReload.value) return '请先重新加载已保存的配置'
  if (inspected.value?.processCode !== processCode.value.trim()) return '请先读取并检查当前钉钉模板'
  return ''
})

onMounted(() => { void load() })

async function load(preserveOnError = false): Promise<void> {
  if (busy.value) return
  loading.value = true
  loadError.value = ''
  actionError.value = ''
  try {
    confirmed.value = await getGeneralOaTemplate()
    processCode.value = confirmed.value?.processCode ?? ''
    inspected.value = null
    needsReload.value = false
  } catch (error) {
    const message = apiErrorMessage(error, '普通费用 OA 模板加载失败，请重试')
    if (preserveOnError) actionError.value = message
    else loadError.value = message
  } finally {
    loading.value = false
  }
}

async function reload(): Promise<void> {
  if (busy.value) return
  if (processCode.value.trim() !== (confirmed.value?.processCode ?? '') || inspected.value) {
    confirming.value = true
    try {
      await ElMessageBox.confirm('重新加载会替换本页尚未保存的模板配置，是否继续？', '重新加载目录', {
        type: 'warning', confirmButtonText: '重新加载', cancelButtonText: '取消',
        showClose: false, closeOnClickModal: false,
      })
    } catch { return }
    finally { confirming.value = false }
  }
  await load(true)
}

async function inspect(): Promise<void> {
  if (busy.value || needsReload.value) return
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(processCode.value.trim())) {
    actionError.value = '请填写有效的普通费用模板 processCode'
    return
  }
  inspecting.value = true
  actionError.value = ''
  inspected.value = null
  try {
    inspected.value = await inspectGeneralOaTemplate(processCode.value.trim())
  } catch (error) {
    actionError.value = apiErrorMessage(error, '钉钉模板读取失败，请检查 processCode 和应用权限')
  } finally {
    inspecting.value = false
  }
}

async function save(): Promise<void> {
  if (busy.value || saveDisabledReason.value || !inspected.value) return
  const inspectedTemplate = inspected.value
  confirming.value = true
  try {
    await ElMessageBox.confirm(
      '保存后，此模板将用于员工发起普通费用报销 OA 审批。',
      '确认保存普通费用 OA 模板',
      { type: 'warning', confirmButtonText: '确认保存', cancelButtonText: '取消', showClose: false, closeOnClickModal: false },
    )
  } catch { return }
  finally { confirming.value = false }
  if (saveDisabledReason.value || inspected.value !== inspectedTemplate) return
  saving.value = true
  actionError.value = ''
  try {
    confirmed.value = await confirmGeneralOaTemplate({
      processCode: inspectedTemplate.processCode,
      schemaFingerprint: inspectedTemplate.schemaFingerprint,
      expectedConfigVersion: confirmed.value?.configVersion ?? 0,
    })
    ElMessage.success('普通费用 OA 模板已保存')
  } catch (error) {
    actionError.value = apiErrorMessage(error, '普通费用 OA 模板保存失败，请重试')
    if (apiErrorCode(error) === 'OA_GENERAL_TEMPLATE_CHANGED') needsReload.value = true
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <el-card
    id="general-oa-template"
    v-loading="loading || inspecting || saving"
    shadow="never"
    class="oa-template-card"
  >
    <template #header>
      <div class="oa-template-heading">
        <div>
          <h2>普通费用报销模板</h2>
          <p>预算选项在报销时自动同步。读取模板后，系统按正式字段自动核对。</p>
        </div>
        <div class="oa-template-status">
          <el-tag :type="statusView.type === 'error' ? 'danger' : statusView.type">
            {{ statusView.label }}
          </el-tag>
          <span v-if="confirmed">配置版本 {{ confirmed.configVersion }}</span>
        </div>
      </div>
    </template>

    <el-alert
      v-if="loadError"
      :title="loadError"
      type="error"
      show-icon
      :closable="false"
      class="admin-load-error"
    >
      <el-button
        link
        type="primary"
        @click="load()"
      >
        重新加载
      </el-button>
    </el-alert>
    <template v-else>
      <el-alert
        :title="statusView.detail"
        :type="statusView.type"
        show-icon
        :closable="false"
        class="oa-template-alert"
      />
      <el-alert
        v-if="actionError"
        :title="actionError"
        type="error"
        show-icon
        :closable="false"
        class="oa-template-alert"
      >
        <el-button
          v-if="needsReload"
          link
          type="primary"
          :disabled="busy"
          @click="reload"
        >
          重新加载目录
        </el-button>
      </el-alert>
      <el-form
        label-position="top"
        :disabled="busy"
        @submit.prevent="inspect"
      >
        <section class="oa-template-section">
          <el-form-item label="普通费用 processCode">
            <el-input
              v-model="processCode"
              maxlength="128"
              placeholder="例如 PROC-EXPENSE"
              aria-label="普通费用 processCode"
              @input="inspected = null"
            />
          </el-form-item>
          <div
            v-if="inspected"
            class="oa-template-schema-summary"
          >
            <strong>{{ inspected.templateName }}</strong>
            <span>已核对字段：{{ inspected.fields.map(field => field.label).join('、') }}</span>
          </div>
          <div
            v-else-if="confirmed"
            class="oa-template-schema-summary"
          >
            <strong>{{ confirmed.templateName }}</strong>
          </div>
        </section>
        <div class="oa-template-actions">
          <span
            v-if="saveDisabledReason"
            class="field-help oa-template-save-reason"
            role="status"
          >{{ saveDisabledReason }}</span>
          <el-button
            :loading="inspecting"
            :disabled="busy || needsReload"
            @click="inspect"
          >
            读取并检查模板
          </el-button>
          <el-button
            type="primary"
            :loading="saving"
            :disabled="Boolean(saveDisabledReason) || busy"
            :title="saveDisabledReason"
            @click="save"
          >
            保存模板
          </el-button>
        </div>
      </el-form>
    </template>
  </el-card>
</template>

<style scoped src="./oaTemplateSettings.css"></style>

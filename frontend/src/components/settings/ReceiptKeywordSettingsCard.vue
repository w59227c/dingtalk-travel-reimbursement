<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import { getExpenseCategories } from '@/api/expenses'
import {
  createReceiptKeyword,
  deleteReceiptKeyword,
  listReceiptKeywords,
  updateReceiptKeyword,
} from '@/api/receiptKeywords'
import type { ReceiptKeywordMapping, ReceiptKeywordTransportType } from '@/api/receiptKeywords'
import type { ExpenseCategoryMetadata } from '@/types/expenses'

const keywordMappings = ref<ReceiptKeywordMapping[]>([])
const keywordCategories = ref<ExpenseCategoryMetadata[]>([])
const loading = ref(false)
const saving = ref(false)
const loadError = ref('')
const dialogOpen = ref(false)
const editingKeywordId = ref<number | null>(null)
const validationError = ref('')
const form = reactive({ keyword: '', categoryId: '', transportType: 'other' as ReceiptKeywordTransportType })
const transportGroups: Array<{ id: ReceiptKeywordTransportType; label: string }> = [
  { id: 'other', label: '通用' },
  { id: 'ride_hailing', label: '网约车' },
  { id: 'taxi', label: '出租车' },
]

const configurableCategories = computed(() =>
  keywordCategories.value.filter((category) => category.id !== 'other'),
)
const keywordGroups = computed(() =>
  keywordCategories.value.map((category) => ({
    category,
    mappings: keywordMappings.value.filter(
      (mapping) => mapping.categoryId === category.id,
    ),
  })),
)
function mappingGroups(categoryId: string, mappings: ReceiptKeywordMapping[]) {
  return categoryId === 'local_transport'
    ? transportGroups.map((group) => ({ ...group, mappings: mappings.filter((mapping) => (mapping.transportType ?? 'other') === group.id) }))
    : [{ id: 'other' as const, label: '', mappings }]
}
const dialogTitle = computed(() => {
  if (editingKeywordId.value !== null) return '编辑分类关键词'
  const category = keywordCategories.value.find(
    (item) => item.id === form.categoryId,
  )
  return category ? `为${category.name}添加关键词` : '新增分类关键词'
})

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const [mappings, categories] = await Promise.all([
      listReceiptKeywords(),
      getExpenseCategories(),
    ])
    keywordMappings.value = mappings
    keywordCategories.value = categories.filter((category) => category.manualSelectable)
  } catch (error) {
    keywordMappings.value = []
    keywordCategories.value = []
    loadError.value = apiErrorMessage(error, '分类关键词加载失败，请重试')
  } finally {
    loading.value = false
  }
}

function openCreate(categoryId: string, transportType: ReceiptKeywordTransportType = 'other'): void {
  editingKeywordId.value = null
  validationError.value = ''
  Object.assign(form, { keyword: '', categoryId, transportType })
  dialogOpen.value = true
}

function openEdit(mapping: ReceiptKeywordMapping): void {
  editingKeywordId.value = mapping.id
  validationError.value = ''
  Object.assign(form, {
    keyword: mapping.keyword,
    categoryId: mapping.categoryId,
    transportType: mapping.transportType ?? 'other',
  })
  dialogOpen.value = true
}

async function save(): Promise<void> {
  const keyword = form.keyword.trim().replace(/\s+/g, ' ')
  if (keyword.length < 2) {
    validationError.value = /^\d$/.test(keyword)
      ? '单个数字容易误匹配日期、金额和票据号码，请填写至少 2 个字符'
      : '关键词至少填写 2 个字符'
    return
  }
  validationError.value = ''
  if (!configurableCategories.value.some((category) => category.id === form.categoryId)) {
    ElMessage.warning('请选择费用类别')
    return
  }
  saving.value = true
  try {
    const input = { keyword, categoryId: form.categoryId,
      ...(form.categoryId === 'local_transport' ? { transportType: form.transportType } : {}),
    }
    if (editingKeywordId.value === null) await createReceiptKeyword(input)
    else await updateReceiptKeyword(editingKeywordId.value, input)
    dialogOpen.value = false
    ElMessage.success('分类关键词已保存')
    await load()
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, '分类关键词保存失败，请重试'))
  } finally {
    saving.value = false
  }
}

async function remove(mapping: ReceiptKeywordMapping): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除关键词“${mapping.keyword}”吗？`, '删除分类关键词', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      showClose: false,
      closeOnClickModal: false,
    })
    await deleteReceiptKeyword(mapping.id)
    ElMessage.success('分类关键词已删除')
    await load()
  } catch (error) {
    if (error === 'cancel' || error === 'close') return
    ElMessage.error(apiErrorMessage(error, '分类关键词删除失败，请重试'))
  }
}
</script>

<template>
  <el-card
    v-loading="loading"
    shadow="never"
  >
    <div class="settings-keyword-heading">
      <div>
        <h2>票据分类关键词</h2>
        <p>按费用类别维护关键词；点击关键词可修改，点击 × 可删除。</p>
        <p>不同类别同时命中时保留为“其他”；专用票据仍会结合版式结构识别。</p>
        <p>市内交通费分为通用、网约车、出租车；网约车关键词用于识别交通类型，并要求关联行程单。</p>
      </div>
    </div>
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
    <div
      v-else
      class="settings-keyword-groups"
    >
      <section
        v-for="group in keywordGroups"
        :key="group.category.id"
        class="settings-keyword-group"
        :aria-label="`${group.category.name}分类关键词`"
      >
        <strong>{{ group.category.name }}</strong>
        <div
          v-if="group.category.id === 'other'"
          class="settings-keyword-tags"
        >
          <span class="settings-keyword-empty">未命中或冲突时自动归入，无需配置关键词</span>
        </div>
        <div
          v-else
          class="settings-keyword-subgroups"
          :class="{ 'settings-keyword-subgroups--transport': group.category.id === 'local_transport' }"
        >
          <div
            v-for="subgroup in mappingGroups(group.category.id, group.mappings)"
            :key="subgroup.id"
            class="settings-keyword-subgroup"
            :aria-label="subgroup.label ? `${group.category.name}${subgroup.label}关键词` : undefined"
          >
            <span
              v-if="subgroup.label"
              class="settings-keyword-subgroup-label"
            >{{ subgroup.label }}</span>
            <div class="settings-keyword-tags">
              <el-tag
                v-for="mapping in subgroup.mappings"
                :key="mapping.id"
                closable
                disable-transitions
                class="settings-keyword-tag"
                title="点击修改关键词"
                @click="openEdit(mapping)"
                @close.stop="remove(mapping)"
              >
                {{ mapping.keyword }}
              </el-tag>
              <span
                v-if="subgroup.mappings.length === 0"
                class="settings-keyword-empty"
              >暂无关键词</span>
            </div>
            <el-button
              link
              type="primary"
              @click="openCreate(group.category.id, subgroup.id)"
            >
              添加
            </el-button>
          </div>
        </div>
      </section>
    </div>
  </el-card>

  <el-dialog
    v-model="dialogOpen"
    :title="dialogTitle"
    width="min(520px, calc(100vw - 32px))"
  >
    <el-form label-position="top">
      <el-form-item label="关键词">
        <el-input
          v-model="form.keyword"
          maxlength="100"
          show-word-limit
          placeholder="例如：文具、通信服务费"
          @input="validationError = ''"
        />
        <p
          v-if="validationError"
          class="field-error"
          role="alert"
        >
          {{ validationError }}
        </p>
        <p
          v-else
          class="field-help"
        >
          至少 2 个字符，避免单个数字误匹配日期、金额和票据号码。
        </p>
      </el-form-item>
      <el-form-item
        v-if="form.categoryId === 'local_transport'"
        label="交通类型"
      >
        <el-select
          v-model="form.transportType"
          class="full-width"
          aria-label="关键词交通类型"
        >
          <el-option
            v-for="group in transportGroups"
            :key="group.id"
            :label="group.label"
            :value="group.id"
          />
        </el-select>
        <p class="field-help">
          网约车需关联行程单；出租车不提供行程单关联，通用类型不要求。
        </p>
      </el-form-item>
      <el-form-item
        v-if="editingKeywordId !== null"
        label="移动到类别"
      >
        <el-select
          v-model="form.categoryId"
          class="full-width"
        >
          <el-option
            v-for="category in configurableCategories"
            :key="category.id"
            :label="category.name"
            :value="category.id"
          />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogOpen = false">
        取消
      </el-button>
      <el-button
        type="primary"
        :loading="saving"
        :disabled="saving"
        @click="save"
      >
        保存
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.settings-keyword-subgroups { min-width: 0; grid-column: 2 / -1; }
.settings-keyword-subgroup { display: flex; align-items: center; gap: 12px; }
.settings-keyword-subgroup + .settings-keyword-subgroup { margin-top: 12px; }
.settings-keyword-subgroup-label { flex: 0 0 3em; color: var(--el-text-color-secondary); }
.settings-keyword-subgroup .settings-keyword-tags { flex: 1; min-width: 0; }
.settings-keyword-subgroups--transport .settings-keyword-subgroup { padding: 16px 0; }
.settings-keyword-subgroups--transport .settings-keyword-subgroup:first-child { padding-top: 4px; }
.settings-keyword-subgroups--transport .settings-keyword-subgroup:last-child { padding-bottom: 4px; }
.settings-keyword-subgroups--transport .settings-keyword-subgroup + .settings-keyword-subgroup {
  margin-top: 0;
  border-top: 1px solid var(--el-border-color-lighter);
}
.settings-keyword-subgroups--transport .settings-keyword-subgroup-label {
  color: var(--el-text-color-primary);
  font-weight: 600;
}
@media (max-width: 700px) {
  .settings-keyword-subgroups { grid-column: 1 / -1; grid-row: 2; }
}
@media (max-width: 420px) {
  .settings-keyword-subgroup { flex-wrap: wrap; gap: 8px; }
  .settings-keyword-subgroup-label { flex-basis: 100%; }
}
</style>

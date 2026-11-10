<script setup lang="ts">
withDefaults(defineProps<{
  name: string
  previewAvailable: boolean
  previewLoading: boolean
  previewLabel?: string
  summary?: string
}>(), { summary: '', previewLabel: undefined })
defineEmits<{ preview: [] }>()
</script>

<template>
  <div class="expense-material-file">
    <button
      v-if="previewAvailable"
      type="button"
      class="material-preview"
      :disabled="previewLoading"
      :aria-label="previewLabel"
      @click="$emit('preview')"
    >
      {{ name }} · 预览
    </button>
    <span
      v-else
      class="expense-material-file__name"
    >{{ name }}</span>
    <p
      v-if="summary"
      class="linked-proof-summary"
    >
      {{ summary }}
    </p>
    <div
      v-if="$slots.actions"
      class="material-actions"
    >
      <slot name="actions" />
    </div>
  </div>
</template>

<style scoped>
.expense-material-file { display: grid; min-width: 0; gap: 6px; font-size: 12px; line-height: 1.6; }
.expense-material-file + .expense-material-file { padding-top: 8px; border-top: 1px solid #eaeef4; }
.material-preview { justify-self: start; max-width: 100%; padding: 0; border: 0; background: none; color: var(--el-color-primary); cursor: pointer; overflow-wrap: anywhere; text-align: left; font: inherit; font-weight: 600; }
.expense-material-file__name { color: #667085; overflow-wrap: anywhere; }
.linked-proof-summary { margin: 0; color: #98a2b3; overflow-wrap: anywhere; }
.material-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 8px; }
.material-actions :deep(.el-button) { margin-left: 0; }
</style>

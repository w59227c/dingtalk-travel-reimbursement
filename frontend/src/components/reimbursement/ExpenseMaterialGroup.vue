<script setup lang="ts">
defineProps<{ label: string, count?: number }>()
</script>

<template>
  <section
    class="expense-material-group"
    :aria-label="label"
  >
    <div class="expense-material-group__heading">
      <div class="expense-material-group__title">
        <strong>{{ label }}</strong>
        <small v-if="count !== undefined">{{ count }} 份</small>
      </div>
      <div
        v-if="$slots.actions"
        class="expense-material-group__actions"
      >
        <slot name="actions" />
      </div>
    </div>
    <slot />
  </section>
</template>

<style scoped>
.expense-material-group { display: grid; min-width: 0; gap: 8px; color: #667085; font-size: 12px; line-height: 1.6; }
.expense-material-group + .expense-material-group { padding-top: 12px; border-top: 1px solid #e4e7ed; }
.expense-material-group__heading { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 6px 10px; min-width: 0; }
.expense-material-group__title { display: flex; align-items: baseline; flex-wrap: wrap; gap: 8px; }
.expense-material-group__title strong { font-weight: 600; }
.expense-material-group__title small { color: #98a2b3; font-size: 12px; }
.expense-material-group__actions { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.expense-material-group__actions :deep(.el-button) { margin-left: 0; }
</style>

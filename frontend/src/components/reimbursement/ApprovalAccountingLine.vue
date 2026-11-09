<script setup lang="ts">
withDefaults(defineProps<{
  company?: string
  budget?: string
}>(), { company: '', budget: '' })
</script>

<template>
  <span
    v-if="company || budget"
    class="approval-accounting-line"
  >
    <span
      v-if="company"
      aria-label="所属公司"
    >{{ company }}</span><span
      v-if="company && budget"
      aria-hidden="true"
    > · </span><span
      v-if="budget"
      aria-label="预算代码与项目"
    >{{ budget }}</span>
  </span>
</template>

<style scoped>
.approval-accounting-line {
  display: block;
  min-width: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}
</style>

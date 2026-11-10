<script setup lang="ts">
import type { ExpenseReviewField, ExpenseReviewHint } from '@/utils/expenseReview'

defineProps<{ hints: readonly ExpenseReviewHint[], field: ExpenseReviewField }>()
</script>

<template>
  <ul
    v-if="hints.length"
    class="expense-review-hints"
    :data-review-field="field"
  >
    <li
      v-for="hint in hints"
      :key="hint.code"
    >
      {{ hint.message }}
    </li>
  </ul>
</template>

<style scoped>
.expense-review-hints { display: grid; gap: 3px; margin: 4px 0 0; padding: 0; list-style: none; color: #8b5a16; font-size: 12px; font-weight: 400; line-height: 1.5; overflow-wrap: anywhere; }
</style>

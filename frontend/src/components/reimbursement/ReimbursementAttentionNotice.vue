<script setup lang="ts">
import { WarningFilled } from '@element-plus/icons-vue'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{
  notice: { key: string; title: string; target: string; tone: 'error' | 'warning' } | null
  mobile: boolean
  footerHeight: number
  suspended: boolean
}>()
const emit = defineEmits<{ reveal: [] }>()
const host = ref<HTMLElement | null>(null)
const targetVisible = ref(false)
let observer: IntersectionObserver | undefined
let generation = 0

watch(() => [props.notice?.key, props.footerHeight, props.suspended] as const, async () => {
  const current = ++generation
  observer?.disconnect()
  targetVisible.value = false
  await nextTick()
  if (current !== generation || !props.notice || props.suspended) return
  const target = host.value?.closest('.reimbursement-page')?.querySelector(props.notice.target)
  if (!target || typeof IntersectionObserver === 'undefined') return
  observer = new IntersectionObserver((entries) => {
    if (current !== generation) return
    const entry = entries.find((item) => item.target === target)
    if (entry) targetVisible.value = entry.isIntersecting && entry.intersectionRatio >= 0.25
  }, { rootMargin: `0px 0px -${props.footerHeight + 16}px 0px`, threshold: [0, 0.25] })
  observer.observe(target)
}, { immediate: true, flush: 'post' })

onBeforeUnmount(() => { generation++; observer?.disconnect() })
</script>

<template>
  <div
    ref="host"
    class="attention-notice-host"
    :class="{ 'attention-notice-host--visible': notice && !suspended && !targetVisible }"
  >
    <aside
      v-if="notice && !suspended && !targetVisible"
      class="attention-notice"
      :class="{ 'attention-notice--mobile': mobile, 'attention-notice--warning': notice.tone === 'warning' }"
      :style="{ '--attention-footer-height': `${footerHeight}px` }"
      role="status"
      aria-live="polite"
      data-testid="reimbursement-attention"
    >
      <el-icon aria-hidden="true">
        <WarningFilled />
      </el-icon>
      <span>{{ notice.title }}</span>
      <el-button
        link
        type="primary"
        @click="emit('reveal')"
      >
        查看并处理
      </el-button>
    </aside>
  </div>
</template>

<style scoped>
.attention-notice-host { display: contents; }
.attention-notice-host--visible { display: block; height: 96px; }
.attention-notice {
  position: fixed;
  z-index: 40;
  right: 24px;
  bottom: 24px;
  display: flex;
  align-items: center;
  gap: 10px;
  width: min(480px, calc(100vw - 32px));
  padding: 12px 16px;
  border: 1px solid var(--el-color-danger-light-7);
  border-radius: 12px;
  background: var(--el-bg-color);
  box-shadow: 0 6px 24px rgb(23 32 51 / 14%);
  box-sizing: border-box;
}
.attention-notice > .el-icon { flex: none; color: var(--el-color-danger); font-size: 18px; }
.attention-notice > span { display: -webkit-box; min-width: 0; flex: 1; overflow: hidden; color: var(--el-text-color-primary); font-size: 13px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow-wrap: anywhere; }
.attention-notice .el-button { flex: none; margin: 0; min-height: 32px; }
.attention-notice--warning { border-color: var(--el-color-warning-light-7); }
.attention-notice--warning > .el-icon { color: var(--el-color-warning-dark-2); }
.attention-notice--mobile { right: auto; left: 50%; bottom: calc(var(--attention-footer-height, 0px) + 8px); transform: translateX(-50%); width: min(536px, calc(100vw - 16px)); padding: 6px 12px; }
.attention-notice--mobile .el-button { min-height: 44px; }
</style>

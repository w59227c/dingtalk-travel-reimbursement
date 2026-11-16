<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

import { apiErrorMessage } from '@/api/errors'
import {
  listAdminDepartments, lookupAdminDirectoryLabels, searchAdminEmployees,
  type DirectoryEntry, type DirectoryKind,
} from '@/api/settings'
import { useAuthStore } from '@/stores/auth'

const ids = defineModel<string[]>({ required: true })
const props = withDefaults(defineProps<{
  kind: DirectoryKind
  label: string
  limit?: number
  disabled?: boolean
  readOnly?: boolean
}>(), { limit: 100, disabled: false, readOnly: false })

const auth = useAuthStore()
const open = ref(false)
const chosen = ref<string[]>([])
const names = ref(new Map<string, string>())
const entries = ref<DirectoryEntry[]>([])
const ancestors = ref<DirectoryEntry[]>([])
const query = ref('')
const busy = ref(false)
const searched = ref(false)
const error = ref('')
let active: AbortController | undefined

const noun = computed(() => props.kind === 'department' ? '部门' : '员工')
const currentParent = computed(() => ancestors.value.at(-1)?.id ?? '1')

function remember(items: DirectoryEntry[]): void {
  for (const item of items) names.value.set(item.id, item.name)
}

watch(() => [auth.session?.operator?.userId ?? auth.session?.user.userId, auth.isAdmin] as const,
  () => {
    active?.abort()
    open.value = false
    names.value.clear()
  })

watch(() => ({ ids: [...ids.value], user: auth.session?.operator?.userId ?? auth.session?.user.userId }),
  ({ ids: values }, _previous, onCleanup) => {
    const pending = values.filter(id => !names.value.has(id))
    if (!pending.length) return
    const request = new AbortController()
    onCleanup(() => request.abort())
    void (async () => {
      for (let index = 0; index < pending.length && !request.signal.aborted; index += 20) {
        try {
          const items = await lookupAdminDirectoryLabels(props.kind, pending.slice(index, index + 20), request.signal)
          if (!request.signal.aborted) remember(items)
        } catch { break }
      }
    })()
  }, { immediate: true })

async function requestEntries(): Promise<void> {
  const lookup = query.value.trim()
  if (props.kind === 'employee' && !lookup) return
  active?.abort()
  const request = new AbortController()
  active = request
  busy.value = true
  error.value = ''
  searched.value = false
  entries.value = []
  try {
    const results = props.kind === 'department'
      ? await listAdminDepartments(currentParent.value, request.signal)
      : await searchAdminEmployees(lookup, request.signal)
    if (!request.signal.aborted && open.value) {
      const parentIds = new Set(['1', ...ancestors.value.map(value => value.id)])
      entries.value = props.kind === 'department'
        ? results.filter(entry => !parentIds.has(entry.id)) : results
      remember(entries.value)
      searched.value = true
    }
  } catch (cause) {
    if (!request.signal.aborted) error.value = apiErrorMessage(cause, `无法读取${noun.value}，请重试`)
  } finally {
    if (active === request) {
      active = undefined
      busy.value = false
    }
  }
}

function clearSearch(): void {
  active?.abort()
  entries.value = []
  searched.value = false
  error.value = ''
}

function start(): void {
  if (props.disabled || props.readOnly) return
  chosen.value = [...ids.value]
  ancestors.value = []
  query.value = ''
  clearSearch()
  open.value = true
  if (props.kind === 'department') void requestEntries()
}

function navigate(next: DirectoryEntry[]): void {
  ancestors.value = next
  void requestEntries()
}

function toggle(id: string): void {
  if (chosen.value.includes(id)) chosen.value = chosen.value.filter(item => item !== id)
  else if (chosen.value.length < props.limit) chosen.value = [...chosen.value, id]
}

function confirm(): void {
  if (props.disabled) return
  ids.value = chosen.value
  open.value = false
}

watch(open, value => { if (!value) active?.abort() })
watch(() => props.disabled, value => { if (value) open.value = false })
onBeforeUnmount(() => active?.abort())
</script>

<template>
  <div
    class="directory-grant-field"
    role="group"
    :aria-label="label"
  >
    <div class="directory-grant-list">
      <el-tag
        v-for="id in ids"
        :key="id"
        :closable="!disabled && !readOnly"
        :title="id"
        @close="ids = ids.filter(item => item !== id)"
      >
        {{ names.get(id) ?? id }}
      </el-tag>
      <span v-if="!ids.length">未配置</span>
    </div>
    <el-button
      v-if="!readOnly"
      :disabled="disabled"
      @click="start"
    >
      选择{{ noun }}
    </el-button>
    <el-dialog
      v-if="!readOnly"
      v-model="open"
      :title="`选择${noun}`"
      width="min(520px, calc(100vw - 32px))"
    >
      <div
        v-if="kind === 'employee'"
        class="directory-search"
      >
        <el-input
          v-model="query"
          aria-label="员工姓名或 userId"
          placeholder="输入员工姓名或 userId"
          maxlength="128"
          @input="clearSearch"
          @keyup.enter="requestEntries"
        />
        <el-button
          type="primary"
          :disabled="!query.trim()"
          :loading="busy"
          @click="requestEntries"
        >
          搜索
        </el-button>
      </div>
      <nav
        v-else
        class="directory-breadcrumbs"
        aria-label="部门路径"
      >
        <el-button
          link
          :disabled="!ancestors.length"
          @click="navigate([])"
        >
          组织架构
        </el-button>
        <el-button
          v-for="(item, index) in ancestors"
          :key="item.id"
          link
          :disabled="index === ancestors.length - 1"
          @click="navigate(ancestors.slice(0, index + 1))"
        >
          {{ item.name }}
        </el-button>
      </nav>
      <p
        v-if="error"
        class="directory-error"
        role="alert"
      >
        {{ error }}
        <el-button
          v-if="kind === 'department'"
          link
          type="primary"
          @click="requestEntries"
        >
          重试
        </el-button>
      </p>
      <p
        v-else-if="busy || (searched && !entries.length)"
        role="status"
      >
        {{ busy ? '正在读取通讯录…' : `没有可选的${noun}` }}
      </p>
      <ul
        v-else-if="entries.length"
        class="directory-choices"
      >
        <li
          v-for="entry in entries"
          :key="entry.id"
        >
          <label>
            <input
              type="checkbox"
              :checked="chosen.includes(entry.id)"
              :disabled="chosen.length >= limit && !chosen.includes(entry.id)"
              @change="toggle(entry.id)"
            >
            <span>{{ entry.name }}</span>
            <small v-if="kind === 'employee'">{{ entry.id }}</small>
          </label>
          <el-button
            v-if="kind === 'department'"
            link
            type="primary"
            :aria-label="`查看${entry.name}的下级部门`"
            @click="navigate([...ancestors, entry])"
          >
            下级部门
          </el-button>
        </li>
      </ul>
      <template #footer>
        <span class="directory-count">已选 {{ chosen.length }} 项</span>
        <el-button @click="open = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :disabled="disabled"
          @click="confirm"
        >
          确定
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.directory-grant-field { width: 100%; }
.directory-grant-list, .directory-breadcrumbs { display: flex; flex-wrap: wrap; gap: 6px; }
.directory-grant-list { margin-bottom: 6px; }
.directory-search { display: flex; gap: 8px; }
.directory-choices { list-style: none; margin: 12px 0 0; padding: 0; max-height: 45vh; overflow-y: auto; }
.directory-choices li { display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 6px 0; }
.directory-choices label { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; cursor: pointer; }
.directory-choices small, .directory-count { color: var(--el-text-color-secondary); }
.directory-count { margin-right: 10px; }
.directory-error { color: var(--el-color-danger); }
</style>

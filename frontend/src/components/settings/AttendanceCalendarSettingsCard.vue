<script setup lang="ts">
import { computed, inject, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { matchedRouteKey, onBeforeRouteLeave } from 'vue-router'
import { apiErrorCode, apiErrorMessage } from '@/api/errors'
import { applyAttendanceNotice, deleteAttendanceCalendar, listAttendanceCalendars, parseAttendanceNotice, attendanceSyncStatus, checkAttendanceYear } from '@/api/attendanceCalendars'
import { useAuthStore } from '@/stores/auth'
import type { AttendanceCalendarCorrection, AttendanceCalendarDay, AttendanceCalendarYear, AttendanceNoticePreview, AttendanceSyncStatus } from '@/types/attendance'
import { calendarRanges } from '@/utils/calendarMaintenance'
import CalendarArrangementPreview from './CalendarArrangementPreview.vue'
import CalendarCorrectionEditor from './CalendarCorrectionEditor.vue'

const props = withDefaults(defineProps<{ mobile?: boolean }>(), { mobile: false })
const auth = useAuthStore()
const exampleYear = new Date().getFullYear()
const noticeSearchExample = `${exampleYear}年部分节假日安排的通知`
const timorUrl = `https://timor.tech/api/holiday/year/${exampleYear}/`
const holidayCnUrl = `https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/${exampleYear}.json`
const years = ref<AttendanceCalendarYear[]>([])
const sync = ref<AttendanceSyncStatus | null>(null), fetchYear = ref(exampleYear)
const syncJobs = computed(() => sync.value?.items.filter((job) => job.year >= exampleYear || job.status !== 'completed') ?? [])
const standalone = ref(false), manualBaseline = ref('')
const busy = ref(false), loaded = ref(false), error = ref(''), message = ref(''), open = ref(false)
const mode = ref<'paste' | 'correct' | 'review' | 'view'>('paste'), text = ref('')
const preview = ref<AttendanceNoticePreview | null>(null), viewed = ref<AttendanceCalendarYear | null>(null)
const correction = ref<AttendanceCalendarCorrection | null>(null)
const selectedYear = ref(0)
const previewMonth = ref(1), viewMonth = ref(1), expandedSources = ref<number[]>([])
const calendarPanel = ref<HTMLElement | null>(null)
const parseFeedback = ref<HTMLElement | null>(null), correctionFeedback = ref<HTMLElement | null>(null), requestFeedback = ref<HTMLElement | null>(null)
const sourceId = useId()
watch(preview, () => { expandedSources.value = [] }, { flush: 'sync' })
let controller: AbortController | null = null, generation = 0
const unsaved = computed(() => open.value && mode.value !== 'view' && (!!text.value.trim() || standalone.value && JSON.stringify(correction.value) !== manualBaseline.value))
const selectedChange = computed(() => preview.value?.changes.find((item) => item.year === selectedYear.value))
const title = computed(() => mode.value === 'view' ? `${viewed.value?.year} 年安排` : mode.value === 'review' ? '核对年度安排' : mode.value === 'correct' ? standalone.value ? '手动填写年度安排' : '修正年度安排' : '录入年度安排')
const correctionComplete = computed(() => !!correction.value?.year && !!correction.value.ranges.length && correction.value.ranges.every((item) => item.name.trim() && item.startDate && item.endDate))
watch(correction, () => {
  if (mode.value === 'review' || mode.value === 'correct') { preview.value = null; mode.value = 'correct'; error.value = '' }
}, { deep: true, flush: 'sync' })
const totalDays = computed(() => preview.value?.sections.reduce((value, item) => ({ rest: value.rest + item.restDates.length, work: value.work + item.workDates.length }), { rest: 0, work: 0 }))
function counts(days: AttendanceCalendarDay[]): string { return `放假 ${days.filter((day) => !day.working).length} 天 · 补班 ${days.filter((day) => day.working).length} 天` }
function dateRanges(dates: string[]): { start: string; end: string; label: string }[] {
  const label = (date: string) => Number(date.slice(0, 4)) === preview.value?.year ? date.slice(5) : date
  return calendarRanges(dates.map((date) => ({ date, working: false, name: '' }))).map((range) => {
    const text = range.start.slice(0, 4) !== range.end.slice(0, 4) ? `${range.start} 至 ${range.end}` : range.start === range.end ? label(range.start) : `${label(range.start)} 至 ${label(range.end)}`
    return { start: range.start, end: range.end, label: text }
  })
}
function firstMonth(days: AttendanceCalendarDay[]): number { return Number(days[0]?.date.slice(5, 7) ?? 1) }
function selectPreviewYear(year: number): void {
  const change = preview.value?.changes.find((item) => item.year === year)
  if (!change) return
  selectedYear.value = year; previewMonth.value = firstMonth(change.days)
}
async function locateDate(date: string): Promise<void> {
  const year = Number(date.slice(0, 4))
  if (!preview.value?.changes.some((item) => item.year === year)) return
  selectedYear.value = year; previewMonth.value = Number(date.slice(5, 7))
  await nextTick()
  calendarPanel.value?.scrollIntoView?.({ block: 'nearest' })
}
function toggleSource(index: number): void {
  expandedSources.value = expandedSources.value.includes(index) ? expandedSources.value.filter((value) => value !== index) : [...expandedSources.value, index]
}
async function revealFeedback(): Promise<void> {
  await nextTick()
  const target = error.value ? requestFeedback.value : mode.value === 'correct' ? correctionFeedback.value : parseFeedback.value
  target?.focus({ preventScroll: true })
  target?.scrollIntoView?.({ block: 'start' })
}
function invalidate(): void {
  generation++; controller?.abort(); controller = null; busy.value = false; open.value = false
  text.value = ''; preview.value = null; correction.value = null; viewed.value = null; error.value = ''; message.value = ''
}
watch(() => [auth.session?.user.userId, auth.isAdmin], () => { invalidate(); years.value = []; loaded.value = false }, { flush: 'sync' })
watch(text, () => { preview.value = null; correction.value = null; error.value = ''; if (mode.value === 'review' || mode.value === 'correct') mode.value = 'paste' }, { flush: 'sync' })
function beforeUnload(event: BeforeUnloadEvent): void { if (unsaved.value) event.preventDefault() }
const routeRecord = inject(matchedRouteKey, undefined)
if (routeRecord?.value) onBeforeRouteLeave(async () => !busy.value && await discard())
onBeforeUnmount(() => { window.removeEventListener('beforeunload', beforeUnload); invalidate() })
async function run(action: (signal: AbortSignal) => Promise<void>): Promise<void> {
  if (busy.value) return
  const active = new AbortController(), version = generation
  controller = active; busy.value = true; error.value = ''; message.value = ''
  try { await action(active.signal) }
  catch (failure) {
    if (version === generation && !active.signal.aborted) {
      error.value = apiErrorMessage(failure, '日历操作失败，请重试')
      if (apiErrorCode(failure) === 'ATTENDANCE_CALENDAR_CONFLICT') { preview.value = null; mode.value = correction.value ? 'correct' : 'paste' }
    }
  }
  finally {
    if (version === generation) {
      busy.value = false; controller = null
      // Loading changes can replace dialog slot nodes; focus the settled DOM.
      if (open.value && (error.value || preview.value && !preview.value.canSave)) await revealFeedback()
    }
  }
}
async function discard(): Promise<boolean> {
  if (!unsaved.value) return true
  try { await ElMessageBox.confirm('年度安排尚未保存，是否放弃本次录入？', '未保存的年度安排', { confirmButtonText: '放弃录入', cancelButtonText: '继续核对', type: 'warning' }); return true }
  catch { return false }
}
function paste(): void { if (!busy.value) { standalone.value = false; text.value = ''; preview.value = null; correction.value = null; viewed.value = null; mode.value = 'paste'; error.value = ''; open.value = true } }
function manual(): void {
  if (busy.value) return
  paste(); standalone.value = true; mode.value = 'correct'
  correction.value = { year: exampleYear, ranges: ['元旦', '春节', '清明节', '劳动节', '端午节', '中秋节', '国庆节'].map((name) => ({ name, startDate: '', endDate: '', working: false })) }
  manualBaseline.value = JSON.stringify(correction.value)
}
const statusLabels = { scheduled: '等待年度获取开始', pending: '等待获取', running: '正在获取', retry: '等待重试', completed: '已维护，本轮已完成', paused: '已删除，自动获取已暂停' }
function timeLabel(value: string | null): string { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—' }
async function fetchOnline(): Promise<void> {
  if (!fetchYear.value) return
  await run(async (signal) => {
    const state = await checkAttendanceYear(fetchYear.value, signal)
    if (signal.aborted) return
    sync.value = state
    const items = await listAttendanceCalendars(signal)
    if (signal.aborted) return
    years.value = items; loaded.value = true
    const job = state.items.find((item) => item.year === fetchYear.value)
    if (job?.status === 'completed') message.value = `${fetchYear.value} 年安排已维护，本轮自动获取已完成`
    else if (job?.message) error.value = job.message
  })
}
async function close(done?: () => void): Promise<void> {
  if (busy.value || !await discard()) return
  open.value = false; text.value = ''; preview.value = null; viewed.value = null; error.value = ''; done?.()
}
async function load(): Promise<void> {
  await run(async (signal) => {
    const [items, status] = await Promise.all([listAttendanceCalendars(signal), attendanceSyncStatus(signal)])
    if (!signal.aborted) {
      if (!loaded.value && items.some((item) => item.year === exampleYear && item.complete)) fetchYear.value = Math.min(9999, exampleYear + 1)
      years.value = items; sync.value = status; loaded.value = true
    }
  })
}
onMounted(() => { window.addEventListener('beforeunload', beforeUnload); void load() })
async function view(item: AttendanceCalendarYear): Promise<void> {
  await run(async (signal) => {
    const items = await listAttendanceCalendars(signal)
    if (signal.aborted) return
    years.value = items
    const current = items.find((value) => value.year === item.year)
    if (!current) throw new Error('该年日历已被删除，请核对最新列表')
    viewed.value = current; viewMonth.value = firstMonth(current.days); mode.value = 'view'; open.value = true
  })
}
async function parse(): Promise<void> {
  if (!text.value.trim()) return
  correction.value = null
  const original = text.value
  await run(async (signal) => {
    const result = await parseAttendanceNotice(original, signal)
    if (signal.aborted || text.value !== original) return
    preview.value = result
    if (result.canSave) { selectPreviewYear(result.year!); mode.value = 'review' }
  })
}
function beginCorrection(): void {
  if (busy.value) return
  const original = preview.value
  mode.value = 'correct'; error.value = ''; preview.value = null
  if (correction.value) return
  const ranges = original?.sections.flatMap((section) => [false, true].flatMap((working) => {
    const values = calendarRanges((working ? section.workDates : section.restDates).map((date) => ({ date, working, name: section.name })))
    if (!working && !values.length) return [{ name: section.name, startDate: '', endDate: '', working }]
    return values.map((range) => ({ name: range.name, startDate: range.start, endDate: range.end, working }))
  })) ?? []
  for (const name of ['元旦', '春节', '清明节', '劳动节', '端午节', '中秋节', '国庆节']) {
    if (!ranges.some((item) => !item.working && item.name.includes(name))) ranges.push({ name, startDate: '', endDate: '', working: false })
  }
  correction.value = { year: original?.year ?? 0, ranges }
}
async function previewCorrection(): Promise<void> {
  if (!correction.value || !correctionComplete.value) return
  const patch = { year: correction.value.year, ranges: correction.value.ranges.map((item) => ({ ...item })) }
  const original = text.value
  await run(async (signal) => {
    const result = await parseAttendanceNotice(original, signal, patch)
    if (signal.aborted || text.value !== original) return
    preview.value = result
    if (result.canSave) { selectPreviewYear(result.year!); mode.value = 'review' }
  })
}
function editText(): void { mode.value = 'paste'; preview.value = null; error.value = '' }
async function save(): Promise<void> {
  const plan = preview.value
  if (!plan?.canSave || mode.value !== 'review') return
  await run(async (signal) => {
    const result = correction.value ? await applyAttendanceNotice(text.value, plan, signal, correction.value) : await applyAttendanceNotice(text.value, plan, signal)
    if (signal.aborted) return
    years.value = result.items; loaded.value = true; open.value = false; text.value = ''; preview.value = null
    message.value = result.unchanged ? `${result.year} 年安排与已保存内容一致，无需更新` : `${result.year} 年安排已保存，相关年度安排已同步更新`
    sync.value = await attendanceSyncStatus(signal)
  })
}
async function remove(item: AttendanceCalendarYear): Promise<void> {
  await run(async (signal) => {
    const latest = await listAttendanceCalendars(signal)
    if (signal.aborted) return
    years.value = latest
    if (!latest.some((value) => value.year === item.year && value.revision === item.revision)) throw new Error('该年日历已变更，请核对最新列表后再删除')
    try { await ElMessageBox.confirm(`删除 ${item.year} 年年度安排及其跨年日期，并暂停该年自动获取。涉及缺失年份的市内补助将暂不能确认；已有填写内容保留。可通过立即获取或手动录入恢复。`, '删除年度安排', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }) }
    catch { return }
    if (signal.aborted) return
    const items = await deleteAttendanceCalendar(item.year, item.revision, signal)
    if (!signal.aborted) { years.value = items; open.value = false; viewed.value = null; message.value = `${item.year} 年年度安排已删除` }
    sync.value = await attendanceSyncStatus(signal)
  })
}
</script>

<template>
  <el-card
    id="attendance-calendar"
    shadow="never"
    class="content-card calendar-settings-card"
    data-testid="attendance-calendar-settings"
  >
    <template #header>
      <div class="calendar-card-heading">
        <strong>节假日与调休</strong><div class="calendar-entry-actions">
          <el-button
            :disabled="busy"
            @click="manual"
          >
            手动填写
          </el-button><el-button
            type="primary"
            :disabled="busy"
            @click="paste"
          >
            粘贴内容录入
          </el-button>
        </div>
      </div>
    </template>
    <p class="calendar-intro">
      系统自动获取年度安排；也可粘贴内容或手动填写，维护市内出差的默认出勤日历。
    </p>
    <section
      class="calendar-sync"
      :class="{ 'is-mobile': props.mobile }"
      aria-label="年度日历自动获取"
    >
      <div class="calendar-sync-heading">
        <strong>自动获取</strong><el-tag
          size="small"
          :type="sync?.enabled ? 'success' : 'info'"
        >
          {{ sync?.enabled ? '已开启' : sync ? '后台已暂停' : '正在读取' }}
        </el-tag>
      </div>
      <p>每年 11 月 1 日起，每 5 天从 holiday-cn 获取下一年安排，校验通过后自动保存，成功后结束本轮。首次启动会补查当年。</p>
      <ul
        v-if="sync"
        class="calendar-sync-jobs"
      >
        <li
          v-for="job in syncJobs"
          :key="job.year"
        >
          <div><strong>{{ job.year }} 年</strong><span>{{ statusLabels[job.status] }}</span></div>
          <small v-if="job.lastAttemptAt">上次检查：{{ timeLabel(job.lastAttemptAt) }}</small>
          <small v-if="job.nextAttemptAt">下次检查：{{ timeLabel(job.nextAttemptAt) }}</small>
          <small
            v-if="job.message"
            class="calendar-error"
          >{{ job.message }}</small>
        </li>
      </ul>
      <div class="calendar-fetch-action">
        <label>获取年份<el-input-number
          v-model="fetchYear"
          :min="1"
          :max="9999"
          :precision="0"
          :disabled="busy"
          aria-label="联网获取年份"
        /></label><el-button
          :disabled="busy || !fetchYear"
          :loading="busy && !open"
          @click="fetchOnline"
        >
          立即获取
        </el-button><el-button
          link
          :disabled="busy"
          @click="load"
        >
          刷新状态
        </el-button>
      </div>
      <small>立即获取用于补齐缺失年份；已维护的年份会保留。修改已有安排请粘贴新内容或手动填写。</small>
    </section>
    <p
      v-if="!years.length && loaded"
      class="calendar-muted"
    >
      尚未维护年度安排，相关年份的市内补助暂不能确认报销。
    </p>
    <ul
      v-if="years.length"
      class="calendar-years"
    >
      <li
        v-for="item in years"
        :key="item.year"
      >
        <div class="calendar-year-info">
          <strong>{{ item.year }} 年 <el-tag
            v-if="!item.complete"
            type="info"
            size="small"
          >仅跨年补充</el-tag></strong><small>{{ counts(item.days) }}<template v-if="item.legacy"> · 旧版安排</template></small>
        </div>
        <div class="calendar-year-actions">
          <el-button
            link
            type="primary"
            :disabled="busy"
            :aria-label="`查看 ${item.year} 年日历`"
            @click="view(item)"
          >
            查看
          </el-button><el-button
            v-if="item.complete"
            link
            type="danger"
            :disabled="busy"
            :aria-label="`删除 ${item.year} 年安排`"
            @click="remove(item)"
          >
            删除
          </el-button>
        </div>
      </li>
    </ul>
    <p
      v-if="busy && !open"
      role="status"
      class="calendar-muted"
    >
      正在读取日历…
    </p>
    <p
      v-if="!open && error"
      role="alert"
      class="calendar-error"
    >
      {{ error }} <el-button
        link
        type="primary"
        :disabled="busy"
        @click="load"
      >
        重新加载
      </el-button>
    </p>
    <p
      v-if="message"
      role="status"
      class="calendar-success"
    >
      {{ message }}
    </p>
    <p class="calendar-footnote">
      仅影响新带入的默认日期；实际加班可补选，已保存的出勤选择保持不变。
    </p>
    <el-dialog
      v-model="open"
      :title="title"
      width="960px"
      :fullscreen="props.mobile"
      class="calendar-notice-dialog"
      :before-close="close"
      :close-on-click-modal="false"
      :close-on-press-escape="!busy"
      :show-close="!busy"
    >
      <template v-if="mode !== 'view'">
        <ol
          class="notice-steps"
          aria-label="录入步骤"
        >
          <li :class="{ active: mode === 'paste' || mode === 'correct' }">
            {{ standalone ? '1 手动填写日期' : '1 粘贴完整内容' }}
          </li><li :class="{ active: mode === 'review' }">
            2 核对并保存
          </li>
        </ol>
        <template v-if="mode === 'paste'">
          <p class="calendar-intro">
            三种来源任选一种，打开网页后全选并复制内容到下方，系统自动识别格式。
          </p>
          <details
            class="calendar-source-guide"
            open
          >
            <summary>去哪里复制？（3 种方式）</summary>
            <ol>
              <li>
                <a
                  href="https://www.gov.cn/"
                  target="_blank"
                  rel="noopener noreferrer"
                >中国政府网</a> 搜索“{{ noticeSearchExample }}”，打开国务院办公厅发布的年度通知，复制标题和完整正文。查询其他年份时，修改搜索词中的年份。
              </li>
              <li>
                <a
                  :href="timorUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                >提莫节假日 API · {{ exampleYear }} 年</a>：复制页面全部 JSON，网址末尾保留斜杠 /。
              </li>
              <li>
                <a
                  :href="holidayCnUrl"
                  target="_blank"
                  rel="noopener noreferrer"
                >holiday-cn 年度数据 · {{ exampleYear }} 年</a>：复制页面全部 JSON；如出现格式化开关，只复制从 { 到 } 的数据正文。
              </li>
            </ol>
            <p>提莫和 holiday-cn 查询其他年份时，替换各自网址中的 {{ exampleYear }}。不要只粘贴网址，也不要混合多个来源；临时调整通知暂不支持。</p>
          </details>
          <label
            class="notice-input-label"
            for="holiday-notice-text"
          >通知正文或年度 JSON</label>
          <el-input
            id="holiday-notice-text"
            v-model="text"
            type="textarea"
            :rows="10"
            :maxlength="20000"
            show-word-limit
            :disabled="busy"
            placeholder="粘贴完整年度通知标题和正文，或提莫／holiday-cn 的完整年度 JSON。&#10;不要只填网址。"
          />
          <p class="calendar-footnote">
            离线解析，不联网获取内容；保存日期安排，不保存粘贴的原文。
          </p>
          <p
            v-if="correction"
            class="calendar-footnote"
          >
            已保留手动修正；修改原文或重新解析将重置修正内容。
          </p>
          <div
            v-if="preview && !preview.canSave"
            ref="parseFeedback"
            role="alert"
            tabindex="-1"
            class="notice-issues"
          >
            <strong>尚不能保存，可修改原文重新解析或手动修正日期</strong><ul>
              <li
                v-for="(issue, index) in preview.errors"
                :key="index"
              >
                {{ issue.message }}<details v-if="issue.source">
                  <summary>对应原文</summary><p>{{ issue.source }}</p>
                </details>
              </li>
            </ul>
          </div>
          <el-button
            v-if="preview && !preview.canSave || correction"
            :disabled="busy"
            @click="beginCorrection"
          >
            {{ correction ? '继续手动修正' : '手动修正日期' }}
          </el-button>
        </template>
        <template v-else-if="mode === 'correct' && correction">
          <div class="notice-correction-heading">
            <p>{{ standalone ? '无需粘贴内容，填写完整年度放假及补班安排后生成预览。' : '修正结果需重新生成预览，确认后才会保存。' }}</p><el-button
              v-if="!standalone"
              :disabled="busy"
              @click="editText"
            >
              返回修改原文
            </el-button>
          </div>
          <details
            v-if="!standalone"
            class="calendar-source-guide"
          >
            <summary>查看完整来源内容</summary><p>{{ text }}</p>
          </details>
          <CalendarCorrectionEditor
            v-model="correction"
            :disabled="busy"
            :mobile="props.mobile"
            :standalone="standalone"
          />
          <div
            v-if="preview && !preview.canSave"
            ref="correctionFeedback"
            role="alert"
            tabindex="-1"
            class="notice-issues"
          >
            <strong>修正内容尚不能保存</strong><ul>
              <li
                v-for="(issue, index) in preview.errors"
                :key="index"
              >
                {{ issue.message }}
              </li>
            </ul>
          </div>
        </template>
        <template v-else-if="mode === 'review' && preview">
          <div class="notice-review-heading">
            <div>
              <h3>{{ preview.year }} 年年度安排</h3><p class="notice-source-label">
                {{ preview.sourceFormat === 'manual' ? standalone ? '手动填写，请核对全年安排' : '已手动修正，请对照来源核对' : `已识别：${preview.sourceLabel}` }}
              </p><p>
                {{ preview.sections.length }} 项节日安排 · 放假 {{ totalDays?.rest }} 天 · 补班 {{ totalDays?.work }} 天<template v-if="preview.changes.length > 1">
                  （含跨年日期）
                </template>
              </p>
            </div><div class="notice-review-actions">
              <el-button
                :disabled="busy"
                @click="beginCorrection"
              >
                {{ standalone ? '返回填写' : correction ? '继续修正日期' : '修正识别结果' }}
              </el-button><el-button
                v-if="!standalone"
                :disabled="busy"
                @click="editText"
              >
                返回修改原文
              </el-button>
            </div>
          </div>
          <p
            v-if="preview.unchanged"
            class="notice-callout"
          >
            与已保存内容一致，无需更新。
          </p>
          <p
            v-else
            class="notice-callout"
          >
            请核对以下日期。确认后会更新本年度安排；跨年日期单独补充，其他年度通知的安排会保留。
          </p>
          <div
            v-for="(issue, index) in preview.warnings"
            :key="index"
            class="notice-warning"
          >
            {{ issue.message }}<details v-if="issue.source">
              <summary>对应原文</summary><p>{{ issue.source }}</p>
            </details>
          </div>
          <div
            class="notice-review-grid"
            :class="{ 'is-mobile': props.mobile }"
          >
            <section
              class="notice-sections"
              aria-label="识别出的节日安排"
            >
              <p class="notice-locate-hint">
                点击日期可定位{{ props.mobile ? '下方' : '右侧' }}日历
              </p>
              <article
                v-for="(section, index) in preview.sections"
                :key="section.name"
              >
                <div class="notice-section-heading">
                  <strong>{{ section.name }}</strong>
                  <button
                    v-if="!standalone"
                    :id="`${sourceId}-toggle-${index}`"
                    type="button"
                    class="notice-source-toggle"
                    :aria-label="`${['notice', 'manual'].includes(preview.sourceFormat) ? '核对原文' : '查看数据'}：${section.name}`"
                    :aria-expanded="expandedSources.includes(index)"
                    :aria-controls="`${sourceId}-source-${index}`"
                    @click="toggleSource(index)"
                  >
                    <span
                      class="notice-source-chevron"
                      aria-hidden="true"
                    >▸</span>{{ ['notice', 'manual'].includes(preview.sourceFormat) ? '核对原文' : '查看数据' }}
                  </button>
                </div>
                <p class="notice-date-row">
                  <span class="rest-text">放假</span>
                  <span class="notice-date-links">
                    <button
                      v-for="range in dateRanges(section.restDates)"
                      :key="range.start"
                      type="button"
                      :aria-label="`在日历中查看${section.name}放假：${range.label}`"
                      @click="locateDate(range.start)"
                    >{{ range.label }}</button>
                  </span>
                </p>
                <p
                  v-if="section.workDates.length"
                  class="notice-date-row"
                >
                  <span class="work-text">补班</span>
                  <span class="notice-date-links">
                    <button
                      v-for="range in dateRanges(section.workDates)"
                      :key="range.start"
                      type="button"
                      :aria-label="`在日历中查看${section.name}补班：${range.label}`"
                      @click="locateDate(range.start)"
                    >{{ range.label }}</button>
                  </span>
                </p>
                <div
                  v-show="expandedSources.includes(index)"
                  :id="`${sourceId}-source-${index}`"
                  class="notice-source-content"
                  role="region"
                  :aria-labelledby="`${sourceId}-toggle-${index}`"
                >
                  <p>{{ section.source }}</p>
                </div>
              </article>
            </section>
            <section
              ref="calendarPanel"
              class="notice-calendar-panel"
              aria-label="保存后的日历预览"
            >
              <strong class="notice-preview-label">保存后的日历</strong>
              <div
                v-if="preview.changes.length > 1"
                class="notice-year-options"
                aria-label="预览年度"
              >
                <el-button
                  v-for="change in preview.changes"
                  :key="change.year"
                  size="small"
                  :type="selectedYear === change.year ? 'primary' : 'default'"
                  :aria-pressed="selectedYear === change.year"
                  @click="selectPreviewYear(change.year)"
                >
                  {{ change.year }} 年{{ change.mode === 'adjacent' ? ' · 跨年日期' : ' · 年度安排' }}
                </el-button>
              </div>
              <template v-if="selectedChange">
                <p
                  v-if="selectedChange.mode === 'adjacent'"
                  class="calendar-footnote"
                >
                  只补充或移除本通知涉及的日期{{ !selectedChange.complete ? '，该年仍未完整维护' : '' }}。
                </p><CalendarArrangementPreview
                  v-model:month="previewMonth"
                  :year="selectedChange.year"
                  :days="selectedChange.days"
                  :show-details="false"
                  :mobile="props.mobile"
                />
              </template>
            </section>
          </div>
          <section
            class="notice-differences"
            aria-label="与已保存安排的差异"
          >
            <h4>本次更新</h4><details
              v-for="change in preview.changes"
              :key="change.year"
            >
              <summary>
                {{ change.year }} 年{{ change.mode === 'adjacent' ? ' · 跨年日期' : '' }}：新增 {{ change.added.length }} 天，修改 {{ change.modified.length }} 天，移除 {{ change.removed.length }} 天<template v-if="!change.changed">
                  · 无变化
                </template>
              </summary><ul>
                <li
                  v-for="day in change.added"
                  :key="`add-${day.date}`"
                >
                  新增 {{ day.date }} · {{ day.working ? '补班' : '放假' }} · {{ day.name }}
                </li><li
                  v-for="day in change.modified"
                  :key="`modify-${day.after.date}`"
                >
                  修改 {{ day.after.date }} · {{ day.before.working ? '补班' : '放假' }} {{ day.before.name }} → {{ day.after.working ? '补班' : '放假' }} {{ day.after.name }}
                </li><li
                  v-for="day in change.removed"
                  :key="`remove-${day.date}`"
                >
                  移除 {{ day.date }} · {{ day.working ? '补班' : '放假' }} · {{ day.name }}
                </li>
              </ul>
            </details>
          </section>
        </template>
      </template>
      <template v-else-if="viewed">
        <p class="calendar-intro">
          {{ viewed.noticeTitle || (viewed.legacy ? '旧版已保存安排，可粘贴完整年度通知重新核对。' : `来自 ${viewed.sourceYears.join('、')} 年通知的跨年日期，该年尚未完整维护。`) }}
        </p>
        <p class="calendar-muted">
          {{ counts(viewed.days) }} · 更新于 {{ viewed.updatedAt.slice(0, 10) }}
        </p>
        <CalendarArrangementPreview
          v-model:month="viewMonth"
          :year="viewed.year"
          :days="viewed.days"
          :mobile="props.mobile"
        />
      </template>
      <p
        v-if="error"
        ref="requestFeedback"
        role="alert"
        tabindex="-1"
        class="calendar-error"
      >
        {{ error }}
      </p>
      <template #footer>
        <div class="notice-footer">
          <span>{{ mode === 'view' ? '日历仅供查看，可重新录入以更新安排' : mode === 'paste' ? '解析不会修改已保存的安排' : mode === 'correct' ? '请完整核对全年放假与补班安排' : preview?.unchanged ? '内容一致，已有日历无需更新' : '确认保存后生效，已有出勤选择保持不变' }}</span><div>
            <el-button
              :disabled="busy"
              @click="close()"
            >
              {{ mode === 'view' ? '关闭' : '取消' }}
            </el-button><el-button
              v-if="mode === 'paste'"
              type="primary"
              :loading="busy"
              :disabled="busy || !text.trim()"
              @click="parse"
            >
              解析内容
            </el-button><el-button
              v-else-if="mode === 'correct'"
              type="primary"
              :loading="busy"
              :disabled="busy || !correctionComplete"
              @click="previewCorrection"
            >
              {{ standalone ? '生成预览' : '生成修正预览' }}
            </el-button><el-button
              v-else-if="mode === 'review'"
              type="primary"
              :loading="busy"
              :disabled="busy || !preview?.canSave"
              @click="save"
            >
              {{ preview?.unchanged ? '完成核对' : '确认保存' }}
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </el-card>
</template>

<style scoped>
.calendar-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.calendar-card-heading { flex-wrap: wrap; }
.calendar-entry-actions { display: flex; gap: 8px; flex-wrap: wrap; }.calendar-entry-actions :deep(.el-button + .el-button) { margin-left: 0; }
.calendar-sync { padding: 16px; margin-bottom: 20px; background: var(--el-fill-color-light); border-radius: 6px; min-width: 0; }
.calendar-sync-heading { display: flex; align-items: center; gap: 10px; }
.calendar-sync p, .calendar-sync small { font-size: 12px; color: var(--el-text-color-secondary); line-height: 1.7; }
.calendar-sync-jobs { list-style: none; padding: 0; display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 12px; }
.calendar-sync.is-mobile .calendar-sync-jobs { grid-template-columns: minmax(0,1fr); }
.calendar-sync-jobs li { padding: 10px 12px; background: var(--el-bg-color); border-radius: 4px; }
.calendar-sync-jobs li > div { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 13px; }.calendar-sync-jobs small { display: block; }
.calendar-fetch-action { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 14px 0 8px; }.calendar-fetch-action label { display: flex; align-items: center; gap: 8px; font-size: 13px; }.calendar-fetch-action :deep(.el-button + .el-button) { margin-left: 0; }
.calendar-intro { margin: 0 0 16px; color: var(--el-text-color-regular); line-height: 1.7; }
.calendar-intro a, .calendar-source-guide a { color: var(--el-color-primary); text-decoration: underline; text-underline-offset: 2px; }
.calendar-source-guide { margin-bottom: 18px; padding: 10px 14px; background: var(--el-fill-color-light); border-radius: 4px; color: var(--el-text-color-regular); line-height: 1.7; }
.calendar-source-guide summary { font-weight: 600; }.calendar-source-guide ol { padding-left: 20px; margin: 8px 0; }.calendar-source-guide li { margin: 8px 0; }.calendar-source-guide p { margin: 8px 0 0; color: var(--el-text-color-secondary); }
.notice-review-heading .notice-source-label { margin-bottom: 6px; color: var(--el-color-primary); }
.calendar-muted, .calendar-footnote { color: var(--el-text-color-secondary); line-height: 1.6; }
.calendar-footnote { margin: 14px 0 0; font-size: 12px; }
.calendar-years { list-style: none; margin: 0; padding: 0; }
.calendar-years li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 0; border-top: 1px solid var(--el-border-color-lighter); }
.calendar-year-info { min-width: 0; }.calendar-year-info strong { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }.calendar-year-info small { display: block; color: var(--el-text-color-secondary); margin-top: 6px; }
.calendar-year-actions { display: flex; flex-shrink: 0; gap: 14px; }.calendar-year-actions :deep(.el-button + .el-button) { margin-left: 0; }
.calendar-error { color: var(--el-color-danger); line-height: 1.6; }.calendar-success { color: var(--el-color-success); }
.notice-steps { display: flex; list-style: none; padding: 0; margin: 0 0 24px; gap: 20px; color: var(--el-text-color-placeholder); font-size: 13px; }.notice-steps .active { color: var(--el-color-primary); font-weight: 600; }
.notice-input-label { display: block; margin-bottom: 8px; font-weight: 600; }
.notice-review-heading { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }.notice-review-heading h3 { margin: 0 0 8px; font-size: 20px; }.notice-review-heading p { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.notice-review-actions { display: flex; gap: 8px; flex-wrap: wrap; }.notice-review-actions :deep(.el-button + .el-button) { margin-left: 0; }
.notice-correction-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }.notice-correction-heading p { margin: 0; color: var(--el-text-color-secondary); }
.notice-callout, .notice-warning, .notice-issues { padding: 12px 16px; border-radius: 4px; line-height: 1.7; margin: 12px 0 20px; }.notice-callout { background: var(--el-fill-color-light); }.notice-warning { background: var(--el-color-warning-light-9); color: var(--el-color-warning-dark-2); }.notice-issues { background: var(--el-color-danger-light-9); color: var(--el-color-danger); }
.notice-issues ul { padding-left: 18px; }.notice-issues li { margin: 8px 0; }
.notice-review-grid { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1fr); gap: 28px; }.notice-review-grid.is-mobile { grid-template-columns: minmax(0,1fr); gap: 20px; }
.notice-sections article { padding: 10px 0; border-bottom: 1px solid var(--el-border-color-lighter); }.notice-sections article:first-child { padding-top: 0; }.notice-sections p { margin: 4px 0; line-height: 1.6; font-size: 13px; }.rest-text { color: #b44942; }.work-text { color: #2469bc; }
.notice-sections .notice-locate-hint { margin: 0 0 12px; font-size: 12px; color: var(--el-text-color-secondary); }
.notice-section-heading { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }.notice-section-heading strong { min-width: 0; overflow-wrap: anywhere; }
.notice-source-toggle { flex-shrink: 0; display: inline-flex; align-items: center; gap: 4px; padding: 4px 0; border: 0; background: transparent; color: var(--el-text-color-secondary); font: inherit; font-size: 12px; cursor: pointer; }
.notice-source-chevron { display: inline-block; width: 10px; text-align: center; }.notice-source-toggle[aria-expanded="true"] .notice-source-chevron { transform: rotate(90deg); }
.notice-source-content { margin-top: 8px; padding: 8px 12px; background: var(--el-fill-color-light); border-radius: 4px; color: var(--el-text-color-secondary); white-space: pre-wrap; overflow-wrap: anywhere; }
.notice-date-row { display: flex; gap: 6px; align-items: baseline; }.notice-date-row > span:first-child { flex-shrink: 0; }
.notice-date-links { display: flex; gap: 4px 10px; flex-wrap: wrap; min-width: 0; }.notice-date-links button { padding: 2px 0; border: 0; background: transparent; color: var(--el-text-color-regular); font: inherit; text-align: left; cursor: pointer; text-decoration: underline; text-decoration-color: var(--el-border-color); text-underline-offset: 3px; }
.notice-date-links button:hover, .notice-source-toggle:hover { color: var(--el-color-primary); }.notice-date-links button:focus-visible, .notice-source-toggle:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: 2px; border-radius: 2px; }
.notice-calendar-panel { min-width: 0; align-self: start; position: sticky; top: 0; }.notice-review-grid.is-mobile .notice-calendar-panel { position: static; }.notice-preview-label { display: block; margin-bottom: 16px; }.notice-year-options { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }.notice-year-options :deep(.el-button + .el-button) { margin-left: 0; }
.notice-calendar-panel .calendar-footnote { margin: 0 0 12px; }
.notice-differences { padding-top: 10px; margin-top: 20px; border-top: 1px solid var(--el-border-color-lighter); }.notice-differences h4 { margin: 8px 0 12px; }.notice-differences details { margin: 10px 0; line-height: 1.7; }.notice-differences ul { padding-left: 18px; font-size: 12px; }
details { font-size: 12px; color: var(--el-text-color-secondary); }summary { cursor: pointer; }details p { white-space: pre-wrap; overflow-wrap: anywhere; }
.notice-footer { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }.notice-footer > span { font-size: 12px; color: var(--el-text-color-secondary); text-align: left; }.notice-footer > div { display: flex; margin-left: auto; }
</style>
<style>
.calendar-notice-dialog { max-width: calc(100vw - 32px); max-height: calc(100dvh - 64px); margin: 32px auto; border-radius: 8px; display: flex; flex-direction: column; }
.calendar-notice-dialog .el-dialog__body { flex: 1; overflow-y: auto; min-height: 0; }.calendar-notice-dialog .el-dialog__header, .calendar-notice-dialog .el-dialog__footer { flex-shrink: 0; }
.calendar-notice-dialog .el-dialog__footer { border-top: 1px solid var(--el-border-color-lighter); padding-top: 14px; }
.calendar-notice-dialog.is-fullscreen { margin: 0; max-height: none; max-width: none; display: flex; flex-direction: column; border-radius: 0; }
.calendar-notice-dialog.is-fullscreen .el-dialog__body { flex: 1; overflow-y: auto; min-height: 0; }
.calendar-notice-dialog.is-fullscreen .el-dialog__footer { flex-shrink: 0; border-top: 1px solid var(--el-border-color-lighter); padding-bottom: max(16px,env(safe-area-inset-bottom)); }
</style>

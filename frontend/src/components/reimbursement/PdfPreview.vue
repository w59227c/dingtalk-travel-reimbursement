<script setup lang="ts">
import { ArrowLeft, ArrowRight, Refresh } from '@element-plus/icons-vue'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import pdfWorkerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'

const props = defineProps<{
  source: Blob
}>()

const surface = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const pageNumber = ref(1)
const pageCount = ref(0)
const loading = ref(false)
const rendering = ref(false)
const errorMessage = ref('')

type LoadingTask = { promise: Promise<PdfDocument>; destroy: () => Promise<void> }
type RenderTask = { promise: Promise<void>; cancel: () => void }
type PdfPage = {
  getViewport: (input: { scale: number }) => { width: number; height: number }
  render: (input: { canvas: HTMLCanvasElement; canvasContext: CanvasRenderingContext2D; viewport: unknown }) => RenderTask
}
type PdfDocument = {
  numPages: number
  getPage: (number: number) => Promise<PdfPage>
  destroy: () => Promise<void>
}

let loadingTask: LoadingTask | null = null
let pdfDocument: PdfDocument | null = null
let renderTask: RenderTask | null = null
let resizeObserver: ResizeObserver | null = null
let loadGeneration = 0
let renderGeneration = 0

function isCancelledRender(error: unknown): boolean {
  return error instanceof Error && error.name === 'RenderingCancelledException'
}

async function releaseDocument(): Promise<void> {
  renderGeneration += 1
  renderTask?.cancel()
  renderTask = null
  const activeLoadingTask = loadingTask
  const activeDocument = pdfDocument
  loadingTask = null
  pdfDocument = null
  if (activeLoadingTask) await activeLoadingTask.destroy().catch(() => undefined)
  else if (activeDocument) await activeDocument.destroy().catch(() => undefined)
}

async function renderPage(): Promise<void> {
  const document = pdfDocument
  const currentCanvas = canvas.value
  const currentSurface = surface.value
  if (!document || !currentCanvas || !currentSurface) return

  const generation = ++renderGeneration
  renderTask?.cancel()
  renderTask = null
  rendering.value = true
  errorMessage.value = ''
  try {
    const page = await document.getPage(pageNumber.value)
    const baseViewport = page.getViewport({ scale: 1 })
    const availableWidth = Math.max(currentSurface.clientWidth, 280)
    const cssScale = availableWidth / baseViewport.width
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
    const viewport = page.getViewport({ scale: cssScale * pixelRatio })
    const context = currentCanvas.getContext('2d')
    if (!context) throw new Error('当前浏览器无法创建 PDF 画布')

    currentCanvas.width = Math.ceil(viewport.width)
    currentCanvas.height = Math.ceil(viewport.height)
    currentCanvas.style.width = `${Math.ceil(viewport.width / pixelRatio)}px`
    currentCanvas.style.height = `${Math.ceil(viewport.height / pixelRatio)}px`
    const task = page.render({ canvas: currentCanvas, canvasContext: context, viewport })
    renderTask = task
    await task.promise
    if (generation === renderGeneration && renderTask === task) renderTask = null
  } catch (error) {
    if (generation === renderGeneration && !isCancelledRender(error)) {
      errorMessage.value = '这一页暂时无法显示，请重试'
    }
  } finally {
    if (generation === renderGeneration) rendering.value = false
  }
}

async function loadDocument(): Promise<void> {
  const generation = ++loadGeneration
  loading.value = true
  errorMessage.value = ''
  pageNumber.value = 1
  pageCount.value = 0
  await releaseDocument()
  try {
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs')
    if (generation !== loadGeneration) return
    pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl
    const data = new Uint8Array(await props.source.arrayBuffer())
    if (generation !== loadGeneration) return
    const task = pdfjs.getDocument({ data, isEvalSupported: false }) as unknown as LoadingTask
    loadingTask = task
    const document = await task.promise
    if (generation !== loadGeneration) return
    pdfDocument = document
    pageCount.value = document.numPages
    await nextTick()
    await renderPage()
  } catch {
    if (generation === loadGeneration) errorMessage.value = 'PDF 加载失败，请重试'
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

async function changePage(nextPage: number): Promise<void> {
  if (nextPage < 1 || nextPage > pageCount.value || nextPage === pageNumber.value) return
  pageNumber.value = nextPage
  await nextTick()
  await renderPage()
}

watch(() => props.source, () => {
  void loadDocument()
}, { immediate: true })

onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !surface.value) return
  let previousWidth = surface.value.clientWidth
  resizeObserver = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width ?? 0
    if (!width || Math.abs(width - previousWidth) < 1) return
    previousWidth = width
    void renderPage()
  })
  resizeObserver.observe(surface.value)
})

onBeforeUnmount(() => {
  loadGeneration += 1
  resizeObserver?.disconnect()
  resizeObserver = null
  void releaseDocument()
})
</script>

<template>
  <div
    class="pdf-preview"
    data-testid="pdf-preview"
  >
    <div
      v-if="pageCount > 1"
      class="pdf-preview__toolbar"
      aria-label="PDF 翻页"
    >
      <el-button
        text
        :icon="ArrowLeft"
        :disabled="pageNumber <= 1 || loading || rendering"
        aria-label="上一页"
        @click="changePage(pageNumber - 1)"
      />
      <span>第 {{ pageNumber }} / {{ pageCount }} 页</span>
      <el-button
        text
        :icon="ArrowRight"
        :disabled="pageNumber >= pageCount || loading || rendering"
        aria-label="下一页"
        @click="changePage(pageNumber + 1)"
      />
    </div>
    <div
      ref="surface"
      class="pdf-preview__surface"
      :aria-busy="loading || rendering"
    >
      <div
        v-if="loading"
        class="pdf-preview__state"
      >
        正在加载 PDF…
      </div>
      <div
        v-else-if="errorMessage"
        class="pdf-preview__state pdf-preview__state--error"
        role="alert"
      >
        <span>{{ errorMessage }}</span>
        <el-button
          text
          type="primary"
          :icon="Refresh"
          @click="loadDocument"
        >
          重试
        </el-button>
      </div>
      <canvas
        ref="canvas"
        class="pdf-preview__canvas"
        :class="{ 'pdf-preview__canvas--hidden': loading || Boolean(errorMessage) }"
        aria-label="PDF 页面"
      />
    </div>
  </div>
</template>

<style scoped>
.pdf-preview {
  display: flex;
  min-height: 0;
  flex-direction: column;
  border: 1px solid var(--el-border-color-light);
  border-radius: 10px;
  background: #f2f4f7;
  overflow: hidden;
}
.pdf-preview__toolbar {
  display: flex;
  flex: none;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 42px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: #fff;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.pdf-preview__surface {
  position: relative;
  display: flex;
  min-height: 240px;
  flex: 1;
  align-items: flex-start;
  justify-content: center;
  overflow: auto;
  padding: 12px;
}
.pdf-preview__canvas {
  display: block;
  max-width: 100%;
  background: #fff;
  box-shadow: 0 2px 10px rgb(16 24 40 / 12%);
}
.pdf-preview__canvas--hidden { visibility: hidden; }
.pdf-preview__state {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
}
.pdf-preview__state--error { color: var(--el-color-danger); }
</style>

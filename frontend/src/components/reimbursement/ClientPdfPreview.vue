<script setup lang="ts">
import { ArrowLeft, ArrowRight } from '@element-plus/icons-vue'
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import pdfWorkerUrl from 'pdfjs-dist/legacy/build/pdf.worker.min.mjs?url'
import { ensurePromiseWithResolvers } from '@/utils/pdfRuntime'

const props = defineProps<{
  source: Blob
}>()

const emit = defineEmits<{
  failed: [message: string]
}>()

const surface = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const pageNumber = ref(1)
const pageCount = ref(0)
const loading = ref(false)
const rendering = ref(false)

type LoadingTask = { promise: Promise<PdfDocument>; destroy: () => Promise<void> }
type RenderTask = { promise: Promise<void>; cancel: () => void }
type PdfViewport = { width: number; height: number }
type PdfPage = {
  getViewport: (input: { scale: number }) => PdfViewport
  render: (input: {
    canvas: HTMLCanvasElement
    canvasContext: CanvasRenderingContext2D
    viewport: PdfViewport
  }) => RenderTask
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
let failedGeneration = -1

function isCancelledRender(error: unknown): boolean {
  return error instanceof Error && error.name === 'RenderingCancelledException'
}

function fail(generation: number, message: string): void {
  if (generation !== loadGeneration || failedGeneration === generation) return
  failedGeneration = generation
  emit('failed', message)
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

function boundedScale(viewport: PdfViewport, cssScale: number): number {
  const devicePixelRatio = window.devicePixelRatio
  const pixelRatio = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0
    ? Math.max(devicePixelRatio, 1)
    : 1
  const requested = cssScale * pixelRatio
  const maxEdgeScale = Math.min(2200 / viewport.width, 2200 / viewport.height)
  const maxPixelScale = Math.sqrt(2_500_000 / (viewport.width * viewport.height))
  return Math.max(0.1, Math.min(requested, maxEdgeScale, maxPixelScale))
}

async function renderPage(): Promise<void> {
  const document = pdfDocument
  const currentCanvas = canvas.value
  const currentSurface = surface.value
  if (!document || !currentCanvas || !currentSurface) return

  const generation = ++renderGeneration
  const currentLoadGeneration = loadGeneration
  renderTask?.cancel()
  renderTask = null
  rendering.value = true
  try {
    const page = await document.getPage(pageNumber.value)
    if (generation !== renderGeneration) return
    const baseViewport = page.getViewport({ scale: 1 })
    const availableWidth = Math.max(currentSurface.clientWidth - 24, 280)
    const cssScale = availableWidth / baseViewport.width
    const renderScale = boundedScale(baseViewport, cssScale)
    const viewport = page.getViewport({ scale: renderScale })
    const context = currentCanvas.getContext('2d')
    if (!context) throw new Error('canvas unavailable')

    currentCanvas.width = Math.ceil(viewport.width)
    currentCanvas.height = Math.ceil(viewport.height)
    currentCanvas.style.width = `${Math.ceil(baseViewport.width * cssScale)}px`
    currentCanvas.style.height = `${Math.ceil(baseViewport.height * cssScale)}px`
    const task = page.render({ canvas: currentCanvas, canvasContext: context, viewport })
    renderTask = task
    await task.promise
    if (generation === renderGeneration && renderTask === task) renderTask = null
  } catch (error) {
    if (generation === renderGeneration && !isCancelledRender(error)) {
      fail(currentLoadGeneration, '手机端 PDF 页面渲染失败')
    }
  } finally {
    if (generation === renderGeneration) rendering.value = false
  }
}

async function loadDocument(): Promise<void> {
  const generation = ++loadGeneration
  failedGeneration = -1
  loading.value = true
  pageNumber.value = 1
  pageCount.value = 0
  await releaseDocument()
  try {
    ensurePromiseWithResolvers()
    const pdfjs = await import('pdfjs-dist/legacy/build/pdf.mjs')
    if (generation !== loadGeneration) return
    pdfjs.GlobalWorkerOptions.workerSrc = pdfWorkerUrl
    const data = new Uint8Array(await props.source.arrayBuffer())
    if (generation !== loadGeneration) return
    const task = pdfjs.getDocument({
      data,
      isEvalSupported: false,
      useWorkerFetch: false,
    }) as unknown as LoadingTask
    loadingTask = task
    const document = await task.promise
    if (generation !== loadGeneration) return
    loadingTask = null
    pdfDocument = document
    pageCount.value = document.numPages
    await nextTick()
    await renderPage()
  } catch {
    fail(generation, '手机端 PDF 加载失败')
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
    class="client-pdf-preview"
    data-testid="client-pdf-preview"
  >
    <nav
      v-if="pageCount > 1"
      class="client-pdf-preview__toolbar"
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
    </nav>
    <div
      ref="surface"
      class="client-pdf-preview__surface"
      :aria-busy="loading || rendering"
    >
      <div
        v-if="loading"
        class="client-pdf-preview__state"
      >
        正在加载 PDF…
      </div>
      <canvas
        ref="canvas"
        class="client-pdf-preview__canvas"
        :class="{ 'client-pdf-preview__canvas--hidden': loading }"
        aria-label="PDF 页面"
      />
    </div>
  </div>
</template>

<style scoped>
.client-pdf-preview {
  display: flex;
  width: 100%;
  height: 100%;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  background: #f2f4f7;
}
.client-pdf-preview__toolbar {
  display: flex;
  min-height: 42px;
  flex: none;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-bottom: 1px solid var(--el-border-color-light);
  background: #fff;
  color: var(--el-text-color-regular);
  font-size: 13px;
}
.client-pdf-preview__surface {
  position: relative;
  display: flex;
  min-height: 240px;
  flex: 1;
  align-items: flex-start;
  justify-content: center;
  overflow: auto;
  padding: 12px;
}
.client-pdf-preview__canvas {
  display: block;
  max-width: 100%;
  background: #fff;
  box-shadow: 0 2px 10px rgb(16 24 40 / 12%);
}
.client-pdf-preview__canvas--hidden { visibility: hidden; }
.client-pdf-preview__state {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--el-text-color-secondary);
}
</style>

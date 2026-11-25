import assert from 'node:assert/strict'
import { createWriteStream } from 'node:fs'
import { chmod, mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, dirname, join, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { localRoute, ownedProcess } from './runner-support.mjs'

const repository = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
const backend = join(repository, 'backend')
const root = await mkdtemp(join(tmpdir(), 'expense-browser-smoke-'))
await chmod(root, 0o700)
const log = createWriteStream(join(root, 'server.private.log'), { mode: 0o600 })
const ownedServer = ownedProcess('uv', [
  'run', '--frozen', '--extra', 'dev', 'python', 'tests/browser_smoke_server.py', '--root', root,
], {
  cwd: backend,
  env: { ...process.env, PYTHONPATH: [backend, join(backend, 'tests')].join(delimiter) },
  stdio: ['ignore', 'pipe', 'pipe'],
})
const server = ownedServer.child
server.stdout.pipe(log, { end: false })
server.stderr.pipe(log, { end: false })
let startupError
server.on('error', error => { startupError = error })
let browser
let succeeded = false
let deadline
let currentCase = 'startup'
const unexpectedNetwork = []

async function originWhenReady() {
  for (let attempt = 0; attempt < 200; attempt++) {
    if (startupError) throw startupError
    if (server.exitCode !== null) throw new Error('Synthetic server exited before becoming ready')
    try {
      const { port } = JSON.parse(await readFile(join(root, 'server.private.json'), 'utf8'))
      assert(Number.isInteger(port) && port > 0 && port <= 65535)
      const origin = `http://127.0.0.1:${port}`
      const response = await fetch(`${origin}/__browser_smoke__`, { signal: AbortSignal.timeout(500) })
      if (response.ok) {
        const fixture = await response.json()
        assert.equal(fixture.fixture, 'isolated-browser-smoke')
        return { origin, date: fixture.date }
      }
    } catch { /* Startup includes migrations and fixture configuration. */ }
    await delay(150)
  }
  throw new Error('Synthetic server did not become ready within 30 seconds')
}

async function data(response) {
  assert(response.ok(), `Local API failed with HTTP ${response.status()}`)
  return (await response.json()).data
}

async function seed(context, date) {
  const session = await data(await context.request.post('/api/auth/mock'))
  const headers = { 'X-CSRF-Token': session.csrfToken }
  const pdf = await readFile(join(root, 'synthetic.pdf'))
  const png = await readFile(join(root, 'synthetic.png'))
  const proof = await readFile(join(root, 'synthetic-proof.png'))
  const drafts = {}
  for (const kind of ['travel', 'general']) {
    const options = await data(await context.request.get(`/api/oa/reimbursements/options?kind=${kind}`))
    let draft = await data(await context.request.post('/api/reimbursements/drafts', {
      headers, data: { kind, expectedRevision: 0, input: { items: [] } },
    }))
    const path = `/api/reimbursements/drafts/${encodeURIComponent(draft.id)}`
    if (kind === 'travel') {
      const query = await data(await context.request.get('/api/oa/travel-approvals'))
      const approval = query.items[0]
      assert(approval)
      draft = await data(await context.request.put(`${path}/related-approvals`, {
        headers,
        data: { expectedRevision: draft.revision, selections: [{
          processInstanceId: approval.processInstanceId,
          profileKey: approval.profileKey,
          queryWindow: { from: query.queryWindow.from, to: query.queryWindow.to },
        }] },
      }))
    }
    const files = []
    let proofId
    for (const [name, mimeType, buffer, attachmentKind] of [
      ['合成票据.pdf', 'application/pdf', pdf, 'other'], ['合成凭据.png', 'image/png', png, 'other'],
      ['合成付款凭证.png', 'image/png', proof, 'payment_proof'],
    ]) {
      const uploaded = await data(await context.request.post(`${path}/files`, {
        headers,
        params: { expectedRevision: draft.revision, role: 'ATTACHMENT_ONLY', attachmentKind },
        multipart: { 'files[]': { name, mimeType, buffer } },
      }))
      draft.revision = uploaded.revision
      if (attachmentKind === 'payment_proof') proofId = uploaded.file.id
      else files.push(uploaded.file.id)
    }
    draft = await data(await context.request.put(path, {
      headers,
      data: { expectedRevision: draft.revision, input: {
        ...draft.input,
        ...(kind === 'general' ? {
          companyValue: options.companyOptions[0].value,
          budgetCodeValue: options.budgetCodeOptions[0].value,
        } : {}),
        items: [{ category: 'office', date, displayDate: date, amount: '586.50', receiptCount: 1,
          description: '合成打印耗材', receiptAttachmentFileIds: files,
          ...(kind === 'general' ? { subjectValue: options.subjectOptions[0].value } : {}),
        }],
      } },
    }))
    assert(proofId)
    drafts[kind] = { path, id: draft.id, proofId }
  }
  return { drafts, png }
}

async function openKind(page, kind, mobile) {
  await page.getByRole('button', { name: kind === 'general' ? '普通费用报销' : '差旅报销', exact: true }).click()
  await page.getByTestId('autosave-status').getByText('已保存', { exact: true }).waitFor()
  if (mobile) await page.locator('.mobile-step-nav').getByRole('button', { name: /费用材料/ }).click()
}

async function previewMaterials(page, mobile, kind, png) {
  currentCase = `${mobile ? 'mobile' : 'desktop'}-${kind}-preview`
  await openKind(page, kind, mobile)
  await page.evaluate(() => {
    const active = new Set()
    const create = URL.createObjectURL.bind(URL)
    const revoke = URL.revokeObjectURL.bind(URL)
    window.smokePreviewUrls = active
    URL.createObjectURL = blob => { const url = create(blob); active.add(url); return url }
    URL.revokeObjectURL = url => { active.delete(url); revoke(url) }
  })
  let pageRequests = 0
  let failSecondPage = true
  const routePattern = '**/files/*/preview/pages/*'
  await page.route(routePattern, localRoute(new URL(page.url()).origin, async route => {
    const pageNumber = Number(new URL(route.request().url()).pathname.split('/').at(-1))
    pageRequests++
    // Inject bounded failures and synthetic pages; this smoke needs no OCR renderer/model.
    if (pageRequests === 1 || (pageNumber === 2 && failSecondPage)) {
      if (pageNumber === 2) failSecondPage = false
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({
        error: { code: pageRequests === 1 ? 'PDF_PREVIEW_RESOURCE_LIMIT' : 'SYNTHETIC_PAGE_UNAVAILABLE',
          message: '合成页面暂时无法读取' },
      }) })
    }
    await route.fulfill({ status: 200, contentType: 'image/png', body: png,
      headers: { 'x-pdf-page-number': String(pageNumber), 'x-pdf-page-count': '2' } })
  }))
  try {
    const source = page.locator('.material-preview:visible').filter({ hasText: '合成票据.pdf' }).first()
    await source.click()
    const dialog = page.getByRole('dialog', { name: '票据预览：合成票据.pdf' })
    if (mobile) {
      const client = dialog.getByTestId('client-pdf-preview')
      await client.getByText('第 1 / 2 页').waitFor()
      await client.getByRole('button', { name: '下一页', exact: true }).click()
      await client.getByText('第 2 / 2 页').waitFor()
      assert.equal(await dialog.locator('iframe').count(), 0)
    } else {
      await dialog.locator('iframe').waitFor()
      assert((await dialog.locator('iframe').getAttribute('src')).startsWith('blob:'))
    }
    await dialog.getByRole('button', { name: '无法显示？使用兼容预览' }).click()
    await dialog.getByTestId('pdf-preview-page-status').getByText('第 1 / 2 页').waitFor()
    await dialog.getByRole('button', { name: '下一页', exact: true }).click()
    await dialog.getByRole('button', { name: '重试预览' }).waitFor()
    await dialog.getByRole('button', { name: '重试预览' }).click()
    await dialog.getByTestId('pdf-preview-page-status').getByText('第 2 / 2 页').waitFor()
    assert.equal(pageRequests, 4)
    await dialog.getByRole('button', { name: '返回快速预览' }).click()
    await dialog.locator(mobile ? 'canvas[aria-label="PDF 页面"]:visible' : 'iframe').waitFor()
    await dialog.locator('.el-dialog__headerbtn').click()
    await source.dispatchEvent('click')
    await dialog.waitFor()
    await delay(400)
    assert(await dialog.isVisible())
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await page.locator('.material-preview:visible').filter({ hasText: '合成凭据.png' }).first().click()
    const imageDialog = page.getByRole('dialog', { name: '票据预览：合成凭据.png' })
    await imageDialog.getByTestId('receipt-preview-page').waitFor()
    await page.waitForFunction(() => {
      const image = document.querySelector('.el-dialog img')
      return image && image.complete && image.naturalWidth > 0
    })
    await imageDialog.getByRole('button', { name: '关闭', exact: true }).click()
    await imageDialog.waitFor({ state: 'hidden' })
    await page.waitForFunction(() => window.smokePreviewUrls.size === 0)
  } finally { await page.unroute(routePattern) }
  console.log(`PASS ${currentCase}`)
}

async function approvalSelection(page, mobile) {
  currentCase = `${mobile ? 'mobile' : 'desktop'}-approval-selection`
  await openKind(page, 'general', false)
  if (mobile) await page.locator('.mobile-step-nav').getByRole('button', { name: /基本信息/ }).click()
  let queries = 0
  const results = []
  const onResponse = response => {
    if (new URL(response.url()).pathname === '/api/oa/general-approvals') {
      queries++
      results.push(response.json().then(body => body.data))
    }
  }
  page.on('response', onResponse)
  try {
    let section
    const expand = async () => {
      section = page.getByTestId('general-approval-section')
      if (await section.getByRole('switch').getAttribute('aria-checked') !== 'true') {
        await section.locator('.el-switch:visible').click()
      }
      await section.getByTestId('general-approval-query-complete').waitFor()
    }
    const choose = async name => {
      await section.locator('.approval-category-filter .el-select:visible').click()
      await page.getByRole('option', { name, exact: true }).click()
    }
    const save = async row => {
      const response = page.waitForResponse(r => r.request().method() === 'PUT' && r.url().includes('/general-approvals'))
      await row.locator('.el-checkbox:visible').click()
      assert((await response).ok())
    }
    await expand()
    const pages = await Promise.all(results)
    assert(queries >= 2, 'The browser must follow the signed continuation automatically')
    const items = pages.flatMap(page => page.items)
    assert.equal(new Set(items.map(item => item.processInstanceId)).size, 122)
    assert(items.some(item => item.status === 'RUNNING'))
    assert(items.some(item => item.status === 'TERMINATED'))
    assert(items.some(item => item.result === 'refuse'))
    const beforeFilter = queries
    if (mobile) await section.getByRole('button', { name: '筛选审批', exact: true }).click()
    const keyword = section.getByLabel('搜索本人审批', { exact: true })
    await keyword.fill('synthetic-search-42')
    await keyword.press('Enter')
    assert((await section.locator('.travel-approval-row').count()) > 0)
    await keyword.fill('')
    await choose('合成采购申请')
    const first = section.locator('.travel-approval-row').first()
    const title = await first.locator('strong').innerText()
    await save(first)
    await choose('合成出差申请')
    const selected = section.locator('.travel-approval-row').filter({ has: page.getByText(title, { exact: true }) })
    await selected.getByText('已关联 · 不符合当前筛选', { exact: true }).waitFor()
    assert(await selected.getByRole('checkbox').isChecked())
    assert.equal(queries, beforeFilter)
    await page.reload()
    await openKind(page, 'general', false)
    await expand()
    await choose('合成采购申请')
    const restored = section.locator('.travel-approval-row').filter({ has: page.getByText(title, { exact: true }) })
    assert(await restored.getByRole('checkbox').isChecked())
    await save(restored)
  } finally { page.off('response', onResponse) }
  console.log(`PASS ${currentCase}`)
}

async function associatePaymentProof(page, context, mobile, kind, draft) {
  currentCase = `${mobile ? 'mobile' : 'desktop'}-${kind}-proof-association`
  await openKind(page, kind, mobile)
  const before = await data(await context.request.get(draft.path))
  const filesBefore = await data(await context.request.get(`${draft.path}/files`))
  assert.deepEqual(before.input.items[0].paymentProofFileIds ?? [], [])
  const open = async () => {
    await page.getByRole('button', { name: '关联付款凭证', exact: true }).first().click()
    const dialog = page.getByRole('dialog', { name: '关联付款凭证', exact: true })
    await dialog.waitFor()
    return dialog
  }
  const choose = async dialog => {
    await dialog.locator('.el-select').click()
    await page.getByRole('option', { name: /合成付款凭证\.png/ }).click()
    await dialog.getByTestId('proof-picker-summary').click()
    await dialog.locator('.association-selected-files').getByRole('button', { name: '合成付款凭证.png · 预览', exact: true }).waitFor()
  }
  const cancel = async dialog => {
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    // Cover the autosave debounce window before asserting that cancellation wrote nothing.
    await delay(700)
  }
  const confirm = async dialog => {
    const saved = page.waitForResponse(response => response.request().method() === 'PUT'
      && new URL(response.url()).pathname === draft.path)
    void saved.catch(() => {})
    await dialog.getByRole('button', { name: '确认关联', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert((await saved).ok())
    await page.getByTestId('autosave-status').getByText('已保存', { exact: true }).waitFor()
  }

  let dialog = await open()
  await choose(dialog)
  await cancel(dialog)
  const canceled = await data(await context.request.get(draft.path))
  assert.deepEqual(canceled.input, before.input)
  assert.equal(canceled.revision, before.revision)

  dialog = await open()
  assert.equal(await dialog.locator('.association-selected-files').count(), 0)
  await choose(dialog)
  await confirm(dialog)
  const linked = await data(await context.request.get(draft.path))
  assert.deepEqual(linked.input.items[0].paymentProofFileIds, [draft.proofId])
  assert.deepEqual(linked.input.items[0].receiptAttachmentFileIds, before.input.items[0].receiptAttachmentFileIds)

  await page.reload()
  await openKind(page, kind, mobile)
  dialog = await open()
  await dialog.locator('.association-selected-files').getByRole('button', { name: '合成付款凭证.png · 预览', exact: true }).waitFor()
  await dialog.getByRole('button', { name: '移除选择', exact: true }).click()
  await cancel(dialog)
  const removalCanceled = await data(await context.request.get(draft.path))
  assert.deepEqual(removalCanceled.input, linked.input)
  assert.equal(removalCanceled.revision, linked.revision)

  dialog = await open()
  await dialog.getByRole('button', { name: '移除选择', exact: true }).click()
  await confirm(dialog)
  const unlinked = await data(await context.request.get(draft.path))
  assert.deepEqual(unlinked.input.items[0].paymentProofFileIds ?? [], [])
  assert.deepEqual(unlinked.input.items[0].receiptAttachmentFileIds, before.input.items[0].receiptAttachmentFileIds)
  assert.equal(unlinked.input.items[0].amount, before.input.items[0].amount)
  const filesAfter = await data(await context.request.get(`${draft.path}/files`))
  assert.deepEqual(filesAfter.items, filesBefore.items)
  console.log(`PASS ${currentCase}`)
}

async function saveBeforeLeaving(page, context, mobile, kind, draft) {
  currentCase = `${mobile ? 'mobile' : 'desktop'}-${kind}-save-navigation`
  await openKind(page, kind, mobile)
  const edit = async () => {
    if (mobile) await page.locator('.mobile-step-nav').getByRole('button', { name: /费用材料/ }).click()
    const rows = page.locator(mobile ? '.expense-mobile-card:visible' : '.expense-table .el-table__row:visible')
    await rows.first().getByRole('button', { name: '编辑', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '编辑费用明细' })
    await dialog.waitFor()
    return dialog
  }
  let dialog = await edit()
  const original = await dialog.getByLabel('金额（元）', { exact: true }).inputValue()
  const changed = (Number(original) + 0.01).toFixed(2)
  let markStarted
  let failStarted
  let release
  const started = new Promise((resolve, reject) => { markStarted = resolve; failStarted = reject })
  void started.catch(() => {})
  const gate = new Promise(resolve => { release = resolve })
  const pattern = '**/api/reimbursements/drafts/*'
  let delayed = false
  await page.route(pattern, localRoute(new URL(page.url()).origin, async route => {
    if (route.request().method() !== 'PUT' || delayed || new URL(route.request().url()).pathname !== draft.path) {
      return route.fallback()
    }
    delayed = true
    try {
      const response = await route.fetch()
      assert(response.ok())
      markStarted()
      await gate
      await route.fulfill({ response })
    } catch (error) {
      failStarted(error)
      await route.abort().catch(() => {})
    }
  }))
  try {
    await dialog.getByLabel('金额（元）', { exact: true }).fill(changed)
    await dialog.getByRole('button', { name: '保存', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    const navigation = page.getByRole('link', { name: '系统设置', exact: true }).click()
    void navigation.catch(() => {})
    await Promise.race([started, delay(15_000, undefined, { ref: false }).then(() => {
      throw new Error('Expected a local save before entering settings')
    })])
    assert.equal(new URL(page.url()).pathname, mobile ? '/m' : '/')
    release()
    await navigation
    await page.waitForURL(`**${mobile ? '/m/settings' : '/admin/settings'}`)
    await page.getByRole('heading', { name: '系统设置', exact: true }).waitFor()
    // Calendar initialization shares the settings leave guard with calendar edits.
    await page.getByTestId('attendance-calendar-settings').locator('button:enabled')
      .filter({ hasText: '手动填写' }).waitFor()
    await page.getByRole('link', { name: '报销单', exact: true }).click()
    await page.waitForURL(mobile ? '**/m' : '**/')
    await openKind(page, kind, mobile)
    dialog = await edit()
    assert.equal(await dialog.getByLabel('金额（元）', { exact: true }).inputValue(), changed)
    const saved = await data(await context.request.get(draft.path))
    await dialog.getByLabel('金额（元）', { exact: true }).fill((Number(changed) + 7).toFixed(2))
    await dialog.getByRole('button', { name: '取消', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await delay(700)
    const canceled = await data(await context.request.get(draft.path))
    assert.deepEqual(canceled.input, saved.input)
    assert.equal(canceled.revision, saved.revision)
    dialog = await edit()
    await dialog.getByLabel('金额（元）', { exact: true }).fill(original)
    await dialog.getByRole('button', { name: '保存', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await page.getByTestId('autosave-status').getByText('已保存', { exact: true }).waitFor()
  } finally { release(); await page.unroute(pattern) }
  console.log(`PASS ${currentCase}`)
}

async function run() {
  const { origin, date } = await originWhenReady()
  const executablePath = process.env.BROWSER_SMOKE_EXECUTABLE_PATH
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) })
  const bootstrap = await browser.newContext({ baseURL: origin })
  let fixture
  try { fixture = await seed(bootstrap, date) } finally { await bootstrap.close() }
  for (const mobile of [false, true]) {
    const context = await browser.newContext({ baseURL: origin, serviceWorkers: 'block',
      viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1100 } })
    await context.route('**/*', route => {
      const url = new URL(route.request().url())
      if (!['http:', 'https:'].includes(url.protocol)) return route.continue()
      if (url.origin === origin) return route.continue()
      unexpectedNetwork.push(url.hostname)
      return route.abort('blockedbyclient')
    })
    const page = await context.newPage()
    page.setDefaultTimeout(15_000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    try {
      await data(await context.request.post('/api/auth/mock'))
      await page.goto(mobile ? '/m' : '/')
      for (const kind of ['travel', 'general']) await previewMaterials(page, mobile, kind, fixture.png)
      await approvalSelection(page, mobile)
      for (const kind of ['travel', 'general']) {
        await associatePaymentProof(page, context, mobile, kind, fixture.drafts[kind])
        await saveBeforeLeaving(page, context, mobile, kind, fixture.drafts[kind])
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false)
      assert.deepEqual(errors, [])
    } catch (error) {
      await page.screenshot({ path: join(root, `${currentCase}.private.png`), animations: 'disabled' }).catch(() => {})
      throw error
    } finally { await context.close() }
  }
  assert.deepEqual(unexpectedNetwork, [])
}

try {
  const interrupted = new Promise((_, reject) => {
    process.once('SIGINT', () => reject(new Error('Browser smoke interrupted')))
    process.once('SIGTERM', () => reject(new Error('Browser smoke terminated')))
    deadline = setTimeout(() => reject(new Error(`Browser smoke timed out during ${currentCase}`)), 180_000)
  })
  await Promise.race([run(), interrupted])
  succeeded = true
  console.log('PASS all 14 desktop/mobile browser smoke scenarios')
} catch (error) {
  log.write(`${error.stack}\n`)
  console.error(`FAIL ${currentCase}: ${error.message}\nPrivate artifacts: ${root}`)
  process.exitCode = 1
} finally {
  clearTimeout(deadline)
  await browser?.close().catch(() => {})
  await ownedServer.stop()
  await new Promise(resolve => log.end(resolve))
  if (succeeded) await rm(root, { recursive: true, force: true })
}

// Manual UI-only fixture. Run through an isolated Vite port, never a real OA session.
// Every application HTTP request is rejected by the adapter below.
import { createApp, h } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import '../../src/styles/main.css'
import ExpenseItemsCard from '../../src/components/reimbursement/ExpenseItemsCard.vue'
import { http } from '../../src/api/http'
import { useExpenseStore } from '../../src/stores/expense'
import { useReimbursementDraftStore } from '../../src/stores/reimbursementDraft'
import type { ReimbursementDraft, ReimbursementDraftFile } from '../../src/types/reimbursements'

const pinia = createPinia()
setActivePinia(pinia)
const expense = useExpenseStore()
const drafts = useReimbursementDraftStore()
const scenario = new URLSearchParams(location.search).get('scenario')
http.defaults.adapter = async () => { throw new Error('隔离验收页禁止业务网络请求') }
expense.refreshCalculations = async () => undefined
expense.categories = [
  { id: 'lodging', name: '住宿费', order: 1, manualSelectable: true },
  { id: 'local_transport', name: '市内交通费', order: 2, manualSelectable: true },
  { id: 'rail_fare', name: '火车票', order: 3, manualSelectable: true },
  { id: 'other', name: '其他费用', order: 4, manualSelectable: true },
]
drafts.currentDraft = {
  id: 'ui-only', status: 'DRAFT', revision: 1, department: { id: '1', name: '界面验收部门' },
  templateConfigVersion: 1, relatedApprovalCount: 0, expiresAt: '', createdAt: '', updatedAt: '', lockedAt: null,
  template: { processCode: 'UI-ONLY', configVersion: 1, schemaFingerprint: 'ui-only' },
  input: { ocrDispositionVersion: 1, companyValue: '示例公司', budgetCodeValue: '示例预算', trip: null, items: [], dismissedOcrFileIds: [] },
  totals: { expenseTotal: '0.00', subsidyTotal: '0.00', totalAmount: '0.00', receiptCount: 0, uppercaseAmount: '', subsidy: null },
  relatedApprovals: [], relatedApprovalSummary: null,
} satisfies ReimbursementDraft
const baseFile = (id: string, name: string): ReimbursementDraftFile => ({
  id, name, role: 'ATTACHMENT_ONLY', attachmentKind: 'other', sortOrder: 1, status: 'ACTIVE',
  mediaType: 'application/pdf', sizeBytes: 235900, ocrStatus: 'COMPLETE', ocrResult: null,
})
const taxi = {
  ...baseFile('invoice-1', '打车发票3.pdf'), role: 'EXPENSE_SOURCE' as const,
  ocrResult: { fileId: 'invoice-1', type: 'invoice', categoryId: 'local_transport', categoryName: '市内交通费',
    date: '2026-07-06', amount: '9.20', description: '示例存储技术有限公司东门→示例产业园酒店',
    transportType: 'other' as const, receiptCount: 1 as const, source: 'ocr' as const, confidence: '0.96', warnings: [], status: 'recognized' as const, error: null },
}
const itinerary = {
  ...baseFile('trip-1', '打车行程单3.pdf'), attachmentKind: 'itinerary' as const,
  ocrResult: { fileId: 'trip-1', version: 1 as const, kind: 'itinerary' as const, status: 'recognized' as const, source: 'pdf_text' as const,
    pageCount: 1, processedPageCount: 1, complete: true, warnings: [], error: null,
    summary: { currency: 'CNY', amount: '9.20', startDate: '2026-07-06', endDate: '2026-07-06', invoiceNumbers: [], orderNumbers: [] },
    trips: [{ page: 1, row: 1, date: '2026-07-06', amount: '9.20', origin: '示例存储技术有限公司东门', destination: '示例产业园酒店', invoiceNumbers: [], orderNumbers: [] }],
  },
}
const proof = { ...baseFile('payment-1', '酒店住宿付款截图-电子支付记录.png'), attachmentKind: 'payment_proof' as const, mediaType: 'image/png' }
const hotelBill: ReimbursementDraftFile = { ...baseFile('hotel-1', '住宿明细示例.pdf'), attachmentKind: 'hotel_bill',
  materialClassification: { status: 'classified', kind: 'hotel_bill', reason: null, pageCount: 1 },
  hotelBillDetails: { guest: '示例住客', checkIn: '2026-07-06', checkOut: '2026-07-07', nights: 1,
    nightlyRate: '580.00', total: '580.00', currency: 'CNY', warnings: ['HOTEL_BILL_REVIEW_REQUIRED'] },
}
const unknown = { ...baseFile('unknown-1', '手机拍摄材料-待确认.jpg'), materialClassification: {
  status: 'needs_confirmation' as const, kind: 'unknown' as const, reason: '未能确定材料用途，请确认后继续。', pageCount: 1,
} }
drafts.files = [taxi, itinerary, proof, hotelBill, unknown]
if (new URLSearchParams(location.search).get('scenario') === 'amount-only') {
  Object.assign(taxi.ocrResult, { date: '2026-09-01', description: null,
    transportType: 'ride_hailing', requiresItinerary: true,
    warnings: ['INVOICE_DATE_USED_AS_OCCURRENCE', 'MANUAL_REVIEW_REQUIRED'] })
}
expense.upsertDraftOcrItem(taxi)
expense.upsertManualItem({ category: 'lodging', description: '住宿待补住宿明细和付款凭证', date: '2026-07-06', displayDate: '2026-07-06', amount: '680.00', receiptCount: 1 })
expense.upsertManualItem({ category: 'lodging', description: '已附住宿明细和付款凭证的住宿费用', date: '2026-07-07', displayDate: '2026-07-07', amount: '580.00', receiptCount: 1, paymentProofFileIds: [proof.id], hotelBillFileIds: [hotelBill.id] })
expense.upsertManualItem({ category: 'lodging', description: '低于 500 元仍需住宿明细', date: '2026-07-08', displayDate: '2026-07-08', amount: '300.00', receiptCount: 1 })
expense.upsertManualItem({ category: 'rail_fare', description: 'G1234 高铁，超过 500 元免付款凭证', date: '2026-07-07', displayDate: '2026-07-07', amount: '650.00', receiptCount: 1, railType: 'high_speed' })
if (new URLSearchParams(location.search).get('scenario') === 'payment-expense') {
  drafts.files = [{ ...baseFile('payment-card-fee', '制卡费银行付款凭证.jpg'), attachmentKind: 'payment_proof', mediaType: 'image/jpeg',
    materialClassification: { status: 'classified', kind: 'payment_proof', reason: null, pageCount: 1 },
    paymentDetails: { amount: '50.00', date: '2026-07-09', description: '制卡费', categoryId: null },
  }]
  expense.items = []
}
if (scenario === 'pipeline') {
  drafts.files = []
  expense.items = []
}
drafts.uploadFile = async (file, role = 'EXPENSE_SOURCE', attachmentKind = 'other', autoClassify = false) => {
  await new Promise((resolve) => setTimeout(resolve, scenario === 'pipeline' ? 4000 : 700))
  const uploaded = { ...baseFile(crypto.randomUUID(), file.name), role, attachmentKind,
    materialClassification: autoClassify ? { status: 'needs_confirmation' as const, kind: 'unknown' as const, reason: '隔离样例上传，仅演示用途确认。', pageCount: 1 } : null }
  drafts.files.push(uploaded)
  return { draftId: 'ui-only', revision: ++drafts.currentDraft!.revision, file: uploaded }
}
drafts.recognizeFile = async (fileId) => {
  const file = drafts.files.find((entry) => entry.id === fileId)!
  if (scenario === 'pipeline') {
    await new Promise((resolve) => setTimeout(resolve, 8000))
    file.role = 'EXPENSE_SOURCE'
    file.materialClassification = { status: 'classified', kind: 'expense', reason: null, pageCount: 1 }
    file.ocrResult = { fileId, type: 'invoice', categoryId: 'other', categoryName: '其他费用',
      date: '2026-09-07', amount: '50.00', description: `${file.name} 示例费用`,
      receiptCount: 1, source: 'ocr', confidence: '0.96', warnings: [], status: 'recognized', error: null }
  }
  return { draftId: 'ui-only', revision: drafts.currentDraft!.revision, file }
}
drafts.removeFile = async (fileId) => {
  await new Promise((resolve) => setTimeout(resolve, 300))
  drafts.files = drafts.files.filter((file) => file.id !== fileId)
  return { draftId: 'ui-only', revision: ++drafts.currentDraft!.revision, deletedFileId: fileId }
}
drafts.updateFile = async (fileId, change) => {
  const file = drafts.files.find((entry) => entry.id === fileId)!
  Object.assign(file, change)
  file.materialClassification = { status: 'confirmed', kind: 'other', reason: null, pageCount: 1 }
  return { draftId: 'ui-only', revision: ++drafts.currentDraft!.revision, file }
}
const app = createApp({ render: () => h('main', { style: 'max-width:1200px;margin:24px auto;padding:0 16px' }, [
  h('h1', { style: 'font-size:24px' }, '智能差旅报销 · 材料交互验收'),
  h('p', { style: 'color:#667085;font-size:13px' }, '隔离内存数据，不免登、不保存到服务器、不创建 OA。上传与关联只影响本页。'),
  ...(new URLSearchParams(location.search).get('scenario') === 'payment-expense'
    ? [h('p', { 'data-testid': 'payment-expense-state', style: 'color:#667085;font-size:13px' },
      `已录入 ${expense.items.length} 笔费用；${expense.items.map((item) => `¥${item.amount} · ${item.source} · 发票来源 ${item.sourceFileId || '无'} · 已附凭证 ${item.paymentProofFileIds?.length ?? 0}`).join('；')}`)] : []),
  ...(scenario === 'pipeline' ? [
    h('button', { 'data-testid': 'start-pipeline', class: 'el-button el-button--primary', disabled: drafts.processingFiles,
      onClick: () => {
        const input = document.querySelector<HTMLInputElement>('[data-testid="durable-expense-input"]')!
        const selected = new DataTransfer()
        for (const name of ['A.pdf', 'B.pdf', 'C.pdf']) selected.items.add(new File(['isolated UI fixture'], name, { type: 'application/pdf' }))
        input.files = selected.files
        input.dispatchEvent(new Event('change', { bubbles: true }))
      },
    }, '开始模拟批次'),
    h('p', { 'data-testid': 'pipeline-published-count' }, `费用列表已发布 ${expense.items.length} 笔（批次完成前应保持 0）`),
  ] : []),
  h(ExpenseItemsCard, { durable: true }),
]) })
app.use(pinia).use(ElementPlus).mount('#app')

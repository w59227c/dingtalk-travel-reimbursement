import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { diagnoseReceipt, type ReceiptDiagnostic } from '@/api/receiptDiagnostics'
import { useAuthStore } from '@/stores/auth'
import ReceiptDiagnosticsCard from './ReceiptDiagnosticsCard.vue'

vi.mock('@/api/receiptDiagnostics', () => ({ diagnoseReceipt: vi.fn() }))
enableAutoUnmount(afterEach)

const report: ReceiptDiagnostic = {
  requestId: 'test-request', keywordRuleCount: 10,
  recognition: {
    categoryName: '办公费', amount: '12.30', date: '2026-09-03',
    _materialClassification: { kind: 'expense', reason: '识别到发票结构', pageCount: 1 },
    diagnostic: {
      recognizedText: '<script>合成文具</script>', recognizedTextTruncated: false,
      sources: ['paddle'], complete: true, parser: 'GenericInvoiceParser', policy: 'keyword_match',
      matchedRules: [{ keyword: '合成文具', category: 'office', categoryName: '办公费', transportType: null }],
      classificationText: '合成文具', parserScores: [{ parser: 'GenericInvoiceParser', score: 0.8 }],
    },
  },
}

describe.each([false, true])('receipt diagnostic card mobile=%s', (mobile) => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    vi.mocked(diagnoseReceipt).mockResolvedValue(report)
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() }))
  })
  afterEach(() => vi.unstubAllGlobals())
  function card() { return mount(ReceiptDiagnosticsCard, { props: { mobile }, global: { plugins: [ElementPlus] } }) }
  async function choose(wrapper: ReturnType<typeof card>, name = 'synthetic.png') {
    const picker = wrapper.get<HTMLInputElement>('input[type=file]')
    Object.defineProperty(picker.element, 'files', { value: [new File(['test'], name)], configurable: true })
    await picker.trigger('change')
  }

  it('shows escaped OCR text, matching evidence and actual classification on explicit request', async () => {
    const wrapper = card()
    await choose(wrapper)
    await wrapper.get('[aria-label="诊断参考年份"]').setValue('2026')
    expect(diagnoseReceipt).not.toHaveBeenCalled()
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(diagnoseReceipt).toHaveBeenCalledWith(expect.any(File), 2026, expect.any(AbortSignal))
    expect(wrapper.get('[aria-label="诊断识别原文"]').text()).toBe('<script>合成文具</script>')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.text()).toContain('“合成文具” → 办公费')
    expect(wrapper.text()).toContain('通用发票匹配到一个费用类别')
    expect(wrapper.get('[aria-label="诊断分类输入文字"]').text()).toBe('合成文具')
    await wrapper.findAll('button').find((button) => button.text() === '清空')!.trigger('click')
    expect(wrapper.find('[aria-label="票据诊断结果"]').exists()).toBe(false)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:test')
  })

  it('rejects invalid input and reports errors without a false empty result', async () => {
    const wrapper = card()
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('请先选择一张票据')
    await choose(wrapper, 'test.txt')
    expect(wrapper.text()).toContain('请选择 JPG、PNG 或单页 PDF')
    await choose(wrapper)
    await wrapper.get('[aria-label="诊断参考年份"]').setValue('1800')
    await wrapper.get('form').trigger('submit')
    expect(wrapper.text()).toContain('参考年份应在')
    expect(diagnoseReceipt).not.toHaveBeenCalled()
    await wrapper.get('[aria-label="诊断参考年份"]').setValue('2026')
    vi.mocked(diagnoseReceipt).mockRejectedValueOnce(new Error('timeout'))
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('timeout')
    expect(wrapper.find('[aria-label="票据诊断结果"]').exists()).toBe(false)
  })

  it.each(['clear', 'file', 'year', 'identity', 'unmount'])('invalidates a pending response after %s', async (action) => {
    let finish!: (data: ReceiptDiagnostic) => void
    vi.mocked(diagnoseReceipt).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const wrapper = card()
    await choose(wrapper)
    await wrapper.get('form').trigger('submit')
    const signal = vi.mocked(diagnoseReceipt).mock.calls[0]![2]!
    if (action === 'clear') await wrapper.findAll('button').find((button) => button.text() === '取消并清空')!.trigger('click')
    if (action === 'file') await choose(wrapper, 'other.png')
    if (action === 'year') await wrapper.get('[aria-label="诊断参考年份"]').setValue('2026')
    if (action === 'identity') useAuthStore().$patch({ session: { user: { userId: 'changed' }, isAdmin: true } })
    if (action === 'unmount') wrapper.unmount()
    expect(signal.aborted).toBe(true)
    finish(report)
    await flushPromises()
    if (action !== 'unmount') expect(wrapper.find('[aria-label="票据诊断结果"]').exists()).toBe(false)
  })
  it('separates native PDF, image OCR and derived evidence in the report', async () => {
    vi.mocked(diagnoseReceipt).mockResolvedValueOnce({ ...report, recognition: {
      ...report.recognition, diagnostic: { ...report.recognition.diagnostic,
        sources: ['pdf_text', 'paddle'],
        extractions: [
          { page: 1, source: 'pdf_text', text: '价税合计12.30', truncated: false, usedForParsing: true },
          { page: 1, source: 'paddle', text: '合成路线', truncated: false, usedForParsing: true },
        ],
        derivedFields: [{ page: 1, text: '销售方名称：合成商户', truncated: false }],
      },
    } })
    const wrapper = card()
    await choose(wrapper)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const raw = wrapper.get('[aria-label="诊断识别原文"]')
    expect(raw.text()).toContain('PDF 原生文字')
    expect(raw.text()).toContain('图片 OCR')
    expect(raw.text()).not.toContain('销售方名称')
    expect(wrapper.text()).toContain('版面推导字段')
    expect(wrapper.text()).toContain('销售方名称：合成商户')
    expect(wrapper.text()).toContain('不是 OCR 原始输出')
  })

})

import ElementPlus from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import type { DefineComponent } from 'vue'

import ReimbursementAttentionNotice from './ReimbursementAttentionNotice.vue'

afterEach(() => { vi.unstubAllGlobals(); document.body.innerHTML = '' })

it.each([false, true])('keeps an offscreen error visible until viewed or resolved, mobile=%s', async (mobile) => {
  let notify!: IntersectionObserverCallback
  const disconnect = vi.fn()
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback: IntersectionObserverCallback) { notify = callback }
    observe = vi.fn()
    disconnect = disconnect
  })
  document.body.innerHTML = '<main class="reimbursement-page"><div data-attention-target="save"></div><div id="notice"></div></main>'
  const target = document.querySelector('[data-attention-target="save"]')!
  const notice = { key: 'draft-save', title: '保存失败，内容仍保留在本页', target: '[data-attention-target="save"]', tone: 'error' as const }
  const component = ReimbursementAttentionNotice as DefineComponent<{
    notice: typeof notice | null; mobile: boolean; footerHeight: number; suspended: boolean
  }>
  const wrapper = mount(component, {
    attachTo: '#notice', props: { notice, mobile, footerHeight: mobile ? 116 : 0, suspended: false },
    global: { plugins: [ElementPlus] },
  })
  await flushPromises()
  const visibility = (visible: boolean) => notify([{ target, isIntersecting: visible, intersectionRatio: visible ? 1 : 0 } as IntersectionObserverEntry], {} as IntersectionObserver)
  visibility(false)
  await flushPromises()
  expect(wrapper.get('[data-testid="reimbursement-attention"]').text()).toContain('保存失败')
  await wrapper.get('button').trigger('click')
  expect(wrapper.emitted('reveal')).toHaveLength(1)
  visibility(true)
  await flushPromises()
  expect(wrapper.find('aside').exists()).toBe(false)
  visibility(false)
  await flushPromises()
  expect(wrapper.find('aside').exists()).toBe(true)
  await wrapper.setProps({ suspended: true })
  expect(wrapper.find('aside').exists()).toBe(false)
  await wrapper.setProps({ suspended: false })
  await flushPromises()
  expect(wrapper.find('aside').exists()).toBe(true)
  const oldNotify = notify
  await wrapper.setProps({ notice: null })
  oldNotify([{ target, isIntersecting: false, intersectionRatio: 0 } as IntersectionObserverEntry], {} as IntersectionObserver)
  await flushPromises()
  expect(wrapper.find('aside').exists()).toBe(false)
  wrapper.unmount()
  expect(disconnect).toHaveBeenCalled()
})

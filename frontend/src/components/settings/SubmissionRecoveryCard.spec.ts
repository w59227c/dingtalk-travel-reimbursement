import { createPinia } from 'pinia'
import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { recoverReimbursementSubmission } from '@/api/submissionRecovery'
import { useAuthStore } from '@/stores/auth'
import type { ReimbursementSubmission } from '@/types/reimbursements'
import SubmissionRecoveryCard from './SubmissionRecoveryCard.vue'

vi.mock('@/api/submissionRecovery', () => ({
  recoverReimbursementSubmission: vi.fn(),
}))

const verifying: ReimbursementSubmission = {
  submissionId: 'submission-1',
  draftId: 'draft-1',
  status: 'VERIFYING',
  statusVersion: 3,
  attemptCount: 1,
  processInstanceId: 'oa-1',
  businessId: null,
  approvalUrl: null,
  error: null,
  pollAfterMs: 1000,
  createdAt: '2026-09-14T00:00:00Z',
  updatedAt: '2026-09-14T00:00:01Z',
  submittedAt: null,
}

describe('SubmissionRecoveryCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(recoverReimbursementSubmission).mockResolvedValue(verifying)
  })

  it.each([false, true])('requires and submits the administrator verification note when attaching an OA instance, mobile=%s', async (mobile) => {
    const error = vi.spyOn(ElMessage, 'error').mockImplementation(() => undefined as never)
    vi.spyOn(ElMessage, 'success').mockImplementation(() => undefined as never)
    const wrapper = mount(SubmissionRecoveryCard, { props: { mobile }, global: { plugins: [createPinia(), ElementPlus] } })
    expect(wrapper.get('details').attributes('open')).toBeUndefined()
    const inputs = wrapper.findAll('input')
    await inputs[0]!.setValue('submission-1')
    await inputs[1]!.setValue('oa-1')

    await wrapper.findAll('button').find((button) => button.text().includes('绑定审批'))!.trigger('click')
    expect(error).toHaveBeenCalledWith('请填写提交记录 ID、钉钉审批实例 ID 和核对依据')
    expect(recoverReimbursementSubmission).not.toHaveBeenCalled()

    await wrapper.get('[data-testid="verification-note"]').setValue('已按发起人、时间和全部表单字段核对')
    await wrapper.findAll('button').find((button) => button.text().includes('绑定审批'))!.trigger('click')
    await flushPromises()

    expect(recoverReimbursementSubmission).toHaveBeenCalledWith('submission-1', {
      action: 'ATTACH_INSTANCE',
      processInstanceId: 'oa-1',
      verificationNote: '已按发起人、时间和全部表单字段核对',
    })
    expect(wrapper.find('.submission-recovery-result.el-alert--success').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('carries the same verification note into the not-created recovery decision, mobile=%s', async (mobile) => {
    vi.spyOn(ElMessage, 'success').mockImplementation(() => undefined as never)
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    vi.mocked(recoverReimbursementSubmission).mockResolvedValueOnce({
      submissionId: 'submission-1',
      draftId: 'draft-1',
      status: 'ORPHAN_CLEANUP',
      statusVersion: 3,
      attemptCount: 1,
      processInstanceId: null,
      businessId: null,
      approvalUrl: null,
      error: null,
      pollAfterMs: 1000,
      createdAt: '2026-09-14T00:00:00Z',
      updatedAt: '2026-09-14T00:00:01Z',
      submittedAt: null,
    })
    const wrapper = mount(SubmissionRecoveryCard, { props: { mobile }, global: { plugins: [createPinia(), ElementPlus] } })
    await wrapper.findAll('input')[0]!.setValue('submission-1')
    await wrapper.get('[data-testid="verification-note"]').setValue('已检查钉钉审批列表，确认未创建')

    await wrapper.findAll('button').find((button) => button.text().includes('确认未创建'))!.trigger('click')
    await flushPromises()

    expect(recoverReimbursementSubmission).toHaveBeenCalledWith('submission-1', {
      action: 'CONFIRM_NOT_CREATED',
      verificationNote: '已检查钉钉审批列表，确认未创建',
      confirmUncertainUploadsAbsent: false,
    })
    expect(wrapper.find('.submission-recovery-result.el-alert--success').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('blocks duplicate recovery and discards a response after identity changes, mobile=%s', async (mobile) => {
    const pinia = createPinia()
    const success = vi.spyOn(ElMessage, 'success').mockImplementation(() => undefined as never)
    let finish!: (value: ReimbursementSubmission) => void
    vi.mocked(recoverReimbursementSubmission).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const wrapper = mount(SubmissionRecoveryCard, { props: { mobile }, global: { plugins: [pinia, ElementPlus] } })
    await wrapper.findAll('input')[0]!.setValue('submission-1')
    await wrapper.findAll('input')[1]!.setValue('oa-1')
    await wrapper.get('[data-testid="verification-note"]').setValue('合成核对依据')
    const button = wrapper.findAllComponents({ name: 'ElButton' })[0]!
    button.vm.$emit('click')
    button.vm.$emit('click')
    await flushPromises()
    expect(recoverReimbursementSubmission).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent({ name: 'ElForm' }).props('disabled')).toBe(true)
    useAuthStore(pinia).$patch({ session: { user: { userId: 'changed' }, isAdmin: true } })
    finish(verifying)
    await flushPromises()
    expect(success).not.toHaveBeenCalled()
    expect(wrapper.find('.submission-recovery-result').exists()).toBe(false)
    expect(wrapper.findAll('input')[0]!.element.value).toBe('')
    wrapper.unmount()
  })

  it.each([false, true])('keeps the second remote-file confirmation and audit note, mobile=%s', async (mobile) => {
    vi.spyOn(ElMessageBox, 'confirm').mockResolvedValue({} as never)
    vi.spyOn(ElMessage, 'success').mockImplementation(() => undefined as never)
    vi.mocked(recoverReimbursementSubmission).mockRejectedValueOnce({
      isAxiosError: true, response: { data: { error: { code: 'OA_REMOTE_FILE_CONFIRMATION_REQUIRED' } } },
    }).mockResolvedValueOnce({ ...verifying, status: 'ORPHAN_CLEANUP' })
    const wrapper = mount(SubmissionRecoveryCard, { props: { mobile }, global: { plugins: [createPinia(), ElementPlus] } })
    await wrapper.findAll('input')[0]!.setValue('submission-1')
    await wrapper.get('[data-testid="verification-note"]').setValue('合成核对依据')
    await wrapper.findAll('button').find((button) => button.text().includes('确认未创建'))!.trigger('click')
    await flushPromises()
    expect(ElMessageBox.confirm).toHaveBeenCalledTimes(2)
    expect(recoverReimbursementSubmission).toHaveBeenNthCalledWith(2, 'submission-1', {
      action: 'CONFIRM_NOT_CREATED', verificationNote: '合成核对依据', confirmUncertainUploadsAbsent: true,
    })
    expect(wrapper.text()).toContain('正在清理未关联附件')
    expect(wrapper.find('.submission-recovery-result.el-alert--success').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([false, true])('does not submit a cancelled confirmation, mobile=%s', async (mobile) => {
    vi.spyOn(ElMessageBox, 'confirm').mockRejectedValueOnce('cancel')
    const wrapper = mount(SubmissionRecoveryCard, { props: { mobile }, global: { plugins: [createPinia(), ElementPlus] } })
    await wrapper.findAll('input')[0]!.setValue('submission-1')
    await wrapper.get('[data-testid="verification-note"]').setValue('合成核对依据')
    await wrapper.findAll('button').find((button) => button.text().includes('确认未创建'))!.trigger('click')
    await flushPromises()
    expect(recoverReimbursementSubmission).not.toHaveBeenCalled()
    expect(wrapper.findComponent({ name: 'ElForm' }).props('disabled')).toBe(false)
    wrapper.unmount()
  })

})

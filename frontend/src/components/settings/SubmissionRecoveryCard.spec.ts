import ElementPlus, { ElMessage, ElMessageBox } from 'element-plus'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { recoverReimbursementSubmission } from '@/api/submissionRecovery'
import SubmissionRecoveryCard from './SubmissionRecoveryCard.vue'

vi.mock('@/api/submissionRecovery', () => ({
  recoverReimbursementSubmission: vi.fn(),
}))

describe('SubmissionRecoveryCard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(recoverReimbursementSubmission).mockResolvedValue({
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
    })
  })

  it('requires and submits the administrator verification note when attaching an OA instance', async () => {
    const error = vi.spyOn(ElMessage, 'error').mockImplementation(() => undefined as never)
    vi.spyOn(ElMessage, 'success').mockImplementation(() => undefined as never)
    const wrapper = mount(SubmissionRecoveryCard, { global: { plugins: [ElementPlus] } })
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
    wrapper.unmount()
  })

  it('carries the same verification note into the not-created recovery decision', async () => {
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
    const wrapper = mount(SubmissionRecoveryCard, { global: { plugins: [ElementPlus] } })
    await wrapper.findAll('input')[0]!.setValue('submission-1')
    await wrapper.get('[data-testid="verification-note"]').setValue('已检查钉钉审批列表，确认未创建')

    await wrapper.findAll('button').find((button) => button.text().includes('确认未创建'))!.trigger('click')
    await flushPromises()

    expect(recoverReimbursementSubmission).toHaveBeenCalledWith('submission-1', {
      action: 'CONFIRM_NOT_CREATED',
      verificationNote: '已检查钉钉审批列表，确认未创建',
      confirmUncertainUploadsAbsent: false,
    })
    wrapper.unmount()
  })
})

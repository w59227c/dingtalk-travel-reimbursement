import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  getMe,
  getPublicConfig,
  loginWithDingTalk,
  loginWithMock,
  logout as logoutRequest,
  selectDepartment as selectDepartmentRequest,
  selectDepartmentFromTravelApproval as selectDepartmentFromTravelApprovalRequest,
} from '@/api/auth'
import { setCsrfToken, setUnauthorizedHandler } from '@/api/http'
import type { AuthSession } from '@/types/auth'
import type { ReimbursementRelatedApprovalSelection } from '@/types/reimbursements'
import { requestDingTalkAuthCode } from '@/utils/dingtalk'
import { useExpenseStore } from '@/stores/expense'
import { useReimbursementDraftStore } from '@/stores/reimbursementDraft'
import { useReimbursementSubmissionStore } from '@/stores/reimbursementSubmission'

export type AuthStatus =
  | 'idle'
  | 'loading'
  | 'mock_required'
  | 'department_required'
  | 'authenticated'
  | 'unauthorized'
  | 'error'

export const useAuthStore = defineStore('auth', () => {
  const status = ref<AuthStatus>('idle')
  const session = ref<AuthSession | null>(null)
  const errorMessage = ref('')
  const appTitle = ref('智能差旅费报销申请')
  let bootstrapPromise: Promise<void> | null = null

  const initialized = computed(() => status.value !== 'idle' && status.value !== 'loading')
  const isAdmin = computed(() => session.value?.isAdmin === true)

  function sameSessionScope(left: AuthSession | null, right: AuthSession): boolean {
    return left?.user.userId === right.user.userId
      && left.selectedDepartment?.id === right.selectedDepartment?.id
  }

  function applySession(value: AuthSession): void {
    if (!sameSessionScope(session.value, value)) {
      useReimbursementDraftStore().reset()
      if (session.value !== null) {
        useExpenseStore().reset()
        useReimbursementSubmissionStore().reset()
      }
    }
    session.value = value
    setCsrfToken(value.csrfToken)
    status.value = value.selectedDepartment ? 'authenticated' : 'department_required'
    errorMessage.value = ''
  }

  function clearAsUnauthorized(): void {
    useExpenseStore().reset()
    useReimbursementDraftStore().reset()
    useReimbursementSubmissionStore().reset()
    session.value = null
    setCsrfToken(null)
    if (status.value !== 'loading') status.value = 'unauthorized'
  }

  setUnauthorizedHandler(clearAsUnauthorized)

  async function refreshMe(): Promise<void> {
    applySession(await getMe())
  }

  async function refreshPublicConfig() {
    const config = await getPublicConfig()
    appTitle.value = config.appTitle?.trim() || '智能差旅费报销申请'
    document.title = appTitle.value
    useExpenseStore().setReceiptUploadLimits(config.uploadLimits)
    useExpenseStore().setExpenseItemLimit(config.expenseLimits.maxItems)
    useReimbursementSubmissionStore().oaSubmissionEnabled = config.oaSubmissionEnabled === true
    return config
  }

  async function runBootstrap(): Promise<void> {
    status.value = 'loading'
    errorMessage.value = ''
    const config = await refreshPublicConfig()
    try {
      await refreshMe()
      return
    } catch (error) {
      if (!axios.isAxiosError(error) || error.response?.status !== 401) throw error
    }

    if (config.authMockEnabled) {
      status.value = 'mock_required'
      return
    }
    if (!config.corpId || !config.clientId) {
      throw new Error('钉钉应用公开配置不完整')
    }
    const authCode = await requestDingTalkAuthCode(config.corpId, config.clientId)
    applySession(await loginWithDingTalk(authCode))
    await refreshMe()
  }

  async function bootstrap(force = false): Promise<void> {
    if (!force && initialized.value) return
    if (bootstrapPromise) return bootstrapPromise
    bootstrapPromise = runBootstrap()
      .catch((error: unknown) => {
        status.value = 'error'
        errorMessage.value = error instanceof Error ? error.message : '免登失败，请重新进入'
      })
      .finally(() => {
        bootstrapPromise = null
      })
    return bootstrapPromise
  }

  async function useDevelopmentMock(): Promise<void> {
    status.value = 'loading'
    try {
      applySession(await loginWithMock())
      await refreshMe()
    } catch (error) {
      status.value = 'error'
      errorMessage.value = error instanceof Error ? error.message : '开发免登失败'
    }
  }

  async function selectDepartment(departmentId: string): Promise<void> {
    if (!session.value) return
    useExpenseStore().reset()
    useReimbursementDraftStore().reset()
    useReimbursementSubmissionStore().reset()
    session.value.selectedDepartment = await selectDepartmentRequest(departmentId)
    status.value = 'authenticated'
  }

  async function selectDepartmentFromTravelApproval(
    selection: ReimbursementRelatedApprovalSelection,
  ): Promise<void> {
    if (!session.value) return
    useExpenseStore().reset()
    useReimbursementDraftStore().reset()
    useReimbursementSubmissionStore().reset()
    const selected = await selectDepartmentFromTravelApprovalRequest(selection)
    session.value.departments = [
      selected,
      ...session.value.departments.filter(
        (item) => item.id !== selected.id && item.name !== selected.name,
      ),
    ]
    session.value.selectedDepartment = selected
    status.value = 'authenticated'
  }

  async function logout(): Promise<void> {
    status.value = 'loading'
    useExpenseStore().reset()
    useReimbursementDraftStore().reset()
    useReimbursementSubmissionStore().reset()
    try {
      await logoutRequest()
    } finally {
      clearAsUnauthorized()
      status.value = 'unauthorized'
    }
  }

  return {
    status,
    session,
    errorMessage,
    initialized,
    isAdmin,
    appTitle,
    bootstrap,
    refreshMe,
    refreshPublicConfig,
    useDevelopmentMock,
    selectDepartment,
    selectDepartmentFromTravelApproval,
    logout,
  }
})

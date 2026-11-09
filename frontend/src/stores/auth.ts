import axios from 'axios'
import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import {
  getMe,
  getPublicConfig,
  loginWithDingTalk,
  loginWithMock,
  logout as logoutRequest,
  selectDepartmentFromTravelApproval as selectDepartmentFromTravelApprovalRequest,
} from '@/api/auth'
import { setCsrfToken, setUnauthorizedHandler } from '@/api/http'
import type { AuthSession, ReimbursementDepartmentResolution } from '@/types/auth'
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
  let scopeGeneration = 0
  let identityReadVersion = 0
  let departmentRequestVersion = 0
  watch(() => [status.value, session.value?.user.userId, session.value?.selectedDepartment?.id,
    session.value?.selectedDepartment?.name, session.value?.csrfToken], () => { scopeGeneration++ }, { flush: 'sync' })

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
    const generation = scopeGeneration, version = ++identityReadVersion
    const value = await getMe()
    if (generation !== scopeGeneration || version !== identityReadVersion) return
    applySession(value)
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

  async function selectDepartmentFromTravelApproval(
    selection: ReimbursementRelatedApprovalSelection,
    selectedDepartmentId?: string,
  ): Promise<ReimbursementDepartmentResolution & { isCurrent: () => boolean }> {
    if (!session.value) throw new Error('登录状态已失效，请重新进入')
    const origin = session.value
    const generation = scopeGeneration, version = ++departmentRequestVersion
    const acceptsRequest = () => generation === scopeGeneration
      && version === departmentRequestVersion && session.value === origin
    let resolution: ReimbursementDepartmentResolution
    try {
      resolution = await selectDepartmentFromTravelApprovalRequest(selection, selectedDepartmentId)
    } catch (error) {
      if (!acceptsRequest()) throw new Error('登录或部门状态已变化，请在当前页面重新选择审批', { cause: error })
      // The server may already have changed the department before the response
      // was lost. Read its session; never repeat the department mutation blindly.
      try { await refreshMe() }
      catch {
        if (acceptsRequest()) {
          errorMessage.value = '暂时无法确认报销部门，请重新登录后继续，已保存内容会保留'
          status.value = 'error'
        }
      }
      throw error
    }
    if (!acceptsRequest()) {
      throw new Error('登录或部门状态已变化，请在当前页面重新选择审批')
    }
    session.value.departments = resolution.departments
    if (resolution.selectedDepartment) {
      const scopeChanged = session.value.selectedDepartment?.id !== resolution.selectedDepartment.id
      if (scopeChanged) {
        useExpenseStore().reset()
        useReimbursementDraftStore().reset()
        useReimbursementSubmissionStore().reset()
      }
      session.value.selectedDepartment = resolution.selectedDepartment
      status.value = 'authenticated'
    }
    const acceptedGeneration = scopeGeneration
    return { ...resolution, isCurrent: () => acceptedGeneration === scopeGeneration
      && version === departmentRequestVersion && session.value === origin }
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
    selectDepartmentFromTravelApproval,
    logout,
  }
})

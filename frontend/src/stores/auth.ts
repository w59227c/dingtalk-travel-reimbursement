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
import { apiErrorCode } from '@/api/errors'
import { setCsrfToken, setReimbursementContext, setUnauthorizedHandler } from '@/api/http'
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

const reimbursementTabKey = 'reimbursement.delegation'

function actualUserId(session: AuthSession | null): string | undefined {
  return session?.operator?.userId ?? session?.user.userId
}

function cachedContext(forUser: string): string | null {
  try {
    const raw = sessionStorage.getItem(reimbursementTabKey)
    if (!raw) return null
    const value = JSON.parse(raw) as { userId?: unknown; context?: unknown } | null
    return value && typeof value.context === 'string' && value.userId === forUser ? value.context : null
  } catch { return null }
}

function rememberContext(session: AuthSession | null): void {
  try {
    if (session?.operator && session.reimbursementContext) {
      sessionStorage.setItem(reimbursementTabKey, JSON.stringify({
        userId: session.operator.userId,
        context: session.reimbursementContext,
      }))
    } else sessionStorage.removeItem(reimbursementTabKey)
  } catch { /* A denied storage operation affects only reload restoration. */ }
}

export function isDelegationContextError(error: unknown): boolean {
  const code = apiErrorCode(error)
  return code === 'REIMBURSEMENT_DELEGATION_FORBIDDEN' || code === 'REIMBURSEMENT_CONTEXT_INVALID'
}

export const useAuthStore = defineStore('auth', () => {
  const status = ref<AuthStatus>('idle')
  const session = ref<AuthSession | null>(null)
  const errorMessage = ref('')
  const appTitle = ref('智能差旅费报销申请')
  const reimbursementContextNotice = ref('')
  let bootstrapPromise: Promise<void> | null = null
  let scopeGeneration = 0
  let identityReadVersion = 0
  let departmentRequestVersion = 0
  watch(() => [status.value, actualUserId(session.value), session.value?.user.userId, session.value?.selectedDepartment?.id,
    session.value?.selectedDepartment?.name, session.value?.csrfToken, session.value?.reimbursementContext],
  () => { scopeGeneration++ }, { flush: 'sync' })

  const initialized = computed(() => status.value !== 'idle' && status.value !== 'loading')
  const isAdmin = computed(() => session.value?.isAdmin === true)

  function sameSessionScope(left: AuthSession | null, right: AuthSession): boolean {
    return left?.user.userId === right.user.userId
      && actualUserId(left) === actualUserId(right)
      && left.selectedDepartment?.id === right.selectedDepartment?.id
      && left.selectedDepartment?.name === right.selectedDepartment?.name
  }

  function applySession(value: AuthSession): void {
    if (!sameSessionScope(session.value, value)) {
      useReimbursementDraftStore().reset()
      if (session.value !== null) {
        useExpenseStore().reset()
        useReimbursementSubmissionStore().reset()
      }
    }
    setReimbursementContext(value.reimbursementContext ?? null)
    setCsrfToken(value.csrfToken)
    rememberContext(value)
    session.value = value
    status.value = value.selectedDepartment ? 'authenticated' : 'department_required'
    errorMessage.value = ''
  }

  function applyReimbursementContext(value: AuthSession): void {
    if (!session.value || actualUserId(value) !== actualUserId(session.value)) {
      throw new Error('登录人已变化，请重新进入报销')
    }
    applySession(value)
    reimbursementContextNotice.value = ''
  }

  function clearAsUnauthorized(): void {
    useExpenseStore().reset()
    useReimbursementDraftStore().reset()
    useReimbursementSubmissionStore().reset()
    session.value = null
    setReimbursementContext(null)
    setCsrfToken(null)
    rememberContext(null)
    reimbursementContextNotice.value = ''
    if (status.value !== 'loading') status.value = 'unauthorized'
  }

  setUnauthorizedHandler(clearAsUnauthorized)

  async function refreshMe(): Promise<void> {
    const generation = scopeGeneration, version = ++identityReadVersion
    let value: AuthSession
    try {
      value = await getMe()
    } catch (error) {
      if (!session.value?.operator || !isDelegationContextError(error)
        || generation !== scopeGeneration || version !== identityReadVersion) throw error
      value = await getMe(null)
      if (generation !== scopeGeneration || version !== identityReadVersion) return
      reimbursementContextNotice.value = '代报权限或办理对象已变化，已返回本人报销'
    }
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
      if (session.value) {
        await refreshMe()
        return
      }
      setReimbursementContext(null)
      const ownSession = await getMe()
      const context = cachedContext(ownSession.user.userId)
      if (context && ownSession.canDelegateReimbursements) {
        try {
          const delegated = await getMe(context)
          if (delegated.operator?.userId === ownSession.user.userId && delegated.reimbursementContext) {
            applySession(delegated)
            return
          }
        } catch (error) {
          if (axios.isAxiosError(error) && error.response?.status === 401) throw error
          reimbursementContextNotice.value = isDelegationContextError(error)
            ? '代报权限或办理对象已变化，已返回本人报销'
            : '暂时无法恢复代报，已返回本人报销'
        }
      } else if (context) {
        reimbursementContextNotice.value = '代报权限已变化，已返回本人报销'
      }
      applySession(ownSession)
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
    if (resolution.reimbursementContext) {
      setReimbursementContext(resolution.reimbursementContext)
      session.value.reimbursementContext = resolution.reimbursementContext
      rememberContext(session.value)
    }
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
    reimbursementContextNotice,
    applyReimbursementContext,
    bootstrap,
    refreshMe,
    refreshPublicConfig,
    useDevelopmentMock,
    selectDepartmentFromTravelApproval,
    logout,
  }
})

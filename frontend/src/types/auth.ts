export interface Department {
  id: string
  name: string
}

export interface AuthUser {
  userId: string
  name: string
}

export interface AuthSession {
  user: AuthUser
  departments: Department[]
  selectedDepartment: Department | null
  isAdmin: boolean
  csrfToken: string
  canDelegateReimbursements?: boolean
  operator?: AuthUser
  reimbursementContext?: string
}

export interface ReimbursementEmployee extends AuthUser {
  departmentNames?: string[]
}

export interface ReimbursementDepartmentResolution {
  selectedDepartment: Department | null
  selectionRequired: boolean
  departments: Department[]
  existingDraftDepartment?: Department
  reservedDraftId?: string
  reimbursementContext?: string
}

export interface PublicConfig {
  appTitle?: string
  corpId: string
  clientId: string
  authMockEnabled: boolean
  oaSubmissionEnabled: boolean
  uploadLimits: ReceiptUploadLimits
  expenseLimits: ExpenseLimits
}

export interface ReceiptUploadLimits {
  maxFiles: number
  maxFileBytes: number
  maxSessionBytes: number
}

export interface ExpenseLimits {
  maxItems: number
}

export interface ApiEnvelope<T> {
  success: true
  data: T
}

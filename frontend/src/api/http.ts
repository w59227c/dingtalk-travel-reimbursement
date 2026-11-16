import axios from 'axios'

let csrfToken: string | null = null
let reimbursementContext: string | null = null
let authGeneration = 0
const requestAuthGenerations = new WeakMap<object, number>()
let unauthorizedHandler: (() => void) | null = null

export const http = axios.create({
  baseURL: '/api',
  timeout: 10_000,
  withCredentials: true,
  headers: {
    Accept: 'application/json',
  },
})

export function setCsrfToken(token: string | null): void {
  csrfToken = token
  authGeneration += 1
}

export function setReimbursementContext(value: string | null): void {
  if (reimbursementContext !== value) authGeneration += 1
  reimbursementContext = value
}

export function reimbursementContextHeaders(value?: string | null) {
  return value === undefined ? undefined : { 'X-Reimbursement-Context': value ?? false }
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

http.interceptors.request.use((config) => {
  requestAuthGenerations.set(config, authGeneration)
  const method = config.method?.toUpperCase()
  if (csrfToken && method && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    config.headers.set('X-CSRF-Token', csrfToken)
  }
  if (reimbursementContext && config.url &&
    /^\/(?:me(?:$|\/department\/)|reimbursements(?:\/|$)|oa\/(?:reimbursements(?:\/|$)|travel-approvals$)|calculate\/)/.test(config.url)) {
    config.headers.set('X-Reimbursement-Context', reimbursementContext, false)
  }
  return config
}, undefined, { synchronous: true })

http.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401
      && error.config && requestAuthGenerations.get(error.config) === authGeneration) {
      setCsrfToken(null)
      unauthorizedHandler?.()
    }
    return Promise.reject(error)
  },
)

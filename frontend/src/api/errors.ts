import axios from 'axios'

interface ErrorEnvelope {
  error?: {
    code?: unknown
    message?: unknown
  }
}

function errorEnvelope(error: unknown): ErrorEnvelope | null {
  if (!axios.isAxiosError(error)) return null
  const data: unknown = error.response?.data
  return data && typeof data === 'object' ? (data as ErrorEnvelope) : null
}

export function apiErrorCode(error: unknown): string | null {
  const code = errorEnvelope(error)?.error?.code
  return typeof code === 'string' && code ? code : null
}

export function apiErrorMessage(error: unknown, fallback: string): string {
  const message = errorEnvelope(error)?.error?.message
  if (typeof message === 'string' && message.trim()) return message.trim()
  if (error instanceof Error && error.message && !axios.isAxiosError(error)) return error.message
  return fallback
}


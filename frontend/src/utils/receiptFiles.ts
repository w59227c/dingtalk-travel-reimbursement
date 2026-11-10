import type { ReceiptUploadLimits } from '@/types/auth'

export const DEFAULT_RECEIPT_LIMITS: Readonly<ReceiptUploadLimits> = {
  maxFiles: 200,
  maxFileBytes: 20 * 1024 * 1024,
  maxSessionBytes: 100 * 1024 * 1024,
}

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KiB`
  return `${Number((bytes / (1024 * 1024)).toFixed(1))} MiB`
}

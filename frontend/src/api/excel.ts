export const DEFAULT_EXCEL_FILENAME = '差旅费报销单.xlsx'

function safeDownloadedFilename(value: string, fallback: string): string {
  const leaf = value
    .split(/[\\/]/)
    .at(-1)
    ?.split('')
    .filter((character) => {
      const code = character.charCodeAt(0)
      return code > 31 && code !== 127
    })
    .join('')
    .trim()
  if (!leaf || !leaf.toLowerCase().endsWith('.xlsx')) return fallback
  return leaf
}

export function filenameFromContentDisposition(
  header: string | undefined,
  fallback = DEFAULT_EXCEL_FILENAME,
): string {
  if (!header) return fallback
  const encoded = header.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1]
  if (encoded) {
    try {
      return safeDownloadedFilename(decodeURIComponent(encoded.trim()), fallback)
    } catch {
      return fallback
    }
  }
  const quoted = header.match(/filename\s*=\s*"([^"]+)"/i)?.[1]
  const plain = quoted ?? header.match(/filename\s*=\s*([^;]+)/i)?.[1]
  return plain ? safeDownloadedFilename(plain.trim(), fallback) : fallback
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  try {
    anchor.href = url
    anchor.download = filename
    anchor.hidden = true
    document.body.append(anchor)
    anchor.click()
  } finally {
    anchor.remove()
    URL.revokeObjectURL(url)
  }
}

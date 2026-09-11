import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  downloadBlob,
  filenameFromContentDisposition,
} from '@/api/excel'

describe('Excel download API', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('prefers the RFC 5987 filename and removes path components', () => {
    const encoded = encodeURIComponent('目录/差旅费报销单-测试.xlsx')
    expect(
      filenameFromContentDisposition(
        `attachment; filename=expense-report.xlsx; filename*=UTF-8''${encoded}`,
      ),
    ).toBe('差旅费报销单-测试.xlsx')
    expect(filenameFromContentDisposition('attachment; filename="unsafe.txt"')).toBe(
      '差旅费报销单.xlsx',
    )
  })

  it('downloads through a temporary object URL and always revokes it', () => {
    const createObjectURL = vi.fn(() => 'blob:test')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const blob = new Blob(['xlsx'])

    downloadBlob(blob, '差旅费报销单.xlsx')

    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(click).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test')
    expect(document.querySelector('a[download="差旅费报销单.xlsx"]')).toBeNull()
  })
})

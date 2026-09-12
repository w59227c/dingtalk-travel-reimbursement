interface AuthCodeResult {
  code: string
}

interface DingTalkCallbacks {
  corpId: string
  clientId?: string
  success?: (result: AuthCodeResult) => void
  fail?: (error: unknown) => void
}

interface DingTalkBridge {
  ready?: (callback: () => void) => void
  error?: (callback: (error: unknown) => void) => void
  requestAuthCode?: (
    input: DingTalkCallbacks,
  ) => Promise<AuthCodeResult> | AuthCodeResult | void
  downloadFile?: (
    input: NativeDownloadCallbacks,
  ) => Promise<NativeDownloadResult> | NativeDownloadResult | void
  openDocument?: (
    input: NativeOpenCallbacks,
  ) => Promise<Record<string, never>> | Record<string, never> | void
}

const AUTH_CODE_TIMEOUT_MS = 8_000
const DOCUMENT_OPEN_TIMEOUT_MS = 75_000

interface NativeDownloadResult {
  filePath?: string
}

interface NativeDownloadCallbacks {
  url: string
  header: Record<string, string>
  success?: (result: NativeDownloadResult) => void
  fail?: (error: unknown) => void
}

interface NativeOpenCallbacks {
  filePath: string
  fileType: string
  success?: (result: Record<string, never>) => void
  fail?: (error: unknown) => void
}

export interface DingTalkDocumentInput {
  url: string
  headers: Record<string, string>
  fileType: string
}

function readableError(error: unknown, fallback = '无法从钉钉获取免登授权码'): Error {
  if (error instanceof Error) return error
  if (error && typeof error === 'object') {
    const message = 'errorMessage' in error ? error.errorMessage : undefined
    if (typeof message === 'string' && message.trim()) return new Error(message)
  }
  return new Error(fallback)
}

export async function requestDingTalkAuthCode(corpId: string, clientId: string): Promise<string> {
  // Existing sessions and development Mock do not need the large DingTalk SDK.
  // Load it only when a real免登 handshake is actually required.
  const { default: dd } = await import('dingtalk-jsapi')
  const bridge = dd as unknown as DingTalkBridge
  return new Promise((resolve, reject) => {
    let settled = false
    const succeed = (result: AuthCodeResult) => {
      if (!settled && result?.code) {
        settled = true
        clearTimeout(timeout)
        resolve(result.code)
      }
    }
    const fail = (error: unknown) => {
      if (!settled) {
        settled = true
        clearTimeout(timeout)
        reject(readableError(error))
      }
    }
    const invoke = () => {
      const request = bridge.requestAuthCode
      if (!request) {
        fail(new Error('当前钉钉客户端不支持 dd.requestAuthCode，请升级客户端后重试'))
        return
      }
      try {
        const result = request({ corpId, clientId, success: succeed, fail })
        if (result && typeof (result as Promise<AuthCodeResult>).then === 'function') {
          void (result as Promise<AuthCodeResult>).then(succeed, fail)
        } else if (result && 'code' in result) {
          succeed(result)
        }
      } catch (error) {
        fail(error)
      }
    }

    const timeout = setTimeout(
      () => fail(new Error('钉钉免登响应超时，请从公司钉钉工作台重新打开本应用')),
      AUTH_CODE_TIMEOUT_MS,
    )
    bridge.error?.(fail)
    if (typeof bridge.ready === 'function') bridge.ready(invoke)
    else invoke()
  })
}

export async function downloadAndOpenDingTalkDocument(
  input: DingTalkDocumentInput,
): Promise<void> {
  const { default: dd } = await import('dingtalk-jsapi')
  const bridge = dd as unknown as DingTalkBridge
  return new Promise((resolve, reject) => {
    let settled = false
    const succeed = () => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      resolve()
    }
    const fail = (error: unknown) => {
      if (settled) return
      settled = true
      clearTimeout(timeout)
      reject(readableError(error, '钉钉文件预览失败，请升级钉钉后重试'))
    }
    const invoke = async () => {
      if (!bridge.downloadFile || !bridge.openDocument) {
        fail(new Error('当前钉钉客户端不支持文件预览，请升级钉钉后重试'))
        return
      }
      try {
        const downloaded = await invokeBridge<NativeDownloadResult>(bridge.downloadFile, {
          url: input.url,
          header: input.headers,
        })
        if (!downloaded.filePath) {
          throw new Error('钉钉未返回已下载文件，请升级钉钉后重试')
        }
        await invokeBridge<Record<string, never>>(bridge.openDocument, {
          filePath: downloaded.filePath,
          fileType: input.fileType,
        })
        succeed()
      } catch (error) {
        fail(error)
      }
    }
    const timeout = setTimeout(
      () => fail(new Error('钉钉文件下载超时，请检查网络后重试')),
      DOCUMENT_OPEN_TIMEOUT_MS,
    )
    bridge.error?.(fail)
    if (typeof bridge.ready === 'function') bridge.ready(() => void invoke())
    else void invoke()
  })
}

function invokeBridge<T>(
  method: (input: never) => Promise<T> | T | void,
  input: Record<string, unknown>,
): Promise<T> {
  return new Promise((resolve, reject) => {
    let settled = false
    const succeed = (result: T) => {
      if (settled) return
      settled = true
      resolve(result)
    }
    const fail = (error: unknown) => {
      if (settled) return
      settled = true
      reject(error)
    }
    try {
      const result = method({ ...input, success: succeed, fail } as never)
      if (result && typeof (result as Promise<T>).then === 'function') {
        void (result as Promise<T>).then(succeed, fail)
      } else if (result !== undefined) {
        succeed(result as T)
      }
    } catch (error) {
      fail(error)
    }
  })
}

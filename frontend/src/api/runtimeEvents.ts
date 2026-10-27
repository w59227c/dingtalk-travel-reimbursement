import { http } from '@/api/http'
import type { ApiEnvelope } from '@/types/auth'
import type { RuntimeEventPage, RuntimeEventSearch } from '@/types/runtimeEvents'

export async function listRuntimeEvents(params: RuntimeEventSearch = {}, signal?: AbortSignal): Promise<RuntimeEventPage> {
  const response = await http.get<ApiEnvelope<RuntimeEventPage>>('/admin/runtime-events', { params, signal })
  return response.data.data
}

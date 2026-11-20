import { http } from './http'
import type { ApiEnvelope } from '@/types/auth'
import type {
  ConfirmOaTemplateCatalogInput,
  GeneralOaTemplate,
  GeneralOaTemplateInspection,
  InspectOaTemplateCatalogInput,
  OaTemplateCatalog,
  OaTemplateCatalogInspection,
  OaTemplateCatalogState,
} from '@/types/oa'

const TEMPLATE_OPERATION_TIMEOUT_MS = 60_000

export async function getOaTemplateCatalog(): Promise<OaTemplateCatalogState> {
  const response = await http.get<ApiEnvelope<OaTemplateCatalogState>>(
    '/admin/oa/templates/catalog',
  )
  return response.data.data
}

export async function inspectOaTemplateCatalog(
  input: InspectOaTemplateCatalogInput,
): Promise<OaTemplateCatalogInspection> {
  const response = await http.post<ApiEnvelope<OaTemplateCatalogInspection>>(
    '/admin/oa/templates/catalog/inspect',
    input,
    { timeout: TEMPLATE_OPERATION_TIMEOUT_MS },
  )
  return response.data.data
}

export async function confirmOaTemplateCatalog(
  input: ConfirmOaTemplateCatalogInput,
): Promise<OaTemplateCatalog> {
  const response = await http.put<ApiEnvelope<OaTemplateCatalog>>(
    '/admin/oa/templates/catalog',
    input,
    { timeout: TEMPLATE_OPERATION_TIMEOUT_MS },
  )
  return response.data.data
}

export async function getGeneralOaTemplate(): Promise<GeneralOaTemplate | null> {
  const response = await http.get<ApiEnvelope<GeneralOaTemplate | null>>('/admin/oa/templates/general')
  return response.data.data
}

export async function inspectGeneralOaTemplate(processCode: string): Promise<GeneralOaTemplateInspection> {
  const response = await http.post<ApiEnvelope<GeneralOaTemplateInspection>>(
    '/admin/oa/templates/general/inspect', { processCode }, { timeout: TEMPLATE_OPERATION_TIMEOUT_MS },
  )
  return response.data.data
}

export async function confirmGeneralOaTemplate(input: {
  processCode: string
  schemaFingerprint: string
  expectedConfigVersion: number
}): Promise<GeneralOaTemplate> {
  const response = await http.put<ApiEnvelope<GeneralOaTemplate>>(
    '/admin/oa/templates/general', input, { timeout: TEMPLATE_OPERATION_TIMEOUT_MS },
  )
  return response.data.data
}

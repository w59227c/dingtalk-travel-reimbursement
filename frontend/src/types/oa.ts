export type OaTemplateCompatibilityStatus =
  | 'UNCONFIGURED'
  | 'COMPATIBLE'
  | 'CATALOG_CHANGED'
  | 'DRIFTED'

export interface OaFormOption {
  value: string
  label: string
  key: string | null
}

export interface OaRelatedTemplatePolicy {
  mode: string
  processCodes: string[]
}

export interface OaFormComponent {
  componentId: string
  componentType: string
  label: string
  bizAlias: string | null
  required: boolean
  disabled: boolean
  hidden: boolean
  ancestorDisabled: boolean
  ancestorHidden: boolean
  nested: boolean
  inSubtable: boolean
  unsupportedContainerAncestor: boolean
  format: string | null
  unit: string | null
  options: OaFormOption[]
  parentComponentId: string | null
  relatedTemplatePolicy: OaRelatedTemplatePolicy | null
  compatibleLogicalFields: string[]
}

export interface OaFormSchema {
  processCode: string
  formCode: string | null
  formUuid: string | null
  modifiedAt: string | null
  status: string
  templateName: string
  title: string
  schemaFingerprint: string
  components: OaFormComponent[]
}

export interface OaLogicalField {
  key: string
  label: string
  supportedComponentTypes: string[]
  required?: boolean
}

export type OaFieldMappings = Record<string, string>

export interface OaReimbursementTemplateInspection {
  processCode: string
  schema: OaFormSchema
  logicalFields: OaLogicalField[]
  mappings: OaFieldMappings | null
  confirmedSchemaFingerprint: string | null
}

export interface OaTravelTemplateInspection {
  profileKey: string
  displayName: string
  processCode: string
  schema: OaFormSchema
  logicalFields: OaLogicalField[]
  mappings: OaFieldMappings | null
  travelTypeOption: OaFormOption | null
  travelTypeMappings?: Record<string, OaFormOption>
  confirmedSchemaFingerprint: string | null
}

export interface OaTemplateCatalogInspection {
  configured: boolean
  compatibilityStatus: OaTemplateCompatibilityStatus
  requiresConfirmation: boolean
  isSubmissionReady: boolean
  configuredConfigVersion: number | null
  reimbursement: OaReimbursementTemplateInspection
  travelProfiles: OaTravelTemplateInspection[]
}

export interface OaReimbursementTemplateCatalog {
  processCode: string
  schema: OaFormSchema
  logicalFields: OaLogicalField[]
  mappings: OaFieldMappings
}

export interface OaTravelTemplateCatalog extends OaTravelTemplateInspection {
  schemaFingerprint: string
  mappings: OaFieldMappings
  travelTypeOption: OaFormOption
  confirmedSchemaFingerprint: string
}

export interface OaTemplateCatalog {
  configVersion: number
  processCode: string
  templateName: string
  schemaFingerprint: string
  confirmedSchemaFingerprint: string
  compatibilityStatus: OaTemplateCompatibilityStatus
  isSubmissionReady: boolean
  requiresConfirmation: boolean
  reimbursement: OaReimbursementTemplateCatalog
  travelProfiles: OaTravelTemplateCatalog[]
  allowedTravelProcessCodes: string[]
  lastCheckedAt: string
  confirmedAt: string
  updatedAt: string
}

export interface OaTemplateCatalogState {
  configured: boolean
  configVersion: number | null
  compatibilityStatus: OaTemplateCompatibilityStatus
  isSubmissionReady: boolean
  requiresConfirmation: boolean
  catalog: OaTemplateCatalog | null
}

export interface OaTravelProfileHeader {
  profileKey: string
  displayName: string
  processCode: string
}

export interface InspectOaTemplateCatalogInput {
  reimbursementProcessCode: string
  travelProfiles: OaTravelProfileHeader[]
}

export interface ConfirmOaReimbursementTemplateInput {
  processCode: string
  schemaFingerprint: string
  mappings: OaFieldMappings
}

export interface ConfirmOaTravelTemplateInput extends OaTravelProfileHeader {
  schemaFingerprint: string
  mappings: OaFieldMappings
  travelTypeOption: OaFormOption
  travelTypeMappings?: Record<string, OaFormOption>
}

export interface ConfirmOaTemplateCatalogInput {
  expectedConfigVersion: number | null
  reimbursement: ConfirmOaReimbursementTemplateInput
  travelProfiles: ConfirmOaTravelTemplateInput[]
}

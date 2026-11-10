import { http } from './http'

export interface SuccessResponse<T> {
  success: true
  data: T
}

export interface ReadinessData {
  status: 'ready'
  checks: {
    database: 'ok'
    excelTemplate: 'ok'
    tempStorage: 'ok'
    ocr: 'disabled' | 'development_fake' | 'configured'
  }
}

export async function fetchReadiness(): Promise<ReadinessData> {
  const response = await http.get<SuccessResponse<ReadinessData>>('/ready')
  return response.data.data
}

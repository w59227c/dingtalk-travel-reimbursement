import { http } from '@/api/http'
import type { ApiEnvelope } from '@/types/auth'
import type { AttendanceNoticePreview, AttendanceNoticeResult, AttendanceCalendarYear, AttendanceCalendarCorrection } from '@/types/attendance'

export async function listAttendanceCalendars(signal?: AbortSignal): Promise<AttendanceCalendarYear[]> {
  return (await http.get<ApiEnvelope<{ items: AttendanceCalendarYear[] }>>('/admin/attendance-calendars', { signal })).data.data.items
}
export async function parseAttendanceNotice(text: string, signal?: AbortSignal, correction?: AttendanceCalendarCorrection): Promise<AttendanceNoticePreview> {
  return (await http.post<ApiEnvelope<AttendanceNoticePreview>>('/admin/attendance-calendars/parse', { text, ...(correction ? { correction } : {}) }, { signal })).data.data
}
export async function applyAttendanceNotice(text: string, preview: AttendanceNoticePreview, signal?: AbortSignal, correction?: AttendanceCalendarCorrection): Promise<AttendanceNoticeResult> {
  return (await http.post<ApiEnvelope<AttendanceNoticeResult>>('/admin/attendance-calendars/apply', { text, ...(correction ? { correction } : {}), previewDigest: preview.previewDigest, expectedRevisions: preview.expectedRevisions }, { signal })).data.data
}
export async function deleteAttendanceCalendar(year: number, revision: string, signal?: AbortSignal): Promise<AttendanceCalendarYear[]> {
  return (await http.delete<ApiEnvelope<{ items: AttendanceCalendarYear[] }>>(`/admin/attendance-calendars/${year}`, { params: { revision }, signal })).data.data.items
}

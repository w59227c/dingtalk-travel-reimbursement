export interface AttendanceCalendar {
  years: number[]
  days: Record<string, { working: boolean; name: string }>
}
export interface AttendanceCalendarDay { date: string; working: boolean; name: string }
export interface AttendanceCorrectionRange { name: string; startDate: string; endDate: string; working: boolean }
export interface AttendanceCalendarCorrection { year: number; ranges: AttendanceCorrectionRange[] }
export interface AttendanceCalendarYear {
  year: number
  revision: string
  updatedAt: string
  days: AttendanceCalendarDay[]
  complete: boolean
  noticeTitle: string
  sourceYears: number[]
  legacy: boolean
}
export interface AttendanceNoticeIssue { message: string; source: string }
export interface AttendanceNoticeChange {
  year: number
  complete: boolean
  mode: 'annual' | 'adjacent'
  days: AttendanceCalendarDay[]
  added: AttendanceCalendarDay[]
  removed: AttendanceCalendarDay[]
  modified: { before: AttendanceCalendarDay; after: AttendanceCalendarDay }[]
  changed: boolean
}
export interface AttendanceNoticePreview {
  year: number | null
  title: string
  sourceFormat: 'notice' | 'timor' | 'holiday_cn' | 'unknown' | 'manual'
  sourceLabel: string
  canSave: boolean
  previewDigest: string
  expectedRevisions: Record<string, string | null>
  sections: { name: string; source: string; restDates: string[]; workDates: string[] }[]
  errors: AttendanceNoticeIssue[]
  warnings: AttendanceNoticeIssue[]
  changes: AttendanceNoticeChange[]
  unchanged: boolean
}
export interface AttendanceNoticeResult { items: AttendanceCalendarYear[]; year: number; unchanged: boolean }
export interface AttendanceSyncJob {
  year: number
  status: 'scheduled' | 'pending' | 'running' | 'retry' | 'completed' | 'paused'
  lastAttemptAt: string | null
  nextAttemptAt: string | null
  completedAt: string | null
  errorCode: string
  message: string
}
export interface AttendanceSyncStatus { enabled: boolean; startMonth: number; intervalDays: number; source: string; items: AttendanceSyncJob[] }

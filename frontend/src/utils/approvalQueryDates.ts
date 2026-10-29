const DAY_MS = 86400000
export const APPROVAL_QUERY_HINT = '按审批发起日期查询：默认最近 180 天，可查最近 365 个自然日，单次最多 180 天。'

export function shanghaiToday(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-US', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now)
  const part = (name: string) => parts.find((value) => value.type === name)!.value
  return `${part('year')}-${part('month')}-${part('day')}`
}

function dateKey(date: Date): string {
  return `${String(date.getFullYear()).padStart(4, '0')}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}

function validDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 10) === value
}

function earliestDate(today: string): string {
  return new Date(Date.parse(today) - 364 * DAY_MS).toISOString().slice(0, 10)
}

export function approvalQueryDateError(from: string, to: string, today = shanghaiToday()): string {
  if (!from && !to) return ''
  if (!from || !to) return '自定义日期范围需要同时填写开始和结束日期'
  if (!validDate(from) || !validDate(to)) return '请填写有效的查询日期'
  if (from > to) return '开始日期不能晚于结束日期'
  if (from < earliestDate(today)) return `仅支持查询最近 365 个自然日发起的审批，请选择 ${earliestDate(today)} 至 ${today} 内的日期；单次最多 180 天`
  if (to > today) return `审批查询结束日期不能晚于今天（${today}，上海时间）`
  if ((Date.parse(to) - Date.parse(from)) / DAY_MS >= 180) return '单次查询最多 180 天，请缩小日期范围'
  return ''
}

function outsideHistory(value: string, today: string): boolean {
  return value < earliestDate(today) || value > today
}

export function approvalQueryStartDisabled(date: Date, to: string, today = shanghaiToday()): boolean {
  const value = dateKey(date)
  return outsideHistory(value, today) || !!to && validDate(to) && (value > to || (Date.parse(to) - Date.parse(value)) / DAY_MS >= 180)
}

export function approvalQueryEndDisabled(date: Date, from: string, today = shanghaiToday()): boolean {
  const value = dateKey(date)
  return outsideHistory(value, today) || !!from && validDate(from) && (value < from || (Date.parse(value) - Date.parse(from)) / DAY_MS >= 180)
}

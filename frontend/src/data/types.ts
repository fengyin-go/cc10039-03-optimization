/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  回填数据统计?: BackfillStats
  回填完成记录?: BackfillCompletionRecord
  [field: string]: unknown
}

/** 回填任务完成后交付的完成记录：带任务和处理条数。 */
export type BackfillCompletionRecord = {
  seq: number
  task_id: string
  task_name: string
  table_name: string
  time_range: string
  record_count: number
}

/** 治理报告上的回填数据统计：明细是计入完成量的任务清单。 */
export type BackfillTaskDetail = {
  task_id: string
  task_name: string
  table_name: string
  time_range: string
  record_count: number
}

export type BackfillStats = {
  completed_backfills?: number
  completed_tasks: number
  total_records: number
  tasks: BackfillTaskDetail[]
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  entry?: EntryRow | null
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

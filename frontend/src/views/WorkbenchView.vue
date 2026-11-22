<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <span v-if="meta.key === 'backfill_job' && row.status === '回填中'" class="row-extra-actions">
              <input
                v-model="completeCounts[String(row.id)]"
                class="inline-input"
                :data-testid="`complete-count-${row.id}`"
                placeholder="处理条数"
              />
              <button
                class="link"
                type="button"
                :data-testid="`complete-${row.id}`"
                @click="completeRow(row)"
              >
                完成回填
              </button>
            </span>
            <button
              v-if="meta.key === 'backfill_job' && row.status === '已完成' && row.回填完成记录"
              class="link"
              type="button"
              :data-testid="`redeliver-${row.id}`"
              @click="redeliverCompletion(row)"
            >
              重送完成回执
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section
      v-if="meta.key === 'govern_report' && backfillStatsReports.length"
      class="backfill-stats"
      data-testid="backfill-stats"
    >
      <div
        v-for="report in backfillStatsReports"
        :key="`backfill-${report.id}`"
        class="backfill-stats-card"
        :data-testid="`backfill-stats-${report.id}`"
      >
        <h3 class="section-title">
          回填数据统计
          <small>报告 #{{ report.id }} · {{ report['报告周期'] }}</small>
        </h3>
        <div class="analysis-summary">
          <article class="analysis-card">
            <span class="stat-label">回填完成任务数</span>
            <strong class="stat-value" :data-testid="`backfill-completed-tasks-${report.id}`">
              {{ report.回填数据统计?.completed_tasks ?? 0 }}
            </strong>
          </article>
          <article class="analysis-card">
            <span class="stat-label">回填条数</span>
            <strong class="stat-value" :data-testid="`backfill-total-records-${report.id}`">
              {{ report.回填数据统计?.total_records ?? 0 }}
            </strong>
          </article>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>任务编号</th>
              <th>任务名称</th>
              <th>目标湖表</th>
              <th>回填时间范围</th>
              <th>处理条数</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="task in report.回填数据统计?.tasks ?? []"
              :key="`${report.id}-${task.task_id}`"
              :data-testid="`backfill-task-${report.id}-${task.task_id}`"
            >
              <td>{{ task.task_id }}</td>
              <td>{{ task.task_name || '—' }}</td>
              <td>{{ task.table_name || '—' }}</td>
              <td>{{ task.time_range || '—' }}</td>
              <td>{{ task.record_count }}</td>
            </tr>
            <tr v-if="!(report.回填数据统计?.tasks ?? []).length">
              <td colspan="5" class="empty-state">暂无计入的回填完成记录</td>
            </tr>
          </tbody>
        </table>
        <p class="page-foot">
          <span>
            明细相加 =
            {{ (report.回填数据统计?.tasks ?? []).reduce((sum, task) => sum + task.record_count, 0) }} 条，
            与回填条数一致
          </span>
        </p>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  completeBackfill,
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  redeliverCompletion as resendCompletion,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const completeCounts = ref<Record<string, string>>({})
// 治理报告额外展示回填数据统计，明细与总量同源；每份报告展示各自确认的任务
const backfillStatsReports = computed<EntryRow[]>(() =>
  rows.value.filter((item) => item.回填数据统计),
)
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function completeRow(row: EntryRow) {
  const raw = completeCounts.value[String(row.id)] ?? ''
  const count = Number(raw)
  if (!raw.trim() || !Number.isInteger(count) || count < 0) {
    errorMessage.value = '请填写处理条数（非负整数）'
    return
  }
  errorMessage.value = ''
  try {
    const result = await completeBackfill(Number(row.id), count)
    if (!result.ok) throw new Error(result.message)
    delete completeCounts.value[String(row.id)]
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '完成回填失败'
  }
}

async function redeliverCompletion(row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await resendCompletion(Number(row.id))
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重送完成回执失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

"""治理报告领域服务：定期生成湖内治理报告和自动优化建议。

治理报告是跨模块数据流的最终汇聚点：汇总冷热分层迁移、小文件合并、
保留策略清理、质量监控结果和血缘链路状态，生成综合治理报告。

回填数据统计采用增量同步：只消费回填完成记录台账中水位线之后新增的记录，
连续生成报告时不再重扫已确认的历史完成记录。同一任务的重复完成回执只计一次，
回填中的任务不进入完成量。
"""
from __future__ import annotations

import copy
import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GovernReportService(BaseModuleService):
    MODULE = "govern_report"
    ENTITY = "治理报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告周期", "治理指标", "优化建议", "执行状态", "审批状态"]
    REQUIRED_FIELDS = ["报告周期", "治理指标", "优化建议"]
    STATUS_ORDER = ["待生成", "已生成", "已审批", "已执行"]
    ACTION_RULES = {"生成报告": "已生成", "执行优化": "已执行"}
    NEGATIVE_ACTIONS = []

    # ---- 回填数据统计 ----

    @staticmethod
    def _recompute_stats(stats: dict[str, Any]) -> dict[str, Any]:
        """按任务明细复算计入的记录总量，保证总量与明细相加一致。"""
        tasks = [dict(task) for task in stats.get("tasks", [])]
        tasks.sort(key=lambda task: int(task.get("seq", 0)))
        total_records = sum(int(task.get("record_count", 0)) for task in tasks)
        return {
            **stats,
            "tasks": tasks,
            "completed_tasks": len(tasks),
            "total_records": total_records,
        }

    def sync_backfill_stats(self) -> dict[str, Any]:
        """增量同步回填数据统计。

        只读取上次确认水位线之后新增的回填完成事件，事件里的任务号再到
        完成记录台账取首份记录，重复回执不会重复累计。
        返回的是待确认副本，调用方确认后才会落库。
        """
        current = store.govern_backfill_stats()
        watermark = int(current.get("event_watermark", 0))
        # 先固定本次消费上限：期间新到的回填完成事件留给下一次报告
        upper_bound = store.event_log_length()

        tasks = {
            str(task["task_id"]): dict(task)
            for task in current.get("tasks", [])
        }
        for event in store.events_since(watermark):
            if int(event["id"]) > upper_bound:
                continue
            if event.get("event_type") != "backfill_completed_to_govern":
                continue
            payload = event.get("payload", {})
            task_id = str(payload.get("task_id", ""))
            if not task_id or task_id in tasks:
                continue
            # 回填中的任务不占完成量：还没跑完的任务即使有回执也不计入
            task_row = store.find("backfill_job", int(task_id)) if task_id.isdigit() else None
            if task_row is not None and task_row.get("status") in ("待调度", "回填中"):
                continue
            # 以完成记录台账里的首份记录为准，重复回执带来的条数不参与累计
            record = store.backfill_completion_for(task_id)
            if record is None:
                record = {
                    "task_id": task_id,
                    "task_name": payload.get("task_name", ""),
                    "table_name": payload.get("table_name", ""),
                    "time_range": payload.get("time_range", ""),
                    "record_count": int(payload.get("total_records", 0)),
                    "seq": int(payload.get("completion_seq", 0)),
                }
            tasks[task_id] = {
                "task_id": task_id,
                "task_name": record.get("task_name", ""),
                "table_name": record.get("table_name", ""),
                "time_range": record.get("time_range", ""),
                "record_count": int(record.get("record_count", 0)),
                "seq": int(record.get("seq", 0)),
            }

        synced = {
            "event_watermark": upper_bound,
            "completed_tasks": 0,
            "total_records": 0,
            "tasks": list(tasks.values()),
            "updated_at": datetime.datetime.now().isoformat(),
        }
        return self._recompute_stats(synced)

    @staticmethod
    def _stats_for_report(stats: dict[str, Any]) -> dict[str, Any]:
        """把内部统计整理成报告里的回填数据统计块，明细与总量同源。"""
        recomputed = GovernReportService._recompute_stats(stats)
        tasks = [
            {
                "task_id": task["task_id"],
                "任务编号": task["task_id"],
                "task_name": task.get("task_name", ""),
                "任务名称": task.get("task_name", ""),
                "table_name": task.get("table_name", ""),
                "time_range": task.get("time_range", ""),
                "record_count": task["record_count"],
                "处理条数": task["record_count"],
            }
            for task in recomputed["tasks"]
        ]
        return {
            "completed_backfills": recomputed["completed_tasks"],
            "completed_tasks": recomputed["completed_tasks"],
            "完成任务数": recomputed["completed_tasks"],
            "total_records": recomputed["total_records"],
            "回填条数": recomputed["total_records"],
            "tasks": tasks,
            "任务明细": tasks,
        }

    def current_backfill_stats(self) -> dict[str, Any]:
        """值班员查看的回填数据统计：按明细复算总量后返回。"""
        stats = self._recompute_stats(store.govern_backfill_stats())
        return copy.deepcopy(stats)

    def generate_report(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """生成治理报告：跨模块聚合数据生成综合治理报告。
        这是整个系统的最终数据产物消费入口。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "治理报告不存在"
        if entry.get("status") not in ("待生成", "已生成"):
            return None, f"治理报告状态为{entry.get('status')}，不可生成"

        # 0) 增量同步回填完成记录（先算后确认；中途退出不落库）
        # 生成中新到的回填不进本次统计，留给之后的报告，避免中途重试换一组任务
        draft = store.govern_backfill_draft(entry_id)
        if draft is None:
            draft = self.sync_backfill_stats()
            store.stage_govern_backfill_draft(entry_id, draft)
        synced_backfill_stats = draft
        backfill_summary = self._stats_for_report(synced_backfill_stats)

        # 1) 汇总冷热分层迁移
        tier_events = store.events_for_module("storage_tier")
        tier_migrations = [e for e in tier_events if e["event_type"] == "tier_migration_executed"]
        total_migrated = sum(
            e["payload"].get("migrated_partition_count", 0) for e in tier_migrations
        )

        # 2) 汇总小文件合并
        compaction_events = store.events_for_module("file_compaction")
        completed_compactions = [e for e in compaction_events if e["event_type"] == "compaction_completed"]

        # 3) 汇总保留策略清理
        retention_events = store.events_for_module("retention_mgr")
        completed_cleanups = [e for e in retention_events if e["event_type"] == "retention_cleanup_executed"]

        # 4) 汇总质量监控结果
        quality_events = store.events_for_module("quality_monitor")
        quality_failures = [e for e in quality_events if e["event_type"] == "quality_failed_to_lineage"]
        quality_passes = [e for e in quality_events if e["event_type"] == "quality_passed_to_govern"]

        # 5) 汇总血缘链路状态
        lineage_rows = store.rows("lineage_graph")
        broken_lineages = [l for l in lineage_rows if l.get("status") == "链路断裂"]
        uncertain_lineages = [l for l in lineage_rows if l.get("status") == "待验证"]

        # 组装报告
        report_data = {
            "report_period": entry.get("报告周期", ""),
            "generated_at": datetime.datetime.now().isoformat(),
            "storage_governance": {
                "tier_migrations": len(tier_migrations),
                "total_partitions_migrated": total_migrated,
                "completed_compactions": len(completed_compactions),
                "completed_cleanups": len(completed_cleanups),
            },
            "quality_monitoring": {
                "quality_checks": len(quality_passes) + len(quality_failures),
                "failures": len(quality_failures),
                "passes": len(quality_passes),
            },
            "lineage_health": {
                "total_lineages": len(lineage_rows),
                "broken": len(broken_lineages),
                "uncertain": len(uncertain_lineages),
                "healthy": len(lineage_rows) - len(broken_lineages) - len(uncertain_lineages),
            },
            "backfill_summary": backfill_summary,
        }

        # 计算优化建议
        suggestions = []
        if len(broken_lineages) > 0:
            suggestions.append(f"修复 {len(broken_lineages)} 条断裂血缘链路")
        if len(quality_failures) > 0:
            suggestions.append(f"处理 {len(quality_failures)} 个质量不达标指标")
        if total_migrated > 0:
            suggestions.append(f"冷热迁移有效，已迁移 {total_migrated} 个分区")
        if len(completed_compactions) > 0:
            suggestions.append(f"小文件合并完成 {len(completed_compactions)} 个任务")

        # 确认回填统计：报告到这里才算完整生成，中途退出则保持原水位线
        store.save_govern_backfill_stats(synced_backfill_stats)
        store.discard_govern_backfill_draft(entry_id)

        entry["status"] = "已生成"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["优化建议"] = "；".join(suggestions) if suggestions else "当前治理状态良好"
        entry["执行状态"] = "报告已生成"
        entry["回填数据统计"] = copy.deepcopy(backfill_summary)

        store.log_event("govern_report_generated", "govern_report", "govern_report", {
            "report_id": entry_id,
            "report_data": report_data,
        })

        return entry, (
            f"治理报告已生成：迁移分区={total_migrated}，质量检查={len(quality_passes) + len(quality_failures)}，"
            f"血缘链路={len(lineage_rows)}，回填任务={backfill_summary['completed_tasks']}，"
            f"回填条数={backfill_summary['total_records']}"
        )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """读取报告时按回填数据统计上的任务明细复算记录总量。

        已生成的报告以自己确认过的那组任务为准；还没生成过的报告读台账里
        已确认的最新统计，只作为预览，不写回报告记录。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        stats = entry.get("回填数据统计")
        if stats is None:
            preview = dict(entry)
            preview["回填数据统计"] = self._stats_for_report(store.govern_backfill_stats())
            return preview
        entry["回填数据统计"] = self._stats_for_report(stats)
        return entry

    def list_entries(self, **kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        """列表同样按各自报告确认过的任务明细复算，重新进入页面即可核对。"""
        rows, total = super().list_entries(**kwargs)
        result = []
        for row in rows:
            item = dict(row)
            if item.get("回填数据统计") is not None:
                item["回填数据统计"] = self._stats_for_report(item["回填数据统计"])
            result.append(item)
        return result, total

    def execute_optimization(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行治理优化：根据报告建议触发实际的治理操作。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "治理报告不存在"
        if entry.get("status") not in ("已生成", "已审批"):
            return None, f"治理报告状态为{entry.get('status')}，不可执行优化"

        # 触发血缘链路修复
        broken_lineages = [
            l for l in store.rows("lineage_graph")
            if l.get("status") == "链路断裂"
        ]
        for lin in broken_lineages:
            lin["pending"] = True

        # 触发质量监控重新检查
        quality_rules = store.rows("quality_monitor")
        for qr in quality_rules:
            if qr.get("status") == "已启用":
                qr["pending"] = True

        entry["status"] = "已执行"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["执行状态"] = "优化已执行"

        store.log_event("govern_optimization_executed", "govern_report", "govern_report", {
            "report_id": entry_id,
            "broken_lineages_triggered": len(broken_lineages),
            "quality_rules_retriggered": len(quality_rules),
        })

        return entry, f"治理优化已执行：触发 {len(broken_lineages)} 条血缘修复、{len(quality_rules)} 个质量规则"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "生成报告":
            return self.generate_report(entry_id)
        if action == "执行优化":
            return self.execute_optimization(entry_id)
        return super().run_action(entry_id, action)
"""数据回填任务领域服务：管理历史数据回填、补数和断点续做。

支持回填检查点（checkpoint）：中断后可从上次位置继续，不会重复处理已完成的记录。
上游接收 Schema 演进的不兼容变更事件，下游联动治理报告记录回填结果。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "回填任务"
    OPERATION_TERM = "调度"
    LIST_FIELDS = ["任务名称", "目标湖表", "回填时间范围", "处理记录数", "执行状态"]
    REQUIRED_FIELDS = ["任务名称", "目标湖表", "回填时间范围"]
    STATUS_ORDER = ["待调度", "回填中", "已完成", "回填失败"]
    ACTION_RULES = {"启动回填": "回填中", "取消回填": "回填失败"}
    NEGATIVE_ACTIONS = []

    # 回填检查点：任务 id → 已处理记录偏移
    _checkpoints: dict[int, int] = {}

    def start_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """启动回填任务：检查 Schema 变更状态后启动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "待调度":
            return None, f"回填任务状态为{entry.get('status')}，不可启动"

        table_name = entry.get("目标湖表", "")
        time_range = entry.get("回填时间范围", "")

        # 检查同湖表是否有执行中的回填
        active_backfills = [
            bj for bj in store.rows(self.MODULE)
            if bj.get("目标湖表") == table_name
            and bj.get("status") == "回填中"
            and bj.get("id") != entry_id
        ]
        if active_backfills:
            return None, f"湖表 {table_name} 已有回填任务在执行中"

        # 初始化检查点
        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["处理记录数"] = str(checkpoint)

        store.log_event("backfill_started", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "table_name": table_name,
            "time_range": time_range,
            "checkpoint": checkpoint,
        })

        return entry, f"回填任务 {entry.get('任务名称')} 已启动，时间范围={time_range}，检查点从 {checkpoint} 开始"

    def checkpoint_backfill(self, entry_id: int, processed_count: int) -> tuple[dict[str, Any] | None, str]:
        """更新回填检查点，支持断点续做。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可保存检查点"

        self._checkpoints[entry_id] = processed_count
        entry["处理记录数"] = str(processed_count)

        store.log_event("backfill_checkpoint", "backfill_job", "backfill_job", {
            "task_id": entry_id,
            "checkpoint": processed_count,
        })

        return entry, f"回填任务检查点已更新：已处理 {processed_count} 条记录"

    def complete_backfill(self, entry_id: int, total_records: int) -> tuple[dict[str, Any] | None, str]:
        """完成回填任务，交付一份带任务和处理条数的回填完成记录。

        完成记录是治理报告汇总回填完成量和回填条数的唯一凭据；同一任务
        重复提交完成（重复回执）时复用首份记录，不会产生第二条。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"

        # 重复回执：任务已完成且已有完成记录，按首份记录幂等返回
        if entry.get("status") == "已完成":
            record = store.backfill_completion_for(str(entry_id))
            if record is not None:
                store.log_event("backfill_completed_to_govern", "backfill_job", "govern_report", {
                    "task_id": entry_id,
                    "task_name": record.get("task_name", ""),
                    "table_name": record.get("table_name", ""),
                    "total_records": record.get("record_count", 0),
                    "completion_seq": record.get("seq", 0),
                    "duplicated": True,
                })
                return entry, (
                    f"回填任务 {entry.get('任务名称')} 已完成，"
                    f"重复完成回执已按任务 {entry_id} 去重，累计仍为 {record.get('record_count', 0)} 条记录"
                )
            return None, f"回填任务状态为{entry.get('status')}，不可完成"

        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可完成"

        table_name = entry.get("目标湖表", "")

        entry["status"] = "已完成"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["处理记录数"] = str(total_records)

        # 清理检查点
        self._checkpoints.pop(entry_id, None)

        # 交付回填完成记录（同任务重复完成只保留首份）
        record, duplicated = store.record_backfill_completion(
            str(entry_id),
            task_name=entry.get("任务名称", ""),
            table_name=table_name,
            time_range=entry.get("回填时间范围", ""),
            record_count=total_records,
        )
        entry["回填完成记录"] = dict(record)

        # 通知治理报告
        store.log_event("backfill_completed_to_govern", "backfill_job", "govern_report", {
            "task_id": entry_id,
            "task_name": record["task_name"],
            "table_name": table_name,
            "total_records": record["record_count"],
            "completion_seq": record["seq"],
            "duplicated": duplicated,
        })

        if duplicated:
            return entry, (
                f"回填任务 {entry.get('任务名称')} 已完成，"
                f"重复完成回执已按任务 {entry_id} 去重，累计仍为 {record['record_count']} 条记录"
            )
        return entry, f"回填任务 {entry.get('任务名称')} 已完成，总计 {total_records} 条记录"

    def redeliver_completion(
        self, entry_id: int, total_records: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """重送一条回填完成回执：用于完成记录重复送达的场景，按任务号幂等。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"

        record = store.backfill_completion_for(str(entry_id))
        if record is None:
            return None, f"回填任务 {entry_id} 还没有完成记录，无法重送回执"

        record_count = record.get("record_count", 0)
        _, duplicated = store.record_backfill_completion(
            str(entry_id),
            task_name=record.get("task_name", ""),
            table_name=record.get("table_name", ""),
            time_range=record.get("time_range", ""),
            record_count=record_count,
        )
        store.log_event("backfill_completed_to_govern", "backfill_job", "govern_report", {
            "task_id": entry_id,
            "task_name": record.get("task_name", ""),
            "table_name": record.get("table_name", ""),
            "total_records": record_count,
            "completion_seq": record.get("seq", 0),
            "duplicated": duplicated,
        })

        return entry, (
            f"回填任务 {entry.get('任务名称')} 的完成回执已重送，"
            f"按任务 {entry_id} 去重后仍为 {record_count} 条记录"
        )

    def completion_record_for(self, entry_id: int) -> dict[str, Any] | None:
        """查询任务对应的回填完成记录。"""
        return store.backfill_completion_for(str(entry_id))

    def completion_records(self) -> list[dict[str, Any]]:
        """交付给治理报告的全部回填完成记录，按确认顺序排列。"""
        return store.backfill_completion_records()

    def fail_backfill(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回填失败：保留检查点以便恢复。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "回填任务不存在"
        if entry.get("status") != "回填中":
            return None, f"回填任务状态为{entry.get('status')}，不可标记失败"

        checkpoint = self._checkpoints.get(entry_id, 0)

        entry["status"] = "回填失败"
        entry["pending"] = False
        entry["abnormal"] = True

        store.log_event("backfill_failed", "backfill_job", "retention_mgr", {
            "task_id": entry_id,
            "failed_at_checkpoint": checkpoint,
        })

        return entry, f"回填任务失败，检查点保留在 {checkpoint} 条，可从该位置恢复"

    def get_checkpoint(self, entry_id: int) -> int | None:
        """获取回填检查点。"""
        return self._checkpoints.get(entry_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启动回填":
            return self.start_backfill(entry_id)
        return super().run_action(entry_id, action)
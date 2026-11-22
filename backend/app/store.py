"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
支持跨模块查询、快照记录和流水日志，用于支撑跨模块数据流。
"""
from __future__ import annotations

import copy
import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 跨模块事件日志：记录所有跨模块操作
        self._events: list[dict[str, Any]] = []
        # 模块快照备份：用于检查点和恢复
        self._snapshots: dict[str, list[dict[str, Any]]] = {}
        # 回填完成记录台账：task_id → 唯一的完成记录（重复回执只保留一份）
        self._backfill_completion_records: dict[str, dict[str, Any]] = {}
        # 回填完成记录按确认顺序追加，seq 单调递增供治理报告追溯
        self._backfill_completion_log: list[dict[str, Any]] = []
        self._backfill_completion_seq = 0
        # 治理报告的回填数据统计：event_watermark 之前的事件已确认计入，不再重复扫描
        self._govern_backfill_stats: dict[str, Any] = {
            "event_watermark": 0,
            "completed_tasks": 0,
            "total_records": 0,
            "tasks": [],
            "updated_at": None,
        }
        # 生成中的报告草稿：中途退出后重试仍复用首次确认的那组任务
        self._govern_backfill_drafts: dict[int, dict[str, Any]] = {}

    # ---- 基础读写 ----

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: str) -> list[dict[str, Any]]:
        """按指定字段值查找记录，支持跨模块关联查询。"""
        return [
            row for row in self.rows(module)
            if str(row.get(field, "")).casefold() == value.casefold()
        ]

    def upsert(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        """插入或更新记录；存在相同 id 时覆盖，否则追加。"""
        rows = self.rows(module)
        entry_id = entry.get("id")
        for i, row in enumerate(rows):
            if row.get("id") == entry_id:
                rows[i] = entry
                return entry
        rows.append(entry)
        return entry

    # ---- 跨模块事件 ----

    def log_event(
        self, event_type: str, source_module: str, target_module: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """记录跨模块操作事件，供血缘和审计追溯。"""
        event = {
            "id": len(self._events) + 1,
            "event_type": event_type,
            "source_module": source_module,
            "target_module": target_module,
            "timestamp": datetime.datetime.now().isoformat(),
            "payload": payload,
        }
        self._events.append(event)
        return event

    def events_for_module(self, module: str) -> list[dict[str, Any]]:
        """查询与指定模块相关的所有事件。"""
        return [
            e for e in self._events
            if e["source_module"] == module or e["target_module"] == module
        ]

    def events_between(self, source: str, target: str) -> list[dict[str, Any]]:
        """查询两个模块之间的单向事件。"""
        return [
            e for e in self._events
            if e["source_module"] == source and e["target_module"] == target
        ]

    def event_log_length(self) -> int:
        """当前事件日志长度，可作为增量消费的水位线。"""
        return len(self._events)

    def events_since(self, start: int) -> list[dict[str, Any]]:
        """读取水位线之后新增的事件，不重复扫描历史日志。"""
        if start < 0:
            start = 0
        return list(self._events[start:])

    # ---- 回填完成记录 ----

    def record_backfill_completion(
        self,
        task_id: str,
        *,
        task_name: str = "",
        table_name: str = "",
        time_range: str = "",
        record_count: int = 0,
        source: str = "backfill_job",
    ) -> tuple[dict[str, Any], bool]:
        """登记一条回填完成记录。

        同一任务重复送到完成回执时只保留首次确认的记录，返回 duplicated=True，
        调用方据此避免在任务数和条数上重复累计。
        """
        key = str(task_id)
        existing = self._backfill_completion_records.get(key)
        if existing is not None:
            return existing, True

        self._backfill_completion_seq += 1
        record = {
            "seq": self._backfill_completion_seq,
            "task_id": key,
            "task_name": task_name,
            "table_name": table_name,
            "time_range": time_range,
            "record_count": int(record_count),
            "source": source,
            "recorded_at": datetime.datetime.now().isoformat(),
        }
        self._backfill_completion_records[key] = record
        self._backfill_completion_log.append(record)
        return record, False

    def backfill_completion_records(self) -> list[dict[str, Any]]:
        """按登记顺序返回全部已确认的回填完成记录（副本，避免外部改写台账）。"""
        return [dict(record) for record in self._backfill_completion_log]

    def backfill_completion_for(self, task_id: str) -> dict[str, Any] | None:
        record = self._backfill_completion_records.get(str(task_id))
        return dict(record) if record is not None else None

    def govern_backfill_stats(self) -> dict[str, Any]:
        """返回治理报告已确认的回填数据统计（不重扫历史完成记录）。"""
        return self._govern_backfill_stats

    def save_govern_backfill_stats(self, stats: dict[str, Any]) -> None:
        """确认一份回填统计落库。

        水位线只前进不后退，任务明细按任务号并集保留，避免先生成、后确认的
        旧快照把已确认的任务覆盖掉，造成后续报告再次扫描历史完成记录。
        """
        previous = self._govern_backfill_stats
        merged: dict[str, Any] = {}
        for task in [*previous.get("tasks", []), *stats.get("tasks", [])]:
            merged.setdefault(str(task["task_id"]), task)
        tasks = sorted(merged.values(), key=lambda task: int(task.get("seq", 0)))
        self._govern_backfill_stats = {
            **stats,
            "event_watermark": max(
                int(previous.get("event_watermark", 0)),
                int(stats.get("event_watermark", 0)),
            ),
            "tasks": [dict(task) for task in tasks],
            "completed_tasks": len(tasks),
            "total_records": sum(int(task.get("record_count", 0)) for task in tasks),
        }

    def govern_backfill_draft(self, report_id: int) -> dict[str, Any] | None:
        """取生成中报告已固定的回填统计草稿，用于中途退出后的重试。"""
        return self._govern_backfill_drafts.get(int(report_id))

    def stage_govern_backfill_draft(self, report_id: int, stats: dict[str, Any]) -> None:
        self._govern_backfill_drafts[int(report_id)] = stats

    def discard_govern_backfill_draft(self, report_id: int) -> None:
        self._govern_backfill_drafts.pop(int(report_id), None)

    # ---- 快照与检查点 ----

    def create_snapshot(self, module: str) -> str:
        """为指定模块创建深拷贝快照，返回快照标识。"""
        timestamp = datetime.datetime.now().isoformat()
        key = f"{module}@{timestamp}"
        self._snapshots[key] = copy.deepcopy(self.rows(module))
        return key

    def get_snapshot(self, key: str) -> list[dict[str, Any]] | None:
        """按标识获取快照副本。"""
        raw = self._snapshots.get(key)
        return copy.deepcopy(raw) if raw is not None else None

    def snapshot_keys(self) -> list[str]:
        return sorted(self._snapshots)

    # ---- 概览 ----

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    def cross_module_stats(self) -> dict[str, object]:
        """跨模块统计：事件数、快照数、关联模块对。"""
        pairs: set[tuple[str, str]] = set()
        for e in self._events:
            pairs.add((e["source_module"], e["target_module"]))
        return {
            "event_count": len(self._events),
            "snapshot_count": len(self._snapshots),
            "linked_pairs": len(pairs),
            "linked_modules": sorted(set(
                m for p in pairs for m in p
            )),
        }


store = Store()

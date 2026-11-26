"""维度快照领域服务：冻结校验维度全量状态，产出快照版本数据集。

快照能否继续，取决于共享闭链判定（domain.closure）与闭链台账的送交确认：
- 只有可核对到一次「有效闭链」，且该闭链已经过接手人确认，快照页才能继续使用；
- 闭链编号与拉链页保持同一份；
- 快照草稿（快照编号、快照日期）可按原编号回查，区间冲突时给出能否继续发布的说明。
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.domain.closure import evaluate_chain_closure, next_available_interval
from app.services.base import BaseModuleService
from app.services.closure_ledger import closure_ledger
from app.store import store


class DimSnapshotService(BaseModuleService):
    MODULE = "dim_snapshot"
    ENTITY = "快照版本"
    LIST_FIELDS = ["快照编号", "所属维度", "快照日期", "维度行数", "当前链占比", "快照摘要值", "引用批次", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "所属维度", "快照日期"]
    STATUS_ORDER = ["生成中", "可用", "已过期", "已重建"]
    ACTION_RULES = {"标记可用": "可用", "标记过期": "已过期", "重建快照": "已重建"}
    NEGATIVE_ACTIONS = []

    def freeze_snapshot(
        self, snapshot_id: str, dim_name: str, snapshot_date: str,
        ref_batch: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """冻结维度快照：计算当前维度全量状态。"""
        scd_rows = store.rows("scd_chain")
        dim_rows = [
            r for r in scd_rows
            if r.get("所属维度") == dim_name
        ]
        total = len(dim_rows)
        current_count = sum(1 for r in dim_rows if r.get("是否当前") == "是")
        ratio = current_count / total if total > 0 else 0.0

        checksum = hashlib.md5(
            str(sorted(r.get("代理键", "") for r in dim_rows)).encode()
        ).hexdigest()[:8]

        rows = store.rows(self.MODULE)
        entry = {
            "快照编号": snapshot_id, "所属维度": dim_name,
            "快照日期": snapshot_date, "维度行数": total,
            "当前链占比": f"{ratio:.1%}", "快照摘要值": checksum,
            "引用批次": ref_batch, "生成时间": snapshot_date,
            "status": "可用", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"维度 {dim_name} 快照已生成：{total} 行", entry

    def verify_snapshot_consistency(
        self, dim_name: str, snapshot_date: str,
    ) -> tuple[bool, str, int]:
        """验证同一快照日期的维度行数一致性。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("所属维度") == dim_name and r.get("快照日期") == snapshot_date:
                count = int(r.get("维度行数", 0))
                return True, f"快照一致：{count} 行", count
        return False, f"维度 {dim_name} 无 {snapshot_date} 快照", 0

    # ------------------------------------------------------------------
    # 闭链送交 / 接手确认 / 快照可用性
    # ------------------------------------------------------------------
    def send_closure_handoff(
        self, chain_id: str, dim_name: str, snapshot_id: str, snapshot_date: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """跨模块送交：把一次有效闭链送交维度快照，等待接手确认。"""
        return closure_ledger.register_handoff(
            chain_id, dim_name, snapshot_id, snapshot_date)

    def confirm_handoff(self, handoff_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        """接手人确认送交；确认后快照页才能使用对应闭链结论。"""
        return closure_ledger.confirm_handoff(handoff_id)

    def check_closure_for_snapshot(self, chain_id: str) -> dict[str, Any]:
        """快照页核对一次闭链是否有效、是否已确认、当前可用区间是什么。

        只有可核对到一次有效闭链时才返回 usable=True，页面据此放行后续动作。
        """
        verdict = evaluate_chain_closure(chain_id)
        closure = closure_ledger.get_closure(chain_id)
        handoffs = closure_ledger.list_handoffs(chain_id)
        confirmed = next(
            (h for h in handoffs if h["status"] == "已确认"), None)
        conflicted = next(
            (h for h in handoffs if h["status"] == "冲突失效"), None)

        usable = verdict.valid and confirmed is not None
        interval = next_available_interval(
            str(find_dim_for_chain(chain_id))) if usable else ("", "")

        message: str
        can_continue_publish = False
        if conflicted is not None and not verdict.valid:
            # 先前已受理的草稿，其闭链后来被另一窗口标记区间冲突
            message = ("先前已受理的快照草稿对应闭链已被另一窗口标记区间冲突，"
                       "不能按原区间继续发布，请调整区间后重新送交")
        elif not verdict.valid:
            message = f"未核对到有效闭链：{verdict.reason}，快照暂不能继续"
        elif confirmed is None:
            message = "已核对到有效闭链，但尚未完成接手确认，确认后才能使用"
        else:
            can_continue_publish = True
            message = (f"闭链 {verdict.closure_no} 有效且已确认，"
                       f"可按区间 {interval[0]}~{interval[1]} 继续发布")

        return {
            "chain_id": chain_id,
            "closure_no": verdict.closure_no,
            "verdict": verdict.as_dict(),
            "handoffs": handoffs,
            "confirmed_handoff": confirmed,
            "usable": usable,
            "can_continue_publish": can_continue_publish,
            "available_interval": {"start": interval[0], "end": interval[1]},
            "message": message,
        }

    def lookup_draft(self, snapshot_id: str) -> dict[str, Any]:
        """返回快照草稿能以原编号查到的闭链核对结果（供重开/刷新后继续核对）。"""
        handoff = next(
            (h for h in closure_ledger.list_handoffs()
             if h["snapshot_id"] == snapshot_id), None)
        if handoff is None:
            return {
                "snapshot_id": snapshot_id,
                "found": False,
                "message": f"未找到快照编号 {snapshot_id} 的闭链送交记录",
            }
        check = self.check_closure_for_snapshot(handoff["chain_id"])
        return {
            "snapshot_id": snapshot_id,
            "found": True,
            "snapshot_date": handoff["snapshot_date"],
            "handoff_id": handoff["handoff_id"],
            "handoff_status": handoff["status"],
            **check,
        }


def find_dim_for_chain(chain_id: str) -> str:
    from app.domain.closure import find_chain_row
    row = find_chain_row(chain_id)
    return str(row.get("所属维度", "")) if row else ""

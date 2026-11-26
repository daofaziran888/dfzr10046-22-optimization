"""闭链判定共享领域模块。

同一份闭链判定同时服务于「拉链加工」与「维度快照」两个模块：
- 拉链页用它判定一行拉链当前是否处于有效闭链；
- 快照页用它核对一次闭链结论能否被继续使用。

规则只在本文件实现一次，避免两个模块各写一份导致口径漂移。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.store import store

SCD_MODULE = "scd_chain"
# 有效闭链只认这一状态；区间冲突、已作废、加工中都不能作为闭链结论使用。
CLOSED_STATUS = "已闭链"
CONFLICT_STATUS = "区间冲突"


@dataclass
class ClosureVerdict:
    """一次闭链核对的结论，对拉链页和快照页结构完全一致。"""

    valid: bool
    reason: str
    chain_id: str
    closure_no: str | None = None
    status: str = ""
    interval: tuple[str, str] = ("", "")
    abnormal: bool = False
    # 该结论是否已经由快照侧（接手人）确认；未确认前快照页不得直接使用。
    handoff_confirmed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "reason": self.reason,
            "chain_id": self.chain_id,
            "closure_no": self.closure_no,
            "status": self.status,
            "interval_start": self.interval[0],
            "interval_end": self.interval[1],
            "abnormal": self.abnormal,
            "handoff_confirmed": self.handoff_confirmed,
            **self.extra,
        }


def _parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def find_chain_row(chain_id: str) -> dict[str, Any] | None:
    for row in store.rows(SCD_MODULE):
        if row.get("拉链编号") == chain_id:
            return row
    return None


def chain_interval(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("生效起日", "")), str(row.get("生效止日", ""))


def interval_overlaps(
    start_a: str, end_a: str, start_b: str, end_b: str,
) -> bool:
    """半开区间重叠判定：[start, end)，首尾相接不算重叠。"""
    a_start, a_end = _parse_day(start_a), _parse_day(end_a)
    b_start, b_end = _parse_day(start_b), _parse_day(end_b)
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start < b_end and b_start < a_end


def evaluate_chain_closure(chain_id: str) -> ClosureVerdict:
    """对单条拉链做闭链判定（不依赖闭链台账，纯按拉链当前状态计算）。

    兼容历史数据：种子里已经是「已闭链」的记录没有闭链台账条目，
    仍可通过本函数核对出有效闭链，闭链编号沿用拉链编号保持一致。
    """
    row = find_chain_row(chain_id)
    if row is None:
        return ClosureVerdict(False, f"拉链 {chain_id} 不存在", chain_id)

    status = str(row.get("status", ""))
    interval = chain_interval(row)
    abnormal = bool(row.get("abnormal"))
    closure_no = row.get("闭链编号") or chain_id

    if status != CLOSED_STATUS:
        reason = f"拉链状态为「{status or '加工中'}」，未形成有效闭链"
        return ClosureVerdict(False, reason, chain_id, closure_no, status,
                              interval, abnormal)
    if abnormal or status == CONFLICT_STATUS:
        return ClosureVerdict(False, "拉链已被标记区间冲突，闭链结论失效",
                              chain_id, closure_no, status, interval, abnormal)

    start_dt, end_dt = _parse_day(interval[0]), _parse_day(interval[1])
    if start_dt is None or end_dt is None:
        return ClosureVerdict(False, "拉链生效区间日期无效，不能闭链",
                              chain_id, closure_no, status, interval, abnormal)
    if end_dt < start_dt:
        return ClosureVerdict(False,
                              f"生效止日 {interval[1]} 早于生效起日 {interval[0]}",
                              chain_id, closure_no, status, interval, abnormal)
    return ClosureVerdict(True, "闭链有效", chain_id, closure_no, status,
                          interval, abnormal)


def next_available_interval(dim_name: str) -> tuple[str, str]:
    """快照页可用的下一步区间：取该维度未冲突、当前生效拉链的区间。

    只有能核对到一次有效闭链的维度才会给出可用区间，否则返回空，
    由调用方向操作员说明快照暂不能继续。
    """
    rows = [
        r for r in store.rows(SCD_MODULE)
        if r.get("所属维度") == dim_name
        and r.get("status") == CLOSED_STATUS
        and not r.get("abnormal")
    ]
    if not rows:
        return "", ""
    valid_rows = []
    for row in rows:
        verdict = evaluate_chain_closure(str(row.get("拉链编号", "")))
        if verdict.valid:
            valid_rows.append(row)
    if not valid_rows:
        return "", ""
    starts = [str(r.get("生效起日", "")) for r in valid_rows]
    ends = [str(r.get("生效止日", "")) for r in valid_rows]
    return min(starts), max(ends)

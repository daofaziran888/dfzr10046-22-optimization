"""闭链台账：闭链请求的版本追踪、冲突失效、跨模块送交与接手确认。

设计要点：
- 同一拉链每次发起闭链都生成单调递增的请求版本号（request_seq），
  旧请求的晚到回包只能对自己的版本负责，不能覆盖拉链上的新操作；
- 闭链编号在拉链行与快照送交之间共用同一份，刷新后两处保持一致；
- 跨模块送交维度快照后，必须由快照侧「接手人确认」，快照页才能使用闭链结论；
- 同一拉链被另一窗口标记区间冲突后，在途闭链与已送交结论统一失效，留痕可查。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.closure import (
    CLOSED_STATUS,
    ClosureVerdict,
    chain_interval,
    evaluate_chain_closure,
    find_chain_row,
)
from app.store import store


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ClosureLedgerService:
    def __init__(self) -> None:
        # chain_id -> 台账记录
        self._closures: dict[str, dict[str, Any]] = {}
        # handoff_id -> 送交登记
        self._handoffs: dict[str, dict[str, Any]] = {}
        # chain_id -> 操作日志（追加写，只增不改，模拟断线/冲突也留痕）
        self._logs: dict[str, list[dict[str, Any]]] = {}
        self._handoff_seq = 0
        self._seeded = False

    def _ensure_seed(self) -> None:
        """把种子数据里已经闭链、但没有台账的历史记录补登为第 0 版。

        继续兼容已有闭链记录的查询：老数据没有请求台账也能按拉链编号查到结论。
        """
        if self._seeded:
            return
        for row in store.rows("scd_chain"):
            chain_id = str(row.get("拉链编号", ""))
            if chain_id and row.get("status") == CLOSED_STATUS and chain_id not in self._closures:
                closure_no = f"CL-{chain_id}"
                row["闭链编号"] = closure_no
                self._closures[chain_id] = {
                    "chain_id": chain_id,
                    "closure_no": closure_no,
                    "request_seq": 0,
                    "status": "有效",
                    "interval": chain_interval(row),
                    "end_date": row.get("生效止日", ""),
                    "created_at": str(row.get("生成时间") or _now()),
                    "updated_at": _now(),
                    "legacy": True,
                }
        self._seeded = True

    def _log(self, chain_id: str, action: str, detail: str,
             request_seq: int | None = None, source: str = "拉链加工") -> None:
        self._logs.setdefault(chain_id, []).append({
            "ts": _now(),
            "action": action,
            "detail": detail,
            "request_seq": request_seq,
            "source": source,
        })

    # ------------------------------------------------------------------
    # 请求版本：发起 / 晚到回包收敛
    # ------------------------------------------------------------------
    def open_request(self, chain_id: str) -> dict[str, Any]:
        """发起一次闭链请求，返回带请求版本号的在途记录。

        同一拉链有新操作时旧请求作废：这里递增 request_seq，
        旧请求即使晚到也无法再用 settle_request 认领当前行。
        """
        self._ensure_seed()
        prev = self._closures.get(chain_id)
        seq = int(prev["request_seq"]) + 1 if prev else 1
        closure_no = f"CL-{chain_id}"
        record = {
            "chain_id": chain_id,
            "closure_no": closure_no,
            "request_seq": seq,
            "status": "在途",
            "interval": ("", ""),
            "end_date": "",
            "created_at": _now(),
            "updated_at": _now(),
            "legacy": False,
        }
        self._closures[chain_id] = record
        self._log(chain_id, "发起闭链", f"请求版本 v{seq} 已受理，等待接口回执", seq)
        return dict(record)

    def current_seq(self, chain_id: str) -> int:
        self._ensure_seed()
        record = self._closures.get(chain_id)
        return int(record["request_seq"]) if record else 0

    def is_current_request(self, chain_id: str, request_seq: int) -> bool:
        """预检回包版本是否仍是当前请求；不是则说明该拉链已有更新操作。"""
        self._ensure_seed()
        record = self._closures.get(chain_id)
        return record is not None and int(record["request_seq"]) == int(request_seq)

    def settle_request(
        self, chain_id: str, request_seq: int, verdict: ClosureVerdict,
        end_date: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """收敛一次闭链回包。

        晚到回包防护：request_seq 小于当前版本说明该拉链已有更新的操作，
        旧回包只留痕，绝不替用户认领新操作、不覆盖当前行状态。
        """
        self._ensure_seed()
        record = self._closures.get(chain_id)
        if record is None:
            self._log(chain_id, "回包丢弃",
                      f"v{request_seq} 无对应台账请求，拒绝认领", request_seq)
            return False, "闭链请求不存在或已重置，请刷新后重新核对", None
        if int(request_seq) != int(record["request_seq"]):
            self._log(
                chain_id, "回包过期",
                f"v{request_seq} 晚到，当前已是 v{record['request_seq']}，旧回执不覆盖新操作",
                request_seq,
            )
            return False, (
                f"该回执属于旧请求 v{request_seq}，拉链当前已是 "
                f"v{record['request_seq']}，请按最新状态核对"
            ), None
        if verdict.valid:
            record["status"] = "有效"
            record["interval"] = verdict.interval
            record["end_date"] = end_date
            record["updated_at"] = _now()
            row = find_chain_row(chain_id)
            if row is not None:
                row["闭链编号"] = record["closure_no"]
            self._log(chain_id, "闭链生效",
                      f"v{request_seq} 闭链至 {end_date}（{record['closure_no']}）",
                      request_seq)
            return True, f"闭链生效：{record['closure_no']}", dict(record)
        record["status"] = "失败"
        record["updated_at"] = _now()
        self._log(chain_id, "闭链失败", f"v{request_seq} 未生效：{verdict.reason}",
                  request_seq)
        return False, verdict.reason, dict(record)

    # ------------------------------------------------------------------
    # 冲突：另一窗口标记后，在途/有效闭链统一失效
    # ------------------------------------------------------------------
    def invalidate_by_conflict(self, chain_id: str, reason: str) -> None:
        self._ensure_seed()
        record = self._closures.get(chain_id)
        if record is not None:
            record["status"] = "冲突失效"
            record["updated_at"] = _now()
            self._log(chain_id, "冲突失效",
                      f"v{record['request_seq']} 被标记区间冲突：{reason}",
                      record["request_seq"], source="另一窗口")
        # 已送交快照但尚未/已经确认的，统一标记送交失效。
        for handoff in self._handoffs.values():
            if handoff["chain_id"] == chain_id and handoff["status"] != "冲突失效":
                handoff["status"] = "冲突失效"
                handoff["updated_at"] = _now()
                self._log(chain_id, "送交失效",
                          f"送交单 {handoff['handoff_id']} 因区间冲突失效",
                          handoff["request_seq"], source="维度快照")

    # ------------------------------------------------------------------
    # 跨模块送交：快照侧接手确认后才可使用闭链结论
    # ------------------------------------------------------------------
    def register_handoff(
        self, chain_id: str, dim_name: str, snapshot_id: str, snapshot_date: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        self._ensure_seed()
        verdict = evaluate_chain_closure(chain_id)
        record = self._closures.get(chain_id)
        if record is None or record.get("status") != "有效":
            return False, "拉链没有有效闭链，不能向快照送交", None
        if not verdict.valid:
            return False, f"闭链核对未通过：{verdict.reason}", None

        self._handoff_seq += 1
        handoff_id = f"HO-{self._handoff_seq:04d}"
        closure_no = record["closure_no"]
        handoff = {
            "handoff_id": handoff_id,
            "chain_id": chain_id,
            "closure_no": closure_no,
            "dim_name": dim_name,
            "snapshot_id": snapshot_id,
            "snapshot_date": snapshot_date,
            "interval": verdict.interval,
            "request_seq": record["request_seq"],
            # 待接手确认：确认前快照页不能使用该闭链结论。
            "status": "待确认",
            "created_at": _now(),
            "updated_at": _now(),
        }
        self._handoffs[handoff_id] = handoff
        self._log(chain_id, "送交快照",
                  f"{handoff_id} 送交维度快照，闭链编号 {closure_no}，等待接手确认",
                  record["request_seq"], source="维度快照")
        return True, f"送交已登记：{handoff_id}，等待快照侧接手确认", dict(handoff)

    def confirm_handoff(self, handoff_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        handoff = self._handoffs.get(handoff_id)
        if handoff is None:
            return False, f"送交单 {handoff_id} 不存在", None
        if handoff["status"] == "冲突失效":
            return False, "送交单已因区间冲突失效，不能确认", None
        # 确认时再核对一次闭链仍有效，避免确认期间状态已变。
        verdict = evaluate_chain_closure(handoff["chain_id"])
        if not verdict.valid:
            handoff["status"] = "冲突失效"
            return False, f"确认时闭链已失效：{verdict.reason}", None
        handoff["status"] = "已确认"
        handoff["updated_at"] = _now()
        self._log(handoff["chain_id"], "接手确认",
                  f"{handoff_id} 已确认，快照可按区间 {handoff['interval'][0]}~"
                  f"{handoff['interval'][1]} 使用闭链 {handoff['closure_no']}",
                  handoff["request_seq"], source="维度快照")
        return True, f"送交 {handoff_id} 已接手确认", dict(handoff)

    # ------------------------------------------------------------------
    # 查询：闭链结论 / 送交单 / 操作日志，兼容历史闭链记录
    # ------------------------------------------------------------------
    def get_closure(self, chain_id: str) -> dict[str, Any] | None:
        self._ensure_seed()
        record = self._closures.get(chain_id)
        if record is None:
            # 兼容：没有台账但拉链本身已闭链（历史记录），现场核对一份返回。
            verdict = evaluate_chain_closure(chain_id)
            if verdict.valid:
                return {
                    "chain_id": chain_id,
                    "closure_no": verdict.closure_no,
                    "request_seq": 0,
                    "status": "有效",
                    "interval": verdict.interval,
                    "legacy": True,
                }
            return None
        result = dict(record)
        verdict = evaluate_chain_closure(chain_id)
        result["verdict"] = verdict.as_dict()
        result["handoffs"] = [
            dict(h) for h in self._handoffs.values() if h["chain_id"] == chain_id
        ]
        return result

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        handoff = self._handoffs.get(handoff_id)
        return dict(handoff) if handoff else None

    def list_handoffs(self, chain_id: str | None = None) -> list[dict[str, Any]]:
        items = list(self._handoffs.values())
        if chain_id:
            items = [h for h in items if h["chain_id"] == chain_id]
        return [dict(h) for h in items]

    def operation_logs(self, chain_id: str) -> list[dict[str, Any]]:
        self._ensure_seed()
        return [dict(item) for item in self._logs.get(chain_id, [])]

    def closure_no_for(self, chain_id: str) -> str | None:
        self._ensure_seed()
        record = self._closures.get(chain_id)
        if record:
            return str(record["closure_no"])
        row = find_chain_row(chain_id)
        return str(row["闭链编号"]) if row and row.get("闭链编号") else None


closure_ledger = ClosureLedgerService()

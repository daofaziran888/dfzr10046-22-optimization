"""拉链加工领域服务：闭链校验与重叠冲突检测，属性失效联动转换，产出拉链记录数据集。

闭链相关动作统一走闭链台账（services.closure_ledger）与共享判定
（domain.closure），保证拉链页与快照页用的是同一份闭链口径，
并支持请求版本追踪、晚到回包丢弃与冲突失效留痕。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.domain.closure import (
    CLOSED_STATUS,
    ClosureVerdict,
    evaluate_chain_closure,
    interval_overlaps,
)
from app.services.base import BaseModuleService
from app.services.closure_ledger import closure_ledger
from app.store import store


class ScdChainService(BaseModuleService):
    MODULE = "scd_chain"
    ENTITY = "拉链记录"
    LIST_FIELDS = ["拉链编号", "所属维度", "代理键", "变更字段", "生效起日", "生效止日", "是否当前", "链上前序"]
    REQUIRED_FIELDS = ["拉链编号", "所属维度", "代理键"]
    STATUS_ORDER = ["加工中", "已闭链", "区间冲突", "已作废"]
    ACTION_RULES = {"闭链记录": "已闭链", "标记冲突": "区间冲突", "作废记录": "已作废"}
    NEGATIVE_ACTIONS = ["作废记录"]

    def open_closure_request(self, chain_id: str) -> dict[str, Any] | None:
        """发起闭链请求：登记一个带请求版本号的在途请求，供断线恢复核对。"""
        if not any(r.get("拉链编号") == chain_id for r in store.rows(self.MODULE)):
            return None
        return closure_ledger.open_request(chain_id)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用动作入口也接入闭链台账，保证两条路径口径与闭链编号一致。

        - 闭链记录：自动补一个请求版本并按当前生效止日收敛，写闭链编号与留痕；
        - 标记冲突：走 mark_conflict，同步失效在途闭链与跨模块送交；
        - 其余动作（作废记录）回落到基类通用流转。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        chain_id = str(entry.get("拉链编号", ""))

        if action == "闭链记录":
            end_date = str(entry.get("生效止日", ""))
            request = self.open_closure_request(chain_id)
            ok, message, updated = self.close_chain_record(
                chain_id, end_date, None,
                request["request_seq"] if request else None)
            return (updated, message) if ok else (None, message)

        if action == "标记冲突":
            changed = self.mark_conflict(
                chain_id, str(entry.get("冲突原因") or "页面标记区间冲突"))
            if not changed:
                return None, f"拉链 {chain_id} 不存在"
            return store.find(self.MODULE, entry_id), f"{self.ENTITY}已{action}"

        return super().run_action(entry_id, action)

    def close_chain_record(
        self, chain_id: str, end_date: str, predecessor_id: str | None = None,
        request_seq: int | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """闭链操作：校验起止日期与链上前序收敛条件。

        带 request_seq 时按请求版本收敛，旧请求的晚到回包不会覆盖新操作；
        不传 request_seq 时（历史调用/测试）按当前最新请求或即时登记处理。
        """
        rows = store.rows(self.MODULE)
        target = None
        for r in rows:
            if r.get("拉链编号") == chain_id:
                target = r
                break
        if target is None:
            return False, f"拉链 {chain_id} 不存在", None

        # 晚到回包防护必须在改动拉链行之前：旧请求版本只能留痕，
        # 不能替用户认领新操作，更不能把当前行改成已闭链。
        if request_seq is not None and \
                not closure_ledger.is_current_request(chain_id, request_seq):
            current = closure_ledger.current_seq(chain_id)
            closure_ledger.settle_request(
                chain_id, request_seq,
                ClosureVerdict(False, "旧请求晚到", chain_id), end_date)
            return False, (
                f"该回执属于旧请求 v{request_seq}，拉链当前已是 v{current}，"
                "旧回执不覆盖新操作，请按最新状态核对"
            ), None

        try:
            end_dt = date.fromisoformat(end_date)
            start_dt = date.fromisoformat(target.get("生效起日", end_date))
        except ValueError:
            if request_seq is not None:
                closure_ledger.settle_request(
                    chain_id, request_seq,
                    ClosureVerdict(False, "日期格式无效", chain_id), end_date)
            return False, "日期格式无效", None

        if end_dt < start_dt:
            reason = f"生效止日 {end_date} 早于生效起日 {target.get('生效起日')}"
            if request_seq is not None:
                closure_ledger.settle_request(
                    chain_id, request_seq, ClosureVerdict(False, reason, chain_id),
                    end_date)
            return False, reason, None

        if predecessor_id:
            pred = None
            for r in rows:
                if r.get("拉链编号") == predecessor_id:
                    pred = r
                    break
            if pred is None:
                return False, f"链上前序 {predecessor_id} 不存在", None
            pred_end = pred.get("生效止日", "")
            if pred_end != target.get("生效起日", ""):
                return False, (
                    f"前序止日 {pred_end} 与当前起日 {target.get('生效起日')} 不首尾相接"
                ), None
            target["链上前序"] = predecessor_id

        target["生效止日"] = end_date
        target["是否当前"] = "否"
        target["status"] = CLOSED_STATUS
        target["pending"] = False
        target["abnormal"] = False

        if request_seq is None:
            # 兼容无版本号的历史/直接调用：没有在途请求时补登记一个再收敛。
            if closure_ledger.get_closure(chain_id) is None:
                closure_ledger.open_request(chain_id)
            request_seq = closure_ledger.current_seq(chain_id)

        verdict = evaluate_chain_closure(chain_id)
        ok, message, record = closure_ledger.settle_request(
            chain_id, request_seq, verdict, end_date)
        if not ok:
            return False, message, None
        target["闭链编号"] = record["closure_no"]
        return True, f"拉链 {chain_id} 已闭链至 {end_date}（闭链编号 {record['closure_no']}）", dict(target)

    def detect_overlap_conflicts(self, dim_name: str) -> list[dict[str, Any]]:
        """检测指定维度下的拉链区间重叠冲突。"""
        rows = store.rows(self.MODULE)
        dim_rows = sorted(
            [r for r in rows if r.get("所属维度") == dim_name],
            key=lambda r: r.get("生效起日", ""),
        )
        if len(dim_rows) < 2:
            return []

        conflicts: list[dict[str, Any]] = []
        for i, a in enumerate(dim_rows):
            a_start = a.get("生效起日", "")
            a_end = a.get("生效止日", "")
            for b in dim_rows[i + 1:]:
                b_start = b.get("生效起日", "")
                b_end = b.get("生效止日", "")
                if interval_overlaps(a_start, a_end, b_start, b_end):
                    conflicts.append({
                        "chain_a": a.get("拉链编号"),
                        "chain_b": b.get("拉链编号"),
                        "a_interval": f"[{a_start}, {a_end}]",
                        "b_interval": f"[{b_start}, {b_end}]",
                        "overlap_start": max(a_start, b_start),
                        "overlap_end": min(a_end, b_end),
                    })
        return conflicts

    def mark_conflict(self, chain_id: str, reason: str) -> bool:
        """标记拉链记录为区间冲突状态，并让其在途/有效闭链与送交结论失效。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("拉链编号") == chain_id:
                r["status"] = "区间冲突"
                r["abnormal"] = True
                r["冲突原因"] = reason
                closure_ledger.invalidate_by_conflict(chain_id, reason)
                return True
        return False

    def closure_status(self, chain_id: str) -> dict[str, Any] | None:
        """供页面刷新后核对：返回该拉链当前闭链结论、闭链编号与操作日志。"""
        closure = closure_ledger.get_closure(chain_id)
        if closure is None:
            verdict = evaluate_chain_closure(chain_id)
            return {
                "chain_id": chain_id,
                "closure_no": None,
                "verdict": verdict.as_dict(),
                "logs": closure_ledger.operation_logs(chain_id),
            }
        closure = dict(closure)
        closure["logs"] = closure_ledger.operation_logs(chain_id)
        return closure

    def apply_attr_invalidation(
        self, dim_name: str, attr_name: str, default_value: str,
    ) -> int:
        """属性失效联动：将指定维度属性失效后回填默认值并标记受影响的拉链。"""
        rows = store.rows(self.MODULE)
        count = 0
        for r in rows:
            if r.get("所属维度") == dim_name:
                r["变更字段"] = f"{attr_name}→{default_value}（属性已失效）"
                count += 1
        return count

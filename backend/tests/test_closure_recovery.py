"""闭链失败恢复、跨模块送交与接手确认的端到端领域测试。

覆盖：
- 发起闭链的请求版本与晚到回包丢弃；
- 另一窗口标记区间冲突后在途/送交结论失效；
- 跨模块送交快照、接手确认前快照不可用；
- 快照草稿按原编号回查、闭链编号在拉链与快照间一致；
- 历史闭链记录（无台账）继续可查询。
"""
from __future__ import annotations

from app.domain.closure import evaluate_chain_closure
from app.seed import SEED_ROWS
from app.services.closure_ledger import closure_ledger
from app.services.dim_snapshot import DimSnapshotService
from app.services.scd_chain import ScdChainService
from app.store import store
import pytest


def _reset_ledger() -> None:
    closure_ledger._closures.clear()
    closure_ledger._handoffs.clear()
    closure_ledger._logs.clear()
    closure_ledger._handoff_seq = 0
    closure_ledger._seeded = False


@pytest.fixture(autouse=True)
def _reset_state():
    """每个用例都从种子数据与空台账开始，避免共享内存 store 跨用例污染。"""
    for name, rows in SEED_ROWS.items():
        store.rows(name)[:] = [dict(row) for row in rows]
    _reset_ledger()
    yield


def _chain(chain_id: str) -> dict:
    for row in store.rows("scd_chain"):
        if row.get("拉链编号") == chain_id:
            return row
    raise KeyError(chain_id)


def test_legacy_closed_chain_still_queryable():
    """种子里已闭链的历史记录没有台账，也能按拉链编号核对出有效闭链。"""
    _reset_ledger()
    verdict = evaluate_chain_closure("SCD-002")
    assert verdict.valid
    assert verdict.closure_no == "SCD-002"
    closure = closure_ledger.get_closure("SCD-002")
    assert closure is not None and closure["status"] == "有效"
    assert closure["request_seq"] == 0 and closure["legacy"] is True


def test_start_then_close_keeps_single_closure_no():
    """发起→提交成功后，拉链行与台账共用同一闭链编号，刷新后两处一致。"""
    _reset_ledger()
    scd = ScdChainService()
    req = scd.open_closure_request("SCD-001")
    assert req["request_seq"] == 1 and req["status"] == "在途"

    ok, msg, entry = scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])
    assert ok and entry is not None
    closure_no = entry["闭链编号"]
    assert closure_no == "CL-SCD-001"
    # 拉链行与台账闭链编号一致
    assert _chain("SCD-001")["闭链编号"] == closure_no
    status = scd.closure_status("SCD-001")
    assert status["closure_no"] == closure_no
    assert status["verdict"]["valid"] is True
    # 操作日志留痕：发起、生效各一条
    actions = [log["action"] for log in status["logs"]]
    assert "发起闭链" in actions and "闭链生效" in actions


def test_late_response_of_old_request_cannot_claim_new_operation():
    """同一拉链有新操作时，旧请求的晚到回包不能替用户认领新操作。"""
    _reset_ledger()
    scd = ScdChainService()
    first = scd.open_closure_request("SCD-001")
    # 用户/另一窗口又发起了新的闭链请求（旧请求被新版本取代）
    second = scd.open_closure_request("SCD-001")
    assert second["request_seq"] == first["request_seq"] + 1

    # 旧请求的回包晚到
    ok, msg, entry = scd.close_chain_record("SCD-001", "2026-10-15", None, first["request_seq"])
    assert ok is False and entry is None
    assert "旧请求" in msg or "v1" in msg
    # 拉链没有被旧回包改动，仍在加工中
    assert _chain("SCD-001")["status"] == "加工中"
    logs = scd.closure_status("SCD-001")["logs"]
    assert any(log["action"] == "回包过期" for log in logs)

    # 新请求正常收敛
    ok2, _, entry2 = scd.close_chain_record("SCD-001", "2026-10-20", None, second["request_seq"])
    assert ok2 and entry2["status"] == "已闭链"


def test_conflict_from_other_window_invalidates_inflight_and_handoff():
    """另一窗口标记区间冲突：在途闭链与已送交结论统一失效，快照不可继续。"""
    _reset_ledger()
    scd = ScdChainService()
    snap = DimSnapshotService()
    req = scd.open_closure_request("SCD-001")
    scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])

    ok, _, handoff = snap.send_closure_handoff(
        "SCD-001", "客户维度", "SNAP-DRAFT-1", "2026-10-15")
    assert ok and handoff["status"] == "待确认"
    ok2, _, confirmed = snap.confirm_handoff(handoff["handoff_id"])
    assert ok2 and confirmed["status"] == "已确认"
    assert snap.check_closure_for_snapshot("SCD-001")["usable"] is True

    # 另一窗口对同一行标记冲突
    changed = scd.mark_conflict("SCD-001", "跨窗口区间重叠")
    assert changed
    assert _chain("SCD-001")["status"] == "区间冲突"
    closure = closure_ledger.get_closure("SCD-001")
    assert closure["status"] == "冲突失效"
    assert closure_ledger.get_handoff(handoff["handoff_id"])["status"] == "冲突失效"

    check = snap.check_closure_for_snapshot("SCD-001")
    assert check["usable"] is False
    assert check["can_continue_publish"] is False
    assert "区间冲突" in check["message"]
    # 已失效送交不能再确认
    ok3, _, _ = snap.confirm_handoff(handoff["handoff_id"])
    assert ok3 is False


def test_snapshot_usable_only_after_handoff_confirmation():
    """快照页只有在可核对到一次有效闭链且接手确认后才能继续使用。"""
    _reset_ledger()
    scd = ScdChainService()
    snap = DimSnapshotService()
    req = scd.open_closure_request("SCD-001")
    scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])

    # 未送交/未确认前不可用
    assert snap.check_closure_for_snapshot("SCD-001")["usable"] is False

    ok, _, handoff = snap.send_closure_handoff(
        "SCD-001", "客户维度", "SNAP-DRAFT-2", "2026-10-15")
    assert ok
    # 已送交但未确认，仍不可用
    mid = snap.check_closure_for_snapshot("SCD-001")
    assert mid["usable"] is False and "接手确认" in mid["message"]

    snap.confirm_handoff(handoff["handoff_id"])
    final = snap.check_closure_for_snapshot("SCD-001")
    assert final["usable"] is True
    assert final["can_continue_publish"] is True
    # 下一步可用区间来自有效闭链
    assert final["available_interval"]["start"] == "2026-09-01"
    assert final["available_interval"]["end"] == "2026-10-15"
    # 闭链编号在快照核对与拉链之间一致
    assert final["closure_no"] == scd.closure_status("SCD-001")["closure_no"]


def test_draft_lookup_by_original_snapshot_id_preserves_fields():
    """返回快照草稿能以原编号查结果，且保住快照编号与快照日期。"""
    _reset_ledger()
    scd = ScdChainService()
    snap = DimSnapshotService()
    req = scd.open_closure_request("SCD-001")
    scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])
    ok, _, handoff = snap.send_closure_handoff(
        "SCD-001", "客户维度", "SNAP-KEEP-9", "2026-10-15")
    assert ok

    draft = snap.lookup_draft("SNAP-KEEP-9")
    assert draft["found"] is True
    assert draft["snapshot_id"] == "SNAP-KEEP-9"
    assert draft["snapshot_date"] == "2026-10-15"
    assert draft["handoff_id"] == handoff["handoff_id"]

    # 未登记的编号返回 found=False，而不是报错
    missing = snap.lookup_draft("SNAP-NOT-EXIST")
    assert missing["found"] is False


def test_conflict_then_draft_says_cannot_continue_original_interval():
    """先受理草稿、另一窗口标记冲突后，草稿说明不能按原区间继续发布。"""
    _reset_ledger()
    scd = ScdChainService()
    snap = DimSnapshotService()
    req = scd.open_closure_request("SCD-001")
    scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])
    ok, _, handoff = snap.send_closure_handoff(
        "SCD-001", "客户维度", "SNAP-DRAFT-3", "2026-10-15")
    assert ok
    snap.confirm_handoff(handoff["handoff_id"])
    scd.mark_conflict("SCD-001", "区间冲突")

    draft = snap.lookup_draft("SNAP-DRAFT-3")
    assert draft["found"] is True
    assert draft["usable"] is False
    assert draft["can_continue_publish"] is False
    assert "不能按原区间继续发布" in draft["message"]


def test_generic_run_action_also_writes_ledger_and_closure_no():
    """表格通用「闭链记录」动作同样写台账、生成与快照一致的闭链编号。"""
    _reset_ledger()
    scd = ScdChainService()
    # SCD-001 的种子 id=1，生效止日 9999-12-31
    entry, message = scd.run_action(1, "闭链记录")
    assert entry is not None and entry["status"] == "已闭链"
    closure_no = entry["闭链编号"]
    assert closure_no == "CL-SCD-001"
    # 台账、拉链行、共享判定三处闭链编号一致
    assert closure_ledger.closure_no_for("SCD-001") == closure_no
    assert _chain("SCD-001")["闭链编号"] == closure_no
    assert evaluate_chain_closure("SCD-001").closure_no == closure_no
    # 操作日志包含发起与生效
    logs = scd.closure_status("SCD-001")["logs"]
    assert [log["action"] for log in logs][-2:] == ["发起闭链", "闭链生效"]


def test_generic_mark_conflict_action_invalidates_closure():
    """通用「标记冲突」动作同样让在途闭链与送交失效。"""
    _reset_ledger()
    scd = ScdChainService()
    req = scd.open_closure_request("SCD-001")
    scd.close_chain_record("SCD-001", "2026-10-15", None, req["request_seq"])
    entry, _ = scd.run_action(1, "标记冲突")
    assert entry is not None and entry["status"] == "区间冲突"
    assert closure_ledger.get_closure("SCD-001")["status"] == "冲突失效"
    assert evaluate_chain_closure("SCD-001").valid is False


def test_operation_logs_match_after_disconnect_and_conflict():
    """断线恢复场景：发起后未收到回执，另一窗口标记冲突，日志与下一步区间对应。"""
    _reset_ledger()
    scd = ScdChainService()
    # 发起闭链后模拟接口断线：没有调用 close，请求停留在途
    req = scd.open_closure_request("SCD-001")
    assert req["status"] == "在途"
    # 另一窗口对同一行执行标记冲突
    scd.mark_conflict("SCD-001", "恢复期间由另一窗口标记")
    status = scd.closure_status("SCD-001")
    assert status["verdict"]["valid"] is False
    log_actions = [log["action"] for log in status["logs"]]
    assert "发起闭链" in log_actions and "冲突失效" in log_actions
    # 快照侧下一步可用区间：该拉链已冲突，不能提供可用区间
    snap = DimSnapshotService()
    check = snap.check_closure_for_snapshot("SCD-001")
    assert check["available_interval"] == {"start": "", "end": ""}
    assert check["usable"] is False

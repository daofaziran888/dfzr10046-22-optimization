"""闭链协作接口：拉链加工与维度快照之间的闭链请求、送交与接手确认。

这些接口承载通用资源路由无法表达的请求版本、断线恢复与跨模块确认语义；
状态流转仍全部落在领域服务/台账内，路由层不做业务判断。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.domain.closure import evaluate_chain_closure
from app.services.closure_ledger import closure_ledger
from app.services.dim_snapshot import DimSnapshotService
from app.services.scd_chain import ScdChainService

router = APIRouter(prefix="/api/closure", tags=["闭链协作"])

scd_service = ScdChainService()
snapshot_service = DimSnapshotService()


class ApiResult(BaseModel):
    ok: bool
    message: str
    data: dict[str, Any] | None = None


class StartPayload(BaseModel):
    chain_id: str


class ClosePayload(BaseModel):
    chain_id: str
    end_date: str
    predecessor_id: str | None = None
    # 前端发起闭链时拿到的请求版本号；断线重连用同一版本号核对。
    request_seq: int


class ConflictPayload(BaseModel):
    chain_id: str
    reason: str = ""


class HandoffPayload(BaseModel):
    chain_id: str
    dim_name: str
    snapshot_id: str
    snapshot_date: str


class HandoffConfirmPayload(BaseModel):
    handoff_id: str


class DraftQuery(BaseModel):
    snapshot_id: str


@router.post("/start", response_model=ApiResult)
def start_closure(payload: StartPayload) -> ApiResult:
    """发起闭链请求，返回请求版本号；模拟断线时前端凭此版本号恢复核对。"""
    record = scd_service.open_closure_request(payload.chain_id)
    if record is None:
        return ApiResult(ok=False, message=f"拉链 {payload.chain_id} 不存在", data=None)
    return ApiResult(ok=True, message="闭链请求已受理", data=record)


@router.post("/close", response_model=ApiResult)
def close_closure(payload: ClosePayload) -> ApiResult:
    """提交闭链回执；旧请求版本号的晚到回包会被拒绝，不覆盖拉链新操作。"""
    ok, message, entry = scd_service.close_chain_record(
        payload.chain_id, payload.end_date,
        payload.predecessor_id, payload.request_seq,
    )
    data = {"entry": entry} if entry else None
    if data is None:
        # 失败时把当前台账结论带回，便于操作员回到工作台继续核对是否生效。
        data = {"closure": scd_service.closure_status(payload.chain_id)}
    return ApiResult(ok=ok, message=message, data=data)


@router.post("/conflict", response_model=ApiResult)
def mark_conflict(payload: ConflictPayload) -> ApiResult:
    """另一窗口对同一行标记区间冲突：在途闭链与已送交结论统一失效。"""
    changed = scd_service.mark_conflict(payload.chain_id, payload.reason or "跨窗口标记区间冲突")
    if not changed:
        return ApiResult(ok=False, message=f"拉链 {payload.chain_id} 不存在", data=None)
    return ApiResult(
        ok=True,
        message=f"拉链 {payload.chain_id} 已标记区间冲突，相关闭链与送交结论失效",
        data=scd_service.closure_status(payload.chain_id),
    )


@router.get("/status/{chain_id}", response_model=ApiResult)
def closure_status(chain_id: str) -> ApiResult:
    """刷新/断线恢复后核对该拉链闭链是否生效、闭链编号与操作日志。"""
    status = scd_service.closure_status(chain_id)
    if status is None:
        return ApiResult(ok=False, message=f"拉链 {chain_id} 不存在", data=None)
    return ApiResult(ok=True, message="闭链状态已核对", data=status)


@router.get("/verdict/{chain_id}", response_model=ApiResult)
def closure_verdict(chain_id: str) -> ApiResult:
    """共享闭链判定的只读结果，拉链页与快照页消费同一份口径。"""
    verdict = evaluate_chain_closure(chain_id)
    return ApiResult(ok=verdict.valid, message=verdict.reason, data=verdict.as_dict())


@router.post("/handoff", response_model=ApiResult)
def send_handoff(payload: HandoffPayload) -> ApiResult:
    """跨模块送交维度快照；闭链须有效，送交后等待接手确认。"""
    ok, message, handoff = snapshot_service.send_closure_handoff(
        payload.chain_id, payload.dim_name,
        payload.snapshot_id, payload.snapshot_date,
    )
    return ApiResult(ok=ok, message=message, data=handoff)


@router.post("/handoff/confirm", response_model=ApiResult)
def confirm_handoff(payload: HandoffConfirmPayload) -> ApiResult:
    """接手人确认送交，确认后快照页才能使用该闭链结论。"""
    ok, message, handoff = snapshot_service.confirm_handoff(payload.handoff_id)
    return ApiResult(ok=ok, message=message, data=handoff)


@router.get("/handoffs", response_model=ApiResult)
def list_handoffs(chain_id: str | None = None) -> ApiResult:
    return ApiResult(
        ok=True, message="送交清单",
        data={"items": closure_ledger.list_handoffs(chain_id)},
    )


@router.get("/snapshot/check/{chain_id}", response_model=ApiResult)
def snapshot_check(chain_id: str) -> ApiResult:
    """快照页核对：只有可核对到一次有效闭链且已确认时才允许继续使用。"""
    result = snapshot_service.check_closure_for_snapshot(chain_id)
    return ApiResult(ok=result["usable"], message=result["message"], data=result)


@router.post("/snapshot/draft", response_model=ApiResult)
def snapshot_draft(payload: DraftQuery) -> ApiResult:
    """以原快照编号回查草稿对应的闭链结果，重开后继续核对。"""
    result = snapshot_service.lookup_draft(payload.snapshot_id)
    return ApiResult(
        ok=bool(result.get("found")), message=result.get("message", ""), data=result,
    )

/**
 * 闭链协作接口：拉链加工与维度快照共用同一份后端闭链判定。
 * 所有请求都带显式语义，页面只负责把结论展示给操作员。
 */
import { request } from './client'

export type ClosureVerdict = {
  valid: boolean
  reason: string
  chain_id: string
  closure_no: string | null
  status: string
  interval_start: string
  interval_end: string
  abnormal: boolean
  handoff_confirmed: boolean
}

export type Handoff = {
  handoff_id: string
  chain_id: string
  closure_no: string
  dim_name: string
  snapshot_id: string
  snapshot_date: string
  interval: [string, string]
  request_seq: number
  status: '待确认' | '已确认' | '冲突失效' | string
}

export type SnapshotClosureCheck = {
  chain_id: string
  closure_no: string | null
  verdict: ClosureVerdict
  handoffs: Handoff[]
  confirmed_handoff: Handoff | null
  usable: boolean
  can_continue_publish: boolean
  available_interval: { start: string; end: string }
  message: string
}

export type ClosureStatus = {
  chain_id: string
  closure_no: string | null
  request_seq?: number
  status?: string
  interval?: [string, string]
  verdict: ClosureVerdict
  handoffs?: Handoff[]
  logs?: { ts: string; action: string; detail: string; request_seq: number | null; source: string }[]
}

type ApiResult<T> = { ok: boolean; message: string; data: T | null }

async function postJson<T>(path: string, body: unknown): Promise<ApiResult<T>> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as ApiResult<T>
}

async function getJson<T>(path: string): Promise<ApiResult<T>> {
  const response = await request(path)
  if (!response.ok) throw new Error('闭链核对请求失败')
  return (await response.json()) as ApiResult<T>
}

/** 发起闭链请求，返回带请求版本号的在途记录（断线恢复凭此版本号）。 */
export function startClosure(chainId: string) {
  return postJson<{ chain_id: string; closure_no: string; request_seq: number; status: string }>(
    '/api/closure/start',
    { chain_id: chainId },
  )
}

/** 提交闭链回执；旧请求版本号的晚到回包会被后端拒绝。 */
export function closeClosure(payload: {
  chainId: string
  endDate: string
  predecessorId?: string
  requestSeq: number
}) {
  return postJson<{ entry?: EntryRowLike; closure?: ClosureStatus }>('/api/closure/close', {
    chain_id: payload.chainId,
    end_date: payload.endDate,
    predecessor_id: payload.predecessorId ?? null,
    request_seq: payload.requestSeq,
  })
}

/** 另一窗口对同一行标记区间冲突。 */
export function markChainConflict(chainId: string, reason: string) {
  return postJson<ClosureStatus>('/api/closure/conflict', {
    chain_id: chainId,
    reason,
  })
}

/** 刷新/断线恢复后核对闭链是否生效、闭链编号与操作日志。 */
export function fetchClosureStatus(chainId: string) {
  return getJson<ClosureStatus>(`/api/closure/status/${encodeURIComponent(chainId)}`)
}

/** 共享闭链判定只读结果。 */
export function fetchClosureVerdict(chainId: string) {
  return getJson<ClosureVerdict>(`/api/closure/verdict/${encodeURIComponent(chainId)}`)
}

/** 跨模块送交维度快照。 */
export function sendHandoff(payload: {
  chainId: string
  dimName: string
  snapshotId: string
  snapshotDate: string
}) {
  return postJson<Handoff>('/api/closure/handoff', {
    chain_id: payload.chainId,
    dim_name: payload.dimName,
    snapshot_id: payload.snapshotId,
    snapshot_date: payload.snapshotDate,
  })
}

/** 接手人确认送交。 */
export function confirmHandoff(handoffId: string) {
  return postJson<Handoff>('/api/closure/handoff/confirm', { handoff_id: handoffId })
}

export function listHandoffs(chainId?: string) {
  const query = chainId ? `?chain_id=${encodeURIComponent(chainId)}` : ''
  return getJson<{ items: Handoff[] }>(`/api/closure/handoffs${query}`)
}

/** 快照页核对：只有可核对到一次有效闭链且已确认时 usable=true。 */
export function checkSnapshotClosure(chainId: string) {
  return getJson<SnapshotClosureCheck>(
    `/api/closure/snapshot/check/${encodeURIComponent(chainId)}`,
  )
}

/** 以原快照编号回查草稿对应的闭链结果。 */
export function lookupSnapshotDraft(snapshotId: string) {
  return postJson<SnapshotClosureCheck & { found: boolean; snapshot_id: string; snapshot_date?: string; handoff_id?: string; handoff_status?: string }>(
    '/api/closure/snapshot/draft',
    { snapshot_id: snapshotId },
  )
}

type EntryRowLike = {
  id: number
  status: string
  [field: string]: string | number | boolean
}

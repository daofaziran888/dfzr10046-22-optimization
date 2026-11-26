<template>
  <section class="snapshot-panel" data-testid="snapshot-panel">
    <header class="snapshot-head">
      <h3>闭链送交与快照草稿</h3>
      <p class="snapshot-tip">
        快照页只有在可核对到一次「有效闭链」、且送交经接手人确认后才能继续使用；
        已输入的快照编号与快照日期会保存在本机，重开页面不丢失。
      </p>
    </header>

    <div class="snapshot-form">
      <label class="snapshot-field">
        <span>拉链编号</span>
        <input v-model="chainId" data-testid="snap-chain-id" placeholder="如 SCD-001" />
      </label>
      <label class="snapshot-field">
        <span>所属维度</span>
        <input v-model="dimName" data-testid="snap-dim-name" placeholder="如 客户维度" />
      </label>
      <label class="snapshot-field">
        <span>快照编号（草稿）</span>
        <input v-model="snapshotId" data-testid="snap-snapshot-id" placeholder="如 SNAP-DRAFT-1" @input="persistDraft" />
      </label>
      <label class="snapshot-field">
        <span>快照日期（草稿）</span>
        <input v-model="snapshotDate" data-testid="snap-snapshot-date" type="date" @input="persistDraft" />
      </label>
      <div class="snapshot-buttons">
        <button class="btn primary" data-testid="snap-send" type="button" :disabled="busy" @click="send">送交闭链</button>
        <button class="btn" data-testid="snap-check" type="button" :disabled="busy" @click="check">核对闭链</button>
        <button class="btn ghost" data-testid="snap-draft-lookup" type="button" :disabled="busy" @click="lookupDraft">按原编号查草稿</button>
      </div>
    </div>

    <div v-if="handoffs.length" class="handoff-list">
      <h4>送交登记（接手确认后才可使用）</h4>
      <table class="data-table">
        <thead>
          <tr><th>送交单号</th><th>闭链编号</th><th>快照编号</th><th>快照日期</th><th>状态</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in handoffs" :key="h.handoff_id" :data-testid="`handoff-${h.handoff_id}`">
            <td>{{ h.handoff_id }}</td>
            <td>{{ h.closure_no }}</td>
            <td>{{ h.snapshot_id }}</td>
            <td>{{ h.snapshot_date }}</td>
            <td :class="h.status === '已确认' ? 'ok-text' : h.status === '冲突失效' ? 'warn-text' : ''">{{ h.status }}</td>
            <td>
              <button
                v-if="h.status === '待确认'"
                class="link"
                type="button"
                :data-testid="`confirm-${h.handoff_id}`"
                :disabled="busy"
                @click="confirm(h.handoff_id)"
              >
                接手确认
              </button>
              <span v-else class="muted-text">—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="notice" class="snapshot-notice" :class="noticeKind" data-testid="snap-notice">{{ notice }}</p>

    <div v-if="checkResult" class="snapshot-result" data-testid="snap-result">
      <dl class="dag-detail">
        <dt>闭链编号</dt><dd data-testid="snap-closure-no">{{ checkResult.closure_no ?? '—' }}</dd>
        <dt>判定</dt>
        <dd :class="checkResult.verdict.valid ? 'ok-text' : 'warn-text'">
          {{ checkResult.verdict.valid ? '有效闭链' : '未形成有效闭链' }}（{{ checkResult.verdict.reason }}）
        </dd>
        <dt>可否继续发布</dt>
        <dd :class="checkResult.can_continue_publish ? 'ok-text' : 'warn-text'">
          {{ checkResult.can_continue_publish ? '可按原区间继续发布' : '不能按原区间继续发布' }}
        </dd>
        <dt>下一步可用区间</dt>
        <dd data-testid="snap-interval">
          <template v-if="checkResult.available_interval.start">
            {{ checkResult.available_interval.start }} ~ {{ checkResult.available_interval.end }}
          </template>
          <template v-else>暂无可核对的有效区间</template>
        </dd>
      </dl>
      <p class="snapshot-message" data-testid="snap-message">{{ checkResult.message }}</p>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  checkSnapshotClosure,
  confirmHandoff,
  listHandoffs,
  lookupSnapshotDraft,
  sendHandoff,
  type Handoff,
  type SnapshotClosureCheck,
} from '@/api/closure'

const DRAFT_KEY = 'dim_snapshot.closure_draft'

const chainId = ref('')
const dimName = ref('')
const snapshotId = ref('')
const snapshotDate = ref('')
const handoffs = ref<Handoff[]>([])
const checkResult = ref<SnapshotClosureCheck | null>(null)
const notice = ref('')
const noticeKind = ref<'ok' | 'error'>('ok')
const busy = ref(false)

function setNotice(message: string, ok: boolean) {
  notice.value = message
  noticeKind.value = ok ? 'ok' : 'error'
}

/** 保住快照登记表里已经输入的快照编号和快照日期：写入 localStorage。 */
function persistDraft() {
  localStorage.setItem(
    DRAFT_KEY,
    JSON.stringify({
      chainId: chainId.value,
      dimName: dimName.value,
      snapshotId: snapshotId.value,
      snapshotDate: snapshotDate.value,
    }),
  )
}

function restoreDraft() {
  const raw = localStorage.getItem(DRAFT_KEY)
  if (!raw) return
  try {
    const draft = JSON.parse(raw) as Record<string, string>
    chainId.value = draft.chainId ?? ''
    dimName.value = draft.dimName ?? ''
    snapshotId.value = draft.snapshotId ?? ''
    snapshotDate.value = draft.snapshotDate ?? ''
    setNotice('已恢复上次填写的快照草稿，编号与日期保持原样', true)
  } catch {
    /* 草稿损坏时静默忽略，不影响登记 */
  }
}

async function refreshHandoffs() {
  if (!chainId.value.trim()) return
  const result = await listHandoffs(chainId.value.trim())
  if (result.ok && result.data) handoffs.value = result.data.items
}

async function send() {
  if (!chainId.value.trim() || !dimName.value.trim() || !snapshotId.value.trim() || !snapshotDate.value) {
    setNotice('送交需要拉链编号、所属维度、快照编号和快照日期', false)
    return
  }
  busy.value = true
  try {
    persistDraft()
    const result = await sendHandoff({
      chainId: chainId.value.trim(),
      dimName: dimName.value.trim(),
      snapshotId: snapshotId.value.trim(),
      snapshotDate: snapshotDate.value,
    })
    setNotice(result.message, result.ok)
    if (result.ok) await refreshHandoffs()
    await check()
  } finally {
    busy.value = false
  }
}

async function check() {
  if (!chainId.value.trim()) {
    setNotice('请先输入拉链编号再核对', false)
    return
  }
  busy.value = true
  try {
    const result = await checkSnapshotClosure(chainId.value.trim())
    if (result.data) checkResult.value = result.data
    setNotice(result.message, result.ok)
    await refreshHandoffs()
  } finally {
    busy.value = false
  }
}

async function confirm(handoffId: string) {
  busy.value = true
  try {
    const result = await confirmHandoff(handoffId)
    setNotice(result.message, result.ok)
    await refreshHandoffs()
    await check()
  } finally {
    busy.value = false
  }
}

/** 返回快照草稿：以原编号查询结果，编号与日期不被清空。 */
async function lookupDraft() {
  if (!snapshotId.value.trim()) {
    setNotice('请先填写快照编号再按原编号查询', false)
    return
  }
  busy.value = true
  try {
    const result = await lookupSnapshotDraft(snapshotId.value.trim())
    if (result.ok && result.data) {
      checkResult.value = result.data
      if (result.data.chain_id) chainId.value = result.data.chain_id
      if (result.data.snapshot_date) snapshotDate.value = result.data.snapshot_date
      handoffs.value = result.data.handoffs ?? []
      persistDraft()
    }
    setNotice(result.message, result.ok)
  } finally {
    busy.value = false
  }
}

onMounted(() => {
  restoreDraft()
  void refreshHandoffs()
})
</script>

<style scoped>
.snapshot-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.snapshot-head h3 { margin: 0 0 4px; font-size: 15px; }
.snapshot-tip { margin: 0 0 12px; color: var(--muted); font-size: 12px; }
.snapshot-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.snapshot-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.snapshot-field input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.snapshot-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.handoff-list { margin-top: 14px; }
.handoff-list h4 { margin: 0 0 8px; font-size: 13px; }
.snapshot-notice { margin: 12px 0 0; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.snapshot-notice.ok { background: #ecfdf3; color: #027a48; border: 1px solid #a6f4c5; }
.snapshot-notice.error { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.snapshot-result { margin-top: 12px; }
.snapshot-message { font-size: 13px; color: #334155; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; }
.ok-text { color: #027a48; }
.warn-text { color: #b42318; }
.muted-text { color: var(--muted); }
</style>

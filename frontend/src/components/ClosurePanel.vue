<template>
  <section class="closure-panel" data-testid="closure-panel">
    <header class="closure-head">
      <h3>闭链确认与失败恢复</h3>
      <p class="closure-tip">
        发起后若接口断线，可直接回到本面板用原请求版本核对；同一拉链有新操作时，旧请求的晚到回包不会覆盖当前状态。
      </p>
    </header>

    <div class="closure-form">
      <label class="closure-field">
        <span>拉链编号</span>
        <input v-model="chainId" data-testid="closure-chain-id" placeholder="如 SCD-001" @keydown.enter.prevent="recover" />
      </label>
      <label class="closure-field">
        <span>生效止日</span>
        <input v-model="endDate" data-testid="closure-end-date" type="date" />
      </label>
      <label class="closure-field">
        <span>请求版本（断线恢复用）</span>
        <input v-model.number="requestSeq" data-testid="closure-request-seq" type="number" min="1" placeholder="发起后自动带入" />
      </label>
      <div class="closure-buttons">
        <button class="btn primary" data-testid="closure-start" type="button" :disabled="busy" @click="start">发起闭链</button>
        <button class="btn" data-testid="closure-submit" type="button" :disabled="busy" @click="submit">提交闭链回执</button>
        <button class="btn ghost" data-testid="closure-recover" type="button" :disabled="busy" @click="recover">刷新核对</button>
        <button class="btn danger" data-testid="closure-conflict" type="button" :disabled="busy" @click="markConflict">另一窗口标记冲突</button>
      </div>
    </div>

    <p v-if="notice" class="closure-notice" :class="noticeKind" data-testid="closure-notice">{{ notice }}</p>

    <div v-if="status" class="closure-result" data-testid="closure-result">
      <dl class="dag-detail">
        <dt>闭链编号</dt><dd data-testid="result-closure-no">{{ status.closure_no ?? '—' }}</dd>
        <dt>当前状态</dt><dd>{{ status.status ?? status.verdict.status ?? '—' }}</dd>
        <dt>判定结论</dt>
        <dd :class="status.verdict.valid ? 'ok-text' : 'warn-text'">
          {{ status.verdict.valid ? '有效闭链' : '未形成有效闭链' }}（{{ status.verdict.reason }}）
        </dd>
        <dt>生效区间</dt>
        <dd>{{ status.verdict.interval_start || '—' }} ~ {{ status.verdict.interval_end || '—' }}</dd>
      </dl>

      <h4>操作记录（断线/冲突均留痕）</h4>
      <ul class="closure-log" data-testid="closure-log">
        <li v-for="(log, idx) in status.logs ?? []" :key="idx" :data-testid="`log-${idx}`">
          <span class="log-time">{{ log.ts }}</span>
          <span class="log-source">[{{ log.source }}]</span>
          <span class="log-action">{{ log.action }}</span>
          <span class="log-detail">{{ log.detail }}</span>
        </li>
        <li v-if="!(status.logs ?? []).length" class="empty-state">暂无操作记录</li>
      </ul>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'

import {
  closeClosure,
  fetchClosureStatus,
  markChainConflict,
  startClosure,
  type ClosureStatus,
} from '@/api/closure'

const props = defineProps<{ defaultChainId?: string }>()

const chainId = ref(props.defaultChainId ?? '')

// 在表格中选中某行后，把拉链编号同步进面板并自动核对，便于直接核对该行。
watch(
  () => props.defaultChainId,
  (value) => {
    if (value) {
      chainId.value = value
      void recover()
    }
  },
)
const endDate = ref('')
const requestSeq = ref<number | null>(null)
const status = ref<ClosureStatus | null>(null)
const notice = ref('')
const noticeKind = ref<'ok' | 'error'>('ok')
const busy = ref(false)

function setNotice(message: string, ok: boolean) {
  notice.value = message
  noticeKind.value = ok ? 'ok' : 'error'
}

async function start() {
  if (!chainId.value.trim()) {
    setNotice('请先输入拉链编号', false)
    return
  }
  busy.value = true
  try {
    const result = await startClosure(chainId.value.trim())
    if (!result.ok || !result.data) {
      setNotice(result.message, false)
      return
    }
    requestSeq.value = result.data.request_seq
    setNotice(`闭链请求 v${result.data.request_seq} 已受理，若接口断线可凭此版本恢复核对`, true)
    await recover()
  } finally {
    busy.value = false
  }
}

async function submit() {
  if (!chainId.value.trim() || !endDate.value || requestSeq.value == null) {
    setNotice('提交需要拉链编号、生效止日和请求版本号', false)
    return
  }
  busy.value = true
  try {
    const result = await closeClosure({
      chainId: chainId.value.trim(),
      endDate: endDate.value,
      requestSeq: Number(requestSeq.value),
    })
    setNotice(result.message, result.ok)
    await recover()
  } finally {
    busy.value = false
  }
}

async function recover() {
  if (!chainId.value.trim()) {
    setNotice('请先输入拉链编号再核对', false)
    return
  }
  busy.value = true
  try {
    const result = await fetchClosureStatus(chainId.value.trim())
    if (result.ok && result.data) {
      status.value = result.data
      if (typeof result.data.request_seq === 'number') {
        requestSeq.value = result.data.request_seq
      }
      setNotice(result.message, true)
    } else {
      setNotice(result.message, false)
    }
  } finally {
    busy.value = false
  }
}

async function markConflict() {
  if (!chainId.value.trim()) {
    setNotice('请先输入拉链编号', false)
    return
  }
  busy.value = true
  try {
    const result = await markChainConflict(chainId.value.trim(), '另一窗口标记区间冲突')
    setNotice(result.message, result.ok)
    if (result.ok && result.data) status.value = result.data
  } finally {
    busy.value = false
  }
}

defineExpose({ recover })
</script>

<style scoped>
.closure-panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin: 14px 0; }
.closure-head h3 { margin: 0 0 4px; font-size: 15px; }
.closure-tip { margin: 0 0 12px; color: var(--muted); font-size: 12px; }
.closure-form { display: flex; flex-wrap: wrap; gap: 12px; align-items: flex-end; }
.closure-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.closure-field input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.closure-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.btn.danger { color: #b42318; border-color: #f0b4ad; }
.closure-notice { margin: 12px 0 0; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.closure-notice.ok { background: #ecfdf3; color: #027a48; border: 1px solid #a6f4c5; }
.closure-notice.error { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.closure-result { margin-top: 12px; }
.closure-log { list-style: none; margin: 8px 0 0; padding: 0; max-height: 220px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; }
.closure-log li { display: flex; gap: 8px; padding: 6px 10px; border-bottom: 1px solid #f0f2f5; font-size: 12px; flex-wrap: wrap; }
.log-time { color: var(--muted); font-family: monospace; }
.log-source { color: #7c3aed; }
.log-action { font-weight: 600; }
.log-detail { color: #334155; }
.ok-text { color: #027a48; }
.warn-text { color: #b42318; }
</style>

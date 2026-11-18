/**
 * dsh-ask —— info-hub 的 DSH 双弹通道插件（S11）。
 *
 * 契约（键表 / 返回 / 失败语义 / 缺口 / 装载方式）：info-hub/INTERFACE.md §12。
 * 本文件注释只写指针，不抄语义、不抄数值。
 *
 * 缝：Cordis 瀑布 `user-questions/request`（挂到 bundle 级 ctx 上，能收到所有提问）。
 * 纪律：**不注册**同名 `ask_user_question` 工具（会撞 DSH 的 already registered）。
 * 台账：建行 / 读行 / 置 expired 走同目录 `ledger.py`（§11.5 缺口，见 §12.6）；
 *       HTTP 只用于 `/api/deliver/pending` 探活与 `/api/deliver/claim`。
 */

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'info-hub-dsh-ask'
// 只用 ctx.on 的瀑布缝，不注入任何服务（INTERFACE.md §12.1）。
export const inject = []

const execFileAsync = promisify(execFile)

/** §12.3：手机可回传的动作键（只能是 §11.2 的动作键，按位置映射到第一问的选项）。 */
export const PHONE_KEYS = ['ack', 'done', 'snooze']
/** §12.3：手机通知栏最多 3 个按钮（S8 的 _DELIVER_MAX_NOTIF_ACTIONS）。 */
export const MAX_PHONE_OPTIONS = 3

const str = (v) => (typeof v === 'string' ? v.trim() : '')

function positiveInt(v, fallback) {
  const n = Number(v)
  return Number.isInteger(n) && n > 0 ? n : fallback
}

/** §12.2：归一配置；缺省值只在这里，取值源仍是装载块（cordis.patch.yml / 用户配置）。 */
export function normalizeConfig(config) {
  const c = config && typeof config === 'object' ? config : {}
  const base = dirname(fileURLToPath(import.meta.url))
  return {
    enabled: c.enabled === true,
    hub_url: (str(c.hub_url) || 'http://127.0.0.1:8799').replace(/\/+$/, ''),
    phone_mcp_url: str(c.phone_mcp_url) || 'http://<bridge-host>:9500/mcp',
    hub_root: str(c.hub_root) || resolve(base, '../../..'),
    hub_config: str(c.hub_config),
    timeout_ms: positiveInt(c.timeout_ms, 600000),
    poll_ms: positiveInt(c.poll_ms, 500),
    hub_timeout_ms: positiveInt(c.hub_timeout_ms, 3000),
    phone_timeout_ms: positiveInt(c.phone_timeout_ms, 30000),
    ledger_timeout_ms: positiveInt(c.ledger_timeout_ms, 10000),
    python: str(c.python) || 'python3',
    ledger_script: str(c.ledger_script) || join(base, 'ledger.py'),
    owner: str(c.owner) || `dsh-ask:${process.pid}`,
    debug: c.debug === true,
  }
}

function warn(cfg, ...parts) {
  if (cfg && cfg.debug) console.error('[dsh-ask]', ...parts)
}

// --------------------------------------------------------------------- 台账（ledger.py）
/** §12.5：子进程直连同一份 infohub 库；stdout 只放机器可读值。 */
function ledgerClient(cfg) {
  const run = async (args) => {
    const env = { ...process.env, DSH_ASK_HUB_ROOT: cfg.hub_root }
    env.PYTHONPATH = [join(cfg.hub_root, 'src'), process.env.PYTHONPATH].filter(Boolean).join(':')
    if (cfg.hub_config) env.HUB_CONFIG = cfg.hub_config
    try {
      const { stdout } = await execFileAsync(cfg.python, [cfg.ledger_script, ...args], {
        env, timeout: cfg.ledger_timeout_ms, maxBuffer: 4 * 1024 * 1024,
      })
      return String(stdout || '').trim()
    } catch (error) {
      const detail = String(error?.stderr || error?.message || error).trim()
      throw new Error(`ledger.py ${args[0]} 失败: ${detail.slice(0, 400)}`)
    }
  }
  const parse = (text) => {
    const line = text.split('\n').filter(Boolean).pop() || '{}'
    return JSON.parse(line)
  }
  return {
    async add(fields) {
      const args = ['add',
        `--channel=${fields.channel}`, `--mode=${fields.mode}`,
        `--title=${fields.title}`, `--body=${fields.body ?? ''}`]
      if (fields.options) args.push(`--options=${JSON.stringify(fields.options)}`)
      if (fields.layout) args.push(`--layout=${JSON.stringify(fields.layout)}`)
      if (fields.todo_id) args.push(`--todo-id=${fields.todo_id}`)
      if (fields.kind) args.push(`--kind=${fields.kind}`)
      // 不带 --json 时 stdout 只有新 id（§12.5）。
      return run(args)
    },
    async get(id) { return parse(await run(['get', id])) },
    async expire(id) { return parse(await run(['expire', id])) },
    async ack(id, action) {
      const args = ['ack', id]
      if (action) args.push(`--action=${action}`)
      return parse(await run(args))
    },
    async reply(id, text) { return parse(await run(['reply', id, `--text=${text}`])) },
  }
}

// --------------------------------------------------------------------- HTTP / 手机 MCP
async function fetchWithTimeout(url, init, timeoutMs) {
  return fetch(url, { ...init, signal: AbortSignal.timeout(timeoutMs) })
}

/** §12.6：HTTP 只用于这两个口；hub 不可达 ⇒ 抛（调用方只用 DSH 答案）。 */
async function probeHub(cfg) {
  const url = `${cfg.hub_url}/api/deliver/pending?channel=dsh&limit=1`
  const res = await fetchWithTimeout(url, { method: 'GET' }, cfg.hub_timeout_ms)
  if (!res.ok) throw new Error(`hub 探活 HTTP ${res.status}`)
  await res.text()
}

async function claimDelivery(cfg, deliveryId) {
  const res = await fetchWithTimeout(`${cfg.hub_url}/api/deliver/claim`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      id: deliveryId,
      owner: cfg.owner,
      ttl_s: Math.min(604800, Math.max(30, Math.ceil(cfg.timeout_ms / 1000) + 30)),
    }),
  }, cfg.hub_timeout_ms)
  if (!res.ok) throw new Error(`claim HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`)
  await res.text()
}

function parseRpcText(text) {
  for (const line of String(text || '').split('\n')) {
    if (line.startsWith('data: ')) {
      try { return JSON.parse(line.slice(6)) } catch { /* 继续找下一行 */ }
    }
  }
  try { return JSON.parse(text) } catch { return {} }
}

/** streamable-http 的最小客户端：initialize → 会话头 → tools/call（与 phone_remind.bridge_call 同形）。 */
async function mcpTool(cfg, tool, args) {
  const headers = {
    'content-type': 'application/json',
    accept: 'application/json, text/event-stream',
  }
  const init = await fetchWithTimeout(cfg.phone_mcp_url, {
    method: 'POST', headers,
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { protocolVersion: '2025-03-26', capabilities: {},
        clientInfo: { name: 'info-hub-dsh-ask', version: '1' } },
    }),
  }, cfg.phone_timeout_ms)
  if (!init.ok) throw new Error(`手机 MCP initialize HTTP ${init.status}`)
  const sessionId = init.headers.get('mcp-session-id')
  await init.text()
  const hs = { ...headers }
  if (sessionId) {
    hs['mcp-session-id'] = sessionId
    await fetchWithTimeout(cfg.phone_mcp_url, {
      method: 'POST', headers: hs,
      body: JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }),
    }, cfg.phone_timeout_ms).then((r) => r.text()).catch(() => {})
  }
  const res = await fetchWithTimeout(cfg.phone_mcp_url, {
    method: 'POST', headers: hs,
    body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/call',
      params: { name: tool, arguments: args } }),
  }, cfg.phone_timeout_ms)
  if (!res.ok) throw new Error(`手机 MCP tools/call HTTP ${res.status}`)
  const payload = parseRpcText(await res.text())
  if (payload.error) throw new Error(`手机 MCP 错误: ${JSON.stringify(payload.error).slice(0, 300)}`)
  const result = payload.result || {}
  const content = result.content
  if (Array.isArray(content) && content.length) {
    try { return JSON.parse(content[0]?.text ?? '{}') } catch { return { raw: content[0]?.text } }
  }
  return result
}

async function pushPhone(cfg, payload) {
  const out = await mcpTool(cfg, 'hub_deliver', payload)
  if (!out || out.ok === false) {
    throw new Error(`手机拒绝推送: ${JSON.stringify(out).slice(0, 300)}`)
  }
  return out
}

async function dismissPhone(cfg, deliveryId) {
  return mcpTool(cfg, 'hub_dismiss', { delivery_id: deliveryId })
}

// --------------------------------------------------------------------- 轮询手机答案
function sleep(ms) { return new Promise((done) => setTimeout(done, ms)) }

/**
 * §12.4：手机答案只能从台账行看出来（手机侧调 /api/deliver/ack|reply 写库）。
 * 返回 {kind: reply|action|expired|timeout|error|cancelled}；不抛。
 */
export async function waitAnswer(cfg, ledger, deliveryId, { deadline, isCancelled }) {
  for (;;) {
    let row
    try { row = await ledger.get(deliveryId) } catch (error) { return { kind: 'error', error } }
    if (!row) return { kind: 'error', error: new Error(`投递行不见了: ${deliveryId}`) }
    if (row.status === 'replied') return { kind: 'reply', text: String(row.reply ?? ''), row }
    if (row.status === 'acked') return { kind: 'action', action: String(row.action || 'ack'), row }
    if (row.status === 'expired' || row.status === 'failed' || row.status === 'suppressed') {
      return { kind: 'expired', row }
    }
    if (typeof isCancelled === 'function' && isCancelled()) return { kind: 'cancelled', row }
    if (Date.now() >= deadline) return { kind: 'timeout', row }
    await sleep(Math.min(cfg.poll_ms, Math.max(1, deadline - Date.now())))
  }
}

/** 缺省依赖；selftest 用同形状的替身覆盖（全替身，不连真库/真手机）。 */
export function createDeps(cfg) {
  const ledger = ledgerClient(cfg)
  return {
    ledger,
    probeHub: () => probeHub(cfg),
    claim: (id) => claimDelivery(cfg, id),
    pushPhone: (payload) => pushPhone(cfg, payload),
    dismissPhone: (id) => dismissPhone(cfg, id),
    waitAnswer: (id, opts) => waitAnswer(cfg, ledger, id, opts),
  }
}

// --------------------------------------------------------------------- 请求 → 手机载荷
export function normalizeQuestions(raw) {
  const list = Array.isArray(raw) ? raw : []
  return list.map((q, index) => ({
    id: String(q?.id ?? `q${index + 1}`),
    question: String(q?.question ?? ''),
    options: Array.isArray(q?.options)
      ? q.options.map((o) => ({
        label: String(o?.label ?? ''),
        ...(o?.description !== undefined ? { description: String(o.description) } : {}),
      }))
      : [],
    ...(q?.multiSelect !== undefined ? { multiSelect: q.multiSelect === true } : {}),
  }))
}

function truncate(text, max) {
  const s = String(text ?? '').replace(/\s+/g, ' ').trim()
  return s.length <= max ? s : `${s.slice(0, Math.max(0, max - 1))}…`
}

/**
 * §12.3：手机按钮只能用 §11.2 的动作键回传，所以按位置把第一问的选项映射到 ack/done/snooze。
 * 台账列的 options 与这里同源（见 buildLedgerOptions）——手机主页与电脑控制台都直接拿它画按钮。
 */
export function buildPhoneOptions(questions) {
  const q0 = questions[0]
  if (!q0) return []
  const opts = q0.options || []
  if (opts.length === 0) {
    return [{ key: 'ack', label: '知道了', description: truncate(q0.question, 120) }]
  }
  return opts.slice(0, MAX_PHONE_OPTIONS).map((option, index) => ({
    key: PHONE_KEYS[index],
    label: option.label || `选项${index + 1}`,
    description: option.description || '',
  }))
}

/**
 * §12.3：台账/页面 options —— **能点的按钮就是第一问的真实选项**（key 用 §11.2 的稳定动作键，
 * label 用 DSH 的选项原文，这样手机主页点按钮 = 回一个能精确匹配回的 label）。
 * 没有选项时给一个「知道了」（ack）。多问只能按钮答第一问，其余走文本，body 里已写「N 个问题」。
 */
export function buildLedgerOptions(questions) {
  const q0 = questions[0]
  if (!q0) return []
  const opts = q0.options || []
  if (!opts.length) return [{ key: 'ack', label: '知道了', description: truncate(q0.question, 120) }]
  return opts.slice(0, MAX_PHONE_OPTIONS).map((option, index) => ({
    key: PHONE_KEYS[index],
    label: option.label || `选项${index + 1}`,
    description: option.description || '',
  }))
}

/** §12.3：layout v1（title=第一问、body=问题数、input.enabled=true）。 */
export function buildPhonePayload(questions, deliveryId, cfg) {
  const q0 = questions[0] || { question: '', options: [] }
  const title = truncate(q0.question, 80) || 'DSH 提问'
  let body = `${questions.length} 个问题`
  if ((q0.options || []).length > MAX_PHONE_OPTIONS) {
    body += `（手机最多 ${MAX_PHONE_OPTIONS} 个选项，其余请在电脑上作答或文本回复）`
  }
  const options = buildPhoneOptions(questions)
  return {
    delivery_id: deliveryId,
    mode: 'question',
    title,
    body,
    options,
    layout: {
      v: 1,
      title,
      body,
      options,
      input: { enabled: true, placeholder: '回复…' },
    },
    timeout_s: Math.min(86400, Math.ceil(cfg.timeout_ms / 1000)),
  }
}

function exactOptionLabel(question, text) {
  const target = String(text ?? '').trim()
  if (!target) return ''
  for (const option of question?.options || []) {
    const label = String(option.label ?? '').trim()
    if (label && label === target) return label
  }
  return ''
}

/**
 * §12.4：手机答案 → 瀑布返回值 {answers:[{id,selected,custom?}]}。
 * DSH 服务要求「每个问题恰好一次」（BAD_ANSWER），所以未作答的问题补 selected=[]（= 跳过）。
 */
export function buildAnswers(questions, answer) {
  const items = questions.map((q) => ({ id: q.id, selected: [] }))
  if (!items.length) return { answers: [] }
  const q0 = questions[0]
  if (answer.kind === 'reply') {
    const text = String(answer.text ?? '')
    if (text.trim()) {
      const hit = exactOptionLabel(q0, text)
      if (hit) items[0].selected = [hit]
      else items[0].custom = text
    }
    return { answers: items }
  }
  const index = PHONE_KEYS.indexOf(String(answer.action ?? ''))
  const opts = q0.options || []
  if (index >= 0 && index < opts.length) {
    const label = String(opts[index]?.label ?? '')
    if (label) items[0].selected = [label]
  }
  return { answers: items }
}

function isNoProvider(error) {
  const code = String(error?.code ?? '')
  const message = String(error?.message ?? error ?? '')
  return code === 'NO_PROVIDER' || /NO_PROVIDER/.test(message)
}

// --------------------------------------------------------------------- 处理器
export function createHandler(cfg, deps = createDeps(cfg)) {
  /** §12.4：双弹 + 先到先得；只结算一次。 */
  return async function onUserQuestionsRequest(request, next) {
    const questions = normalizeQuestions(request && request.questions)
    if (!questions.length) return next()
    // 契约 §12.2 的 debug：成功路径原本完全静默，「插件没被调用」与「调用了但没答案」看日志分不开。
    warn(cfg, 'request', 
      questions.length, 'agent=', request && request.agent ? 'yes' : 'none')

    // 1. 先调 next() 让 DSH 自带答复器照常工作（卡片先弹）；结果只记不下判断，避免两路互相等。
    let dshCall
    try { dshCall = Promise.resolve(next()) } catch (error) { dshCall = Promise.reject(error) }
    const dshOutcome = dshCall.then(
      (value) => ({ ok: true, value }), (error) => ({ ok: false, error }))

    // 2. 建台账行（这一侧失败才算「手机侧不可用」）；推手机尽力而为 ——
    //    手机主页与常驻通知靠轮询取行，推不动照样能答（§12.4 2026-11-17 修订）。
    let deliveryId = null
    let payload = null
    try {
      await deps.probeHub()
      payload = buildPhonePayload(questions, '', cfg)
      deliveryId = await deps.ledger.add({
        channel: 'dsh',
        mode: 'question',
        title: payload.title,
        body: payload.body,
        options: buildLedgerOptions(questions),
        layout: payload.layout,
      })
      await deps.claim(deliveryId)
    } catch (error) {
      warn(cfg, 'ledger side unavailable:', String(error?.message ?? error))
      if (deliveryId) {
        try { await deps.ledger.expire(deliveryId) } catch { /* 尽力而为 */ }
      }
      deliveryId = null
    }
    if (deliveryId) {
      try {
        await deps.pushPhone({ ...payload, delivery_id: deliveryId })
        warn(cfg, 'phone pushed', deliveryId)
      } catch (error) {
        warn(cfg, 'phone push failed (行保留，手机靠轮询取):', String(error?.message ?? error))
      }
    }

    if (!deliveryId) {
      const outcome = await dshOutcome
      if (outcome.ok) return outcome.value
      throw outcome.error
    }

    const state = { settled: false, dismiss: null }
    const dismissOnce = () => {
      if (state.dismiss) return state.dismiss
      state.dismiss = (async () => {
        try { await deps.dismissPhone(deliveryId) } catch (error) {
          warn(cfg, 'hub_dismiss failed:', String(error?.message ?? error))
        }
      })()
      return state.dismiss
    }

    const settlePhone = async (answer) => {
      if (state.settled) return null
      state.settled = true
      try {
        if (answer.kind === 'reply') await deps.ledger.reply(deliveryId, answer.text)
        else await deps.ledger.ack(deliveryId, answer.action)
      } catch (error) {
        // 手机侧通常已经写过同一行；这里失败不改变已拿到的答案（§12.4）。
        warn(cfg, 'ledger settle failed:', String(error?.message ?? error))
      }
      await dismissOnce()
      return buildAnswers(questions, answer)
    }

    const settleDsh = async (value) => {
      if (!state.settled) {
        state.settled = true
        await Promise.allSettled([deps.ledger.expire(deliveryId), dismissOnce()])
      }
      return value
    }

    const answerPromise = Promise.resolve()
      .then(() => deps.waitAnswer(deliveryId, {
        deadline: Date.now() + cfg.timeout_ms,
        isCancelled: () => state.settled,
      }))
      .catch((error) => ({ kind: 'error', error }))

    const first = await Promise.race([
      dshOutcome.then((outcome) => ({ side: 'dsh', outcome })),
      answerPromise.then((answer) => ({ side: 'phone', answer })),
    ])

    if (first.side === 'dsh') {
      const { outcome } = first
      if (outcome.ok) return settleDsh(outcome.value)
      if (isNoProvider(outcome.error)) {
        // §12.4：DSH 侧没有答复器 ⇒ 只用手机答案（仍受 timeout_ms 约束）。
        const answer = await answerPromise
        if (answer.kind === 'reply' || answer.kind === 'action') return settlePhone(answer)
        await settleDsh(undefined)
        throw outcome.error
      }
      await Promise.allSettled([deps.ledger.expire(deliveryId), dismissOnce()])
      state.settled = true
      throw outcome.error
    }

    const answer = first.answer
    if (answer.kind === 'reply' || answer.kind === 'action') {
      return settlePhone(answer)
    }

    // 手机侧超时 / 行被别人作废 / 读行失败 ⇒ 释放认领、收回卡片，只等 DSH（§12.4）。
    state.settled = true
    await Promise.allSettled([deps.ledger.expire(deliveryId), dismissOnce()])
    const outcome = await dshOutcome
    if (outcome.ok) return outcome.value
    throw outcome.error
  }
}

/** DSH 插件入口：enabled=false 时不注册任何监听（§12.2）。 */
export function apply(ctx, config) {
  const cfg = normalizeConfig(config)
  if (!cfg.enabled) return { enabled: false }
  const handler = createHandler(cfg)
  ctx.on('user-questions/request', handler)
  return { enabled: true }
}

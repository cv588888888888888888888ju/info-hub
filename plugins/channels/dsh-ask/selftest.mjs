/**
 * dsh-ask selftest（A12 / A20）：全替身，不连真库、不连真手机、不弹真通知。
 *
 * 跑法：`cd info-hub/plugins/channels/dsh-ask && node selftest.mjs`；全过退出 0。
 * 判定口径见 INTERFACE.md §12.8；判据原文见任务书 §3 的 A12 / A20。
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { apply, buildAnswers, createHandler, normalizeConfig, waitAnswer } from './index.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const sleep = (ms) => new Promise((done) => setTimeout(done, ms))

// --------------------------------------------------------------------- 替身
function fakeLedger() {
  const rows = new Map()
  const calls = { add: [], get: 0, expire: [], ack: [], reply: [], claim: [] }
  let seq = 0
  return {
    rows,
    calls,
    async add(fields) {
      seq += 1
      const id = `d-test-${String(seq).padStart(2, '0')}`
      rows.set(id, {
        id, status: 'pending', action: null, reply: null,
        lease_owner: null, lease_until: null, ...fields,
      })
      calls.add.push({ id, ...fields })
      return id
    },
    async get(id) {
      calls.get += 1
      const row = rows.get(id)
      return row ? { ...row } : null
    },
    async expire(id) {
      calls.expire.push(id)
      const row = rows.get(id)
      if (row) {
        if (!['acked', 'replied', 'suppressed'].includes(row.status)) row.status = 'expired'
        row.lease_owner = null
        row.lease_until = null
      }
      return row ? { ...row } : null
    },
    async claim(id, owner = 'dsh-ask:test') {
      calls.claim.push({ id, owner })
      const row = rows.get(id)
      if (row) { row.lease_owner = owner; row.lease_until = '2099-01-01T00:00:00+08:00' }
    },
    async ack(id, action) {
      calls.ack.push({ id, action })
      const row = rows.get(id)
      if (row && !['replied', 'suppressed', 'expired'].includes(row.status)) {
        row.status = 'acked'
        row.action = action ?? null
      }
      return row ? { ...row } : null
    },
    async reply(id, text) {
      calls.reply.push({ id, text })
      const row = rows.get(id)
      if (row) { row.status = 'replied'; row.reply = text }
      return row ? { ...row } : null
    },
  }
}

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function makeDeps(ledger, overrides = {}) {
  const state = { dismissCount: 0, waitCalls: 0 }
  const deps = {
    ledger,
    probeHub: async () => {},
    claim: async (id) => { await ledger.claim(id) },
    pushPhone: async () => ({ ok: true }),
    dismissPhone: async () => { state.dismissCount += 1 },
    waitAnswer: () => { state.waitCalls += 1; return new Promise(() => {}) },
    ...overrides,
  }
  return { deps, state }
}

function question(id, text, options = []) {
  return { id, question: text, options }
}

function config(extra = {}) {
  return normalizeConfig({
    enabled: true,
    timeout_ms: 5000,
    poll_ms: 5,
    hub_root: join(here, '../../..'),
    ...extra,
  })
}

// --------------------------------------------------------------------- 断言框架
const results = []
async function check(name, fn) {
  try {
    await fn()
    results.push({ name, ok: true })
    console.log(`PASS  ${name}`)
  } catch (error) {
    results.push({ name, ok: false, error })
    console.log(`FAIL  ${name}`)
    console.log(`      ${String(error && error.stack || error).split('\n').slice(0, 6).join('\n      ')}`)
  }
}

// --------------------------------------------------------------------- 用例
async function main() {
  await check('A12-0 清单：plugin.yaml kind=channel / runtime=node / default_enabled=false', () => {
    const yaml = readFileSync(join(here, 'plugin.yaml'), 'utf8')
    assert.match(yaml, /^name: dsh-ask$/m)
    assert.match(yaml, /^kind: channel$/m)
    assert.match(yaml, /^runtime: node$/m)
    assert.match(yaml, /^entry: index\.mjs:apply$/m)
    assert.match(yaml, /^default_enabled: false$/m)
    const pkg = JSON.parse(readFileSync(join(here, 'package.json'), 'utf8'))
    assert.equal(pkg.type, 'module')
    assert.equal(pkg.main, 'index.mjs')
    assert.equal(pkg.dsh.bundle.patch, './cordis.patch.yml')
  })

  await check('A12-1 DSH 先答：返回 answers 原样；hub_dismiss 恰一次；行 expired；迟到手机答案被丢弃', async () => {
    const ledger = fakeLedger()
    const late = deferred()
    const { deps, state } = makeDeps(ledger, { waitAnswer: () => late.promise })
    const handler = createHandler(config(), deps)
    const dshAnswer = { answers: [{ id: 'q1', selected: ['是'] }] }
    const out = await handler({ questions: [question('q1', '继续吗？', [{ label: '是' }, { label: '否' }])] },
      async () => dshAnswer)
    assert.deepEqual(out, dshAnswer)
    assert.equal(state.dismissCount, 1)
    const row = ledger.rows.get('d-test-01')
    assert.equal(row.status, 'expired')
    late.resolve({ kind: 'action', action: 'done' })
    await sleep(10)
    assert.equal(ledger.calls.ack.length, 0, '迟到的手机答案不得二次结算')
    assert.equal(ledger.calls.reply.length, 0)
    assert.equal(state.dismissCount, 1, 'hub_dismiss 只能一次')
  })

  await check('A12-2 手机先答（选项）：瀑布返回手机答案（selected）；dsh 结果不再结算', async () => {
    const ledger = fakeLedger()
    const dshLater = deferred()
    const { deps, state } = makeDeps(ledger, {
      waitAnswer: async () => ({ kind: 'action', action: 'done' }),
    })
    const handler = createHandler(config(), deps)
    const out = await handler({ questions: [question('q1', '继续吗？', [{ label: '是' }, { label: '否' }])] },
      async () => dshLater.promise)
    assert.deepEqual(out, { answers: [{ id: 'q1', selected: ['否'] }] })
    assert.equal(state.dismissCount, 1)
    assert.deepEqual(ledger.calls.ack, [{ id: 'd-test-01', action: 'done' }])
    assert.equal(ledger.rows.get('d-test-01').status, 'acked')
    dshLater.resolve({ answers: [{ id: 'q1', selected: ['是'] }] })
    await sleep(10)
    assert.equal(ledger.calls.ack.length, 1, '只结算一次')
    assert.equal(state.dismissCount, 1)
    assert.equal(ledger.rows.get('d-test-01').status, 'acked')
  })

  await check('A12-3 手机先答后 DSH 迟到：丢弃 DSH 结果，不二次结算', async () => {
    const ledger = fakeLedger()
    const dshLater = deferred()
    const { deps, state } = makeDeps(ledger, {
      waitAnswer: async () => ({ kind: 'reply', text: '收到，明天做' }),
    })
    const handler = createHandler(config(), deps)
    const out = await handler({ questions: [question('q1', '什么时候做？')] },
      async () => dshLater.promise)
    assert.deepEqual(out, { answers: [{ id: 'q1', selected: [], custom: '收到，明天做' }] })
    assert.deepEqual(ledger.calls.reply, [{ id: 'd-test-01', text: '收到，明天做' }])
    dshLater.resolve({ answers: [{ id: 'q1', selected: ['x'] }] })
    await sleep(10)
    assert.equal(ledger.calls.reply.length, 1)
    assert.equal(ledger.calls.expire.length, 0, '手机先答不得把行置 expired')
    assert.equal(state.dismissCount, 1)
    assert.equal(ledger.rows.get('d-test-01').status, 'replied')
  })

  await check('A12-4 hub 不可达：只用 DSH 答案，不建行、不推手机', async () => {
    const ledger = fakeLedger()
    const { deps, state } = makeDeps(ledger, {
      probeHub: async () => { throw new Error('hub 探活 HTTP 000') },
    })
    const handler = createHandler(config(), deps)
    const dshAnswer = { answers: [{ id: 'q1', selected: [] }] }
    const out = await handler({ questions: [question('q1', '在吗？')] }, async () => dshAnswer)
    assert.deepEqual(out, dshAnswer)
    assert.equal(ledger.calls.add.length, 0)
    assert.equal(state.dismissCount, 0)
    assert.equal(state.waitCalls, 0, 'hub 不可达时不应开始等手机')
  })

  await check('A12-5 推手机失败：行保留、照常等手机轮询答案（DSH 先答则收卡片）', async () => {
    const ledger = fakeLedger()
    const { deps, state } = makeDeps(ledger, {
      pushPhone: async () => { throw new Error('手机 MCP 拒绝') },
    })
    const handler = createHandler(config(), deps)
    const dshAnswer = { answers: [{ id: 'q1', selected: ['是'] }] }
    const out = await handler({ questions: [question('q1', '继续吗？', [{ label: '是' }])] },
      async () => dshAnswer)
    assert.deepEqual(out, dshAnswer)
    assert.equal(ledger.calls.add.length, 1)
    assert.equal(state.waitCalls, 1, '推失败不等于手机侧不可用：仍要等轮询到的答案')
    assert.equal(ledger.rows.get('d-test-01').status, 'expired', 'DSH 先答 ⇒ 该行作废')
    assert.equal(state.dismissCount, 1)
  })

  await check('A12-6 超时：timeout_ms 到点释放认领（清租约）并收回手机卡片，之后仍等 DSH', async () => {
    const ledger = fakeLedger()
    const dshLater = deferred()
    // 用真实 waitAnswer：poll_ms=5 / timeout_ms=40，行一直是 pending。
    const cfg = config({ timeout_ms: 40, poll_ms: 5 })
    const { deps, state } = makeDeps(ledger, {
      waitAnswer: (id, opts) => waitAnswer(cfg, ledger, id, opts),
    })
    const handler = createHandler(cfg, deps)
    let settled = false
    const run = handler({ questions: [question('q1', '继续吗？', [{ label: '是' }, { label: '否' }])] },
      async () => dshLater.promise).then((value) => { settled = true; return value })

    await sleep(30)
    const row = ledger.rows.get('d-test-01')
    assert.equal(row.lease_owner, 'dsh-ask:test', '认领应已写入')
    await sleep(60)
    assert.equal(row.status, 'expired', '超时后行应 expired')
    assert.equal(row.lease_owner, null, '超时后租约必须释放')
    assert.equal(row.lease_until, null)
    assert.equal(state.dismissCount, 1, '超时后收回手机卡片恰一次')
    assert.equal(settled, false, '超时后仍在等 DSH')
    assert.equal(ledger.calls.ack.length, 0)
    assert.equal(ledger.calls.reply.length, 0)

    dshLater.resolve({ answers: [{ id: 'q1', selected: ['是'] }] })
    const out = await run
    assert.deepEqual(out, { answers: [{ id: 'q1', selected: ['是'] }] })
    assert.equal(state.dismissCount, 1, '超时分支的 hub_dismiss 只一次')
  })

  await check('A12-7 自定义文本 → replied；多问题各自恰好一条答案', async () => {
    const ledger = fakeLedger()
    const { deps } = makeDeps(ledger, {
      waitAnswer: async () => ({ kind: 'reply', text: '收到，明天做' }),
    })
    const handler = createHandler(config(), deps)
    const questions = [question('q1', '什么时候做？'), question('q2', '需要谁参与？')]
    const out = await handler({ questions }, async () => new Promise(() => {}))
    assert.equal(out.answers.length, 2)
    assert.deepEqual(out.answers.map((a) => a.id), ['q1', 'q2'])
    assert.deepEqual(out.answers[0], { id: 'q1', selected: [], custom: '收到，明天做' })
    assert.deepEqual(out.answers[1], { id: 'q2', selected: [] })
    assert.deepEqual(ledger.calls.reply, [{ id: 'd-test-01', text: '收到，明天做' }])
    assert.equal(ledger.rows.get('d-test-01').status, 'replied')
  })

  await check('A12-8 DSH 侧 NO_PROVIDER：只用手机答案', async () => {
    const ledger = fakeLedger()
    const { deps } = makeDeps(ledger, {
      waitAnswer: async () => ({ kind: 'action', action: 'ack' }),
    })
    const handler = createHandler(config(), deps)
    const error = new Error('no user-questions answerer accepted the request')
    error.code = 'NO_PROVIDER'
    const out = await handler({ questions: [question('q1', '继续吗？')] }, async () => { throw error })
    assert.deepEqual(out, { answers: [{ id: 'q1', selected: [] }] })
    assert.equal(ledger.rows.get('d-test-01').status, 'acked')
  })

  await check('A12-9 两路皆败：原样抛 DSH 错误', async () => {
    const ledger = fakeLedger()
    const { deps } = makeDeps(ledger, {
      waitAnswer: async () => ({ kind: 'timeout' }),
    })
    const handler = createHandler(config({ timeout_ms: 20, poll_ms: 5 }), deps)
    const boom = new Error('ASK_ABORTED')
    await assert.rejects(
      () => handler({ questions: [question('q1', '继续吗？')] }, async () => { throw boom }),
      (error) => error === boom,
    )
    assert.equal(ledger.rows.get('d-test-01').status, 'expired')
  })

  await check('A20 apply：enabled=false 不注册；enabled=true 注册瀑布监听（不注册同名工具）', () => {
    const off = { handlers: [], on(event, fn) { this.handlers.push({ event, fn }) } }
    apply(off, { enabled: false })
    assert.equal(off.handlers.length, 0)

    const on = { handlers: [], on(event, fn) { this.handlers.push({ event, fn }) } }
    const out = apply(on, { enabled: true, timeout_ms: 1000 })
    assert.equal(out.enabled, true)
    assert.equal(on.handlers.length, 1)
    assert.equal(on.handlers[0].event, 'user-questions/request')
    assert.equal(typeof on.handlers[0].fn, 'function')
    assert.equal(typeof on.tools, 'undefined', '不得注册工具（会撞 already registered）')
  })

  await check('A12-10 buildAnswers：手机选项按位置映射到第一问；未答问题补 skipped', () => {
    const qs = [question('q1', '选一个', [{ label: '甲' }, { label: '乙' }, { label: '丙' }]),
      question('q2', '补充？')]
    assert.deepEqual(buildAnswers(qs, { kind: 'action', action: 'snooze' }),
      { answers: [{ id: 'q1', selected: ['丙'] }, { id: 'q2', selected: [] }] })
    assert.deepEqual(buildAnswers(qs, { kind: 'reply', text: '乙' }),
      { answers: [{ id: 'q1', selected: ['乙'] }, { id: 'q2', selected: [] }] })
  })

  const failed = results.filter((r) => !r.ok)
  console.log(`SUMMARY ${JSON.stringify({ checks: results.length, failed: failed.length, failed_names: failed.map((r) => r.name) })}`)
  if (failed.length) process.exitCode = 1
}

main().catch((error) => {
  console.error('selftest 崩了:', error)
  process.exitCode = 1
})

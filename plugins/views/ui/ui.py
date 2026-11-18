"""本地控制台（M3 UI）。契约见 INTERFACE.md §2.6 / §4.7。

一条纪律：**UI 只能改报表，不能改系统数据**（用户 m01271「报表和系统数据分离」）。
所以除 `GET /api/state`（只读）外，唯一的写入面是 `/api/save`（写报表文本，留痕 author=human）
和 `/api/generate`（只重写自动区块，留痕 author=hub）。本进程只绑 loopback，并校验 Host/Origin。

服务是**单线程**的（`HTTPServer` 而非 ThreadingHTTPServer）：`sqlite3` 连接只能在其创建线程里用，
单线程就没这个问题，本地单用户控制台也不需要并发。

媒体面板（/api/assets、/asset/<id>、/thumbs/<id>）只读允许根里的图片与文档，
契约见 `plugins/views/ui/README-ASSETS.md`。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import signal
import socket
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from infohub import deliver as D
from infohub import views
from infohub.events import iso, iso_from_epoch

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_LOOP_RE = re.compile(r"^(?:127(?:\.\d{1,3}){3}|localhost|\[::1\]|::1)$")

_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>info-hub 控制台</title><style>
:root{--bg:#14161a;--panel:#1c1f25;--line:#2b3038;--fg:#e6e8ec;--dim:#9aa3b2;--ok:#4caf7d;--warn:#d8a657}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.6 system-ui,"Noto Sans CJK SC",sans-serif}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
header b{font-size:15px}header span{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:minmax(300px,1fr) minmax(420px,1.4fr);gap:14px;padding:14px}
section{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
h3{margin:0 0 8px;font-size:13px;color:var(--dim);font-weight:600;letter-spacing:.04em}
table{width:100%;border-collapse:collapse;font-size:12px}td,th{padding:3px 6px;text-align:left;
 border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--dim);font-weight:500}
textarea{width:100%;height:46vh;background:#0f1114;color:var(--fg);border:1px solid var(--line);
 border-radius:6px;padding:10px;font:12px/1.55 ui-monospace,monospace;resize:vertical}
button{background:#2a6df4;color:#fff;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:13px}
button.ghost{background:#252a33;color:var(--fg)}button:disabled{opacity:.5;cursor:default}
.row{display:flex;gap:10px;align-items:center;margin:10px 0 6px}
.msg{color:var(--dim);font-size:12px}.err{color:#e06c75}.ok{color:var(--ok)}
ul{margin:4px 0 0;padding-left:18px;font-size:12px;color:var(--dim)}li{margin:2px 0}
code{background:#0f1114;padding:1px 4px;border-radius:3px}
p.hint{margin:6px 0 0;font-size:12px;color:var(--dim)}
.dlv{border-bottom:1px solid var(--line);padding:7px 0}.dlv:last-child{border-bottom:0}
.composer{display:flex;gap:8px;align-items:flex-start;margin:6px 0 8px}
.composer textarea{height:60px;flex:1}
.atts{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.atts img{height:64px;border-radius:4px;border:1px solid var(--line);object-fit:cover}
.atts .doc{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:3px 7px;color:var(--fg)}
.dlv .row{margin:6px 0 0}.dlv pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;
 background:#0f1114;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px}
#todos{max-height:45vh;overflow:auto}
#media{grid-column:1/-1}
.mediagrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:8px}
.mediagrid figure{margin:0;background:#0f1114;border:1px solid var(--line);border-radius:6px;overflow:hidden}
.mediagrid a{display:block;line-height:0}
.mediagrid img{display:block;width:100%;height:96px;object-fit:cover}
.mediagrid figcaption{font-size:11px;color:var(--dim);padding:4px 6px;line-height:1.3;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style></head><body>
<header><b>info-hub 控制台</b><span id="meta">加载中…</span></header>
<main>
 <div>
  <section><h3>消息（输入 / 归档）</h3>
   <div class="composer"><textarea id="msgin" rows="2" placeholder="打一条消息，Ctrl+Enter 发送"></textarea>
    <button id="msgsend">发送</button></div>
   <div class="row"><label class="msg"><input type="checkbox" id="msgarch"> 显示归档</label>
    <button class="ghost" id="msgreload">刷新</button><span id="msgstate" class="msg"></span></div>
   <div id="messages" class="msg">…</div>
   <p class="hint">归档只从默认列表移走，数据仍在；消息里的图片点开看原图、文档点开即下。</p></section>
  <section style="margin-top:14px"><h3>系统数据（只读）</h3><div id="stats" class="msg">…</div></section>
  <section style="margin-top:14px"><h3>待办（可改：完成 / 忽略 / 重开 / 稍后 1 小时）</h3><div id="todos" class="msg">…</div>
   <div class="msg">清单的每次改动都留痕：<ul id="todorevs"></ul></div>
   <div id="tododetail" class="msg"></div></section>
  <section style="margin-top:14px"><h3>投递（待确认：确认 / 回复）</h3><div id="deliveries" class="msg">…</div>
   <div id="dvmsg" class="msg"></div>
   <p class="hint">「确认」只记「看见了」，不改待办状态；「回复」把原文原样留档（空文本不发请求）。</p></section>
  <section style="margin-top:14px"><h3>通知台账（只读）</h3><div id="notifs" class="msg">…</div></section>
  <section style="margin-top:14px"><h3>已抑制的类别（只读）</h3><div id="sup" class="msg">…</div></section>
 </div>
 <div>
  <section><h3>报表 <span id="rname"></span></h3>
   <textarea id="text" spellcheck="false"></textarea>
   <div class="row"><button id="save">保存（记一次修改）</button>
    <button id="gen" class="ghost">重写自动区块</button>
    <span id="msg" class="msg"></span></div>
   <div class="msg">自动区块之外都是你的文字，机器不会覆盖。</div></section>
  <section style="margin-top:14px"><h3>修改记录</h3><ul id="revs"></ul></section>
 </div>
 <section id="media"><h3>媒体（图片 / 文档，只读）</h3>
  <div class="row"><button class="ghost" id="mediareload">刷新</button>
   <span id="mediamsg" class="msg"></span></div>
  <div class="msg">图片（点开看原图；缩略图按需加载）</div>
  <div id="mediagrid" class="mediagrid"></div>
  <div class="msg" style="margin-top:10px">文档（名称 / 大小 / 时间 / 打开）</div>
  <div id="medialist" class="msg">…</div>
  <p class="hint">只读：这里只列出允许根里的图片与文档，不写任何系统数据。</p>
 </section>
</main>
<template id="delivery-row"><div class="dlv">
 <div><b data-f="title"></b> <code data-f="id"></code></div>
 <div class="msg" data-f="body"></div>
 <div class="msg">通道 <span data-f="channel"></span> · 状态 <span data-f="status"></span></div>
 <details hidden><summary class="msg">layout</summary><pre data-f="layout"></pre></details>
 <div class="row"><button data-act="ack">确认</button><button class="ghost" data-act="reply">回复</button></div>
</div></template>
<template id="message-row"><div class="dlv msgrow">
 <div><b data-f="title"></b> <code data-f="id"></code></div>
 <div class="msg" data-f="body" style="white-space:pre-wrap"></div>
 <div class="atts" data-f="atts"></div>
 <div class="msg"><span data-f="channel"></span> · <span data-f="status"></span> · <span data-f="ts"></span> <span data-f="archived"></span></div>
 <div class="row"><button class="ghost" data-act="archive">归档</button>
  <button data-act="ack">确认</button><button class="ghost" data-act="reply">回复</button></div>
</div></template>
<script>
const $ = (id) => document.getElementById(id);
let report = "";
async function todoDetail(id) {
  const r = await fetch("/api/todo/detail?id=" + encodeURIComponent(id));
  if (r.status === 404) { $("tododetail").textContent = "找不到这条待办：" + id; return; }
  const d = await r.json();
  const t = d.todo;
  const ev = d.evidence.map(e => e.missing
    ? `<li><code>#${e.id}</code> <b>missing</b>（原事件已不在库中）</li>`
    : `<li>${e.ts.slice(5, 19)} · <b>${e.source}/${e.kind}</b> · <code>${JSON.stringify(e.payload).slice(0, 160)}</code></li>`
  ).join("");
  const rv = d.revisions.map(v => `<li>${v.at.slice(5, 16)} · ${v.author} · ${v.field}: ${v.before || "-"} → ${v.after || "-"}（${v.reason || "-"}）</li>`).join("");
  $("tododetail").innerHTML =
    `<h3>依据 · <code>${t.id}</code></h3>` +
    `<div>${t.title}</div>` +
    `<div class="msg">为什么：${t.why || "-"} · 置信度：${t.confidence} · 状态：${t.status}${t.human_override ? "（人改）" : ""}</div>` +
    `<div class="msg">原始事件（${d.evidence.length} 条，只读）：</div><ul>${ev || "<li>（没有依据事件）</li>"}</ul>` +
    `<div class="msg">这条待办的改动记录：</div><ul>${rv || "<li>（还没改过）</li>"}</ul>`;
}

async function todoAct(id, status) {
  const r = await fetch("/api/todo", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: id, status: status, reason: "ui"})});
  const d = await r.json();
  $("msg").textContent = d.error ? ("失败：" + d.error) : `${id} → ${status}（已留痕）`;
  await load();
}
async function todoSnooze(id) {
  const r = await fetch("/api/todo", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: id, snooze_minutes: 60, reason: "ui"})});
  const d = await r.json();
  $("msg").textContent = d.error ? ("失败：" + d.error) : `${id} 推迟 60 分钟`;
  await load();
}
// 消息中心（INTERFACE.md §11.9）：列表 / 发送 / 归档 / 附件渲染。
function attId(rel) {
  return btoa(unescape(encodeURIComponent(rel)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
async function loadMessages() {
  const box = $("messages"), state = $("msgstate");
  const arch = $("msgarch").checked ? "1" : "0";
  state.textContent = "…"; state.className = "msg";
  try {
    const r = await fetch("/api/messages?limit=100&archived=" + arch);
    const d = await r.json();
    if (!r.ok) { box.textContent = "消息加载失败：" + (d.error || r.status); return; }
    const rows = d.messages || [];
    box.innerHTML = "";
    state.textContent = rows.length + " 条" + (arch === "1" ? "（归档）" : "");
    if (!rows.length) { box.textContent = arch === "1" ? "（没有归档消息）" : "（还没有消息，上面打一条）"; return; }
    const tpl = $("message-row");
    for (const row of rows) {
      const el = tpl.content.firstElementChild.cloneNode(true);
      el.dataset.id = row.id;
      const set = (f, v) => {
        const c = el.querySelector('[data-f="' + f + '"]');
        if (c) c.textContent = v == null ? "" : String(v);
      };
      set("id", row.id); set("title", row.title); set("body", row.body);
      set("channel", row.channel); set("status", row.status);
      set("ts", String(row.created_at || "").slice(5, 16).replace("T", " "));
      if (row.archived_at) set("archived", "· 已归档 " + String(row.archived_at).slice(5, 16));
      const atts = el.querySelector('[data-f="atts"]');
      for (const rel of (row.attachments || [])) {
        const id = attId(rel);
        const a = document.createElement("a");
        a.href = "/asset/" + id; a.target = "_blank"; a.rel = "noopener"; a.title = rel;
        if (/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(rel)) {
          const img = document.createElement("img");
          img.loading = "lazy"; img.alt = rel; img.src = "/thumbs/" + id;
          img.onerror = () => { a.textContent = "附件缺失：" + rel; };
          a.appendChild(img);
        } else {
          a.className = "doc"; a.textContent = rel.split("/").pop();
        }
        atts.appendChild(a);
      }
      el.querySelector('[data-act="archive"]').onclick = () => archiveMessage(row.id);
      const ack = el.querySelector('[data-act="ack"]'), rep = el.querySelector('[data-act="reply"]');
      if (row.status !== "pending") { ack.remove(); rep.remove(); }
      else { ack.onclick = () => deliveryAck(row.id); rep.onclick = () => deliveryReply(row.id); }
      box.appendChild(el);
    }
  } catch (e) { box.textContent = "消息加载失败：" + e; }
}
async function archiveMessage(id) {
  const r = await fetch("/api/messages/archive", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: id})});
  const d = await r.json();
  $("msgstate").textContent = r.ok ? (id + " 已归档") : ("归档失败：" + (d.error || r.status));
  $("msgstate").className = r.ok ? "msg ok" : "msg err";
  await loadMessages();
}
async function sendMessage() {
  const box = $("msgin");
  const text = box.value;
  if (!text.trim()) { $("msgstate").textContent = "空消息不发请求"; $("msgstate").className = "msg err"; return; }
  const r = await fetch("/api/messages", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: text})});
  const d = await r.json();
  if (!r.ok) { $("msgstate").textContent = "发送失败：" + (d.error || r.status); $("msgstate").className = "msg err"; return; }
  box.value = "";
  $("msgstate").textContent = "已发送 " + d.id; $("msgstate").className = "msg ok";
  await loadMessages();
}
$("msgsend").onclick = () => sendMessage();
$("msgin").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendMessage(); }
});
$("msgarch").onchange = () => loadMessages();
$("msgreload").onclick = () => loadMessages();
loadMessages().catch(e => { $("messages").textContent = "消息加载失败：" + e; });
// 投递面板（S6）
async function loadDeliveries() {
  const box = $("deliveries");
  try {
    const r = await fetch("/api/deliver/pending?limit=50");
    const d = await r.json();
    if (!r.ok) { box.textContent = "加载失败：" + (d.error || r.status); return; }
    // 控制台自己发的消息已经在「消息」区显示，这里不再重复（只留外部通道待确认的）
    const rows = (d.deliveries || []).filter((x) => x.channel !== "console");
    box.innerHTML = "";
    if (!rows.length) { box.textContent = "（没有待确认的投递）"; return; }
    const tpl = $("delivery-row");
    for (const row of rows) {
      const el = tpl.content.firstElementChild.cloneNode(true);
      el.dataset.id = row.id;
      for (const f of ["id", "title", "body", "channel", "status"]) {
        const cell = el.querySelector('[data-f="' + f + '"]');
        if (cell) cell.textContent = row[f] == null ? "" : String(row[f]);
      }
      const lay = el.querySelector('[data-f="layout"]');
      if (lay && row.layout != null) {
        lay.textContent = typeof row.layout === "string" ? row.layout : JSON.stringify(row.layout, null, 2);
        lay.closest("details").hidden = false;
      }
      el.querySelector('[data-act="ack"]').onclick = () => deliveryAck(row.id);
      el.querySelector('[data-act="reply"]').onclick = () => deliveryReply(row.id);
      box.appendChild(el);
    }
  } catch (e) {
    box.textContent = "投递加载失败：" + e;
  }
}
function dvMsg(text, isOk) {
  $("dvmsg").textContent = text;
  $("dvmsg").className = isOk ? "msg ok" : "msg err";
}
async function deliveryAck(id) {
  const r = await fetch("/api/deliver/ack", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: id})});
  const d = await r.json();
  if (!r.ok) { dvMsg("确认失败：" + (d.error || r.status), false); return; }
  dvMsg(id + " 已确认（" + d.status + "）", true);
  await Promise.all([loadDeliveries(), loadMessages()]);
}
async function deliveryReply(id) {
  const text = prompt("回复 " + id + "（原文原样留档）");
  if (text === null) return;                                    // 取消
  if (!text.trim()) { dvMsg("空回复不发请求", false); return; }  // 空文本不得发请求
  const r = await fetch("/api/deliver/reply", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id: id, text: text})});
  const d = await r.json();
  if (!r.ok) { dvMsg("回复失败：" + (d.error || r.status), false); return; }
  dvMsg(id + " 已回复（" + d.status + "）", true);
  await Promise.all([loadDeliveries(), loadMessages()]);
}
async function load() {
  const r = await fetch("/api/state"); const d = await r.json();
  report = d.report.name;
  $("meta").textContent = `${d.system.events_total} 事件 · ${d.report.path}`;
  const st = d.system.stats;
  $("stats").innerHTML = `<table><tr><th>来源</th><th>条数</th></tr>` +
    Object.entries(st.per_source).sort().map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") +
    `</table><div class="msg">时间范围 ${st.events_first || "-"} → ${st.events_last || "-"}</div>`;
  $("todos").innerHTML = d.system.todos.length
    ? `<table><tr><th>id</th><th>状态</th><th>标题</th><th>操作</th></tr>` + d.system.todos.map(t =>
        `<tr><td><code>${t.id}</code></td><td>${t.status}${t.human_override ? "（人改）" : ""}</td><td>${t.title}</td>` +
        `<td><button onclick="todoDetail('${t.id}')">依据</button>` +
        `<button onclick="todoAct('${t.id}','done')">完成</button>` +
        `<button onclick="todoAct('${t.id}','dropped')">忽略</button>` +
        `<button onclick="todoSnooze('${t.id}')">稍后 1h</button></td></tr>`).join("") + `</table>`
      + `<p class="hint">已完成的待办不在此列（只看未完成）；改了就会进下面的记录。</p>`
    : "（没有未完成待办）";
  $("todorevs").innerHTML = (d.system.todo_revisions || []).length
    ? d.system.todo_revisions.map(v => `<li>${v.at.slice(5, 16)} · ${v.author} · <code>${v.todo_id}</code> · ${v.field}: ${v.before || "-"} → ${v.after || "-"}</li>`).join("")
    : "<li>（还没有改动）</li>";
  $("notifs").innerHTML = d.system.notifications.length
    ? `<table><tr><th>时间</th><th>类别</th><th>结果</th><th>内容</th></tr>` + d.system.notifications.map(n =>
        `<tr><td>${n.ts.slice(5, 16)}</td><td>${n.kind}</td><td>${n.shown ? "已弹" : "失败"}</td><td>${n.body}</td></tr>`).join("") + `</table>`
    : "（还没有通知记录）";
  const sup = d.system.suppressions || [];
  $("sup").innerHTML = sup.length
    ? `<table><tr><th>类别</th><th>压到</th><th>来源</th></tr>` + sup.map(s =>
        `<tr><td><code>${s.kind}</code></td><td>${s.until.slice(5, 16)}</td><td>${s.source || "-"}</td></tr>`).join("") + `</table>`
      + `<p class="hint">解除：<code>./bin/hub-notify unsuppress &lt;类别&gt;</code>（或等它到期）</p>`
    : "（没有被抑制的类别）";
  $("rname").textContent = d.report.name + ".md";
  $("text").value = d.report.text;
  $("revs").innerHTML = d.report.revisions.length
    ? d.report.revisions.map(v => `<li>#${v.rev} · ${v.author} · ${v.at.slice(5, 16)} · ${v.reason || "-"}</li>`).join("")
    : "<li>还没有修改记录</li>";
}
async function post(path, body) {
  $("msg").textContent = "…"; $("msg").className = "msg";
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) { $("msg").textContent = "失败：" + (d.error || r.status); $("msg").className = "msg err"; return; }
  $("msg").textContent = d.changed ? `已保存（修订 #${d.rev}）` : "没有变化，未留痕";
  $("msg").className = "msg ok";
  await load();
}
$("save").onclick = () => post("/api/save", {report, text: $("text").value, reason: "ui"});
$("gen").onclick = () => post("/api/generate", {report});
load().catch(e => { $("meta").textContent = "加载失败：" + e; });
loadDeliveries().catch(e => { $("deliveries").textContent = "投递加载失败：" + e; });
// 媒体面板（契约 plugins/views/ui/README-ASSETS.md §7）：首页数据之后异步加载，
// 缩略图 lazy，媒体挂了不影响别的区块。
function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(1) + " MB";
}
function fmtTime(s) { return String(s || "").slice(5, 16).replace("T", " "); }
function mediaLink(href, text) {
  const a = document.createElement("a");
  a.href = href; a.target = "_blank"; a.rel = "noopener"; a.textContent = text;
  return a;
}
async function loadMedia() {
  const msg = $("mediamsg"), grid = $("mediagrid"), list = $("medialist");
  try {
    const [ri, rd] = await Promise.all([
      fetch("/api/assets?kind=image"), fetch("/api/assets?kind=doc")]);
    const di = await ri.json(), dd = await rd.json();
    if (di.error === "assets-disabled") { msg.textContent = "媒体面板未启用（ui.assets.enabled=false）"; return; }
    msg.textContent = ri.ok && rd.ok ? "" : ("加载失败：" + (di.error || dd.error || ri.status));
    const images = di.items || [];
    grid.innerHTML = "";
    if (!images.length) grid.textContent = "（允许根里还没有图片）";
    for (const it of images) {
      const fig = document.createElement("figure");
      const link = mediaLink(it.url, "");
      const img = document.createElement("img");
      img.loading = "lazy"; img.alt = it.name;
      img.src = it.thumb || ("/thumbs/" + encodeURIComponent(it.id));
      link.appendChild(img);
      const cap = document.createElement("figcaption");
      cap.textContent = it.name;
      cap.title = it.name + " · " + fmtSize(it.size) + " · " + fmtTime(it.mtime);
      fig.appendChild(link); fig.appendChild(cap);
      grid.appendChild(fig);
    }
    const docs = dd.items || [];
    list.innerHTML = "";
    if (!docs.length) { list.textContent = "（允许根里还没有文档）"; return; }
    const table = document.createElement("table");
    const head = table.insertRow();
    for (const h of ["名称", "大小", "时间", "打开"]) {
      const th = document.createElement("th"); th.textContent = h; head.appendChild(th);
    }
    for (const it of docs) {
      const tr = table.insertRow();
      tr.insertCell().textContent = it.name;
      tr.insertCell().textContent = fmtSize(it.size);
      tr.insertCell().textContent = fmtTime(it.mtime);
      tr.insertCell().appendChild(mediaLink(it.url, "打开"));
    }
    list.appendChild(table);
  } catch (e) {
    msg.textContent = "媒体加载失败：" + e;
  }
}
$("mediareload").onclick = () => loadMedia();
loadMedia().catch(e => { $("mediamsg").textContent = "媒体加载失败：" + e; });
</script></body></html>
"""


def _loopback_host(value: str | None) -> bool:
    if not value:
        return False
    host = urlparse(f"//{value}").hostname or ""
    return bool(_LOOP_RE.match(host))


# ---- 媒体面板（只读；契约见 plugins/views/ui/README-ASSETS.md）-------------
# 一条纪律：只把「允许根」里的图片/文档列出来、按需吐字节；不写文件、不写库。
# 值（缺省页大小/上限/缩略图宽度…）的语义见契约 §1/§5；代码里只是没配置时的兜底。

_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_DOC_EXT = {".md", ".txt", ".pdf", ".json", ".csv", ".log"}
_ASSET_CTYPE = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".pdf": "application/pdf",
}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,2048}$")
_ASSET_CACHE: dict = {"key": None, "at": 0.0, "items": None}
_THUMB_CACHE: "OrderedDict[tuple, tuple[bytes, str]]" = OrderedDict()


class AssetError(RuntimeError):
    """媒体面板的请求错误；status 直接进 HTTP 状态码（语义见 README-ASSETS.md §6）。"""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status = status
        self.error = error


def _asset_conf(config) -> dict:
    def _int(key: str, default: int, lo: int, hi: int) -> int:
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(lo, min(hi, value))

    enabled = config.get("ui.assets.enabled", True)
    if not isinstance(enabled, bool):
        enabled = str(enabled).strip().lower() not in ("0", "false", "no", "off", "")
    roots = config.get("ui.assets.roots")
    if not isinstance(roots, list) or not roots:
        roots = [config.get("collect.screenshot.dir") or "screens",
                 config.get("reports_dir") or "reports"]
    norm: list[str] = []
    for item in roots:
        if not isinstance(item, str) or os.path.isabs(item):
            continue
        rel = item.strip().strip("/")
        if rel and rel not in norm:
            norm.append(rel)
    return {
        "enabled": enabled,
        "roots": norm,
        "list_limit": _int("ui.assets.list_limit", 60, 1, 200),
        "max_bytes": _int("ui.assets.max_bytes", 32 * 1024 * 1024, 1024, 512 * 1024 * 1024),
        "max_scan": _int("ui.assets.max_scan", 3000, 1, 50000),
        "thumb_width": _int("ui.assets.thumb_width", 360, 64, 1600),
        "thumb_quality": _int("ui.assets.thumb_quality", 72, 30, 95),
        "thumb_cache": _int("ui.assets.thumb_cache", 64, 1, 512),
        "cache_s": _int("ui.assets.cache_s", 5, 0, 600),
    }


def _is_under(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _asset_roots(config, conf: dict) -> list[tuple[str, Path]]:
    """允许根：配置里相对数据根的目录；解析后跑出数据根的条目直接跳过。"""
    try:
        root_real = Path(config.root).expanduser().resolve()
    except OSError:  # pragma: no cover
        return []
    out: list[tuple[str, Path]] = []
    for rel in conf["roots"]:
        try:
            real = (root_real / rel).resolve()
        except OSError:  # pragma: no cover
            continue
        if real.is_dir() and _is_under(real, root_real):
            out.append((rel, real))
    return out


def _asset_id(rel: str) -> str:
    return base64.urlsafe_b64encode(rel.encode("utf-8")).decode("ascii").rstrip("=")


def _asset_rel(ident: str) -> str:
    """id → 数据根相对路径；字面校验先于任何文件系统操作（README-ASSETS.md §4）。"""
    if not ident or not _ID_RE.match(ident):
        raise AssetError(403, "bad-id")
    try:
        rel = base64.urlsafe_b64decode(ident + "=" * (-len(ident) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise AssetError(403, "bad-id") from None
    if not rel or rel.startswith("/") or "\x00" in rel or "\\" in rel or rel != rel.strip():
        raise AssetError(403, "bad-id")
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise AssetError(403, "bad-id")
    return rel


def _asset_kind(rel: str) -> str | None:
    ext = Path(rel).suffix.lower()
    if ext in _IMAGE_EXT:
        return "image"
    if ext in _DOC_EXT:
        return "doc"
    return None


def _scan_assets(config, conf: dict) -> list[dict]:
    """扫允许根（不跟软链目录、跳过隐藏项、有 max_scan 上限），按 mtime 倒序。"""
    key = (str(Path(config.root).expanduser()), tuple(conf["roots"]), conf["max_scan"])
    now = time.time()
    if conf["cache_s"] and _ASSET_CACHE["key"] == key \
            and now - float(_ASSET_CACHE["at"] or 0.0) < conf["cache_s"]:
        return _ASSET_CACHE["items"]
    items: list[dict] = []
    stop = False
    for allowed_rel, real_root in _asset_roots(config, conf):
        for dirpath, dirnames, filenames in os.walk(str(real_root), followlinks=False):
            # 目录/文件名倒序：撞到 max_scan 上限时保留的是「最新那批」，不是最旧的
            dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), reverse=True)
            for name in sorted(filenames, reverse=True):
                kind = _asset_kind(name) if not name.startswith(".") else None
                if kind is None:
                    continue
                try:
                    real = (Path(dirpath) / name).resolve()
                    if not _is_under(real, real_root) or not real.is_file():
                        continue
                    st = real.stat()
                except OSError:
                    continue
                name_rel = str(real.relative_to(real_root)).replace(os.sep, "/")
                items.append({
                    "id": _asset_id(allowed_rel + "/" + name_rel), "kind": kind,
                    "name": name_rel, "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns, "mtime": iso_from_epoch(st.st_mtime),
                    "rel": allowed_rel + "/" + name_rel,
                })
                if len(items) >= conf["max_scan"]:
                    stop = True
                    break
            if stop:
                break
        if stop:
            break
    items.sort(key=lambda it: (it["mtime_ns"], it["rel"]), reverse=True)
    _ASSET_CACHE.update({"key": key, "at": now, "items": items})
    return items


def _asset_cursor(items: list[dict]) -> str:
    raw = json.dumps([items[-1]["mtime_ns"], items[-1]["rel"]], ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _asset_cursor_key(raw: str) -> tuple[int, str]:
    if not raw or not _ID_RE.match(raw):
        raise AssetError(400, "bad-cursor")
    try:
        key = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise AssetError(400, "bad-cursor") from None
    if not isinstance(key, list) or len(key) != 2 \
            or not isinstance(key[0], int) or not isinstance(key[1], str):
        raise AssetError(400, "bad-cursor")
    return key[0], key[1]


def _asset_entry(config, conf: dict, ident: str) -> tuple[Path, str, str]:
    """id → (真实路径, 相对路径, kind)；越界 403 / 不存在 404 / 太大 413。"""
    rel = _asset_rel(ident)
    kind = _asset_kind(rel)
    if kind is None:
        raise AssetError(403, "type-not-allowed")
    try:
        real = (Path(config.root).expanduser() / rel).resolve()
    except OSError:  # pragma: no cover
        raise AssetError(404, "not-found") from None
    if not any(_is_under(real, r) for _, r in _asset_roots(config, conf)):
        raise AssetError(403, "outside-root")
    if not real.is_file():
        raise AssetError(404, "not-found")
    if real.stat().st_size > conf["max_bytes"]:
        raise AssetError(413, "too-large")
    return real, rel, kind


def _thumb_bytes(path: Path, conf: dict) -> tuple[bytes, str]:
    """有 PIL 就现生成缩略图（内存缓存）；没有/解码失败 ⇒ 原图字节（README-ASSETS.md §3）。"""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, conf["thumb_width"], conf["thumb_quality"])
    hit = _THUMB_CACHE.get(key)
    if hit is not None:
        _THUMB_CACHE.move_to_end(key)
        return hit
    original = (path.read_bytes(), _ASSET_CTYPE.get(path.suffix.lower(), "application/octet-stream"))
    try:
        from PIL import Image
    except ImportError:
        return original
    try:
        import io
        with Image.open(path) as im:
            im.draft("RGB", (conf["thumb_width"] * 2, conf["thumb_width"] * 4))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((conf["thumb_width"], conf["thumb_width"] * 4))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=conf["thumb_quality"])
        value = (buf.getvalue(), "image/jpeg")
    except Exception:  # 坏图 fail-soft：回原图，不给 500
        return original
    _THUMB_CACHE[key] = value
    while len(_THUMB_CACHE) > conf["thumb_cache"]:
        _THUMB_CACHE.popitem(last=False)
    return value


class UiError(RuntimeError):
    """控制台参数/请求错误；继承 RuntimeError 让本体 cli 不 import 插件也能兜住。"""
    pass


def system_snapshot(store, config) -> dict:
    return {
        "events_total": store.stats()["events_total"],
        "stats": store.stats(),
        "todos": [r for r in store.todos(limit=int(config.get("report.todos_limit") or 30))
                  if r["status"] in ("open", "doing")],
        "notifications": store.notifications(limit=int(config.get("report.notifications_limit") or 10)),
        # M3b：清单的改动记录（每次都留痕：谁改的、改了什么、为什么）
        "todo_revisions": store.todo_revisions(limit=int(config.get("ui.todo_revisions_limit") or 8)),
        # M2c：被「忽略此类」压住的类别（到期自动恢复）
        "suppressions": store.suppressions(active_only=True),
    }


def _handler(config, store, report_name: str | None):
    # S15：报表实现已搬到 view 插件；本进程启动时按清单取一次（未安装/未启用 ⇒ ViewError 上抛）
    R = views.module(config, "report")
    state = {"served": False}

    class Handler(BaseHTTPRequestHandler):
        server_version = "info-hub-ui/0.1"

        # ---- 工具 ----
        def log_message(self, fmt, *args):  # 静默（quiet 由 serve 决定）
            if not config.get("ui.quiet_access_log", True):
                super().log_message(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str,
                  extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _guard(self) -> bool:
            if not _loopback_host(self.headers.get("Host")):
                self._json(403, {"error": "host-not-loopback"})
                return False
            if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
                self._json(403, {"error": "cross-site"})
                return False
            origin = self.headers.get("Origin")
            if origin and not _loopback_host(urlparse(origin).netloc):
                self._json(403, {"error": "origin-not-loopback"})
                return False
            return True

        def _body(self) -> dict:
            size = int(self.headers.get("Content-Length") or 0)
            limit = int(config.get("ui.max_body_bytes") or 262144)
            if size > limit:
                raise UiError(f"body too large ({size} > {limit})")
            raw = self.rfile.read(size) if size else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeDecodeError, ValueError) as exc:
                raise UiError(f"bad json: {exc}") from exc
            if not isinstance(data, dict):
                raise UiError("body must be a JSON object")
            return data

        # ---- 投递 HTTP（S5，INTERFACE.md §11.5）----
        def _deliver_id(self, data: dict) -> str:
            delivery_id = data.get("id")
            if not isinstance(delivery_id, str) or not delivery_id.strip():
                raise UiError("id 必须是非空字符串")
            return delivery_id

        def _deliver_pending(self) -> None:
            q = parse_qs(urlparse(self.path).query)
            channel = ((q.get("channel") or [""])[0] or "").strip() or None
            raw = (q.get("limit") or ["50"])[0]
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                self._json(400, {"error": "limit 必须是整数"})
                return
            if not 1 <= limit <= 500:
                self._json(400, {"error": "limit 越界（1 … 500）"})
                return
            self._json(200, {"deliveries": D.pending(store, channel=channel, limit=limit)})

        # ---- 消息中心（INTERFACE.md §11.9）----
        def _messages(self) -> None:
            q = parse_qs(urlparse(self.path).query)
            raw = (q.get("limit") or ["50"])[0]
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                self._json(400, {"error": "limit 必须是整数"})
                return
            if not 1 <= limit <= 500:
                self._json(400, {"error": "limit 越界（1 … 500）"})
                return
            flag = (q.get("archived") or ["0"])[0]
            if flag not in ("0", "1"):
                self._json(400, {"error": "archived 只能是 0 或 1"})
                return
            rows = D.list_rows(store, limit=limit, archived=(flag == "1"))
            self._json(200, {"messages": rows, "archived": flag == "1"})

        def _messages_post(self, route: str, data: dict) -> None:
            try:
                if route == "/api/messages":
                    text = data.get("text")
                    if not isinstance(text, str) or not text.strip():
                        self._json(400, {"error": "text 必须是非空字符串"})
                        return
                    title = str(data.get("title") or "").strip() or text.strip().splitlines()[0][:80]
                    row = D.create(store, channel=str(data.get("channel") or "console"),
                                   title=title, body=text, kind="message",
                                   attachments=data.get("attachments"))
                    self._json(201, row)
                elif route == "/api/messages/archive":
                    delivery_id = self._deliver_id(data)
                    self._json(200, D.archive(store, delivery_id))
                else:
                    self._json(404, {"error": "not found"})
            except D.DeliveryNotFound as exc:
                self._json(404, {"error": str(exc)})
            except (D.DeliveryError, D.EmptyReply, UiError) as exc:
                self._json(400, {"error": str(exc)})

        def _deliver_post(self, route: str, data: dict) -> None:
            try:
                if route == "/api/deliver/ack":
                    delivery_id = self._deliver_id(data)
                    action = data.get("action")
                    if action is not None and not isinstance(action, str):
                        self._json(400, {"error": "action 必须是字符串"})
                        return
                    self._json(200, D.ack(store, delivery_id, action=action))
                elif route == "/api/deliver/reply":
                    delivery_id = self._deliver_id(data)
                    text = data.get("text")
                    if not isinstance(text, str):
                        self._json(400, {"error": "text 必须是字符串"})
                        return
                    self._json(200, D.reply(store, delivery_id, text))
                elif route == "/api/deliver/claim":
                    delivery_id = self._deliver_id(data)
                    owner = data.get("owner")
                    if not isinstance(owner, str) or not owner.strip():
                        self._json(400, {"error": "owner 必须是非空字符串"})
                        return
                    self._json(200, D.claim(store, delivery_id, owner, data.get("ttl_s")))
                else:
                    self._json(404, {"error": "not found"})
            except D.DeliveryNotFound as exc:
                self._json(404, {"error": str(exc)})
            except (D.DeliveryError, D.EmptyReply, UiError) as exc:
                self._json(400, {"error": str(exc)})

# ---- 媒体（只读；契约 README-ASSETS.md）----
        def _assets(self) -> bool:
            """True = 本请求已由媒体路由处理（含 4xx）。"""
            path = unquote(self.path.split("?")[0])
            if path != "/api/assets" and not path.startswith(("/asset/", "/thumbs/")):
                return False
            conf = _asset_conf(config)
            if not conf["enabled"]:
                self._json(404, {"error": "assets-disabled"})
                return True
            try:
                if path == "/api/assets":
                    self._assets_list(conf)
                elif path.startswith("/thumbs/"):
                    self._asset_bytes(conf, path[len("/thumbs/"):], thumb=True)
                else:
                    self._asset_bytes(conf, path[len("/asset/"):], thumb=False)
            except AssetError as exc:
                self._json(exc.status, {"error": exc.error})
            return True

        def _assets_list(self, conf: dict) -> None:
            q = parse_qs(urlparse(self.path).query)
            kind = ((q.get("kind") or ["all"])[0] or "all").strip()
            if kind not in ("image", "doc", "all"):
                raise AssetError(400, "bad-kind")
            try:
                limit = int((q.get("limit") or [str(conf["list_limit"])])[0])
            except (TypeError, ValueError):
                raise AssetError(400, "bad-limit") from None
            if not 1 <= limit <= conf["list_limit"]:
                raise AssetError(400, "bad-limit")
            cursor = (q.get("cursor") or [""])[0]
            items = _scan_assets(config, conf)
            if kind != "all":
                items = [it for it in items if it["kind"] == kind]
            if cursor:
                bound = _asset_cursor_key(cursor)
                items = [it for it in items if (it["mtime_ns"], it["rel"]) < bound]
            page = items[:limit]
            rows = []
            for it in page:
                row = {"id": it["id"], "kind": it["kind"], "name": it["name"],
                       "size": it["size"], "mtime": it["mtime"], "url": "/asset/" + it["id"]}
                if it["kind"] == "image":
                    row["thumb"] = "/thumbs/" + it["id"]
                rows.append(row)
            payload = {"kind": kind, "limit": limit, "items": rows}
            if len(items) > limit:
                payload["nextCursor"] = _asset_cursor(page)
            self._json(200, payload)

        def _asset_bytes(self, conf: dict, ident: str, thumb: bool) -> None:
            real, _rel, kind = _asset_entry(config, conf, ident)
            if thumb:
                if kind != "image":
                    raise AssetError(404, "not-found")
                try:
                    body, ctype = _thumb_bytes(real, conf)
                except OSError:
                    raise AssetError(404, "not-found") from None
                disposition = "inline"
            else:
                disposition = "inline" if kind == "image" else "attachment"
                ctype = _ASSET_CTYPE[real.suffix.lower()]
                try:
                    body = real.read_bytes()
                except OSError:
                    raise AssetError(404, "not-found") from None
            ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", real.name) or "asset"
            self._send(200, body, ctype, extra={
                "Content-Disposition": disposition + '; filename="' + ascii_name
                + "\"; filename*=UTF-8''" + quote(real.name, safe=""),
                "X-Content-Type-Options": "nosniff",
            })

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            state["served"] = True
            try:
                self._get()
            except Exception as exc:  # 任何异常都要变成响应，不能给个空回复
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self) -> None:  # noqa: N802
            state["served"] = True
            try:
                self._post()
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def _get(self) -> None:
            if not self._guard():
                return
            if self._assets():
                return
            if self.path.split("?")[0] == "/api/state":
                self._json(200, {"system": system_snapshot(store, config),
                                 "report": {"name": R.report_path(config, report_name).stem,
                                            "path": str(R.report_path(config, report_name)),
                                            "text": R.read_report(config, report_name),
                                            "revisions": R.revisions(config, store, report_name)}})
                return
            if self.path.split("?")[0] == "/api/todo/detail":
                q = parse_qs(urlparse(self.path).query)
                todo_id = (q.get("id") or [""])[0]
                detail = store.todo_detail(todo_id) if todo_id else None
                if detail is None:
                    self._json(404, {"error": "not found", "id": todo_id})
                else:
                    self._json(200, detail)
                return
            if self.path.split("?")[0] == "/api/todo/revisions":
                q = parse_qs(urlparse(self.path).query)
                todo_id = (q.get("id") or [None])[0]
                self._json(200, {"id": todo_id,
                                 "revisions": store.todo_revisions(todo_id=todo_id,
                                                                   limit=int(config.get("ui.todo_revisions_limit") or 8))})
                return
            if self.path.split("?")[0] == "/api/deliver/pending":
                self._deliver_pending()
                return
            if self.path.split("?")[0] == "/api/messages":
                self._messages()
                return
            if self.path.split("?")[0] in ("/", "/index.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            self._json(404, {"error": "not found"})

        def _post(self) -> None:
            if not self._guard():
                return
            try:
                data = self._body()
            except UiError as exc:
                self._json(400, {"error": str(exc)})
                return
            route = self.path.split("?")[0]
            if route.startswith("/api/deliver/"):
                self._deliver_post(route, data)
                return
            if route.startswith("/api/messages"):
                self._messages_post(route, data)
                return
            name = data.get("report") or report_name
            try:
                if route == "/api/save":
                    text = data.get("text")
                    if not isinstance(text, str):
                        raise UiError("text 必须是字符串")
                    result = R.write_report(config, store, text, author="human",
                                            reason=str(data.get("reason") or "ui"), name=name)
                elif route == "/api/generate":
                    result = R.generate(config, store, reason="ui-generate", name=name)
                elif route == "/api/todo":
                    todo_id = data.get("id")
                    if not isinstance(todo_id, str) or not todo_id:
                        raise UiError("id 必须是非空字符串")
                    if data.get("snooze_minutes") is not None:
                        minutes = int(data["snooze_minutes"])
                        if not 1 <= minutes <= 60 * 24 * 30:
                            raise UiError("snooze_minutes 越界（1 … 43200）")
                        until = iso_from_epoch(time.time() + minutes * 60)
                        if not store.set_todo_snooze(todo_id, until, reason="ui"):
                            self._json(404, {"error": f"待办不存在：{todo_id}"})
                            return
                    status = data.get("status")
                    if status is not None:
                        if status not in ("open", "done", "dropped", "doing"):
                            raise UiError(f"status 非法：{status}（open|doing|done|dropped）")
                        if not store.set_todo_status(todo_id, status, human=True, reason="ui"):
                            self._json(404, {"error": f"待办不存在：{todo_id}"})
                            return
                    result = {"ok": True, "id": todo_id,
                              "todo": next((r for r in store.todos(limit=500) if r["id"] == todo_id), None),
                              "revisions": store.todo_revisions(todo_id=todo_id, limit=5)}
                else:
                    self._json(404, {"error": "not found"})
                    return
            except R.ReportError as exc:
                self._json(400, {"error": str(exc)})
                return
            except (UiError, ValueError) as exc:      # /api/todo 的参数校验失败 ⇒ 400（不是 500）
                self._json(400, {"error": str(exc)})
                return
            result["at"] = result.get("at") or iso()
            self._json(200, result)

    return Handler, state


def serve(config, store, host: str | None = None, port: int | None = None,
          once: bool = False, report_name: str | None = None, out=None) -> dict:
    """起本地控制台。只绑 loopback；`once=True` 处理完一个请求就退出（供验收脚本用）。"""
    import sys

    host = host or str(config.get("ui.host") or "127.0.0.1")
    if not _loopback_host(host) and host != "127.0.0.1":
        raise UiError(f"UI 只允许绑 loopback，拒绝 {host!r}")
    port = int(config.get("ui.port") or 8799) if port is None else int(port)
    handler_cls, state = _handler(config, store, report_name)
    httpd = HTTPServer((host, port), handler_cls)
    httpd.timeout = 0.2
    actual = httpd.socket.getsockname()
    url = f"http://{host}:{actual[1]}/"
    stop = threading.Event()

    def _bye(_signum, _frame):
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):  # 非主线程
            pass

    stream = out or sys.stdout
    print(json.dumps({"ui": url, "host": host, "port": actual[1],
                      "report": views.module(config, "report").report_path(config, report_name).stem,
                      "once": once},
                     ensure_ascii=False), file=stream, flush=True)
    try:
        while not stop.is_set():
            httpd.handle_request()
            if once and state["served"]:
                break
    finally:
        httpd.server_close()
    return {"url": url, "port": actual[1], "served": state["served"], "stopped": stop.is_set()}
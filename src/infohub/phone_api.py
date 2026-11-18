"""手机主页接口（INTERFACE.md §11.10）：给手机 App 的 WebView 提供消息中心。

与电脑控制台（§4.8）的区别：这里可**多地址监听**（缺省 0.0.0.0，局域网直连与 WG 隧道都走它）、**每个请求都要 token**，只开消息中心需要的路由
（列消息 / 发消息 / 归档 / 看附件），不暴露报表、待办、投递台账的其它写入口。
"""
from __future__ import annotations

import hmac
import importlib.util
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from infohub import deliver as D
from infohub.store import Store

_UI_PATH = Path(__file__).resolve().parents[2] / "plugins" / "views" / "ui" / "ui.py"


def _load_ui():
    """按路径载入控制台插件，复用它的媒体路径/缩略图实现（契约 README-ASSETS.md）。"""
    spec = importlib.util.spec_from_file_location("infohub_ui_view", _UI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _detect_public() -> bool:
    """本机是否已有非私有的全局地址（= 可能被公网直连）。

    经隧道/中转对外时本机仍是内网地址，这种情况要显式写 phone_api.exposure: public。
    """
    try:
        out = subprocess.run(["ip", "-j", "addr", "show"], capture_output=True, text=True,
                             timeout=3).stdout
        for iface in json.loads(out or "[]"):
            for a in iface.get("addr_info") or []:
                if a.get("scope") != "global":
                    continue
                ip = ipaddress.ip_address(str(a.get("local")))
                if not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast):
                    return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return False


def _conf(config) -> dict:
    raw = config.get("phone_api") or {}
    if not isinstance(raw, dict):
        raw = {}
    # token 不写死在仓库里：配置非空就用配置，否则 $HUB_ROOT/phone_api.token，再否则首次生成（secret.get_token）。
    from . import secret as _secret
    token = _secret.get_token(
        config, explicit=str(raw.get("token") or config.get("phone_remind.helper_token") or ""))
    try:
        port = int(raw.get("port") or 8800)
    except (TypeError, ValueError):
        port = 8800
    # 多地址：phone_api.hosts（列表或逗号串）优先，退回 phone_api.host。
    # 内网 HTTP（同一 Wi-Fi 直接用 PC 的局域网地址）与 WG 地址可以同时听（§11.10）。
    hosts_raw = raw.get("hosts") or raw.get("host") or "<hub-host>"
    if isinstance(hosts_raw, (list, tuple)):
        hosts = [str(h).strip() for h in hosts_raw if str(h).strip()]
    else:
        hosts = [h.strip() for h in str(hosts_raw).split(",") if h.strip()]
    if not hosts:
        hosts = ["<hub-host>"]
    # 通配地址自己覆盖全部网卡，再绑具体地址会 EADDRINUSE（实测）⇒ 只留它一个。
    # "::" 是 IPv4/IPv6 双栈通配（Linux 默认 net.ipv6.bindv6only=0，v4 走 v4-mapped 地址）。
    if "::" in hosts:
        hosts = ["::"]
    elif "0.0.0.0" in hosts or "*" in hosts:
        hosts = ["0.0.0.0"]
    # 暴露面：public = 公网能直连，lan = 只在私网里（仅作诊断信息，不再驱动页面横幅；
    # 用户 2026-11-17 22:56 定：门禁只有 token 一道，密码整套删掉）。
    exposure = str(raw.get("exposure") or "auto").strip().lower()
    if exposure not in ("public", "lan"):
        exposure = "public" if _detect_public() else "lan"
    return {"enabled": raw.get("enabled", True) is not False,
            "hosts": hosts, "host": hosts[0], "exposure": exposure,
            "port": max(1, min(65535, port)), "token": token}


_PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>info-hub</title><style>
:root{--bg:#12141a;--panel:#1b1e24;--line:#2b3038;--fg:#e8eaee;--dim:#98a1b0}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Noto Sans CJK SC",sans-serif;padding-bottom:env(safe-area-inset-bottom)}
header{position:sticky;top:0;z-index:5;background:rgba(18,20,26,.96);backdrop-filter:blur(6px);
 padding:10px 14px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:10px}
header b{font-size:15px}header span{color:var(--dim);font-size:12px}
textarea{background:#0f1114;color:var(--fg);border:1px solid var(--line);border-radius:10px;
 padding:9px 11px;font:15px/1.45 inherit;resize:none}
button{background:#2a6df4;color:#fff;border:0;border-radius:10px;padding:10px 16px;font-size:14px}
button.ghost{background:#262b34;color:var(--fg)}
.row{display:flex;align-items:center;gap:10px;padding:0 14px 8px;color:var(--dim);font-size:12px}
.msgitem{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:10px 14px;padding:11px 12px}
.msgitem b{font-size:15px}.meta{color:var(--dim);font-size:12px;margin-top:6px}
.body{white-space:pre-wrap;word-break:break-word;margin-top:6px}
.atts{display:flex;flex-wrap:wrap;gap:8px;margin-top:9px;align-items:flex-start}
.atts img{width:106px;height:106px;object-fit:cover;border-radius:8px;border:1px solid var(--line)}
.atts a.doc{font-size:13px;border:1px solid var(--line);border-radius:8px;padding:7px 10px;color:var(--fg);text-decoration:none}
.itemacts{display:flex;gap:8px;margin-top:10px}
.qbox{margin-top:10px;padding:10px;border:1px solid var(--line);border-radius:10px;background:#15181d}
.qbox .opts{display:flex;flex-wrap:wrap;gap:8px}
.qbox button.opt{background:#233046;color:var(--fg);border:1px solid #35507a;font-size:14px;padding:9px 13px}
.qbox textarea{width:100%;height:44px;margin-top:8px}
.qbox .qacts{display:flex;gap:8px;margin-top:8px}
.ans{color:#9fb3c8;font-size:13px;margin-top:6px;white-space:pre-wrap}
.itemacts button{font-size:13px;padding:7px 13px}
.empty{color:var(--dim);text-align:center;padding:40px 20px}
#toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);background:#2b3038;color:#fff;
 padding:9px 16px;border-radius:10px;font-size:13px;opacity:0;transition:opacity .2s;pointer-events:none}
#toast.on{opacity:1}
#lb{position:fixed;inset:0;background:#000;display:none;z-index:99;align-items:center;justify-content:center}
#lb.on{display:flex}
#lb img{max-width:100%;max-height:100%;object-fit:contain}
#lb .x{position:absolute;top:14px;right:14px;background:rgba(40,44,52,.9);border-radius:999px;padding:9px 14px}
</style></head><body>
<header><b>info-hub 消息</b><span id="meta">…</span><button class="ghost" id="gear" style="margin-left:auto;display:none">⚙ 设置</button></header>

<div class="row"><label><input type="checkbox" id="arch"> 显示归档</label><button class="ghost" id="reload" style="margin-left:auto">刷新</button></div>
<div id="list"></div><div id="toast"></div>
<div id="lb"><img alt="图片"><div class="x">关闭</div></div>
<script>
const TOKEN = __TOKEN__;
const HELPER = /[?&]helper=1/.test(location.search);
const $ = (id) => document.getElementById(id);
function toast(t){const el=$("toast");el.textContent=t;el.classList.add("on");setTimeout(()=>el.classList.remove("on"),1800);}
function openLb(src){const lb=$("lb"); lb.querySelector("img").src=src; lb.classList.add("on");}
function closeLb(){const lb=$("lb"); lb.classList.remove("on"); lb.querySelector("img").src="";}
function attId(rel){return btoa(unescape(encodeURIComponent(rel))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");}
function u(path){return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN);}
function isImg(rel){return /\.(png|jpe?g|gif|webp|bmp)$/i.test(rel);}
async function api(path, opts){
  const o = Object.assign({}, opts||{});
  o.headers = Object.assign({"Content-Type":"application/json"}, o.headers||{});
  o.headers["X-Hub-Token"] = TOKEN;
  const r = await fetch(path, o); const d = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}
async function load(){
  const arch = $("arch").checked ? "1" : "0";
  try {
    const d = await api("/api/messages?limit=80&archived=" + arch);
    const rows = d.messages || [];
    $("meta").textContent = rows.length + " 条" + (arch === "1" ? "（归档）" : "");
    const box = $("list"); box.innerHTML = "";
    if (!rows.length){ box.innerHTML = '<div class="empty">还没有消息</div>'; return; }
    for (const m of rows){
      const el = document.createElement("div"); el.className = "msgitem";
      const h = document.createElement("b"); h.textContent = m.title || "(无标题)"; el.appendChild(h);
      if (m.body && m.body !== m.title){ const b = document.createElement("div"); b.className = "body"; b.textContent = m.body; el.appendChild(b); }
      const meta = document.createElement("div"); meta.className = "meta";
      meta.textContent = [m.channel, m.status, String(m.created_at||"").slice(5,16).replace("T"," "),
                          m.archived_at ? "已归档" : ""].filter(Boolean).join(" · ");
      el.appendChild(meta);
      if (m.reply){ const an = document.createElement("div"); an.className = "ans"; an.textContent = "回复：" + m.reply; el.appendChild(an); }
      const atts = m.attachments || [];
      if (atts.length){
        const box2 = document.createElement("div"); box2.className = "atts";
        for (const rel of atts){
          const id = attId(rel);
          if (isImg(rel)){
            const a = document.createElement("a"); a.href = u("/asset/" + id);
            a.onclick = (ev) => { ev.preventDefault(); openLb(u("/asset/" + id)); };
            const img = document.createElement("img"); img.loading = "lazy"; img.alt = rel;
            img.src = u("/thumbs/" + id);
            img.onerror = () => { a.textContent = "附件缺失：" + rel; };
            a.appendChild(img); box2.appendChild(a);
          } else {
            const a = document.createElement("a"); a.className = "doc"; a.href = u("/asset/" + id);
            a.setAttribute("download", rel.split("/").pop() || "file");
            a.textContent = rel.split("/").pop(); box2.appendChild(a);
          }
        }
        el.appendChild(box2);
      }
      // 待答的提问（有选项或 mode=question）：选项 + 打字入口 + 提交 / 跳过
      const opts = Array.isArray(m.options) ? m.options : [];
      if (["pending", "claimed"].includes(m.status) && (opts.length || m.mode === "question")){
        const qb = document.createElement("div"); qb.className = "qbox";
        if (opts.length){
          const ob = document.createElement("div"); ob.className = "opts";
          for (const o of opts){
            const ob2 = document.createElement("button"); ob2.className = "opt";
            ob2.textContent = o.label || o.key; ob2.onclick = () => answer(m.id, o.label || o.key, ob2);
            ob.appendChild(ob2);
          }
          qb.appendChild(ob);
        }
        const tin = document.createElement("textarea");
        tin.placeholder = opts.length ? "或者打字回答…" : "打字回答…";
        const qacts = document.createElement("div"); qacts.className = "qacts";
        const sub = document.createElement("button"); sub.textContent = "提交";
        const skip = document.createElement("button"); skip.className = "ghost"; skip.textContent = "跳过";
        sub.onclick = () => { const t = tin.value.trim(); if (!t){ toast("先点选项或打字"); return; } answer(m.id, t, sub); };
        skip.onclick = () => skipAsk(m.id);
        qacts.appendChild(sub); qacts.appendChild(skip);
        qb.appendChild(tin); qb.appendChild(qacts); el.appendChild(qb);
      }
      const acts = document.createElement("div"); acts.className = "itemacts";
      const ab = document.createElement("button"); ab.className = "ghost"; ab.textContent = "归档";
      ab.onclick = async () => {
        try { await api("/api/messages/archive", {method:"POST", body: JSON.stringify({id: m.id})});
              toast("已归档 " + m.id.slice(-6)); await load(); }
        catch (e){ toast("归档失败：" + e.message); }
      };
      acts.appendChild(ab); el.appendChild(acts);
      box.appendChild(el);
    }
  } catch (e) { $("list").innerHTML = '<div class="empty">加载失败：' + e.message + '</div>'; }
}
async function answer(id, text, btn){
  if (btn) btn.disabled = true;
  try { await api("/api/deliver/reply", {method:"POST", body: JSON.stringify({id: id, text: text})});
        toast("已回复"); await load(); }
  catch (e){ toast("回复失败：" + e.message); if (btn) btn.disabled = false; }
}
async function skipAsk(id){
  try { await api("/api/deliver/skip", {method:"POST", body: JSON.stringify({id: id})});
        toast("已跳过"); await load(); }
  catch (e){ toast("跳过失败：" + e.message); }
}
$("lb").onclick = closeLb;
$("reload").onclick = load;
$("arch").onchange = load;
if (HELPER) $("gear").style.display = "";
$("gear").onclick = () => { location.href = "hub://settings"; };
load();
</script></body></html>
"""


def render_page(token: str) -> bytes:
    """页面只带 token —— 门禁只有 token 一道（用户 2026-11-17 22:56「有了 token 本身也不需要密码」）。"""
    return _PAGE.replace("__TOKEN__", json.dumps(token)).encode("utf-8")


class _ThreadStore:
    """每个请求线程一份 SQLite 连接（sqlite3 连接不能跨线程共享）。

    单线程服务器挡不住两个客户端同时在线（WebView 页面 + 常驻通知轮询 + DSH 插件），
    所以用 ThreadingHTTPServer，并把连接按线程缓存；Store 的其余语义不变（INTERFACE.md §11.10）。
    """

    def __init__(self, path):
        self._path = path
        self._local = __import__("threading").local()

    def __getattr__(self, name):
        st = getattr(self._local, "store", None)
        if st is None:
            st = Store(self._path)
            self._local.store = st
        return getattr(st, name)


def _handler(config, store, ui):
    conf = _conf(config)
    assets = ui._asset_conf(config)

    class Handler(BaseHTTPRequestHandler):
        server_version = "infohub-phone/1"

        def log_message(self, fmt, *args):  # 静音：手机端轮询会刷屏
            pass

        # ---- 基础 ----
        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if not conf["token"]:
                self._json(503, {"error": "phone_api 未配置 token"})
                return False
            given = self.headers.get("X-Hub-Token") or ""
            if not given:
                given = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
            if not hmac.compare_digest(given, conf["token"]):
                self._json(401, {"error": "bad-token"})
                return False
            return True

        def _body(self) -> dict:
            size = int(self.headers.get("Content-Length") or 0)
            if size > 262144:
                raise ValueError("body too large")
            raw = self.rfile.read(size) if size else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("body must be object")
            return data

        # ---- 附件（复用控制台媒体契约）----
        def _send_asset(self, ident: str, thumb: bool) -> None:
            try:
                path, rel, kind = ui._asset_entry(config, assets, ident)
                if thumb and kind == "image":
                    data, ctype = ui._thumb_bytes(path, assets)
                else:
                    data, ctype = path.read_bytes(), ui._ASSET_CTYPE.get(path.suffix.lower(),
                                                                        "application/octet-stream")
            except ui.AssetError as exc:
                self._json(exc.status, {"error": exc.error})
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=300")
            if kind != "image":
                # 让手机端下载器能拿回原始文件名（WebView 的 DownloadListener 只看这个头）
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''%s" % quote(path.name, safe=""))
            self.end_headers()
            self.wfile.write(data)

        # ---- 路由 ----
        def do_GET(self) -> None:
            route, _, _ = self.path.partition("?")
            if not self._authed():
                return
            if route == "/api/settings":
                # 只读诊断：绑定地址 / 端口 / 是否配了 token（helper 的「测试连接」也用它可以区分 401 与 200）
                self._json(200, {"exposure": conf["exposure"],
                                 "public": conf["exposure"] == "public",
                                 "hosts": conf["hosts"], "port": conf["port"],
                                 "token_set": bool(conf["token"])})
                return
            if route in ("/", "/index.html"):
                body = render_page(conf["token"])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/messages":
                q = parse_qs(urlparse(self.path).query)
                try:
                    limit = int((q.get("limit") or ["50"])[0])
                except (TypeError, ValueError):
                    self._json(400, {"error": "limit 必须是整数"})
                    return
                if not 1 <= limit <= 200:
                    self._json(400, {"error": "limit 越界（1 … 200）"})
                    return
                flag = (q.get("archived") or ["0"])[0]
                if flag not in ("0", "1"):
                    self._json(400, {"error": "archived 只能是 0 或 1"})
                    return
                self._json(200, {"messages": D.list_rows(store, limit=limit, archived=(flag == "1"))})
                return
            if route.startswith("/thumbs/"):
                self._send_asset(route[len("/thumbs/"):], True)
                return
            if route.startswith("/asset/"):
                self._send_asset(route[len("/asset/"):], False)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authed():
                return
            route, _, _ = self.path.partition("?")
            try:
                data = self._body()
            except (ValueError, UnicodeDecodeError) as exc:
                self._json(400, {"error": str(exc)})
                return
            try:
                if route == "/api/messages":
                    text = data.get("text")
                    if not isinstance(text, str) or not text.strip():
                        self._json(400, {"error": "text 必须是非空字符串"})
                        return
                    title = str(data.get("title") or "").strip() or text.strip().splitlines()[0][:80]
                    row = D.create(store, channel=str(data.get("channel") or "phone"),
                                   title=title, body=text, kind="message",
                                   attachments=data.get("attachments"))
                    self._json(201, row)
                elif route == "/api/deliver/reply":
                    ident = data.get("id")
                    text = data.get("text")
                    if not isinstance(ident, str) or not ident.strip():
                        self._json(400, {"error": "id 必须是非空字符串"})
                        return
                    self._json(200, D.reply(store, ident, text if isinstance(text, str) else ""))
                elif route == "/api/deliver/skip":
                    ident = data.get("id")
                    if not isinstance(ident, str) or not ident.strip():
                        self._json(400, {"error": "id 必须是非空字符串"})
                        return
                    self._json(200, D.expire(store, ident))
                elif route == "/api/messages/archive":
                    ident = data.get("id")
                    if not isinstance(ident, str) or not ident.strip():
                        self._json(400, {"error": "id 必须是非空字符串"})
                        return
                    self._json(200, D.archive(store, ident))
                else:
                    self._json(404, {"error": "not found"})
            except D.DeliveryNotFound as exc:
                self._json(404, {"error": str(exc)})
            except (D.DeliveryError, D.EmptyReply) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


class _V6Server(ThreadingHTTPServer):
    """IPv6 监听（bind "::" 时同时接受 IPv4-mapped 连接，见 §11.10）。"""

    address_family = socket.AF_INET6


def serve(config, store: Store, host: str | None = None, port: int | None = None) -> int:
    conf = _conf(config)
    if not conf["enabled"]:
        print("phone_api: disabled（phone_api.enabled=false）", file=sys.stderr)
        return 0
    # token 一定非空：没有显式配置时 _conf 已经生成到 $HUB_ROOT/phone_api.token（0600）
    if not str((config.get("phone_api") or {}).get("token") or "").strip() \
            and not str(config.get("phone_remind.helper_token") or "").strip():
        from .secret import token_file
        print("phone_api: token 自动生成于 " + str(token_file(config)), file=sys.stderr)
    ui = _load_ui()
    # 多地址绑定（§11.10）：WG 地址供隧道/中转，局域网地址供同 Wi-Fi 的手机直连；
    # 每个地址起一个 ThreadingHTTPServer，除最后一个外都在后台线程里跑。
    hosts = [host] if host else list(conf["hosts"])
    bind_port = conf["port"] if port is None else int(port)
    if bind_port == 0 and len(hosts) > 1:
        hosts = hosts[:1]
    handler = _handler(config, store, ui)
    # 第一行只报暴露面/监听列表，**不要带 port 键**：端口 0 时这里只有配置值，
    # 真正的绑定端口在第二行（测试脚本按第一处 "port" 解析，带了会指到生产端口）。
    print(json.dumps({"exposure": conf["exposure"], "hosts": conf["hosts"]}), flush=True)
    servers = []
    for one in hosts:
        try:
            # 多线程：WebView 与常驻通知轮询会并发；SQLite 连接按线程隔离，见 _ThreadStore
            # 含 ':' 的地址走 AF_INET6（"::" 即双栈通配）。
            cls = _V6Server if ":" in one else ThreadingHTTPServer
            srv = cls((one, bind_port), handler)
        except OSError as exc:
            print(f"phone_api: 绑 {one}:{bind_port} 失败：{exc}", file=sys.stderr)
            continue
        servers.append(srv)
        actual = srv.server_address[1]
        shown = f"[{one}]" if ":" in one else one  # IPv6 字面量在 URL 里要加方括号
        print(json.dumps({"host": one, "port": actual, "url":
                          f"http://{shown}:{actual}/?token={quote(conf['token'])}"}), flush=True)
    if not servers:
        print("fatal: phone_api 没有任何地址能绑定", file=sys.stderr)
        return 2
    for srv in servers[:-1]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        servers[-1].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            try:
                srv.shutdown()
            except RuntimeError:
                pass
            srv.server_close()
    return 0


def main(argv=None) -> int:
    from infohub.config import load

    argv = list(sys.argv[1:] if argv is None else argv)
    config_path = None
    host = port = None
    i = 0
    while i < len(argv):
        if argv[i] == "--config":
            config_path = argv[i + 1]; i += 2
        elif argv[i] == "--host":
            host = argv[i + 1]; i += 2
        elif argv[i] == "--port":
            port = int(argv[i + 1]); i += 2
        else:
            print(f"fatal: 不认识的参数 {argv[i]}", file=sys.stderr)
            return 2
    config = load(config_path)
    config.ensure_dirs()
    store = _ThreadStore(config.db_path)
    store.migrate()
    try:
        return serve(config, store, host=host, port=port)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

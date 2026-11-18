"""qq-onebot 通道插件：把投递行推到 QQ（OneBot v11 HTTP，SnowLuma 后端）。

单函数入口 send(request)；op 语义与失败语义见同目录 README.md，契约见 INTERFACE.md §11.7d。
台账复用 infohub.deliver（不自己拼 INSERT），只回写白名单列（status/remote_ref/last_error/attempts）。
远端鉴权与调用复用 infohub.qq_bridge（token 在后端机的 token_file 里，不在 info-hub 侧留第二份）。

目标（target）在 request 里以 {"type": "private"|"group", "id": "<uin|group_id>"} 给出；
正文 = title + 换行 + body（body 为空时只发 title）。
"""

from __future__ import annotations

import json

from infohub import deliver as D
from infohub import qq_bridge

#: 台账里的通道名（INTERFACE.md §11.7d 的 qq）
CHANNEL = "qq"
OPS = ("dispatch", "retry", "dismiss")
#: 允许就地回写的列
PATCHABLE = ("status", "remote_ref", "last_error", "attempts")


class ChannelError(Exception):
    """通道入参不合法（未知 op / 缺 target / 目标类型不认识）。"""


class PushError(Exception):
    """QQ 后端拒绝或不可达（调用方退出码 1）。"""


def send(request: dict) -> dict:
    req = dict(request or {})
    op = str(req.get("op") or "dispatch")
    if op == "dispatch":
        return _dispatch(req)
    if op == "retry":
        return _retry(req)
    if op == "dismiss":
        return _dismiss(req)
    raise ChannelError(f"不认识的 op: {op}（只认 {'/'.join(OPS)}）")


def call(config, action: str, payload: dict) -> dict:
    """一次 OneBot action；把 qq_bridge 的异常统一翻成 PushError。"""
    try:
        return qq_bridge.onebot_call(config, action, payload)
    except qq_bridge.QQError as exc:
        raise PushError(str(exc)) from exc


def _target(req: dict) -> tuple[str, str]:
    target = req.get("target") or {}
    if not isinstance(target, dict):
        raise ChannelError("target 必须是 {type,id} 对象")
    typ = str(target.get("type") or "private").strip()
    ident = str(target.get("id") or "").strip()
    if typ not in ("private", "group"):
        raise ChannelError(f"target.type 只认 private/group，收到 {typ!r}")
    if not ident:
        raise ChannelError("target.id 不能为空（私聊填 uin，群聊填 group_id）")
    return typ, ident


def _text(req: dict) -> str:
    title = str(req.get("title") or "").strip()
    body = str(req.get("body") or "").strip()
    if not title and not body:
        raise ChannelError("title 与 body 不能都为空")
    return f"{title}\n{body}".strip() if body and title else (title or body)


def _send_text(config, typ: str, ident: str, text: str) -> dict:
    payload = {"message": [{"type": "text", "data": {"text": text}}]}
    if typ == "group":
        payload["group_id"] = int(ident)
        payload["message_type"] = "group"
    else:
        payload["user_id"] = int(ident)
        payload["message_type"] = "private"
    out = call(config, "send_msg", payload)
    if str(out.get("status") or "") != "ok":
        raise PushError(str(out.get("message") or out.get("wording") or out)[:300])
    data = out.get("data") if isinstance(out.get("data"), dict) else {}
    return {"message_id": data.get("message_id"), "raw": out}


def _dispatch(req: dict) -> dict:
    config, store = req["config"], req["store"]
    typ, ident = _target(req)
    text = _text(req)
    title = str(req.get("title") or "").strip() or text.splitlines()[0][:60]
    body = str(req.get("body") or "")
    mode = D.parse_mode(req.get("mode")) or "single"
    options = D.parse_options(req.get("options"))

    delivery_id = D.new_id()
    row = D.create(store, channel=CHANNEL, title=title, body=body, mode=mode,
                   options=options, todo_id=req.get("todo_id"),
                   kind=req.get("kind") or "msg", delivery_id=delivery_id)
    ref = json.dumps({"type": typ, "id": ident}, ensure_ascii=False)
    try:
        out = _send_text(config, typ, ident, text)
    except PushError as exc:      # 发送失败 ⇒ 行留 pending，记原因，重推或人工处理
        _patch(store, delivery_id, status="pending", remote_ref=ref,
               attempts=1, last_error=str(exc)[:300])
        return {"ok": True, "delivery_id": delivery_id, "mode": mode, "ref": None,
                "transport": "pending", "immediate_error": str(exc)[:300],
                "row": store.delivery(delivery_id) or row}
    mid = out.get("message_id")
    ref = json.dumps({"type": typ, "id": ident, "message_id": mid}, ensure_ascii=False)
    _patch(store, delivery_id, status="delivered", remote_ref=ref, attempts=0, last_error=None)
    return {"ok": True, "delivery_id": delivery_id, "mode": mode, "ref": ref,
            "transport": "onebot", "message_id": mid,
            "row": store.delivery(delivery_id) or row}


def _retry(req: dict) -> dict:
    config, store = req["config"], req["store"]
    delivery_id = str(req.get("delivery_id") or "").strip()
    if not delivery_id:
        raise ChannelError("delivery_id 不能为空")
    row = store.delivery(delivery_id)
    if row is None:
        raise ChannelError(f"投递不存在: {delivery_id}")
    raw = row.get("remote_ref") or "{}"
    try:
        meta = json.loads(raw) if str(raw).strip().startswith("{") else {}
    except json.JSONDecodeError:
        meta = {}
    typ, ident = str(meta.get("type") or "private"), str(meta.get("id") or "")
    if not ident:
        raise ChannelError("该行没有远端目标，重推前需要重新指定 target")
    text = f"{row.get('title') or ''}\n{row.get('body') or ''}".strip()
    try:
        out = _send_text(config, typ, ident, text)
    except PushError as exc:
        _patch(store, delivery_id, status="pending", attempts=int(row.get("attempts") or 0) + 1,
               last_error=str(exc)[:300])
        raise
    mid = out.get("message_id")
    ref = json.dumps({"type": typ, "id": ident, "message_id": mid}, ensure_ascii=False)
    _patch(store, delivery_id, status="delivered", remote_ref=ref, attempts=0, last_error=None)
    return {"ok": True, "delivery_id": delivery_id, "ref": ref, "message_id": mid,
            "row": store.delivery(delivery_id)}


def _dismiss(req: dict) -> dict:
    """撤回一条已发出的 QQ 消息（OneBot delete_msg）；失败抛 PushError。"""
    config, store = req["config"], req["store"]
    delivery_id = str(req.get("delivery_id") or "").strip()
    if not delivery_id:
        raise ChannelError("delivery_id 不能为空")
    row = store.delivery(delivery_id)
    if row is None:
        raise ChannelError(f"投递不存在: {delivery_id}")
    try:
        meta = json.loads(row.get("remote_ref") or "{}")
    except json.JSONDecodeError:
        meta = {}
    mid = meta.get("message_id")
    if not mid:
        raise ChannelError("这一行没有 message_id（可能没真的发出去），无从撤回")
    out = call(config, "delete_msg", {"message_id": int(mid)})
    if str(out.get("status") or "") != "ok":
        raise PushError(str(out.get("message") or out.get("wording") or out)[:300])
    return {"ok": True, "delivery_id": delivery_id, "message_id": mid}


def _patch(store, delivery_id: str, **fields) -> dict | None:
    sets: list[str] = []
    args: list = []
    for key, value in fields.items():
        if key not in PATCHABLE:
            raise ChannelError(f"不允许回写的列: {key}")
        sets.append(f"{key} = ?")
        args.append(value)
    if not sets:
        return store.delivery(delivery_id)
    args.append(delivery_id)
    store.conn.execute(f"UPDATE deliveries SET {', '.join(sets)} WHERE id = ?", args)
    store.conn.commit()
    return store.delivery(delivery_id)

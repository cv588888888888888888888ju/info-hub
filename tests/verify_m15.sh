#!/usr/bin/env bash
# info-hub M15 验收：验证码脱敏（phone-hub 落库前 + 历史数据清洗）。
# 全程合成 payload + 临时库：不碰真库、不碰网络、不碰 ECS（ssh 用假脚本顶替）。
# 判据逐条对应 INTERFACE.md §8 M15；规则语义见 §4.21 / §5.14。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

TMP="$(mktemp -d /tmp/infohub-m15-XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf 'PASS %s\n' "$1"; }
fail() { FAIL=$((FAIL+1)); printf 'FAIL %s  (%s)\n' "$1" "${2:-}"; }
ck() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "期望 $3，实际 $2"; fi; }

R="$TMP/root"; CFG="$TMP/cfg.yaml"; CFG2="$TMP/cfg-off.yaml"
mkdir -p "$R" "$TMP/bin"
python3 - "$ROOT/hub.config.yaml" "$CFG" "$CFG2" <<'PY'
import sys, yaml
raw = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))
for name, node in raw["collect"].items():
    if isinstance(node, dict) and "enabled" in node:
        node["enabled"] = False           # 不碰真设备/真网络
raw["redact"]["enabled"] = True
yaml.safe_dump(raw, open(sys.argv[2], "w", encoding="utf-8"), allow_unicode=True)
raw["redact"]["enabled"] = False
yaml.safe_dump(raw, open(sys.argv[3], "w", encoding="utf-8"), allow_unicode=True)
PY

# 假 ssh：phone_hub.collect 只认 PATH 上的 `ssh`，stdout 就是服务器中枢的形状。
cat > "$TMP/bin/ssh" <<'JSON'
#!/usr/bin/env bash
cat <<'EOF'
[{"ts": 1760000000000, "pkg": "com.android.mms", "app": "短信", "title": "深度求索",
  "text": "【深度求索】验证码：631234，有效期 5 分钟，如非本人操作，请忽略。",
  "key": "0|com.android.mms|1|null|1"},
 {"ts": 1760000001000, "pkg": "com.tencent.mobileqq", "app": "QQ", "title": "示例群聊",
  "text": "这个验证码怎么又失效了", "key": "0|com.tencent.mobileqq|2|null|1"},
 {"ts": 1760000002000, "pkg": "com.android.mms", "app": "短信", "title": "GitHub",
  "text": "Your verification code is 192837. Do not share it.",
  "key": "0|com.android.mms|3|null|1"}]
EOF
JSON
chmod +x "$TMP/bin/ssh"

py() {  # py <root> <cmd>
  local root="$1"; shift
  HUB_ROOT="$root" HUB_CONFIG="$CFG" CFG_OFF="$CFG2" PATH="$TMP/bin:$PATH" python3 - "$@" <<'PY'
import json, os, sys
from infohub import events as E, redact as R
from infohub import sources
phone_hub = sources.load_module("phone-hub")
from infohub.config import load
from infohub.store import Store

SMS = {"ts": 1, "pkg": "com.android.mms", "app": "短信", "title": "深度求索",
       "key": "0|com.android.mms|1|null|1"}
QQ = {"ts": 2, "pkg": "com.tencent.mobileqq", "app": "QQ", "title": "示例群聊",
      "key": "0|com.tencent.mobileqq|2|null|1"}
CN_SAMPLE = "【阅文】920234（阅文登录验证码），请在15分钟内完成操作。"


def snap(store):
    """快照：行数 + 每行的全部列（用来断言「只改 payload」）。"""
    rows = [{"id": r["id"], "ts": r["ts"], "source": r["source"], "kind": r["kind"],
             "fingerprint": r["fingerprint"], "privacy": r["privacy"],
             "ingested_at": r["ingested_at"], "payload": r["payload"]}
            for r in store.iter_events(limit=0)]
    return json.dumps({"count": store.count_events(), "rows": rows}, ensure_ascii=False)


cmd = sys.argv[1]
cfg = load()


def ap(payload, config=cfg):
    return R.apply(dict(payload), config)


if cmd == "unit":
    res = {}
    p = dict(SMS, text=CN_SAMPLE)
    out, s = ap(p)
    res["cn_hit"], res["cn_kind"], res["cn_text"] = s["hit"], s["kind"], out["text"]
    res["placeholder"] = cfg.get("redact.placeholder")
    res["fields_kept"] = ",".join(k for k in ("ts", "pkg", "app", "title", "key")
                                  if out.get(k) != p.get(k)) or "all"
    res["cn_redact_hit"] = bool(out.get("redact", {}).get("hit"))

    chat_text = "这个验证码怎么又失效了"
    out, s = ap(dict(QQ, text=chat_text))
    res["chat_text"], res["chat_has_redact"] = out["text"], "redact" in out

    out, s = ap(dict(SMS, text="Your verification code is 192837. Do not share it."))
    res["en_hit"], res["en_kind"], res["en_text"] = s["hit"], s["kind"], out["text"]

    # 真库里的误杀反例：token 后面没有值 / URL 查询参数里的 token
    _, s = ap(dict(SMS, text="苏妈神了！AMD送免费token！可使用DeepSeek V4flash0313"))
    res["bili_hit"] = s["hit"]
    _, s = ap(dict(SMS, text='更新失败: Get "https://8.2.1.1:1080/config?token=05af8f90bdbb9e3": EOF'))
    res["url_hit"] = s["hit"]

    # 弱证据：字样在、数字串不在 ⇒ 只在短信 App 上脱敏
    _, s = ap(dict(SMS, text="您的验证码已过期，请重新获取"))
    res["weak_sms_hit"], res["weak_sms_kind"] = s["hit"], s["kind"]
    _, s = ap(dict(QQ, text="您的验证码已过期，请重新获取"))
    res["weak_qq_hit"] = s["hit"]

    # find 是纯函数：span 必须指向那串数字
    kind, span = R.find(CN_SAMPLE, cfg)
    res["find_kind"], res["find_span"] = kind, CN_SAMPLE[span[0]:span[1]]
    res["year_kind"] = R.find("验证码 2026年就这样了", cfg)[0]

    # enabled=false ⇒ 原样保留、不加 redact
    out, s = ap(p, load(os.environ["CFG_OFF"]))
    res["off_text"], res["off_hit"] = out["text"], s["hit"]
    res["off_has_redact"] = "redact" in out

    # 幂等：占位符本身含「验证码」三字，第二次必须空手而归
    redacted, _ = ap(p)
    again, s2 = ap(redacted)
    res["idem_hit"], res["idem_text"] = s2["hit"], again["text"]
    print(json.dumps(res, ensure_ascii=False))

elif cmd == "collect":
    store = Store(cfg.db_path); store.migrate()
    events = phone_hub.collect(cfg, store)
    by_ts = {str(ev["payload"].get("ts")): ev for ev in events}
    inserted, _dup = store.insert_events(events)
    row = store.events(source="phone-hub", since="2020-01-01", limit=10)
    by_title = {(r["payload"] or {}).get("title"): r for r in row}
    sms = by_title.get("深度求索") or {}
    chat = by_title.get("示例群聊") or {}
    en = by_title.get("GitHub") or {}
    payload = sms.get("payload") or {}
    # 指纹必须是**脱敏后** payload 算的（接入点在 make_event 之前）
    ev = by_ts.get("1760000000000")
    print(json.dumps({
        "collected": len(events), "inserted": inserted, "count": store.count_events(),
        "sms_text": payload.get("text"),
        "sms_kind": (payload.get("redact") or {}).get("kind"),
        "sms_hit": bool((payload.get("redact") or {}).get("hit")),
        "sms_fields": [payload.get(k) for k in ("ts", "pkg", "app", "title", "key")],
        "sms_fp_ok": ev is not None and ev["fingerprint"] == E.fingerprint(
            "phone-hub", "notify", payload),
        "chat_text": (chat.get("payload") or {}).get("text"),
        "chat_has_redact": "redact" in (chat.get("payload") or {}),
        "en_text": (en.get("payload") or {}).get("text"),
        "en_kind": ((en.get("payload") or {}).get("redact") or {}).get("kind"),
    }, ensure_ascii=False))

elif cmd == "seed_hist":
    store = Store(cfg.db_path); store.migrate()
    rows = [
        E.make_event("phone-hub", "notify",
                     {"ts": 11, "pkg": "com.android.mms", "app": "短信", "title": "农业银行",
                      "text": "【农业银行】验证码420917，请不要告诉他人。",
                      "key": "0|com.android.mms|11|null|1"},
                     ts="2026-10-01T08:00:00+08:00"),
        E.make_event("phone-hub", "notify",
                     {"ts": 12, "pkg": "com.android.mms", "app": "短信", "title": "哔哩哔哩",
                      "text": "【哔哩哔哩】731204为本次登录验证的手机验证码，请勿泄漏。",
                      "key": "0|com.android.mms|12|null|1"},
                     ts="2026-10-01T08:05:00+08:00"),
        E.make_event("phone-hub", "notify",
                     {"ts": 13, "pkg": "com.tencent.mobileqq", "app": "QQ", "title": "示例群聊",
                      "text": "这个验证码怎么又失效了", "key": "0|com.tencent.mobileqq|13|null|1"},
                     ts="2026-10-01T08:10:00+08:00"),
        E.make_event("fs", "fs.change", {"root": "/tmp", "path": "a.txt", "mtime": 1.0, "size": 3},
                     ts="2026-10-01T08:15:00+08:00"),
    ]
    store.insert_events(rows)
    print(snap(store))

elif cmd == "snap":
    print(snap(Store(cfg.db_path)))

elif cmd == "seed_limit":
    store = Store(cfg.db_path); store.migrate()
    samples = [("百度", "【百度】验证码：012345 。"),
               ("智谱AI", "【智谱AI】您的验证码为728194，3 分钟内有效。"),
               ("网易", "[2条]…验证码 098765。")]
    for i, (title, text) in enumerate(samples):
        store.insert_events([E.make_event("phone-hub", "notify",
                                          {"ts": 100 + i, "pkg": "com.android.mms", "app": "短信",
                                           "title": title, "text": text,
                                           "key": f"0|com.android.mms|{100 + i}|null|1"},
                                          ts=f"2026-10-02T09:0{i}:00+08:00")])
    print(store.count_events())
PY
}

get() { python3 -c 'import json,sys;print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"; }

# ---- T1 规则：强证据、字段保留、占位符 ----
out=$(py "$R" unit)
ck "T1a 中文验证码命中（短信）" "$(get "${out}" cn_hit)" "True"
ck "T1b kind = cn-code" "$(get "${out}" cn_kind)" "cn-code"
ck "T1c 正文换成占位符" "$(get "${out}" cn_text)" "$(get "${out}" placeholder)"
ck "T1d 其余字段原样保留" "$(get "${out}" fields_kept)" "all"
ck "T1e 命中时带 redact.hit" "$(get "${out}" cn_redact_hit)" "True"
ck "T1f find 的 span 指向数字串" "$(get "${out}" find_span)" "920234"
ck "T1g find 的 kind = cn-code" "$(get "${out}" find_kind)" "cn-code"

# ---- T2 不误杀 ----
ck "T2a 群聊正文一字不动" "$(get "${out}" chat_text)" "这个验证码怎么又失效了"
ck "T2b 群聊不加 redact 字段" "$(get "${out}" chat_has_redact)" "False"
ck "T2c token 后面没有值 ⇒ 不误杀" "$(get "${out}" bili_hit)" "False"
ck "T2d URL 里的 ?token= ⇒ 不误杀" "$(get "${out}" url_hit)" "False"
ck "T2e 弱证据在群聊上不生效" "$(get "${out}" weak_qq_hit)" "False"
ck "T2f 年份不当验证码" "$(get "${out}" year_kind)" "cn-keyword"

# ---- T3 英文与弱证据 ----
ck "T3a 英文 verification code 命中" "$(get "${out}" en_hit)" "True"
ck "T3b 英文 kind = en-code" "$(get "${out}" en_kind)" "en-code"
ck "T3c 英文正文换成占位符" "$(get "${out}" en_text)" "$(get "${out}" placeholder)"
ck "T3d 弱证据在短信 App 上生效" "$(get "${out}" weak_sms_hit)" "True"
ck "T3e 弱证据 kind = cn-keyword" "$(get "${out}" weak_sms_kind)" "cn-keyword"

# ---- T4 enabled=false 与幂等 ----
ck "T4a enabled=false ⇒ 正文原样" "$(get "${out}" off_text)" "【阅文】920234（阅文登录验证码），请在15分钟内完成操作。"
ck "T4b enabled=false ⇒ 不加 redact" "$(get "${out}" off_has_redact)" "False"
ck "T4c enabled=false ⇒ 摘要 hit=false" "$(get "${out}" off_hit)" "False"
ck "T4d 幂等：第二次 hit=false" "$(get "${out}" idem_hit)" "False"
ck "T4e 幂等：正文仍是占位符" "$(get "${out}" idem_text)" "$(get "${out}" placeholder)"

# ---- T5 采集器接入面：脱敏发生在 insert_events 之前 ----
out=$(py "$R" collect)
ck "T5a 假 ssh 给出 3 条" "$(get "${out}" collected)" "3"
ck "T5b 3 条都入库" "$(get "${out}" count)" "3"
ck "T5c 验证码短信入库是占位符" "$(get "${out}" sms_text)" "[验证码类]"
ck "T5d 采集侧 kind = cn-code" "$(get "${out}" sms_kind)" "cn-code"
ck "T5e 采集侧 hit=true" "$(get "${out}" sms_hit)" "True"
ck "T5f 其余字段原样保留" "$(get "${out}" sms_fields)" "[1760000000000, 'com.android.mms', '短信', '深度求索', '0|com.android.mms|1|null|1']"
ck "T5g 指纹按脱敏后 payload 算" "$(get "${out}" sms_fp_ok)" "True"
ck "T5h 群聊通知正文一字不动" "$(get "${out}" chat_text)" "这个验证码怎么又失效了"
ck "T5i 群聊通知无 redact" "$(get "${out}" chat_has_redact)" "False"
ck "T5j 英文 OTP 入库是占位符" "$(get "${out}" en_text)" "[验证码类]"
ck "T5k 英文 OTP kind = en-code" "$(get "${out}" en_kind)" "en-code"

# ---- T6 历史清洗：只 UPDATE、行数不变、幂等、dry-run 不写库 ----
H="$TMP/hist"; mkdir -p "$H"
out=$(py "$H" seed_hist)
ck "T6a 历史种 4 行（3 通知 + 1 非 phone-hub）" "$(get "${out}" count)" "4"
printf '%s' "$out" > "$TMP/snapA.json"

out=$(HUB_ROOT="$H" HUB_CONFIG="$CFG" "$ROOT/bin/hub-store" redact --source phone-hub --dry-run --json 2>&1)
ck "T6b dry-run 报将命中 2 条" "$(get "${out}" hit)" "2"
ck "T6c dry-run 不写库（updated=0）" "$(get "${out}" updated)" "0"
out=$(py "$H" snap)
python3 - "${out}" "$TMP/snapA.json" <<'PY' && pass "T6d dry-run 后库逐字节没变" || fail "T6d dry-run 写库了"
import json, sys
a = json.loads(open(sys.argv[2], encoding="utf-8").read())
b = json.loads(sys.argv[1])
assert a == b, "快照变了"
PY

out=$(HUB_ROOT="$H" HUB_CONFIG="$CFG" "$ROOT/bin/hub-store" redact --source phone-hub --json 2>&1)
ck "T6e 真清洗 hit=2" "$(get "${out}" hit)" "2"
ck "T6f 真清洗 updated=2" "$(get "${out}" updated)" "2"
out=$(py "$H" snap)
ck "T6g 清洗后总行数不变" "$(get "${out}" count)" "4"
python3 - "${out}" "$TMP/snapA.json" <<'PY' && pass "T6h 只改 payload：ts/fingerprint/privacy/ingested_at 全不动，非 phone-hub 行 payload 也不动" || fail "T6h 动了 payload 以外的东西"
import json, sys
a = json.loads(open(sys.argv[2], encoding="utf-8").read())
b = json.loads(sys.argv[1])
assert a["count"] == b["count"] == 4
for ra, rb in zip(a["rows"], b["rows"]):
    assert ra["id"] == rb["id"]
    for key in ("ts", "source", "kind", "fingerprint", "privacy", "ingested_at"):
        assert ra[key] == rb[key], f"{key} 变了: {ra} -> {rb}"
    if ra["source"] != "phone-hub":
        assert ra["payload"] == rb["payload"], "非 phone-hub 行被动"
PY
python3 - "${out}" <<'PY' && pass "T6i 两条验证码正文变占位符、群聊正文不动" || fail "T6i 正文替换不对"
import json, sys
rows = {r["payload"]["title"]: r["payload"] for r in json.loads(sys.argv[1])["rows"]
        if r["source"] == "phone-hub"}
assert rows["农业银行"]["text"] == "[验证码类]", rows["农业银行"]
assert rows["农业银行"]["redact"]["hit"] is True
assert rows["哔哩哔哩"]["text"] == "[验证码类]"
assert rows["示例群聊"]["text"] == "这个验证码怎么又失效了"
assert "redact" not in rows["示例群聊"]
PY

out=$(HUB_ROOT="$H" HUB_CONFIG="$CFG" "$ROOT/bin/hub-store" redact --source phone-hub --json 2>&1)
ck "T6j 幂等：第二次 hit=0" "$(get "${out}" hit)" "0"
ck "T6k 幂等：第二次 updated=0" "$(get "${out}" updated)" "0"
out=$(py "$H" snap)
ck "T6l 幂等：总行数仍是 4" "$(get "${out}" count)" "4"

# ---- T7 --limit 小步试跑 ----
L="$TMP/lim"; mkdir -p "$L"
ck "T7a 小步库种 3 行" "$(py "$L" seed_limit)" "3"
out=$(HUB_ROOT="$L" HUB_CONFIG="$CFG" "$ROOT/bin/hub-store" redact --source phone-hub --limit 1 --json 2>&1)
ck "T7b --limit 1 只扫 1 行" "$(get "${out}" scanned)" "1"
ck "T7c --limit 1 只改 1 行" "$(get "${out}" updated)" "1"
out=$(py "$L" snap)
python3 - "${out}" <<'PY' && pass "T7d --limit 1 只碰第一行，其余原文还在" || fail "T7d --limit 越界"
import json, sys
rows = [r["payload"] for r in json.loads(sys.argv[1])["rows"]]
assert rows[0]["text"] == "[验证码类]"
assert rows[1]["text"] != "[验证码类]" and "redact" not in rows[1]
assert rows[2]["text"] != "[验证码类]"
PY

# ---- T8 配置键齐全 ----
miss=$(python3 - "$ROOT/hub.config.yaml" <<'PY'
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8"))["redact"]
need = ("enabled", "mode", "placeholder", "patterns_version", "window_chars", "min_digits", "max_digits")
print(",".join(k for k in need if k not in d))
PY
)
[[ -z "$miss" ]] && pass "T8 redact 七个配置键齐全" || fail "T8 配置键" "缺 $miss"

printf '\nM15: pass=%d fail=%d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1

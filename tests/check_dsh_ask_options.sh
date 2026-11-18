#!/usr/bin/env bash
# DSH 双弹插件（§12.3）：台账 options 必须是第一问的真实选项，点按钮 ⇒ 能匹配回 selected。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
MOD="file://$PWD/plugins/channels/dsh-ask/index.mjs"
PASS=0; FAIL=0
ok() { PASS=$((PASS+1)); echo "PASS  $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL  $1（$2）"; }
run() { node --input-type=module -e "$1" 2>/dev/null; }

case1=$(run "
import {buildLedgerOptions} from '$MOD'
const q=[{id:'q1',question:'你从哪边答？',options:[{label:'从手机答'},{label:'从电脑答'}]}]
console.log(JSON.stringify(buildLedgerOptions(q)))")
exp1='[{"key":"ack","label":"从手机答","description":""},{"key":"done","label":"从电脑答","description":""}]'
[ "$case1" = "$exp1" ] && ok "T1 台账 options = 第一问真实选项" || bad "T1" "$case1"

case2=$(run "
import {buildLedgerOptions} from '$MOD'
console.log(JSON.stringify(buildLedgerOptions([{id:'q1',question:'确认一下'}])))")
[ "$case2" = '[{"key":"ack","label":"知道了","description":"确认一下"}]' ] && ok "T2 无选项 ⇒ 单个「知道了」" || bad "T2" "$case2"

case3=$(run "
import {buildAnswers} from '$MOD'
const q=[{id:'q1',question:'你从哪边答？',options:[{label:'从手机答'},{label:'从电脑答'}]}]
console.log(JSON.stringify(buildAnswers(q,{kind:'reply',text:'从电脑答'})))")
[ "$case3" = '{"answers":[{"id":"q1","selected":["从电脑答"]}]}' ] && ok "T3 按 label 回传 ⇒ selected 命中" || bad "T3" "$case3"

case4=$(run "
import {buildAnswers} from '$MOD'
const q=[{id:'q1',question:'你从哪边答？',options:[{label:'从手机答'}]}]
console.log(JSON.stringify(buildAnswers(q,{kind:'reply',text:'我打字说别的'})))")
[ "$case4" = '{"answers":[{"id":"q1","selected":[],"custom":"我打字说别的"}]}' ] && ok "T4 自由文本 ⇒ custom" || bad "T4" "$case4"

case5=$(run "
import {buildLedgerOptions, buildPhonePayload} from '$MOD'
const q=[{id:'q1',question:'你从哪边答？',options:[{label:'从手机答'},{label:'从电脑答'}]}]
const led=buildLedgerOptions(q).map(o=>o.label)
const ph=buildPhonePayload(q,'d-x',{timeout_ms:1000}).options.map(o=>o.label)
console.log(JSON.stringify([led,ph,led.length===ph.length&&led.every((v,i)=>v===ph[i])]))")
[ "$case5" = '[["从手机答","从电脑答"],["从手机答","从电脑答"],true]' ] && ok "T5 台账按钮与手机按钮同源" || bad "T5" "$case5"

echo
echo "$PASS 通过 / $FAIL 失败"
[ "$FAIL" -eq 0 ] && echo "ALL PASS"
[ "$FAIL" -eq 0 ]

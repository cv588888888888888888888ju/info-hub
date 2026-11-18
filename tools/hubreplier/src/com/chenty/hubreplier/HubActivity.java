package com.chenty.hubreplier;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;

import org.json.JSONObject;

/**
 * 单一入口 Activity（hub 侧 adb 调用）。实测：小米 HyperOS 的 Greezer 会丢弃
 * 来自 shell 的广播（"need cached broadcast" / "process is not permitted to auto start"），
 * 而 am start 一个 Activity 不受限制，所以正式投递通道走这里：
 *   adb shell am start -n com.chenty.hubreplier/.HubActivity --es op show --es payload_b64 <b64>
 * 契约见 README.md §2：op = show / reply / action / dismiss。
 * token：Activity 是我们自己的显式入口，token 由这里补上（调用方不必传）；广播路径仍强制校验。
 */
public class HubActivity extends Activity {
    private static final String TAG = "HubReplier";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent i = getIntent();
        String op = i == null ? null : i.getStringExtra("op");
        if (op == null || op.isEmpty()) {
            op = "show";
        }
        Log.i(TAG, "HubActivity op=" + op);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] { "android.permission.POST_NOTIFICATIONS" }, 1);
        }

        try {
            decodePayload(i);
            if ("reply".equals(op) || "test_reply".equals(op)) {
                i.setAction(BuildConfig.ACTION_TEST_REPLY);
                ResponseReceiver.handle(this, i, null);
            } else if ("action".equals(op)) {
                // 脚本化选项答复（Lead 回归用）：走 ResponseReceiver 的选项按钮分支，
                // 但 src 标成 test，便于和「通知栏真点按钮」产生的 src=button 行区分。
                String key = i.getStringExtra("option_key");
                if (key == null || key.isEmpty()) {
                    key = i.getStringExtra("option");
                }
                if (key == null || key.isEmpty()) {
                    Log.w(TAG, "action op: missing option_key");
                } else {
                    i.setAction(BuildConfig.ACTION_RESPONSE);
                    i.putExtra("option_index", 0);
                    i.putExtra("option_text", key);
                    i.putExtra("option_key", key);
                    i.putExtra("src", "test");
                    ResponseReceiver.handle(this, i, null);
                }
            } else if ("dismiss".equals(op)) {
                i.setAction(BuildConfig.ACTION_DISMISS);
                DismissReceiver.handle(this, i);
            } else {
                i.setAction(BuildConfig.ACTION_SHOW);
                ShowReceiver.handle(this, i);
            }
        } catch (Throwable t) {
            Log.e(TAG, "HubActivity dispatch failed op=" + op, t);
        }
        finish();
    }

    /** 把 payload_b64 里的 id/text 摊到 extras（缺少时），方便 reply / dismiss 复用。 */
    private void decodePayload(Intent i) {
        String b64 = i.getStringExtra("payload_b64");
        if (b64 == null || b64.trim().isEmpty()) {
            return;
        }
        try {
            String json = new String(android.util.Base64.decode(b64, android.util.Base64.DEFAULT),
                    java.nio.charset.StandardCharsets.UTF_8);
            JSONObject o = new JSONObject(json);
            if (!i.hasExtra("id") && o.has("id")) {
                i.putExtra("id", o.optString("id"));
            }
            if (!i.hasExtra("text") && o.has("text")) {
                i.putExtra("text", o.optString("text"));
            }
        } catch (Exception e) {
            Log.w(TAG, "decodePayload failed: " + e);
        }
    }
}

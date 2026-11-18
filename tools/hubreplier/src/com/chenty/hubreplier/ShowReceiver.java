package com.chenty.hubreplier;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 投递入口：发一条可回复通知。
 * extras 契约见 README.md §2：优先读整包 JSON extra "payload" / "payload_b64"；
 * 也兼容逐字段 extras（token/id/title/body/mode + --esa options）。
 * options 元素可以是字符串 "OK"，也可以是对象 {"key":"ok","label":"OK"}。
 */
public class ShowReceiver extends BroadcastReceiver {
    private static final String TAG = "HubReplier";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        handle(ctx, intent);
    }

    /** 唯一实现。广播入口和 HubActivity（am start）入口都走这里。 */
    static void handle(Context ctx, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (!BuildConfig.ACTION_SHOW.equals(action)) {
            Log.w(TAG, "SHOW ignored: unexpected action=" + action);
            return;
        }

        String payload = intent.getStringExtra("payload");
        String payloadB64 = intent.getStringExtra("payload_b64");
        if (payloadB64 != null && !payloadB64.trim().isEmpty()) {
            try {
                payload = new String(android.util.Base64.decode(payloadB64, android.util.Base64.DEFAULT),
                        java.nio.charset.StandardCharsets.UTF_8);
            } catch (Exception e) {
                Log.w(TAG, "SHOW rejected: payload_b64 not valid base64: " + e);
                return;
            }
        }

        String token;
        String id;
        String title;
        String body;
        String mode;
        String[] labels;
        String[] keys;

        if (payload != null && !payload.trim().isEmpty()) {
            try {
                JSONObject o = new JSONObject(payload);
                token = o.optString("token", null);
                id = o.optString("id", null);
                title = o.optString("title", null);
                body = o.optString("body", null);
                mode = o.optString("mode", null);
                JSONArray arr = o.optJSONArray("options");
                int n = arr == null ? 0 : Math.min(arr.length(), 3);
                labels = new String[n];
                keys = new String[n];
                for (int i = 0; i < n; i++) {
                    JSONObject opt = arr.optJSONObject(i);
                    if (opt != null) {
                        labels[i] = opt.optString("label", opt.optString("key", ""));
                        keys[i] = opt.optString("key", labels[i]);
                    } else {
                        labels[i] = String.valueOf(arr.opt(i));
                        keys[i] = labels[i];
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "SHOW rejected: payload not valid JSON: " + e);
                return;
            }
        } else {
            token = intent.getStringExtra("token");
            id = intent.getStringExtra("id");
            title = intent.getStringExtra("title");
            body = intent.getStringExtra("body");
            mode = intent.getStringExtra("mode");
            String[] raw = intent.getStringArrayExtra("options");
            int n = raw == null ? 0 : Math.min(raw.length, 3);
            labels = new String[n];
            keys = new String[n];
            for (int i = 0; i < n; i++) {
                labels[i] = raw[i];
                keys[i] = raw[i];
            }
        }

        String want = Endpoints.token(ctx);
        if (want.isEmpty() || !want.equals(token)) {
            Log.w(TAG, "SHOW rejected: bad token");
            return;
        }
        id = safeId(id);
        if (id == null) {
            Log.w(TAG, "SHOW rejected: missing/invalid id");
            return;
        }
        if (title == null || title.isEmpty()) {
            title = "info-hub";
        }
        if (body == null) {
            body = "";
        }
        if (mode == null || mode.isEmpty()) {
            mode = "notify";
        }

        ReplyStore.saveMeta(ctx, id, title, body, mode, labels, keys);
        Notifications.show(ctx, id, title, body, labels, keys, mode);
        Log.i(TAG, "SHOW ok id=" + id + " mode=" + mode + " options=" + labels.length
                + " title=" + title);
    }

    /** id 只允许 [A-Za-z0-9._-]，避免落到文件名里出意外。 */
    static String safeId(String raw) {
        if (raw == null) {
            return null;
        }
        String s = raw.trim();
        if (s.isEmpty() || s.length() > 96) {
            return null;
        }
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            boolean ok = (c >= 97 && c <= 122) || (c >= 65 && c <= 90)
                    || (c >= 48 && c <= 57) || c == 46 || c == 95 || c == 45;
            if (!ok) {
                return null;
            }
        }
        return s;
    }
}

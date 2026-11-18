package com.chenty.hubreplier;

import android.content.Context;
import android.util.Log;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 回执通道：POST 到宿主 info-hub 的 /api/deliver/reply。
 * 地址与 token 都来自设置页（hub_home：url / token），地址选择复用 Endpoints.pick
 * （直连优先、全不通走本机中转）——即与消息页完全同一条链路。
 * 落盘是兜底：HTTP 失败不算答复失败（ReplyStore 已写盘）。
 * 契约见 README.md §4「回执 HTTP」。
 */
final class HubClient {
    private static final String TAG = "HubReplier";
    private static final int CONNECT_TIMEOUT_MS = 2500;
    private static final int READ_TIMEOUT_MS = 4000;

    private HubClient() {}

    /** @return true = 2xx；false = hub 不可达/非 2xx（答复已落盘，不算丢）。 */
    static boolean postReply(Context ctx, String id, String text) {
        String base = Endpoints.pick(ctx);
        if (base == null) {
            Log.w(TAG, "postReply: 没有可用地址（直连与本机中转都不通）");
            return false;
        }
        String token = Endpoints.token(ctx);
        HttpURLConnection c = null;
        try {
            JSONObject body = new JSONObject();
            body.put("id", id);
            body.put("text", text == null ? "" : text);
            byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
            c = (HttpURLConnection) new URL(base + "api/deliver/reply").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(CONNECT_TIMEOUT_MS);
            c.setReadTimeout(READ_TIMEOUT_MS);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("Accept", "application/json");
            if (token != null && !token.isEmpty()) {
                c.setRequestProperty("X-Hub-Token", token);
            }
            OutputStream os = c.getOutputStream();
            os.write(payload);
            os.flush();
            os.close();
            int code = c.getResponseCode();
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            String resp = "";
            if (in != null) {
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] buf = new byte[2048];
                int n;
                while ((n = in.read(buf)) > 0) {
                    bos.write(buf, 0, n);
                }
                in.close();
                resp = bos.toString("UTF-8");
            }
            Log.i(TAG, "postReply " + base + " id=" + id + " => " + code + " " + resp);
            return code >= 200 && code < 300;
        } catch (Exception e) {
            Log.w(TAG, "postReply failed: " + e);
            Endpoints.invalidate();
            return false;
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }
}

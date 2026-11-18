package com.chenty.hubreplier;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * 候选地址选取（契约见 info-hub/INTERFACE.md §11.10）：
 * 内网 HTTP / IPv6 / Cloudflare https / 主机名 / 本机中转，按顺序探测，
 * 第一个回 200 的胜出；全都不通才启用本机中转（SOCKS5 → 手机 MCP 隧道）。
 *
 * prefs（hub_home）：
 *   url   主地址（必填；也可以是 Cloudflare / IPv6 / 内网地址）
 *   urls  备选地址，逗号或换行分隔
 *   relay "0" 表示禁用本机中转（纯内网使用时不装隧道 App 也能用）
 */
final class Endpoints {
    static final String TAG = "HubReplier";
    static final String KEY_URLS = "urls";
    static final String KEY_RELAY = "relay";
    private static final int PROBE_TIMEOUT_MS = 2500;
    private static final long CACHE_MS = 60000L;

    private static String cached;
    private static long cachedAt;

    private Endpoints() {}

    static String token(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);
        String t = sp.getString(HomeActivity.KEY_TOKEN, "");
        return t == null ? "" : t.trim();   // 只认用户在设置页填的值（m05209 澄清：装完手填，不自动补）
    }

    /** 主地址在前、备选在后；都以 / 结尾。 */
    static List<String> candidates(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);
        List<String> raw = new ArrayList<String>();
        String primary = sp.getString(HomeActivity.KEY_URL, "").trim();
        if (!primary.isEmpty()) {
            raw.add(primary);
        }
        String extra = sp.getString(KEY_URLS, "");
        for (String piece : extra.split("[,\\n\\r]+")) {
            String u = piece.trim();
            if (!u.isEmpty() && !raw.contains(u)) {
                raw.add(u);
            }
        }
        // 一条地址都没有就返回空表 —— 不再用编译期缺省候选（用户 m05209 澄清：装完由人在设置页填）
        List<String> out = new ArrayList<String>();
        for (String u : raw) {
            out.add(u.endsWith("/") ? u : u + "/");
        }
        return out;
    }

    /** 探测并缓存选中的 base；全都不可达返回 null。 */
    static String pick(Context ctx) {
        long now = System.currentTimeMillis();
        String hit = cached;
        if (hit != null && now - cachedAt < CACHE_MS) {
            return hit;
        }
        String token = token(ctx);
        List<String> cands = candidates(ctx);
        for (String u : cands) {
            if (probe(u, token)) {
                remember(u);
                Log.i(TAG, "Endpoints picked " + u);
                return u;
            }
        }
        SharedPreferences sp = ctx.getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);
        if (!"0".equals(sp.getString(KEY_RELAY, "1")) && LocalRelay.ensure(ctx)) {
            String relay = LocalRelay.base(ctx);
            if (probe(relay, token)) {
                remember(relay);
                Log.i(TAG, "Endpoints picked relay " + relay);
                return relay;
            }
        }
        Log.w(TAG, "Endpoints: 没有可用地址 " + cands);
        invalidate();
        return null;
    }

    private static void remember(String base) {
        cached = base;
        cachedAt = System.currentTimeMillis();
    }

    /** 轮询失败时调用，下次重新探测（网络切换后不必等 App 重启）。 */
    static void invalidate() {
        cached = null;
        cachedAt = 0L;
    }

    /** 只发一个带 token 的 GET api/messages?limit=1；200 即算可达。 */
    static boolean probe(String base, String token) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "api/messages?limit=1").openConnection();
            c.setConnectTimeout(PROBE_TIMEOUT_MS);
            c.setReadTimeout(PROBE_TIMEOUT_MS);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("X-Hub-Token", token == null ? "" : token);
            int code = c.getResponseCode();
            return code == 200;
        } catch (Exception e) {
            Log.i(TAG, "probe " + base + " 不通：" + e);
            return false;
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }
}

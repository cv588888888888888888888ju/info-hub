package com.chenty.hubreplier;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.drawable.Icon;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * info-hub 自己的常驻通知（不依赖手机 MCP / com.jxev.ai）：
 * 前台服务每 2 分钟向 info-hub 的 phone_api 拉一次待处理数，把结果画成一条不可划掉的常驻通知，
 * 点它直接进主页。URL 与 token 复用 hub_home 偏好（HomeActivity 写入）。
 * 契约见 README.md §2.2 / INTERFACE.md §11.10。
 */
public class HomeSync extends Service {
    private static final String TAG = "HubReplier";
    private static final String CHANNEL = "hub_home_ongoing";
    private static final int NOTIF_ID = 42001;
    private static final long INTERVAL_MS = 30000L; // 强提醒要快：30s 一轮（原来是 120s）

    /** 已经发过通知的 delivery id；下一轮不再重复弹（只更新计数）。 */
    private static final java.util.Set<String> shownIds = new java.util.HashSet<String>();
    /** 已经弹过全屏强提醒页的 delivery id（答完/被收走后移除，允许再推）。 */
    private static final java.util.Set<String> strongShown = new java.util.HashSet<String>();

    private final Handler handler = new Handler(Looper.getMainLooper());
    private Runnable tick;

    /** 供 MainActivity / HomeActivity 调用；url 没配置就什么都不做。 */
    static void start(Context ctx) {
        SharedPreferences sp = ctx.getSharedPreferences("hub_home", Context.MODE_PRIVATE);
        if (sp.getString("url", "").isEmpty()) {
            return;
        }
        Intent i = new Intent(ctx, HomeSync.class);
        try {
            ctx.startForegroundService(i);
        } catch (Exception e) {
            Log.w(TAG, "HomeSync.start failed: " + e);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        ensureChannel(this);
        LocalRelay.ensure(this);
        // 清掉上一个进程遗留的卡片（「已回复：…」这类只该短暂存在），常驻那条随后立即重建
        NotificationManager nmClear = getSystemService(NotificationManager.class);
        if (nmClear != null) {
            nmClear.cancelAll();
        }
        startForeground(NOTIF_ID, build(this, -1, "同步中…"));
        if (tick == null) {
            tick = new Runnable() {
                @Override
                public void run() {
                    poll();
                    handler.postDelayed(this, INTERVAL_MS);
                }
            };
            handler.post(tick);
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (tick != null) {
            handler.removeCallbacks(tick);
            tick = null;
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void poll() {
        final String url;
        final String token;
        SharedPreferences sp = getSharedPreferences("hub_home", Context.MODE_PRIVATE);
        token = sp.getString("token", "");
        String direct = sp.getString("url", "");
        if (direct.isEmpty()) {
            return;
        }
        new Thread(new Runnable() {
            @Override
            public void run() {
                // 候选地址探测必须在后台线程（主线程会抛 NetworkOnMainThreadException）：
                // 内网 HTTP / IPv6 / Cloudflare https 直连优先，全都不通才用本机中转（§11.10）。
                String base = Endpoints.pick(HomeSync.this);
                String url = base != null ? base : direct;
                try {
                    String body = get(url + "api/messages?limit=80&archived=0", token);
                    JSONArray rows = new JSONObject(body).optJSONArray("messages");
                    int pending = 0;
                    int total = rows == null ? 0 : rows.length();
                    Notifications.ensureChannel(HomeSync.this);
                    java.util.Set<String> alive = new java.util.HashSet<String>();
                    for (int i = 0; rows != null && i < rows.length(); i++) {
                        JSONObject m = rows.optJSONObject(i);
                        if (m == null) {
                            continue;
                        }
                        String st = m.optString("status", "");
                        if (!("pending".equals(st) || "claimed".equals(st))) {
                            continue;
                        }
                        pending++;
                        String id = m.optString("id", "");
                        if (id.isEmpty()) {
                            continue;
                        }
                        alive.add(id);
                        // 每条待处理都发一条可回复通知：有选项给按钮（最多 3 个），一律带 RemoteInput 直接回复
                        java.util.List<String> labels = new java.util.ArrayList<String>();
                        java.util.List<String> keys = new java.util.ArrayList<String>();
                        JSONArray opts = m.optJSONArray("options");
                        for (int k = 0; opts != null && k < opts.length() && labels.size() < 3; k++) {
                            Object o = opts.opt(k);
                            if (o instanceof JSONObject) {
                                JSONObject jo = (JSONObject) o;
                                labels.add(jo.optString("label", jo.optString("key", "")));
                                keys.add(jo.optString("key", jo.optString("label", "")));
                            } else if (o != null) {
                                labels.add(String.valueOf(o));
                                keys.add(String.valueOf(o));
                            }
                        }
                        if (shownIds.add(id)) {
                            Notifications.show(HomeSync.this, id,
                                    m.optString("title", "info-hub"),
                                    m.optString("body", ""),
                                    labels.toArray(new String[0]),
                                    keys.toArray(new String[0]),
                                    m.optString("mode", ""));
                        }
                        // kind=strong：再拉一张可回复的全屏页（锁屏也弹），选项与输入都在这页上
                        if ("strong".equals(m.optString("kind", "")) && strongShown.add(id)) {
                            final Intent si = StrongActivity.intentFor(HomeSync.this, id,
                                    m.optString("title", "info-hub"),
                                    m.optString("body", ""),
                                    opts == null ? "" : opts.toString());
                            handler.post(new Runnable() {
                                @Override
                                public void run() {
                                    try {
                                        startActivity(si);
                                    } catch (Exception e) {
                                        Log.w(TAG, "strong start failed: " + e);
                                    }
                                }
                            });
                        }
                    }
                    // 已经答复/归档/被别处收走的：把通知一并收回
                    for (java.util.Iterator<String> it = shownIds.iterator(); it.hasNext(); ) {
                        String id = it.next();
                        if (!alive.contains(id)) {
                            Notifications.dismiss(HomeSync.this, id);
                            strongShown.remove(id);
                            it.remove();
                        }
                    }
                    String text = pending > 0 ? (pending + " 条待处理") : ("没有待处理 · 共 " + total + " 条");
                    NotificationManager nm = getSystemService(NotificationManager.class);
                    if (nm != null) {
                        nm.notify(NOTIF_ID, build(HomeSync.this, pending, text));
                    }
                    Log.i(TAG, "HomeSync pending=" + pending + " total=" + total);
                } catch (Exception e) {
                    Log.w(TAG, "HomeSync poll failed: " + e);
                    Endpoints.invalidate();
                    NotificationManager nm = getSystemService(NotificationManager.class);
                    if (nm != null) {
                        nm.notify(NOTIF_ID, build(HomeSync.this, -1, "连不上 info-hub（" + url + "）"));
                    }
                }
            }
        }).start();
    }

    private static String get(String urlStr, String token) throws Exception {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(urlStr).openConnection();
            c.setConnectTimeout(4000);
            c.setReadTimeout(6000);
            c.setRequestProperty("Accept", "application/json");
            if (token != null && !token.isEmpty()) {
                c.setRequestProperty("X-Hub-Token", token);
            }
            int code = c.getResponseCode();
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in == null) {
                throw new IllegalStateException("HTTP " + code);
            }
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
            }
            in.close();
            if (code >= 400) {
                throw new IllegalStateException("HTTP " + code + " " + bos.toString("UTF-8"));
            }
            return bos.toString("UTF-8");
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private static void ensureChannel(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(CHANNEL, "info-hub 常驻", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("info-hub 待处理数（本体自己发，不经过手机 MCP）");
        ch.setShowBadge(false);
        ch.setSound(null, null);
        ch.enableVibration(false);
        nm.createNotificationChannel(ch);
    }

    private static Notification build(Context ctx, int pending, String text) {
        Intent open = new Intent(ctx, HomeActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pi = PendingIntent.getActivity(
                ctx, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(ctx, CHANNEL)
                .setSmallIcon(Icon.createWithResource(ctx, R.drawable.ic_stat_reply))
                .setContentTitle("info-hub")
                .setContentText(text)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setShowWhen(false)
                .setContentIntent(pi);
        if (pending > 0) {
            b.setNumber(pending);
        }
        return b.build();
    }
}

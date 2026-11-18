package com.chenty.hubreplier;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.util.Log;

/**
 * 通知构建 / 更新。契约见 README.md §2。
 * 只用 framework API（无 androidx），因此不需要 gradle / aar。
 */
final class Notifications {
    private static final String TAG = "HubReplier";

    private Notifications() {}

    /** 通知 id = delivery_id 的稳定正数 hash（DISSMISS 靠它算出同一条）。 */
    static int notifId(String id) {
        int h = id.hashCode() & 0x7fffffff;
        return h == 0 ? 1 : h;
    }

    /** 通知能不能真的弹出来：Android 13+ 要 POST_NOTIFICATIONS，之外看 App 级开关。 */
    static boolean allowed(Context ctx) {
        try {
            if (android.os.Build.VERSION.SDK_INT >= 33
                    && ctx.checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                       != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                return false;
            }
        } catch (Throwable ignored) {
            // 老系统没有这个权限，走下面的 areNotificationsEnabled
        }
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        return nm != null && nm.areNotificationsEnabled();
    }

    static void ensureChannel(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        if (nm.getNotificationChannel(BuildConfig.CHANNEL_ID) == null) {
            NotificationChannel ch = new NotificationChannel(
                    BuildConfig.CHANNEL_ID, "info-hub 可回复通知", NotificationManager.IMPORTANCE_HIGH);
            ch.setDescription("info-hub 投递到手机、可在通知栏直接回复的消息");
            ch.setShowBadge(true);
            ch.enableVibration(true);
            ch.setLockscreenVisibility(Notification.VISIBILITY_PRIVATE);
            nm.createNotificationChannel(ch);
        }
    }

    /** Android 12+ 要求 RemoteInput 的 PendingIntent 必须是 MUTABLE，否则系统填不进结果。 */
    private static PendingIntent pi(Context ctx, Intent intent, int reqCode) {
        return PendingIntent.getBroadcast(ctx, reqCode, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
    }

    private static Intent responseIntent(Context ctx, String id) {
        Intent i = new Intent(ctx, ResponseReceiver.class);
        i.setAction(BuildConfig.ACTION_RESPONSE);
        i.putExtra("id", id);
        return i;
    }

    /**
     * 显示一条可回复通知：title + body + 1 个 RemoteInput 回复动作 + 最多 3 个选项按钮。
     * 同 id 再次调用会替换同一条（notification id 由 id 决定），不会堆两条。
     *
     * @param labels 选项按钮文案
     * @param keys   选项 key（答复落盘用）；null 时用 label
     */
    static void show(Context ctx, String id, String title, String body,
                     String[] labels, String[] keys, String mode) {
        ensureChannel(ctx);
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            Log.e(TAG, "no NotificationManager");
            return;
        }
        int nid = notifId(id);

        // ---- RemoteInput 回复动作 ----
        android.app.RemoteInput ri = new android.app.RemoteInput.Builder(BuildConfig.KEY_REPLY_TEXT)
                .setLabel("回复")
                .build();
        Notification.Action replyAction = new Notification.Action.Builder(
                Icon.createWithResource(ctx, R.drawable.ic_stat_reply), "回复",
                pi(ctx, responseIntent(ctx, id), nid))
                .addRemoteInput(ri)
                .setAllowGeneratedReplies(true)
                .build();

        Notification.Builder b = base(ctx, id, title, body, mode).addAction(replyAction);

        // ---- 选项按钮（最多 3 个；与回复动作合计 4 个时，第 4 个在 MIUI 上收进「更多」）----
        int n = labels == null ? 0 : Math.min(labels.length, 3);
        for (int i = 0; i < n; i++) {
            Intent oi = responseIntent(ctx, id);
            oi.putExtra("option_index", i);
            oi.putExtra("option_text", labels[i]);
            oi.putExtra("option_key", keys != null && i < keys.length && keys[i] != null
                    ? keys[i] : labels[i]);
            Notification.Action a = new Notification.Action.Builder(
                    Icon.createWithResource(ctx, R.drawable.ic_stat_reply), labels[i],
                    pi(ctx, oi, nid + 1 + i)).build();
            b.addAction(a);
        }

        nm.notify(nid, b.build());
        Log.i(TAG, "shown id=" + id + " nid=" + nid + " actions=" + (1 + n) + " mode=" + mode);
    }

    /** 答复之后把同一条通知刷成「已回复」。posted=HTTP 回传是否成功（失败只影响文案）。 */
    static void markAnswered(Context ctx, String id, String title, String answer, boolean posted) {
        ensureChannel(ctx);
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        String suffix = posted ? "（已回传 info-hub）" : "（已落盘，PC 侧可读）";
        String text = "已回复：" + (answer == null ? "" : answer) + suffix;
        Notification.Builder b = base(ctx, id, title, text, "answered").setAutoCancel(true);
        nm.notify(notifId(id), b.build());
        Log.i(TAG, "marked answered id=" + id + " posted=" + posted);
    }

    /** DISMISS：收回该 delivery 的通知。幂等（不存在也不报错）。 */
    static void dismiss(Context ctx, String id) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm == null) {
            return;
        }
        nm.cancel(notifId(id));
        Log.i(TAG, "dismissed id=" + id + " nid=" + notifId(id));
    }

    private static Notification.Builder base(Context ctx, String id, String title, String body,
                                             String mode) {
        long now = System.currentTimeMillis();
        return new Notification.Builder(ctx, BuildConfig.CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_stat_reply)
                .setContentTitle(title == null ? "info-hub" : title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setWhen(now)
                .setShowWhen(true)
                .setCategory(Notification.CATEGORY_MESSAGE)
                .setVisibility(Notification.VISIBILITY_PRIVATE)
                .setAutoCancel(false)
                .setOnlyAlertOnce(true)
                .setSubText(mode == null || mode.isEmpty() || "null".equals(mode)
                        ? null : "hub:" + mode)
                .setExtras(bundleWithId(id));
    }

    private static android.os.Bundle bundleWithId(String id) {
        android.os.Bundle b = new android.os.Bundle();
        b.putString("hub_id", id);
        return b;
    }
}

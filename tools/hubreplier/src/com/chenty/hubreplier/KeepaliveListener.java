package com.chenty.hubreplier;

import android.service.notification.NotificationListenerService;
import android.util.Log;

/**
 * 常驻锚点（不读、不存任何通知内容）。
 * 目的：小米 HyperOS 的 Greezer 会拒绝把广播投递给「cached」进程
 * （日志：Greezer Denial ... need cached broadcast / process is not permitted to auto start），
 * 而系统绑定的 NotificationListenerService 持有服务级 adj，进程不会被判定为 cached，
 * 于是 SHOW / TEST_REPLY / RemoteInput 回执的广播都能照常收到。
 * 授权命令见 README §5（adb shell cmd notification allow_listener ...）。
 */
public class KeepaliveListener extends NotificationListenerService {
    private static final String TAG = "HubReplier";

    @Override
    public void onListenerConnected() {
        Log.i(TAG, "NotificationListener connected (keepalive active)");
    }

    @Override
    public void onListenerDisconnected() {
        Log.w(TAG, "NotificationListener disconnected");
    }
}

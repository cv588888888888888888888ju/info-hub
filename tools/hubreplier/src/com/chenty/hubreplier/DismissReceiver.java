package com.chenty.hubreplier;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * 收回入口：DISMISS + extra id → 取消该 delivery 的通知。幂等。
 * 契约见 README.md §2「action 表」。token 可选：带了就校验，没带也放行（收回是降风险操作）。
 */
public class DismissReceiver extends BroadcastReceiver {
    private static final String TAG = "HubReplier";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        handle(ctx, intent);
    }

    /** 广播入口与 HubActivity（am start）入口共用。 */
    static void handle(Context ctx, Intent intent) {
        if (intent == null || !BuildConfig.ACTION_DISMISS.equals(intent.getAction())) {
            Log.w(TAG, "DISMISS ignored: unexpected action="
                    + (intent == null ? null : intent.getAction()));
            return;
        }
        // token 只与设置页一致才收（服务端不再有写死的值）
        String token = intent.getStringExtra("token");
        String want = Endpoints.token(ctx);
        if (token != null && !token.isEmpty() && !token.equals(want)) {
            Log.w(TAG, "DISMISS rejected: bad token");
            return;
        }
        String id = ShowReceiver.safeId(intent.getStringExtra("id"));
        if (id == null) {
            Log.w(TAG, "DISMISS ignored: missing/invalid id");
            return;
        }
        Notifications.dismiss(ctx, id);
    }
}

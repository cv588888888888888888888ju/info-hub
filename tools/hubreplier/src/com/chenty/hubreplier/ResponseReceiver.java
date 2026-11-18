package com.chenty.hubreplier;

import android.app.RemoteInput;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import android.util.Log;

/**
 * 答复入口。三条路径共用同一个 commit()（落盘 → 回传 → 通知更新）：
 *  - RESPONSE：真实 RemoteInput 结果（系统把文本填进 clipData）
 *  - RESPONSE：选项按钮直发（extras 里带 option_index / option_text）
 *  - TEST_REPLY：仅测试。自己用 RemoteInput.addResultsToIntent 造出与系统回填同形的
 *                clipData，再走同一个 extractReplyText()，证明这条链的代码分支是通的。
 * 广播入口（onReceive）和 HubActivity（am start，用于 MIUI 丢广播时）都走 handle()。
 * 契约见 README.md §4。
 */
public class ResponseReceiver extends BroadcastReceiver {
    private static final String TAG = "HubReplier";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        final PendingResult pr = goAsync();
        boolean asyncOwned;
        try {
            asyncOwned = handle(ctx, intent, new Runnable() {
                @Override
                public void run() {
                    pr.finish();
                }
            });
        } catch (Throwable t) {
            Log.e(TAG, "response dispatch failed", t);
            asyncOwned = false;
        }
        if (!asyncOwned) {
            pr.finish();
        }
    }

    /**
     * @param onDone 处理完（含异步 POST）后回调，可为 null（Activity 入口不需要）。
     * @return true = 异步线程会调用 onDone / 已经处理；false = 同步结束，调用方自己收尾。
     */
    static boolean handle(Context ctx, Intent intent, Runnable onDone) {
        if (intent == null) {
            return false;
        }
        String action = intent.getAction();
        // 通知里的 PendingIntent 是显式、不可导出的（receiver exported=false），默认不带 token；
        // 外部脚本入口若带了 token，则必须与设置页里的 token 一致（或历史缺省 token）。
        String got = intent.getStringExtra("token");
        if (got != null && !got.isEmpty()) {
            String want = Endpoints.token(ctx);
            if (!got.equals(want)) {
                Log.w(TAG, "response rejected: bad token action=" + action);
                return false;
            }
        }
        String id = ShowReceiver.safeId(intent.getStringExtra("id"));
        if (id == null) {
            Log.w(TAG, "response rejected: missing/invalid id");
            return false;
        }
        if (BuildConfig.ACTION_TEST_REPLY.equals(action)) {
            return handleTest(ctx, intent, id, onDone);
        }
        if (BuildConfig.ACTION_RESPONSE.equals(action)) {
            return handleResponse(ctx, intent, id, onDone);
        }
        Log.w(TAG, "response ignored: unexpected action=" + action);
        return false;
    }

    /** 真实路径：RemoteInput 文本，或选项按钮。 */
    private static boolean handleResponse(Context ctx, Intent intent, String id, Runnable onDone) {
        if (intent.hasExtra("option_index")) {
            String label = intent.getStringExtra("option_text");
            String key = intent.getStringExtra("option_key");
            if (key == null || key.isEmpty()) {
                key = label;
            }
            // 真点按钮走系统 PendingIntent，不带 src；op=action 的脚本入口会显式带 src=test。
            String src = intent.getStringExtra("src");
            if (src == null || src.isEmpty()) {
                src = "button";
            }
            // 选项按钮：本地账仍记 key，回传 info-hub 的文字用人类可读的 label（「收到」而不是 yes）
            return commit(ctx, id, src, "action", null, key, label, onDone);
        }
        String text = extractReplyText(intent);
        if (text == null) {
            Log.w(TAG, "response: no RemoteInput results and no text extra, id=" + id);
            return false;
        }
        return commit(ctx, id, "remoteinput", "reply", text, null, null, onDone);
    }

    /**
     * 仅测试路径：用 RemoteInput.addResultsToIntent 造一个和系统回填等价的 intent，
     * 然后走完全相同的 extractReplyText，证明「读 RemoteInput 结果」这段代码是通的。
     */
    private static boolean handleTest(Context ctx, Intent intent, String id, Runnable onDone) {
        String text = intent.getStringExtra("text");
        if (text == null) {
            text = "TEST_REPLY";
        }
        RemoteInput ri = new RemoteInput.Builder(BuildConfig.KEY_REPLY_TEXT).setLabel("回复").build();
        Bundle results = new Bundle();
        results.putCharSequence(BuildConfig.KEY_REPLY_TEXT, text);

        Intent synthetic = new Intent();
        RemoteInput.addResultsToIntent(new RemoteInput[] { ri }, synthetic, results);

        String extracted = extractReplyText(synthetic);
        Log.i(TAG, "TEST_REPLY id=" + id + " injected=" + text + " extracted=" + extracted);
        if (extracted == null) {
            Log.e(TAG, "TEST_REPLY: extract path broken (getResultsFromIntent returned null)");
            return false;
        }
        return commit(ctx, id, "test", "reply", extracted, null, null, onDone);
    }

    /**
     * 唯一取文本的地方：先读 RemoteInput 结果（系统放在 clipData 的 EXTRA_RESULTS_DATA 里），
     * 再退到 extras["text"]。
     */
    static String extractReplyText(Intent intent) {
        Bundle results = RemoteInput.getResultsFromIntent(intent);
        if (results != null) {
            CharSequence cs = results.getCharSequence(BuildConfig.KEY_REPLY_TEXT);
            if (cs != null) {
                return cs.toString();
            }
        }
        return intent.getStringExtra("text");
    }

    /**
     * 唯一落盘点：先写盘（主通道），再异步 POST（兜底），最后把通知刷成「已回复」。
     *
     * @param src  remoteinput / button / test
     * @param kind reply / action
     */
    private static boolean commit(Context ctx, String id, String src, String kind,
                                   String text, String action, String label, final Runnable onDone) {
        ReplyStore.write(ctx, id, src, kind, text, action);
        final String fid = id;
        // 回传文本：自由回复用原文；选项按钮用 label（没有 label 才退回 key）
        final String ftext = (text == null || text.isEmpty())
                ? ((label != null && !label.isEmpty()) ? label : (action == null ? "" : action))
                : text;
        final String title = ReplyStore.metaTitle(ctx, id);
        final Context app = ctx.getApplicationContext();
        Log.i(TAG, "committed id=" + fid + " src=" + src + " kind=" + kind
                + " text=" + text + " action=" + action);

        new Thread(new Runnable() {
            @Override
            public void run() {
                boolean posted = false;
                try {
                    posted = HubClient.postReply(app, fid, ftext);
                } catch (Throwable t) {
                    Log.w(TAG, "postReply threw: " + t);
                } finally {
                    try {
                        Notifications.markAnswered(app, fid, title, ftext, posted);
                    } catch (Throwable t) {
                        Log.w(TAG, "markAnswered threw: " + t);
                    }
                    if (onDone != null) {
                        onDone.run();
                    }
                }
            }
        }, "hub-reply-http").start();
        return true;
    }
}

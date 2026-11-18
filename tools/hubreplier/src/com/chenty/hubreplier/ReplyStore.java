package com.chenty.hubreplier;

import android.content.Context;
import android.os.Build;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.time.format.DateTimeFormatter;

/**
 * 答复落盘。schema 契约见 README.md §3（Lead 侧按字段解析，别改名）：
 *   all.jsonl 每行一个对象：id / kind(reply|action) / text / action / src(remoteinput|button|test) / at
 *   <id>.json 存该 id 的最后一条（同字段）。
 * 内部存储 files/replies 是权威副本（adb run-as 可读）；外部 files/replies 是给 adb pull 的镜像。
 */
final class ReplyStore {
    private static final String TAG = "HubReplier";
    private static final DateTimeFormatter AT_FMT =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssXXX");

    private ReplyStore() {}

    static File dir(Context ctx) {
        File d = new File(ctx.getFilesDir(), "replies");
        if (!d.isDirectory() && !d.mkdirs()) {
            Log.w(TAG, "mkdirs failed: " + d);
        }
        return d;
    }

    /**
     * @param src  remoteinput / button / test
     * @param kind reply（自由文本）/ action（选项）
     * @param text kind=reply 时的原文；kind=action 时传 null
     * @param action kind=action 时的选项 key（无 key 用 label）；kind=reply 时传 null
     */
    static synchronized void write(Context ctx, String id, String src, String kind,
                                   String text, String action) {
        try {
            JSONObject o = new JSONObject();
            o.put("id", id);
            o.put("kind", kind);
            o.put("text", text == null ? JSONObject.NULL : text);
            o.put("action", action == null ? JSONObject.NULL : action);
            o.put("src", src);
            o.put("at", OffsetDateTime.now().format(AT_FMT));

            byte[] json = o.toString(2).getBytes(StandardCharsets.UTF_8);
            writeAtomic(new File(dir(ctx), id + ".json"), json);

            // 追加写：每次一行，绝不重写整个文件（Lead 侧按行号做游标）
            File all = new File(dir(ctx), "all.jsonl");
            try (Writer w = new OutputStreamWriter(new FileOutputStream(all, true),
                    StandardCharsets.UTF_8)) {
                w.write(o.toString());
                w.write((char) 10);
            }

            mirrorToExternal(ctx, id, json, o.toString());
            Log.i(TAG, "stored reply id=" + id + " src=" + src + " kind=" + kind
                    + " text=" + text + " action=" + action);
        } catch (Exception e) {
            Log.e(TAG, "store failed id=" + id, e);
        }
    }

    /** 外部镜像：失败不影响主流程（Android/data 在部分 ROM 上不可写）。 */
    private static void mirrorToExternal(Context ctx, String id, byte[] json, String line) {
        try {
            File ext = ctx.getExternalFilesDir(null);
            if (ext == null) {
                return;
            }
            File d = new File(ext, "replies");
            if (!d.isDirectory() && !d.mkdirs()) {
                return;
            }
            writeAtomic(new File(d, id + ".json"), json);
            try (Writer w = new OutputStreamWriter(new FileOutputStream(new File(d, "all.jsonl"), true),
                    StandardCharsets.UTF_8)) {
                w.write(line);
                w.write((char) 10);
            }
        } catch (Exception e) {
            Log.w(TAG, "external mirror skipped: " + e);
        }
    }

    /** 投递时的元信息（title/options），答复时用来刷「已回复」。 */
    static void saveMeta(Context ctx, String id, String title, String body, String mode,
                         String[] labels, String[] keys) {
        try {
            JSONObject o = new JSONObject();
            o.put("id", id);
            o.put("title", title);
            o.put("body", body);
            o.put("mode", mode);
            JSONArray arr = new JSONArray();
            if (labels != null) {
                for (int i = 0; i < labels.length; i++) {
                    JSONObject opt = new JSONObject();
                    opt.put("key", keys != null && i < keys.length && keys[i] != null
                            ? keys[i] : labels[i]);
                    opt.put("label", labels[i]);
                    arr.put(opt);
                }
            }
            o.put("options", arr);
            o.put("shown_at", OffsetDateTime.now().format(AT_FMT));
            File d = new File(ctx.getFilesDir(), "meta");
            if (!d.isDirectory() && !d.mkdirs()) {
                Log.w(TAG, "meta mkdirs failed: " + d);
            }
            writeAtomic(new File(d, id + ".json"), o.toString(2).getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            Log.w(TAG, "saveMeta failed id=" + id + ": " + e);
        }
    }

    /** 读回投递时的 title；没有就退回默认值（答复路径永不因它失败）。 */
    static String metaTitle(Context ctx, String id) {
        try {
            File f = new File(new File(ctx.getFilesDir(), "meta"), id + ".json");
            if (!f.isFile()) {
                return "info-hub";
            }
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                byte[] buf = new byte[4096];
                int n;
                while ((n = in.read(buf)) > 0) {
                    bos.write(buf, 0, n);
                }
            }
            return new JSONObject(new String(bos.toByteArray(), StandardCharsets.UTF_8))
                    .optString("title", "info-hub");
        } catch (Exception e) {
            return "info-hub";
        }
    }

    private static void writeAtomic(File target, byte[] data) throws Exception {
        File tmp = new File(target.getParentFile(), target.getName() + ".tmp");
        try (FileOutputStream fos = new FileOutputStream(tmp)) {
            fos.write(data);
            fos.getFD().sync();
        }
        if (!tmp.renameTo(target)) {
            try (FileOutputStream fos = new FileOutputStream(target)) {
                fos.write(data);
            }
            //noinspection ResultOfMethodCallIgnored
            tmp.delete();
        }
    }
}

package com.chenty.hubreplier;

import android.app.Activity;
import android.content.Context;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.util.Log;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;

/**
 * info-hub 设置页（契约见 info-hub/INTERFACE.md §11.10）：全部配置都在手机里填，不需要 adb。
 *
 * 门禁只有 token 一道（用户 2026-11-17 22:56：「有了 token 本身也不需要密码，可以删掉」），
 * 所以这里只有四个字段，没有密码相关操作。
 *
 * 入口两个：① 消息页右上角「⚙ 设置」（WebView 拦 hub://settings）；
 * ② 桌面图标（首次没配地址时 MainActivity 直接转到这里）。
 */
public class SettingsActivity extends Activity {
    private static final String TAG = "HubReplier";
    private static final int TIMEOUT_MS = 2500;

    private EditText eUrl;
    private EditText eUrls;
    private EditText eToken;
    private EditText eRelay;
    private TextView out;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        SharedPreferences sp = getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(16);
        box.setPadding(pad, pad, pad, dp(28));
        box.setBackgroundColor(0xFF12141A);

        TextView title = new TextView(this);
        title.setText("info-hub 设置");
        title.setTextSize(20);
        title.setTextColor(0xFFE8EAEE);
        box.addView(title);

        TextView hint = new TextView(this);
        hint.setText("在手机上填好这几项就行（不用 adb），点保存生效。\n"
                + "地址可填多个：第一行是主地址，其余是备选（内网 / IPv6 / Cloudflare 隧道 / 中转都行），"
                + "App 会按顺序挑第一个能连上的。");
        hint.setTextSize(13);
        hint.setTextColor(0xFF98A1B0);
        hint.setPadding(0, dp(6), 0, dp(12));
        box.addView(hint);

        eUrl = field(box, "服务器地址（主）", sp.getString(HomeActivity.KEY_URL, ""), false);
        eUrl.setHint("例：http://<lan-ip>:8800/ 或 http://[2001:db8::1]:8443/");
        eUrls = field(box, "备选地址（每行一个，可空）", sp.getString(Endpoints.KEY_URLS, ""), true);
        eUrls.setHint("https://xxx.trycloudflare.com/\nhttp://[2001:db8::1]:8443/");
        eToken = field(box, "token", sp.getString(HomeActivity.KEY_TOKEN, ""), false);
        eToken.setHint("同服务器 hub.config.yaml 里的 phone_api.token");
        eRelay = field(box, "中转目标（最后一档，可空）", sp.getString(LocalRelay.KEY_RELAY_URL, ""), false);
        eRelay.setHint("http://<hub-host>:8800/");

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(14), 0, 0);
        row.addView(button("保存", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                save();
            }
        }), lp());
        row.addView(button("测试连接", new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                test();
            }
        }), lp());
        box.addView(row);

        out = new TextView(this);
        out.setTextSize(12);
        out.setTextColor(0xFF9FB3C8);
        out.setPadding(0, dp(14), 0, 0);
        out.setText("结果会显示在这里。");
        box.addView(out);

        ScrollView sv = new ScrollView(this);
        sv.addView(box, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(sv);
        fitInsets(sv);
    }

    // ---------- UI 小工具 ----------
    private int dp(int v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    private LinearLayout.LayoutParams lp() {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        p.setMargins(0, 0, dp(8), 0);
        return p;
    }

    private EditText field(LinearLayout box, String label, String value, boolean multi) {
        TextView t = new TextView(this);
        t.setText(label);
        t.setTextSize(13);
        t.setTextColor(0xFF98A1B0);
        t.setPadding(0, dp(10), 0, dp(4));
        box.addView(t);
        EditText e = new EditText(this);
        e.setText(value == null ? "" : value);
        e.setTextSize(14);
        e.setTextColor(0xFFE8EAEE);
        e.setSingleLine(!multi);
        if (multi) {
            e.setMinLines(2);
            e.setGravity(android.view.Gravity.TOP | android.view.Gravity.START);
        }
        e.setInputType(InputType.TYPE_CLASS_TEXT
                | (multi ? InputType.TYPE_TEXT_FLAG_MULTI_LINE : 0));
        box.addView(e, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return e;
    }

    private Button button(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(14);
        b.setOnClickListener(l);
        return b;
    }

    /** Android 15+ 强制 edge-to-edge：留白加在内容容器上。 */
    private void fitInsets(final View content) {
        final int top = dimenPx("status_bar_height");
        final int bot = dimenPx("navigation_bar_height");
        content.setPadding(0, top, 0, bot);
    }

    private int dimenPx(String name) {
        int id = getResources().getIdentifier(name, "dimen", "android");
        return id > 0 ? getResources().getDimensionPixelSize(id) : 0;
    }

    private String v(EditText e) {
        return e.getText().toString().trim();
    }

    private void say(String s) {
        Log.i(TAG, "Settings: " + s.replace('\n', ' '));
        out.setText(s);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    // ---------- 动作 ----------
    private void save() {
        String url = v(eUrl);
        if (url.isEmpty() || !url.startsWith("http")) {
            toast("服务器地址要以 http:// 或 https:// 开头");
            return;
        }
        if (v(eToken).isEmpty()) {
            toast("token 不能为空");
            return;
        }
        SharedPreferences sp = getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE);
        sp.edit()
                .putString(HomeActivity.KEY_URL, url)
                .putString(Endpoints.KEY_URLS, v(eUrls))
                .putString(HomeActivity.KEY_TOKEN, v(eToken))
                .putString(LocalRelay.KEY_RELAY_URL, v(eRelay))
                .apply();
        Endpoints.invalidate();
        toast("已保存");
        say("已保存到本机。地址：" + url + (v(eUrls).isEmpty() ? "" : "（另有备选）"));
        finish();
    }

    private void test() {
        final String url = v(eUrl);
        final String extra = v(eUrls);
        final String token = v(eToken);
        if (url.isEmpty()) {
            toast("先填服务器地址");
            return;
        }
        say("测试中…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final StringBuilder sb = new StringBuilder();
                boolean ok = false;
                for (String base : candidates(url, extra)) {
                    String line = probe(base, token);
                    ok = ok || line.startsWith("可用");
                    sb.append(line).append('\n');
                }
                if (!ok) {
                    // 直连全不通时按消息页同样的回退规则试本机中转（手机只装隧道 App、不接 VPN 的常态）
                    String relay = LocalRelay.baseFor(SettingsActivity.this, url);
                    if (relay != null) {
                        String line = probe(relay, token);
                        sb.append(line.startsWith("可用") ? line + "（经本机中转）"
                                : "本机中转也不通（" + line + "）").append('\n');
                    } else {
                        sb.append("本机中转不可用（未装隧道 App / SOCKS 口没开）\n");
                    }
                }
                final String text = sb.toString().trim();
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        say(text.isEmpty() ? "没有可测的地址" : text);
                    }
                });
            }
        }).start();
    }

    // ---------- 网络 ----------
    private static List<String> candidates(String url, String extra) {
        List<String> out = new ArrayList<String>();
        List<String> raw = new ArrayList<String>();
        if (url != null && !url.trim().isEmpty()) {
            raw.add(url.trim());
        }
        if (extra != null) {
            for (String piece : extra.split("[,\n\\r]+")) {
                String u = piece.trim();
                if (!u.isEmpty() && !raw.contains(u)) {
                    raw.add(u);
                }
            }
        }
        for (String u : raw) {
            out.add(u.endsWith("/") ? u : u + "/");
        }
        return out;
    }

    /** 探一个地址：返回一行「可用 0.32s / 401 token 不对 / HTTP n / 不通」。 */
    private static String probe(String base, String token) {
        long t0 = System.currentTimeMillis();
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "api/messages?limit=1").openConnection();
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setRequestProperty("Accept", "application/json");
            c.setRequestProperty("X-Hub-Token", token == null ? "" : token);
            int code = c.getResponseCode();
            long ms = System.currentTimeMillis() - t0;
            if (code == 200) {
                return "可用 " + ms + "ms  " + base;
            }
            String err = readAll(code >= 400 ? c.getErrorStream() : c.getInputStream());
            if (code == 401) {
                return "401 token 不对  " + base;
            }
            return "HTTP " + code + "  " + base + (err.isEmpty() ? "" : "：" + err);
        } catch (Exception e) {
            return "不通（" + e.getClass().getSimpleName() + "）  " + base;
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private static String readAll(InputStream in) {
        if (in == null) {
            return "";
        }
        try {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[2048];
            int n;
            while ((n = in.read(buf)) > 0) {
                bos.write(buf, 0, n);
            }
            in.close();
            return bos.toString("UTF-8").trim();
        } catch (Exception e) {
            return "";
        }
    }
}

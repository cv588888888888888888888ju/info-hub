package com.chenty.hubreplier;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.util.Log;
import android.view.KeyEvent;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

/**
 * 手机主页：把 info-hub 的消息中心（§11.10 的 / 页面）装进 WebView。
 *
 * 地址与 token 通过 am start 写入（--es url / --es token），之后持久化到私有 SharedPreferences，
 * 从桌面图标进也直接打开上次的地址。没有配置过就什么都不显示（finish）。
 * 文档类附件交给系统下载器（DownloadManager），图片由 WebView 直接渲染。
 */
public class HomeActivity extends Activity {
    private static final String TAG = "HubReplier";
    public static final String PREFS = "hub_home";
    public static final String KEY_URL = "url";
    public static final String KEY_TOKEN = "token";

    private WebView web;
    private boolean loaded;
    private TextView status;   // 常驻操作条上的状态/错误（页面 401 或连不上时显示原因）
    private View statusBar;
    private View notifyBar;
    private boolean loadFailed;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        Intent it = getIntent();
        SharedPreferences sp = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String url = it.getStringExtra("url");
        String token = it.getStringExtra("token");
        if (url != null && url.startsWith("http")) {
            sp.edit().putString(KEY_URL, url).apply();
        } else {
            url = sp.getString(KEY_URL, "");
        }
        if (token != null && !token.isEmpty()) {
            sp.edit().putString(KEY_TOKEN, token).apply();
        } else {
            token = sp.getString(KEY_TOKEN, "");
        }
        // 候选地址与中转目标也能从 am start 写（§11.10），省得改 prefs 文件：
        //   --es urls "https://xxx.trycloudflare.com/,http://[2408::1]:8800/"
        //   --es relay_url "http://<hub-host>:8800/"
        String urls = it.getStringExtra("urls");
        if (urls != null) {
            sp.edit().putString(Endpoints.KEY_URLS, urls).apply();
        }
        String relayUrl = it.getStringExtra("relay_url");
        if (relayUrl != null) {
            sp.edit().putString(LocalRelay.KEY_RELAY_URL, relayUrl).apply();
        }
        // 地址 / token 一律由人在设置页填（用户 2026-11-17 22:51 澄清：不用 adb，装完手填）。
        // 这里只读 prefs，不自动补值、不写回。
        final String configured = url == null ? "" : url;
        final String tokenValue = Endpoints.token(this);

        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // 页面里的「⚙ 设置」用自定义 scheme 进来；拦下它，换成设置页（页面本身不用知道 Activity 名）。
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                if (url != null && url.startsWith("hub://settings")) {
                    openSettings();
                    return true;
                }
                return false;
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!loadFailed) {
                    showStatus("info-hub", false);
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req == null || !req.isForMainFrame()) {
                    return;
                }
                loadFailed = true;
                String desc = err == null ? "" : String.valueOf(err.getDescription());
                showStatus("连不上服务器：" + desc + " —— 点右上「⚙ 设置」改地址", true);
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest req,
                                            WebResourceResponse res) {
                if (req == null || !req.isForMainFrame()) {
                    return;
                }
                loadFailed = true;
                int code = res == null ? 0 : res.getStatusCode();
                String why;
                if (code == 401) {
                    why = "token 不对";
                } else if (code == 403) {
                    why = "被拒绝（403）";
                } else if (code == 404) {
                    why = "地址不存在（404）";
                } else if (code >= 500) {
                    why = "服务器出错（HTTP " + code + "）";
                } else {
                    why = "HTTP " + code;
                }
                showStatus(why + " —— 点右上「⚙ 设置」改正", true);
            }
        });
        web.setWebChromeClient(new WebChromeClient());
        web.setDownloadListener(new android.webkit.DownloadListener() {
            @Override
            public void onDownloadStart(String downloadUrl, String userAgent, String contentDisposition,
                                        String mimeType, long contentLength) {
                try {
                    DownloadManager.Request req = new DownloadManager.Request(Uri.parse(downloadUrl));
                    req.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS,
                            fileName(contentDisposition));
                    DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
                    dm.enqueue(req);
                    Toast.makeText(HomeActivity.this, "开始下载附件", Toast.LENGTH_SHORT).show();
                } catch (Exception e) {
                    Log.w(TAG, "download failed", e);
                    Toast.makeText(HomeActivity.this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                }
            }
        });
        // 常驻操作条：页面挂了（401 / 连不上）也必须能进设置——设置入口不能依赖网页里的 JS。
        statusBar = new LinearLayout(this);
        ((LinearLayout) statusBar).setOrientation(LinearLayout.HORIZONTAL);
        ((LinearLayout) statusBar).setGravity(Gravity.CENTER_VERTICAL);
        statusBar.setPadding(dp(14), dp(4), dp(8), dp(4));
        statusBar.setBackgroundColor(0xFF1B1E24);
        status = new TextView(this);
        status.setTextSize(12);
        status.setTextColor(0xFF98A1B0);
        status.setText("info-hub");
        status.setMaxLines(2);
        statusBar.setVisibility(View.GONE);
        ((LinearLayout) statusBar).addView(status,
                new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button gear = new Button(this);
        gear.setText("⚙ 设置");
        gear.setAllCaps(false);
        gear.setTextSize(12);
        gear.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openSettings();
            }
        });
        ((LinearLayout) statusBar).addView(gear);

        // Android 15+ 强制 edge-to-edge：留白必须加在 WebView 的父容器上。
        // WebView 自己的 setPadding 不移动页面内容（实测标题仍压在状态栏下面）。
        // 通知权限没开就没法收可回复通知：这条常驻提示不依赖网页，点「开启」去授权。
        LinearLayout nb = new LinearLayout(this);
        nb.setOrientation(LinearLayout.HORIZONTAL);
        nb.setGravity(Gravity.CENTER_VERTICAL);
        nb.setBackgroundColor(0xFF3A2A11);
        nb.setPadding(dp(14), dp(4), dp(8), dp(4));
        TextView nt = new TextView(this);
        nt.setTextSize(12);
        nt.setTextColor(0xFFFFC46B);
        nt.setText("通知权限没开：收不到可回复通知");
        nb.addView(nt, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button nbOpen = new Button(this);
        nbOpen.setText("开启");
        nbOpen.setAllCaps(false);
        nbOpen.setTextSize(12);
        nbOpen.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                askNotifyPermission();
            }
        });
        nb.addView(nbOpen);
        notifyBar = nb;
        refreshNotifyBar();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xFF12141A);
        root.addView(notifyBar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(statusBar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        root.addView(web, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        final int fbTop = dimenPx("status_bar_height");
        final int fbBot = dimenPx("navigation_bar_height");
        root.setPadding(0, fbTop, 0, fbBot);
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override
            public WindowInsets onApplyWindowInsets(View v, WindowInsets ins) {
                int top = ins.getSystemWindowInsetTop();
                int bot = ins.getSystemWindowInsetBottom();
                v.setPadding(0, top > 0 ? top : fbTop, 0, bot > 0 ? bot : fbBot);
                return ins;
            }
        });
        setContentView(root);
        HomeSync.start(this);
        root.requestApplyInsets();
        Log.i(TAG, "insets fallback top=" + fbTop + " bottom=" + fbBot);
        loadEndpoint(configured, tokenValue);
    }

    private void openSettings() {
        startActivity(new Intent(HomeActivity.this, SettingsActivity.class));
    }

    /** 操作条状态：error=true 时红字提示（页面 401 / 连不上都走这里）。 */
    private void showStatus(final String text, final boolean error) {
        runOnUiThread(new Runnable() {
            @Override
            public void run() {
                if (status == null || statusBar == null) {
                    return;
                }
                status.setText(text);
                status.setTextColor(0xFFFF5C5C);
                status.setTypeface(null, android.graphics.Typeface.BOLD);
                statusBar.setBackgroundColor(0xFF3A1113);
                // 正常时整条让位（页面自己的「⚙ 设置」就够了）；出错时必须露出这条，设置入口不依赖网页。
                statusBar.setVisibility(error ? View.VISIBLE : View.GONE);
            }
        });
    }

    private int dp(int v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }

    /** 通知权限没开：先请求运行时权限；系统已拒绝过（或用户关了开关）就去系统设置页。 */
    private void askNotifyPermission() {
        try {
            if (Build.VERSION.SDK_INT >= 33
                    && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                       != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[] { "android.permission.POST_NOTIFICATIONS" }, 2);
                return;
            }
            Intent i = new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS);
            i.putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, getPackageName());
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
        } catch (Exception e) {
            Log.w(TAG, "open notification settings failed: " + e);
            Toast.makeText(this, "打不开通知设置，请到系统设置里手动开", Toast.LENGTH_LONG).show();
        }
    }

    private void refreshNotifyBar() {
        if (notifyBar != null) {
            notifyBar.setVisibility(Notifications.allowed(this) ? View.GONE : View.VISIBLE);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        refreshNotifyBar();
        if (code == 2) {
            HomeSync.start(this);
        }
    }

    /** 探测候选地址 → 载入（探测必须离开主线程：主线程会抛 NetworkOnMainThreadException）。 */
    private void loadEndpoint(final String fallback, final String token) {
        loadFailed = false;
        showStatus("正在连接…", false);
        new Thread(new Runnable() {
            @Override
            public void run() {
                String base = Endpoints.pick(HomeActivity.this);
                final String finalUrl = withQuery(base != null ? base : fallback, token);
                Log.i(TAG, "HomeActivity endpoint: " + fallback + " -> "
                        + (base != null ? base : "（全不通，试直连）")
                        + "；载入 " + finalUrl.replaceAll("token=[^&]+", "token=***"));
                if (finalUrl == null || finalUrl.isEmpty()) {
                    loadFailed = true;
                    showStatus("还没有可用地址 —— 点「⚙ 设置」填一个", true);
                    return;
                }
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        if (web != null) {
                            web.loadUrl(finalUrl);
                            loaded = true;
                        }
                    }
                });
            }
        }).start();
    }

    @Override
    protected void onResume() {
        refreshNotifyBar();
        super.onResume();
        // 从设置页回来：地址/token 可能改了，重新挑一次再载入。
        if (loaded && web != null) {
            SharedPreferences sp = getSharedPreferences(PREFS, Context.MODE_PRIVATE);
            loadEndpoint(sp.getString(KEY_URL, ""), sp.getString(KEY_TOKEN, ""));
        }
    }

    /** 拼 token / helper=1（helper=1 让页面显示「⚙ 设置」并允许 hub://settings 跳转）。 */
    private static String withQuery(String base, String token) {
        if (base == null || base.isEmpty()) {
            return base;
        }
        StringBuilder b = new StringBuilder(base);
        char sep = base.contains("?") ? '&' : '?';
        if (token != null && !token.isEmpty() && !base.contains("token=")) {
            b.append(sep).append("token=").append(Uri.encode(token));
            sep = '&';
        }
        if (!base.contains("helper=")) {
            b.append(sep).append("helper=1");
        }
        return b.toString();
    }

    /** 从 Content-Disposition 取文件名（服务端发 filename*=UTF-8''…）；拿不到就退回时间戳。 */
    private String fileName(String disposition) {
        if (disposition != null) {
            java.util.regex.Matcher m = java.util.regex.Pattern
                    .compile("filename\\*?=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?")
                    .matcher(disposition);
            if (m.find()) {
                String raw = m.group(1) != null ? m.group(1) : m.group(2);
                if (raw != null && !raw.isEmpty()) {
                    try {
                        return java.net.URLDecoder.decode(raw, "UTF-8");
                    } catch (java.io.UnsupportedEncodingException e) {
                        return raw;
                    }
                }
            }
        }
        return "hub-" + System.currentTimeMillis();
    }

    private int dimenPx(String name) {
        int id = getResources().getIdentifier(name, "dimen", "android");
        return id > 0 ? getResources().getDimensionPixelSize(id) : 0;
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}

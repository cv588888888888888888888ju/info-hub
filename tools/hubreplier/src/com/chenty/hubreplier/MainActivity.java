package com.chenty.hubreplier;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;

/**
 * 仅用于「让这个包离开 stopped/notLaunched 状态」+ 兜底申请通知权限。
 * Android 3.1+ 新装应用在首次被用户可见地启动前，收不到广播；
 * 所以 install.sh 会 am start 一次本 Activity（它立刻 finish，什么都不显示）。
 */
public class MainActivity extends Activity {
    private static final String TAG = "HubReplier";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Log.i(TAG, "MainActivity onCreate (unstop run)");
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                   != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] { "android.permission.POST_NOTIFICATIONS" }, 1);
        }
        // 没填过地址就先进设置页（手填，不用 adb）；填过则直达消息页。
        String url = getSharedPreferences(HomeActivity.PREFS, Context.MODE_PRIVATE)
                .getString(HomeActivity.KEY_URL, "");
        boolean configured = url != null && !url.trim().isEmpty();
        startActivity(new Intent(this, configured ? HomeActivity.class : SettingsActivity.class));
        finish();
    }
}

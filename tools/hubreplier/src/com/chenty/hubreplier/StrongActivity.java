package com.chenty.hubreplier;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * 手机侧强提醒的可回复全屏页（info-hub 自己的，不依赖 com.jxev.ai）。
 *
 * 由 HomeSync 轮询到 kind=strong 的行时拉起：锁屏/亮屏都整屏显示标题+正文，
 * 有选项就每个选项一个按钮，也可以直接在输入框打字回复；提交走
 * HubClient.postReply → info-hub /api/deliver/reply（台账 status=replied，主页可见）。
 * 契约见 README.md §2.7 / INTERFACE.md §11.7c。
 */
public class StrongActivity extends Activity {
    private static final String TAG = "HubReplier";

    /** 组装拉起意图：extras 只用字符串，避免跨版本类型不稳。 */
    static Intent intentFor(Context ctx, String id, String title, String body, String optionsJson) {
        Intent i = new Intent(ctx, StrongActivity.class);
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        i.putExtra("id", id == null ? "" : id);
        i.putExtra("title", title == null ? "" : title);
        i.putExtra("body", body == null ? "" : body);
        i.putExtra("options", optionsJson == null ? "" : optionsJson);
        return i;
    }

    private String deliveryId = "";
    private EditText input;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        } else {
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                    | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON);
        }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Intent it = getIntent();
        deliveryId = it.getStringExtra("id") == null ? "" : it.getStringExtra("id");
        String title = it.getStringExtra("title");
        String body = it.getStringExtra("body");
        String optionsJson = it.getStringExtra("options");

        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(Color.parseColor("#101216"));
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        col.setPadding(pad, dp(36), pad, pad);
        scroll.addView(col);

        col.addView(label("info-hub 强提醒", 13, "#8ab4ff", true));
        col.addView(label(title == null || title.isEmpty() ? "(无标题)" : title, 26, "#ffffff", true));
        if (body != null && !body.isEmpty()) {
            col.addView(label(body, 17, "#d6dae2", false));
        }

        JSONArray opts = parse(optionsJson);
        for (int i = 0; i < opts.length(); i++) {
            final String value = optionLabel(opts.opt(i));
            if (value.isEmpty()) {
                continue;
            }
            Button b = new Button(this);
            b.setText(value);
            b.setAllCaps(false);
            b.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    submit(value);
                }
            });
            col.addView(b, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        }

        input = new EditText(this);
        input.setHint("在这里打字回复（可留空只确认）");
        input.setTextColor(Color.WHITE);
        input.setHintTextColor(Color.parseColor("#7d8590"));
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE);
        input.setMinLines(2);
        col.addView(input);

        Button send = new Button(this);
        send.setText("回复");
        send.setAllCaps(false);
        send.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                submit(input.getText() == null ? "" : input.getText().toString().trim());
            }
        });
        col.addView(send);

        TextView hint = label("点选项或输入文字后点「回复」；返回键=不回复先关掉", 12, "#7d8590", false);
        hint.setGravity(Gravity.START);
        col.addView(hint);

        setContentView(scroll);
    }

    private TextView label(String text, int sp, String color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(Color.parseColor(color));
        if (bold) {
            t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
        }
        t.setPadding(0, dp(6), 0, dp(10));
        return t;
    }

    private static JSONArray parse(String json) {
        try {
            return json == null || json.isEmpty() ? new JSONArray() : new JSONArray(json);
        } catch (Exception e) {
            return new JSONArray();
        }
    }

    private static String optionLabel(Object o) {
        if (o instanceof JSONObject) {
            JSONObject jo = (JSONObject) o;
            return jo.optString("label", jo.optString("key", ""));
        }
        return o == null ? "" : String.valueOf(o);
    }

    private void submit(final String text) {
        if (deliveryId.isEmpty()) {
            Toast.makeText(this, "缺少投递 id", Toast.LENGTH_SHORT).show();
            return;
        }
        final Context app = getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                boolean ok = HubClient.postReply(app, deliveryId, text);
                final String msg = ok ? "已回复" : "回复失败（会保留在主页）";
                runOnUiThread(new Runnable() {
                    @Override
                    public void run() {
                        Toast.makeText(StrongActivity.this, msg, Toast.LENGTH_SHORT).show();
                        finish();
                    }
                });
            }
        }).start();
    }

    private int dp(int v) {
        return Math.round(getResources().getDisplayMetrics().density * v);
    }
}

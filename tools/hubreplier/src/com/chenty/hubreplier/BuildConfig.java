package com.chenty.hubreplier;

/**
 * 编译期常量（手写，等价于 AGP 生成的 BuildConfig）。
 * 契约见 README.md「§2 AST/EXTRAS 契约」；本文件是这些值的唯一出处。
 */
public final class BuildConfig {
    private BuildConfig() {}

    /** 应用版本（仅用于 README / dumpsys 核对）。 */
    public static final String VERSION = "0.1.0";

    /** 不再有写死的 token：真实 token 由人在设置页填（Endpoints.token(ctx)），
     *  服务端首次启动会自己生成一个（$HUB_ROOT/phone_api.token）。
     *  留空常量只为兼容旧代码里的空判。 */
    public static final String TOKEN = "";

    /** 包名（写死在 BuildConfig 方便脚本 grep）。 */
    public static final String PKG = "com.chenty.hubreplier";

    public static final String ACTION_SHOW = "com.chenty.hubreplier.SHOW";
    public static final String ACTION_RESPONSE = "com.chenty.hubreplier.RESPONSE";
    public static final String ACTION_TEST_REPLY = "com.chenty.hubreplier.TEST_REPLY";
    public static final String ACTION_DISMISS = "com.chenty.hubreplier.DISMISS";

    public static final String CHANNEL_ID = "hub_reply";

    /** RemoteInput 的结果 key，契约的一部分。 */
    public static final String KEY_REPLY_TEXT = "reply_text";
}

"""画面类别词表（M11）。**数据，不是逻辑** —— 改词表不影响算法，改算法要换 vision.version。

命中口径：词表里的每个 token 用「子串包含」匹配 OCR 文本（大小写不敏感）。
这样 `git commit`、`提交代码` 都能命中 `code`，且不需要分词器（中文无空格）。
词的取舍：**要有区分度**，只收「几乎只在这个类别出现」的词；否则会把聊天里的
"这个函数" 判成 code。宁可少收，宁可判 unknown。
"""

from __future__ import annotations

# 类别 → token 列表。顺序无关，得分 = 该类别命中的 token 个数。
CATEGORIES: dict[str, list[str]] = {
    "code": [
        "def ", "class ", "import ", "return ", "const ", "function", "async ", "await ",
        "git ", "commit", "npm ", "yarn ", "traceback", "Traceback", ".py", ".tsx", ".ts",
        "console.log", "print(", "}", ");", "SELECT ", "CREATE TABLE", "docker", "kubectl",
        "编译", "报错", "单元测试", "pytest", "verify_", "src/",
    ],
    "terminal": [
        "$ ", "sudo ", "dnf ", "systemctl", "journalctl", "bash ", "awk ", "grep ",
        "pip ", "python3 ", "ls -", "cd ", "cat ", "chmod", "rc=0", "rc=1", "~/.dsh",
        "feed back", "PASS ", "FAIL ",
    ],
    "doc": [
        "摘要", "正文", "参考文献", "目录", "字数", "章节", "Abstract", "Introduction",
        "Conclusion", "docx", "第 1 章", "第1章", "页码", "引言", "批注",
    ],
    "chat": [
        "在吗", "发消息", "语音通话", "微信", "钉钉", "飞书", "QQ", "撤回了一条",
    ],
    "video": [
        "弹幕", "倍速", "投币", "1080P", "720P", "下一集", "播放列表", "全屏播放",
        "bilibili.com", "youtube.com",
    ],
    "shop": [
        "淘宝", "天猫", "京东", "拼多多", "购物车", "立即购买", "加入购物车", "优惠券",
        "已售", "包邮", "¥", "￥", "店铺", "订单",
    ],
    "mail": [
        "收件箱", "发件人", "主题", "附件", "回复全部", "未读", "Inbox", "Compose",
        "抄送", "Outlook", "Gmail",
    ],
    "design": [
        "图层", "画布", "组件", "导出", "Figma", "Sketch", "蒙版", "像素", "画板",
        "对齐", "描边",
    ],
    "agent": [
        "127.0.0.1:3080", "deepseek harn", "DeepSeek Harn", "智能体", "上下文编队",
        "后台任务", "info-hub", "hub-activity",
    ],
    "edu": [
        "课程", "作业", "习题", "考试", "成绩", "学号", "教案", "课件", "练习题", "答案",
    ],
}

# 「强词」：只在真正做那件事时才出现的词，命中一个顶两个。
# 弱词（上面 CATEGORIES 里的应用名、栏目名）经常只是浏览器标签栏里的一个标题，
# 会出现在每一张图上 —— 所以单靠弱词不足以判类别，见 vision.classify 的加权。
STRONG: dict[str, list[str]] = {
    "code": ["def ", "class ", "Traceback", "git commit", "SELECT ", "CREATE TABLE",
             "console.log", "import ", ".tsx", "npm ", "pytest", "编译", "报错", "verify_"],
    "terminal": ["$ ", "sudo ", "systemctl", "journalctl", "dnf ", "rc=0", "chmod",
                 "grep ", "awk ", "~/.dsh"],
    "doc": ["摘要", "参考文献", "引言", "Abstract", "Introduction", "Conclusion", "批注"],
    "chat": ["在吗", "发消息", "撤回了一条", "语音通话"],
    "video": ["弹幕", "倍速", "投币", "1080P", "720P", "下一集", "播放列表"],
    "shop": ["购物车", "立即购买", "加入购物车", "优惠券", "已售", "包邮"],
    "mail": ["收件箱", "发件人", "回复全部", "抄送"],
    "design": ["图层", "画板", "蒙版", "描边"],
    "agent": [
        "127.0.0.1:3080", "deepseek harn", "DeepSeek Harn", "智能体", "上下文编队",
        "后台任务", "info-hub", "hub-activity",
    ],
    "agent": ["127.0.0.1:3080", "deepseek harn", "DeepSeek Harn", "智能体", "上下文编队",
              "后台任务", "info-hub", "hub-activity"],
    "edu": ["作业", "习题", "学号", "课件", "练习题"],
}


# ---- M12 粗判：工作 / 玩（模型输出 → 两类的同义词表；数据不是逻辑） --------------
# 加类别（学习 / 会议 …）只改这张表 + 换 vlm.VERSION，不改代码（见 INTERFACE.md §4.17）。
VERDICTS: dict[str, list[str]] = {
    "work": [
        "工作", "干活", "办公", "学习", "写代码", "编程", "代码", "终端", "文档",
        "会议", "上课", "功课", "调试", "改bug",
        "work", "coding", "code", "working", "study", "office", "terminal",
    ],
    "play": [
        "玩", "娱乐", "视频", "看视频", "游戏", "打游戏", "购物", "看片", "刷",
        "聊天", "放松", "追剧", "看剧",
        "play", "playing", "entertainment", "video", "game", "games",
        "shopping", "watching", "chatting", "relax",
    ],
}
